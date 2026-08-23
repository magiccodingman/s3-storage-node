# Architecture and invariants

S3 Storage Node is an appliance supervisor around a single guarded SeaweedFS deployment. It is intended for storage targets whose availability, latency, or mount behavior cannot be assumed continuously.

The appliance does not try to make an unreliable filesystem transparent. It creates an explicit boundary: either the full S3 endpoint is certified healthy, or public traffic is withdrawn.

## Components

The container runs these responsibilities:

1. **Guardian** — PID 1 beneath `tini`; owns configuration, the local writer lock, transport selection, worker generations, mounts, probes, process order, fencing, recovery, and health state.
2. **HAProxy** — always listens on the configured public S3 port. Its backend is healthy only while the guardian's `/ready` endpoint returns `200`.
3. **Worker-generation keeper** — owns a private mount and network namespace for one SeaweedFS generation when namespace fencing is enabled.
4. **SeaweedFS master** — stores topology state beneath the appliance persistent state directory.
5. **SeaweedFS volume server** — stores `.dat` files on the selected data transport and `.idx` files on the configured index target.
6. **SeaweedFS filer** — stores namespace and object metadata using either an embedded target, PostgreSQL, or a custom filer configuration.
7. **SeaweedFS S3 gateway** — serves the internal S3 backend reached by HAProxy only while the guardian remains ready.

## Namespace topology

```text
                         public S3
                             │
                         ┌───▼────┐
                         │HAProxy │
                         └───┬────┘
                             │ stable worker address
                ┌────────────▼────────────┐
                │ appliance namespace     │
                │ guardian + health API   │
                │ selector + local lease  │
                │ storage helper parent   │
                └────────────┬────────────┘
                             │ root veth
                             X  physical fence
                             │ worker veth
                ┌────────────▼────────────┐
                │ worker generation       │
                │ private mount namespace │
                │ private network         │
                │ selected data transport │
                │ master/volume/filer/S3  │
                └─────────────────────────┘
```

The appliance and worker namespaces deliberately have different responsibilities. HAProxy and the guardian remain reachable while a storage generation is being fenced or rebuilt. The selected data mount and SeaweedFS processes live inside the worker generation.

## Safety invariants

### One appliance state directory has one guardian

The guardian acquires an exclusive local writer lock beneath `state_dir`. A second guardian sharing that persistent appliance state fails closed.

This lock is intentionally local. The supported ownership model is one appliance per dataset, not multiple independent hosts competing for the same mapped drive.

### One worker generation has one selected data transport

When exclusive failover is enabled, a generation receives either the canonical CIFS transport or one configured SSHFS recovery transport. The appliance never mounts both as simultaneous writers and never switches per individual I/O operation.

CIFS and SSHFS are two routes to the same logical dataset and one failure domain. They must validate the same transport-independent Sentinel V2 document or a compatible existing V1 sentinel.

### SeaweedFS never owns mount lifecycle

SeaweedFS receives only guardian-controlled filesystem paths. It never receives a CIFS source, SSH source, block-device identifier, or storage credential.

### Missing managed mountpoints are not writable

Before mounting a CIFS share, SSHFS route, or block device, the guardian creates the underlying mountpoint as `root:root` with mode `000`. SeaweedFS runs as UID/GID `10001`.

When storage is mounted, the mounted filesystem's permissions apply. When it is absent, the underlying ordinary directory remains inaccessible. This prevents local fallback volume creation.

### Dataset identity precedes service startup

Every active target must pass all applicable checks:

- mounted at the expected path;
- expected filesystem type;
- expected CIFS source or block-device UUID;
- matching sentinel identity and schema;
- expected target role and subdirectory for Sentinel V2;
- sufficient free capacity;
- write, file `fsync`, read-back, checksum, and delete;
- directory `fsync` where supported.

No SeaweedFS process starts until active targets pass.

### Any active target failure withdraws the endpoint

The appliance uses endpoint-wide fail-closed behavior. If active data, embedded metadata, or index storage becomes unsafe, the complete SeaweedFS S3 endpoint is withdrawn. The node does not permit namespace-only or metadata-only operation while object payload storage is unavailable.

### Readiness requires end-to-end proof

A listening socket is insufficient. Recovery requires repeated full storage probes plus an authenticated S3 PUT/GET/DELETE canary and a configurable stability window. HAProxy forwards public traffic only after the guardian completes that proof.

### Storage helpers have deadlines

Potentially blocking mount, unmount, enrollment, layout, and probe operations run in child processes with explicit timeouts. The guardian can withdraw readiness and physically fence a worker even when the kernel traps a helper in storage I/O.

### Unexpected faults use a bounded drain

When storage or a guarded process fails unexpectedly:

1. readiness is withdrawn;
2. the S3 gateway and SeaweedFS processes are stopped under one global deadline;
3. a clean detach is attempted after the volume writer exits;
4. the worker veth is deleted after that clean detach, or immediately when drain/detach cannot complete;
5. the clean/unclean outcome and cause are persisted before replacement;
6. failed transport state is persisted and recovery selects an eligible route.

Readiness fencing is immediate. Physical fencing remains mandatory before replacement, but the bounded drain preserves the remote `.dat` transport long enough for SeaweedFS to finish whenever it can do so safely.

### Controlled switches drain before fencing

A healthy operator-requested transport switch follows a different safe order:

1. readiness is withdrawn;
2. the sole active SeaweedFS generation drains while its current transport remains healthy;
3. the active data mount is cleanly detached;
4. the generation is physically network-fenced and retired;
5. a replacement generation selects and certifies the requested transport.

No replacement writer exists during the drain. If a process or storage helper remains blocked, the persisted request remains pending and replacement is refused.

## State machine

A normal namespace-mode startup follows:

```text
BOOTSTRAPPING
  → SELECTING_TRANSPORT        when failover is enabled
  → CREATING_GENERATION
  → MOUNTING
  → VERIFYING_STORAGE
  → STARTING_SEAWEED
  → VERIFYING_RECOVERY
  → ONLINE
```

An unexpected online failure follows:

```text
ONLINE
  → SUSPECT
  → OFFLINE / DRAINING
  → FENCING              after clean detach, or immediately on drain failure
  → RECOVERING
  → SELECTING_TRANSPORT
  → CREATING_GENERATION
  → MOUNTING
  → VERIFYING_STORAGE
  → STARTING_SEAWEED
  → VERIFYING_RECOVERY
  → ONLINE
```

A controlled operator switch follows:

```text
ONLINE
  → SUSPECT / readiness withdrawn
  → DRAINING
  → active transport detached
  → FENCING
  → RECOVERING
  → SELECTING_TRANSPORT
  → CREATING_GENERATION
  → full certification
  → ONLINE
```

An unexpected upstream read-only index follows a separate offline transaction:

```text
STARTING_SEAWEED / readiness withdrawn
  → upstream /status identifies unexpected ReadOnly volume IDs and collections
  → REPAIRING_INDEXES
  → S3, filer, volume server, and master stop in reverse dependency order
  → every writer is verified gone
  → selected .dat is exposed read-only in a disposable mount namespace
  → weed fix builds a local candidate as the SeaweedFS UID/GID
  → original .idx and .sdx are backed up
  → candidate .idx is atomically installed and stale .sdx removed
  → STARTING_SEAWEED
  → upstream /status accepts each candidate, or the rejected volume is rolled back
  → durability probes, authenticated S3 canary, and stability window
  → ONLINE
```

Repair is never a periodic live-volume operation. Detection uses SeaweedFS's own `ReadOnly` result, and only the reported unexpected IDs are considered. Explicit `expected_readonly_volume_ids` remain untouched.

The remote `.dat` is authoritative. A repair helper enters a nested private mount namespace, bind-mounts only the selected `.dat`, remounts it `ro,nosuid,nodev,noexec`, and projects it through a read-only permission-masking FUSE view. This last layer is necessary because SeaweedFS 4.44 chooses its open mode from Unix permission bits; it lets `weed fix` see a non-writable file without changing the source mode. A final individual read-only bind is inspected with `findmnt`, and an unprivileged write attempt must fail before the bundled `weed` binary runs. The writable staging directory is local and contains only reconstructed derived state.

This authority choice follows the appliance's persistence split: remote `.dat` appends contain the volume records, while local `.idx` and `.sdx` files are reconstructible acceleration state. A hard fence can interrupt an in-flight remote append after a local index update survives, or preserve the data append before its index update. Graceful draining reduces that window but cannot remove it when a transport or process is genuinely wedged.

Each volume has an atomic, fsynced journal beneath `<index path>/.s3-storage-node-repair`. It records source identity and bounded hashes, generation and transport, old/candidate artifacts, attempts, phase history, backups, validation, rollback, and failures. Candidate and backup artifacts share the live index filesystem. Installation is write-ahead journaled and reconciled by recorded hashes after a crash; an ambiguous live artifact fails closed. Backups are retained indefinitely in the first implementation.

Stable upstream status may invalidate an interrupted pre-install trigger before a candidate was installed. In that case only the journal and transaction-owned staging directory are changed: the record becomes `resolved_without_install`, the partial staging artifact is removed, and the attempt does not consume the retry budget for a future genuine incident. Candidate-installed transactions are never resolved this way and still require explicit upstream validation.

SeaweedFS `/status` is the final candidate authority. A repaired ID must still be present and explicitly writable; absence is rejection, not success. Rejected candidates are rolled back after writers stop again and their identical source fingerprint is blocked from automatic retry. Successful volumes in a batch remain repaired when another volume fails.

Configuration, identity, fence, lingering-process, or blocked-helper failures leave the endpoint unavailable and are repeatedly reported. Unknown storage is never initialized unless `allow_initialize` is explicitly enabled.

## Persistence domains

```text
/var/lib/s3-storage-node/master
    SeaweedFS master state

/var/lib/s3-storage-node/guardian
    local writer lock, generation counter, bounded outcome history,
    index-certification state, transport selector state

configured metadata target
    embedded filer database, unless PostgreSQL/custom is selected

configured index target
    SeaweedFS volume indexes and persistent repair journals/backups

selected data transport
    SeaweedFS volume data and dataset sentinel
```

The runtime directory is disposable. It contains generated configuration, copied runtime SSH keys, mountpoints, namespace state, and other ephemeral files.

## Availability boundary

The appliance protects one storage copy. It does not:

- replicate between nodes;
- supervise a distributed SeaweedFS cluster;
- coordinate multiple hosts against one shared dataset;
- turn CIFS and SSHFS into independent replicas;
- provide automatic failback;
- buffer writes on local NVMe;
- claim remote physical-media durability beyond the semantics provided by the selected filesystem.

Independent high availability belongs to an external replication, orchestration, backup, or storage layer. S3 Storage Node's responsibility is to make its own copy fail closed and observable.

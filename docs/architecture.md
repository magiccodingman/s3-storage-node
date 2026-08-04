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

### Unexpected faults remain fence-first

When storage or a guarded process fails unexpectedly:

1. readiness is withdrawn;
2. the root side of the worker veth is deleted;
3. SeaweedFS shutdown is attempted;
4. managed storage is detached;
5. the generation is retired only when safe;
6. failed transport state is persisted and recovery selects an eligible route.

The network fence happens before shutdown because the active storage path is no longer trusted.

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
  → OFFLINE / FENCING
  → DRAINING
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

Configuration, identity, fence, lingering-process, or blocked-helper failures leave the endpoint unavailable and are repeatedly reported. Unknown storage is never initialized unless `allow_initialize` is explicitly enabled.

## Persistence domains

```text
/var/lib/s3-storage-node/master
    SeaweedFS master state

/var/lib/s3-storage-node/guardian
    local writer lock, generation counter, transport selector state

configured metadata target
    embedded filer database, unless PostgreSQL/custom is selected

configured index target
    SeaweedFS volume indexes

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

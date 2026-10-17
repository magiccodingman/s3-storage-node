# S3 Storage Node

**S3 Storage Node turns a network share, attached filesystem, or dedicated disk into a guarded SeaweedFS S3 endpoint that fails closed and repairs itself.**

It is designed for useful but imperfect storage: SMB/CIFS shares, remote storage boxes, storage appliances, hot-swappable disks, and filesystems that may disappear, stall, or reconnect incompletely.

The dangerous failure is not merely that storage becomes unavailable. Linux can leave a dead network filesystem mounted, block I/O for a long time, or reveal the ordinary local directory beneath a vanished mount. A storage service pointed at that pathname can then hang indefinitely—or begin writing to the container host while operators believe it is still writing remotely.

S3 Storage Node gives that storage an explicit safety boundary.

## What it protects against

- **No mount, no write path.** Appliance-managed mountpoints are root-owned and mode `000` beneath the mounted filesystem. SeaweedFS runs unprivileged and cannot create fallback volumes on the container filesystem.
- **Dataset identity is verified.** New targets use a strict, transport-independent Sentinel V2 document. Existing V1 sentinels remain accepted without being silently rewritten.
- **A failed target withdraws the entire endpoint.** HAProxy returns `503` as soon as readiness is withdrawn. The SeaweedFS master, volume server, filer, and S3 gateway are treated as one guarded unit.
- **Blocking storage work cannot trap the guardian.** Mount, unmount, enrollment, layout, and probe operations run in disposable child processes with hard deadlines.
- **Worker generations are physically fenced.** In namespace mode, every SeaweedFS generation has private mount and network namespaces. Deleting its root veth removes its route to remote storage before replacement.
- **Recovery requires end-to-end proof.** Storage identity, capacity, durable write/read/delete probes, upstream SeaweedFS volume status, authenticated S3 canaries, and a stability window must all pass before traffic reopens.
- **CIFS may have an exclusive SSHFS recovery route.** Only one transport is selected for a generation. CIFS and SSHFS are never mounted as simultaneous writers and represent one storage failure domain, not two replicas.
- **Metadata and indexes may be separated.** Bulk `.dat` files can live remotely while filer metadata and `.idx` files remain on fast persistent local storage.
- **Damaged local indexes repair offline.** Unexpected volume-specific read-only states trigger journaled reconstruction from an individually exposed read-only `.dat`, with original-index backups, atomic installation, upstream validation, and rollback on rejection. Global read-only snapshots and byte-identical candidates are refused rather than treated as index damage.
- **Health is observable.** JSON logs, readiness/liveness endpoints, Prometheus metrics, bounded generation outcomes and causes, index trust, SeaweedFS read-only volumes, selected transport, probe results, capacity, failures, and recoveries are exposed.

This does not make a remote filesystem equivalent to an enterprise local disk array. It makes failure explicit and prevents the node from claiming success while its guarded storage path is unsafe.

## Should You Use This?

Before going any further, I want to be very honest about this project.

**You probably shouldn't use it.**

I know. That's a strange thing to say in the README of something I spent considerable time building. But there's a reason this project exists, and understanding that reason is important before deciding whether it's right for you.

This project was born out of necessity, limited resources, and a healthy distrust of network-mounted filesystems.

It was never intended to compete with real S3 object storage. It was intended to make an otherwise questionable storage arrangement as safe, reliable, and recoverable as I could reasonably make it.

And, surprisingly, it got pretty far.

### 1. Can you afford real S3 storage?

If the answer is yes, **seriously consider using that instead.**

Services designed to provide S3-compatible object storage don't require you to put an object storage engine on top of a remote network-mounted filesystem.

This project does.

That introduces an entire category of problems that wouldn't otherwise exist: disconnected mounts, stalled I/O, filesystem inconsistencies, broken processes, metadata issues, and situations where the operating system itself doesn't behave the way you'd like.

I have spent enough time dealing with network-mounted filesystems to develop a very simple philosophy:

**Never trust a network mount to behave correctly, especially when something goes wrong.**

Much of this project's complexity comes directly from lessons learned the hard way.

If you can avoid introducing a network filesystem into your S3 storage path, I strongly recommend doing so.

### 2. Why did I build this, then?

**Because the economics were ridiculous.**

In 2026, storage hardware and infrastructure costs were becoming increasingly difficult to justify on a limited budget.

One of the few genuinely affordable high-capacity storage options I could find was the Hetzner Storage Box.

At the time, my setup cost approximately:

| Component | Monthly cost |
|---|---:|
| Hetzner Storage Box — 20 TB | $46.00 |
| Hetzner VPS — 2 CPU threads, 4 GiB RAM | $6.49 |
| **Total** | **$52.49** |

That's approximately **$0.00262 per GB per month**, using 20,000 GB of advertised capacity.

For someone who needed substantial storage without substantial spending, that was extremely attractive.

There was just one problem.

I needed S3-compatible storage, and the Storage Box was a network-accessible filesystem.

So I did what any reasonable person would do.

I put SeaweedFS on top of it, wrapped it in a custom management system, and spent an unreasonable amount of time teaching it how not to destroy itself.

The result is this project.

### 3. Understand what this project is actually trying to accomplish

The primary goal was never maximum performance.

It was **survival and recovery**.

I knew from the beginning that putting an S3 service over a network-mounted filesystem was asking for trouble.

So rather than assuming the filesystem would behave, I designed around the assumption that it eventually wouldn't.

What happens when the mount disappears?

What happens when I/O hangs indefinitely?

What happens when the storage comes back but isn't actually healthy?

What happens when SeaweedFS becomes unresponsive?

What happens when a worker refuses to terminate?

What happens when the machine runs out of memory under heavy load?

What happens when storage appears healthy but isn't safe to write to?

These aren't hypothetical concerns. They're the kinds of problems that motivated the safeguards in this repository.

The result includes mountpoint protections, dataset identity verification, health monitoring, fail-closed behavior, worker isolation, recovery mechanisms, and admission control.

The philosophy is simple:

**If something goes wrong, stop accepting work, protect the data, recover if possible, and only reopen when the storage is demonstrably healthy.**

That doesn't make the underlying architecture ideal.

It makes an inherently fragile arrangement considerably more survivable.

### 4. How far did I actually push this?

Much further than I originally intended.

For a while, this was serving real workloads across my small-business infrastructure.

Among other things, I used it for:

- GitLab and Harbor container registry storage
- PostgreSQL backups across approximately 16–20 databases
- Proxmox-related backups covering roughly 24 virtual machines
- Various application artifacts and other backup workloads

Backup schedules ranged from approximately every 30 minutes to hourly, daily, and weekly operations.

The biggest challenge wasn't constant traffic.

**It was burst traffic.**

Everything could be relatively quiet, and then multiple backup jobs, registry operations, and infrastructure processes would suddenly start hitting the same storage backend.

Harbor and Proxmox overlapping were particularly good at exposing problems.

And I exposed plenty of them.

Over time, I repeatedly pushed the system beyond what its tiny VPS could comfortably handle, discovered failure modes, and added protections.

Eventually, it became remarkably good at recovering.

Under extreme load, it could still slow down, reject requests, or temporarily become unavailable. But it increasingly learned how to do those things without completely falling apart.

Some of the later benchmarks demonstrated approximately:

- 73 MiB/s large-object writes
- 95 MiB/s large-object reads
- 232 small-object writes per second
- 26,566 mixed operations without errors
- Survival under 128 concurrent large-object reads, with substantial overload errors but without an out-of-memory crash

For a 2-thread, 4 GiB VPS backed by a remote CIFS filesystem, I was genuinely impressed.

**But surviving extreme workloads is not the same thing as being suitable for them.**

And that's an important distinction.

### 5. There are important limitations

First, this is not a replacement for a conventional, professionally operated object storage service.

Second, I strongly discourage treating multiple network-mounted storage targets as if they automatically provide a reliable distributed SeaweedFS cluster.

I haven't validated that kind of architecture with this project, and it's not what the appliance is designed to supervise.

If you're building a larger system, I recommend keeping replication, routing, and storage orchestration above these individual nodes.

In my own environment, I used a separate S3 Orchestrator project in front of the storage nodes rather than expecting the nodes themselves to provide a complete distributed storage solution.

Third, your network topology matters enormously.

My performance results came from running the VPS in the same Hetzner datacenter environment as the Storage Box.

Accessing a Storage Box across a different network path can produce dramatically different results.

**Do not assume my benchmark results represent what you'll get from an arbitrary VPS or remote location.**

Finally, this system can still become unavailable.

The recovery mechanisms are intended to prevent unsafe behavior and restore service where possible. They do not guarantee uninterrupted availability, successful requests during overload, or immunity from data loss.

You still need independent backups, monitoring, and a realistic understanding of the underlying storage.

### 6. Why did I stop using it?

I eventually outgrew it.

My infrastructure expanded, the workloads became heavier, and the value of using more conventional storage started outweighing the savings.

The system had become substantially more stable than I expected, but I was asking increasingly unreasonable things of an architecture that was originally designed to save money.

The final deciding factor was compliance.

My business operates in an environment with specific compliance requirements, and as I approached actual production use, I realized this storage arrangement wasn't suitable for those requirements.

That was the end of the road for my own deployment.

I moved on to storage solutions better suited to my operational and compliance needs.

**I didn't stop using this because the project was worthless. I stopped because my requirements changed.**

And I think that's an important distinction.

### 7. So who is this actually for?

I think this project makes sense for a fairly specific kind of person.

Someone who:

- Needs a substantial amount of inexpensive storage
- Already has access to a network-mounted storage service or appliance
- Needs an S3-compatible interface over that storage
- Has modest or intermittent workloads
- Can tolerate occasional downtime and recovery periods
- Understands the operational risks involved
- Is comfortable maintaining and troubleshooting Linux storage infrastructure

It may be useful for personal infrastructure, homelabs, secondary backup destinations, archival workloads, and certain cost-sensitive environments.

I would be much more hesitant to recommend it for high-availability applications, latency-sensitive workloads, compliance-sensitive production data, or anything where temporary storage unavailability would cause serious problems.

And if your only reason for considering this project is saving a few dollars each month, I'd strongly encourage you to calculate whether those savings are worth the additional complexity.

### Final Thoughts

I'm genuinely proud of this project.

It's a ridiculous architecture. I knew that when I started, and I certainly knew it by the time I finished.

But it's also the result of taking a bad idea seriously enough to understand its failure modes and engineer around them.

It taught me a great deal about network filesystems, storage engines, Linux process isolation, failure recovery, resource management, and the difference between a system that works and a system that survives when things stop working.

And for a while, it provided an enormous amount of storage for approximately $52.49 per month.

That's pretty remarkable.

**Would I recommend building your infrastructure this way if you have better options? No.**

**Am I impressed by how far this particular monstrosity got? Absolutely.**

If you understand what you're getting into, have a legitimate reason to use network-backed storage, and can accept the tradeoffs, this project might be useful to you.

Just remember: the entire reason it has so many safety mechanisms is that I never trusted the underlying architecture in the first place.

## Architecture

```text
                           public S3 :8333
                                  │
                           ┌──────▼──────┐
                           │   HAProxy   │
                           │ fail-closed │
                           └──────┬──────┘
                                  │ worker address, only while /ready = 200
              ┌───────────────────▼───────────────────┐
              │ Guardian / appliance namespace       │
              │ health API • selector • local lease  │
              │ mount helpers • recovery controller  │
              └───────────────────┬───────────────────┘
                                  │ private veth
                                  │ physical fence
              ┌───────────────────▼───────────────────┐
              │ One worker generation namespace      │
              │                                      │
              │ selected CIFS or SSHFS data mount    │
              │ master → volume → filer → S3         │
              │            │        │                │
              │            │        └─ filer metadata│
              │            ├─ .dat volume data       │
              │            └─ .idx volume indexes    │
              └──────────────────────────────────────┘
```

The container remains alive during a storage outage. Its PID 1 guardian owns transport selection, worker generations, mounts, probes, SeaweedFS process order, fencing, recovery, and readiness.

## Supported storage profiles

| Storage profile | Status | Behavior |
|---|---|---|
| SMB/CIFS | Supported | Mounted, certified, monitored, fenced, detached, and repaired by the appliance |
| CIFS with SSHFS recovery | Optional | Exclusively switches generations between two routes to the same sentinel-protected dataset |
| Fixed block device | Supported | UUID and filesystem verified, then mounted; never formatted automatically |
| Existing host path | Supported | Identity and I/O monitored; mount lifecycle remains external |

SSHFS is recovery-only. The canonical data target remains CIFS, namespace fencing is mandatory, fallback is sticky, and failback is manual. rclone, WebDAV, union filesystems, per-I/O path switching, simultaneous writers, and direct SSH-only storage are unsupported.

## Requirements

- Linux Docker host
- Docker Engine with Compose v2
- CIFS kernel support for SMB targets
- `CAP_SYS_ADMIN` for appliance-managed mounts and namespace entry
- `CAP_NET_ADMIN` for worker-generation veth fencing
- AppArmor allowance for mounting; the supplied Compose configuration uses `apparmor=unconfined`
- A usable host `/dev/fuse`; the standard Compose file maps it for read-only index-repair projection and SSHFS recovery
- An already formatted filesystem and explicit Docker device mapping for block devices

The image includes SeaweedFS, `mount.cifs`, SSHFS, HAProxy, filesystem tools, and the guardian, and is published for `linux/amd64` and `linux/arm64`.

## Quick start: CIFS

### 1. Create configuration and secrets

```bash
git clone https://github.com/magiccodingman/s3-storage-node.git
cd s3-storage-node

cp config/config.toml.example config/config.toml
cp secrets/cifs-credentials.example secrets/cifs-credentials
cp secrets/s3-access-key.example secrets/s3-access-key
cp secrets/s3-secret-key.example secrets/s3-secret-key
touch secrets/ssh-identity secrets/ssh-known-hosts
chmod 600 secrets/*
```

The standard Compose file declares both SSH secret paths so every deployment uses one command. CIFS-only deployments may leave `secrets/ssh-identity` and `secrets/ssh-known-hosts` empty; the guardian reads them only when an SSHFS recovery route is configured.

CIFS credentials use `mount.cifs` format:

```ini
username=u123456
password=your-storage-password
domain=
```

Generate S3 credentials:

```bash
openssl rand -hex 16 > secrets/s3-access-key
openssl rand -hex 32 > secrets/s3-secret-key
chmod 600 secrets/s3-access-key secrets/s3-secret-key
```

### 2. Configure and enroll the data target

```toml
[storage.data]
type = "cifs"
source = "//u123456.your-storagebox.de/backup"
mountpoint = "/run/s3-storage-node/mounts/data"
subdirectory = "seaweedfs"
credentials_file = "/run/secrets/cifs_credentials"
sentinel_id = "storagebox-production-01"
allow_initialize = true
io_failure_policy = "soft"
```

`sentinel_id` is the stable logical dataset identity. On first intentional enrollment, `allow_initialize = true` creates the role directories and a strict Sentinel V2 document. After the node reaches `ONLINE`, set `allow_initialize = false` on every active target and restart. A missing or mismatched sentinel then fails closed.

Existing installations with valid V1 sentinels continue to work and are not silently upgraded. See [Dataset sentinel format](docs/sentinel-format.md).

### 3. Start and inspect the node

```bash
docker compose up -d
docker compose logs -f
curl -i http://localhost:9090/ready
```

A healthy node returns HTTP `200` with state `ONLINE`. An unhealthy node returns `503`, and HAProxy also returns `503` on the public S3 port.

### 4. Use the S3 endpoint

The default endpoint is:

```text
http://HOST:8333
```

Example with AWS CLI:

```bash
AWS_ACCESS_KEY_ID="$(cat secrets/s3-access-key)" \
AWS_SECRET_ACCESS_KEY="$(cat secrets/s3-secret-key)" \
aws --endpoint-url http://localhost:8333 s3 mb s3://example
```

## Recommended storage layout

```text
Docker persistent volume
├── master/                 SeaweedFS master state
├── metadata/filer/         embedded filer metadata
├── index/volume-indexes/   persistent .idx files
└── guardian/               local lock, generations, transport state

Remote data target
└── seaweedfs/
    ├── .s3-storage-node.json
    └── volumes/             bulk .dat files
```

The default does **not** require PostgreSQL. Embedded filer metadata is persistent and local. PostgreSQL remains an optional SeaweedFS filer metadata backend only; it is not used for distributed writer leasing or transport failover.

## Optional CIFS-to-SSHFS recovery

Enable namespace fencing and the failover table:

```toml
[appliance]
worker_fencing_mode = "namespace"

[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
primary_priority = 10
failback_policy = "manual"
failure_cooldown_seconds = 60

[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
priority = 20
source = "u123456@u123456.your-storagebox.de:/home"
identity_file = "/run/secrets/ssh_identity"
known_hosts_file = "/run/secrets/ssh_known_hosts"
port = 23
mount_options = []
```

Populate the pinned host-key file and, for key authentication, the private key:

```text
secrets/ssh-known-hosts
secrets/ssh-identity
```

The base Compose file already maps `/dev/fuse` and both secret paths. Start the node normally:

```bash
docker compose up -d
```

Password-authenticated SSHFS may reuse `secrets/cifs-credentials` and does not need a populated private-key file. See [Exclusive CIFS-to-SSHFS failover](docs/exclusive-transport-failover.md) for both authentication modes.

Inspect persisted selection and failure state:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-status \
  --config /etc/s3-storage-node/config.toml
```

Request controlled failback after the primary is healthy:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-select \
  --config /etc/s3-storage-node/config.toml \
  --transport cifs-primary
```

Restoring CIFS does not cause automatic failback.

## Failure and recovery behavior

### Unexpected storage or process failure

1. Readiness changes to `503` and HAProxy withdraws the backend.
2. SeaweedFS shutdown is attempted in reverse dependency order under one global deadline.
3. If writers exit, the active storage route is cleanly detached before physical fencing.
4. If drain or detach fails, the worker is hard-fenced immediately and its indexes are marked suspect.
5. A replacement generation uses upstream `/status` to identify unexpected read-only volumes. A transient all-volume read-only snapshot is held through SeaweedFS's disk-space refresh and is never accepted as a broad repair target. For volume-specific failures, the guardian stops every SeaweedFS writer, reconstructs only those `.idx` files from individually exposed read-only authoritative `.dat` inputs, and retains rollback backups.
6. Rebuilt candidates must be present and writable in upstream `/status`; rejected candidates are rolled back and blocked from automatic loops.
7. A failed transport is persisted and the next eligible exclusive transport is selected after recovery backoff.
8. Storage identity, capacity, full durability probes, SeaweedFS volume status, S3 canaries, and the recovery stability window must pass.
9. Readiness returns to `200` and HAProxy reopens traffic.

Readiness always closes immediately. The physical fence remains mandatory before replacement, while a bounded drain avoids cutting the remote data path before SeaweedFS has had a chance to finish shutdown.

### Controlled operator transport switch

1. Readiness changes to `503`.
2. The sole active SeaweedFS generation drains while its current transport is still healthy.
3. The active mount is cleanly detached.
4. The old generation is physically fenced and retired.
5. A new generation mounts and certifies the requested transport.
6. Traffic reopens only after the full storage and S3 recovery proof succeeds.

No replacement generation exists before the old generation is detached and fenced.

## Metadata and index placement

The three storage roles are configured independently:

```toml
[storage.data]      # SeaweedFS .dat files
[storage.metadata]  # embedded filer metadata, when used
[storage.index]     # SeaweedFS .idx files
```

The logical mappings are separate:

```toml
[metadata]
backend = "embedded"
target = "metadata"
directory = "filer"

[index]
target = "index"
directory = "volume-indexes"
```

Any active target failure withdraws the entire endpoint. To use PostgreSQL for filer metadata, set `metadata.backend = "postgres"` and follow [Configuration](docs/configuration.md).

## Health and metrics

| Endpoint | Purpose |
|---|---|
| `GET :9090/live` | Guardian process is running |
| `GET :9090/ready` | Node is certified safe to receive S3 traffic |
| `GET :9090/healthz` | Detailed appliance, generation history, index trust, SeaweedFS volumes, storage, sentinel, and transport state |
| `GET :9090/metrics` | Prometheus metrics |

The public endpoint is gated by guardian readiness, not merely by whether SeaweedFS has opened a socket.

## Topology and replication boundary

The bundled appliance supervises one master, one guarded volume server, one filer, and one S3 gateway. Its safe default is `default_replication = "000"`. It does not supervise a distributed SeaweedFS cluster, replicate between independent storage nodes, or make CIFS and SSHFS separate replicas.

Cross-node or cross-location redundancy belongs to a higher-level replication, orchestration, backup, or storage system. This node's responsibility is to make its own copy fail closed and report its health accurately.

## Security note

Mounting filesystems inside a container requires meaningful privilege. The supplied Compose configuration grants `CAP_SYS_ADMIN` and `CAP_NET_ADMIN`, relaxes AppArmor for mount operations, and exposes `/dev/fuse`. It does not mount the Docker socket, host root filesystem, host PID namespace, or host network namespace. SeaweedFS and HAProxy run as UID/GID `10001`; the guardian alone performs privileged lifecycle work.

Protect configuration, CIFS credentials, SSH private keys, trusted host keys, S3 credentials, block devices, `/dev/fuse`, persistent state, and the detailed health endpoint.

## Documentation

- [Architecture and invariants](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Storage backends](docs/storage-backends.md)
- [Dataset sentinel format](docs/sentinel-format.md)
- [Operations and recovery](docs/operations.md)
- [Worker-generation fencing](docs/worker-generation-fencing.md)
- [Exclusive CIFS-to-SSHFS failover](docs/exclusive-transport-failover.md)
- [Bounded S3 admission control](docs/s3-admission-control.md)
- [Kernel-blocked transport recovery and quarantine](docs/blocked-transport-recovery.md)
- [Security model](docs/security.md)
- [Release workflow](docs/releasing.md)

## License

Apache License 2.0. SeaweedFS is a separate project also distributed under the Apache License 2.0.

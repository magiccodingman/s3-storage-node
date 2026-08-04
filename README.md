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
- **Recovery requires end-to-end proof.** Storage identity, capacity, durable write/read/delete probes, SeaweedFS startup, authenticated S3 canaries, and a stability window must all pass before traffic reopens.
- **CIFS may have an exclusive SSHFS recovery route.** Only one transport is selected for a generation. CIFS and SSHFS are never mounted as simultaneous writers and represent one storage failure domain, not two replicas.
- **Metadata and indexes may be separated.** Bulk `.dat` files can live remotely while filer metadata and `.idx` files remain on fast persistent local storage.
- **Health is observable.** JSON logs, readiness/liveness endpoints, Prometheus metrics, generation state, selected transport, probe results, capacity, failures, and recoveries are exposed.

This does not make a remote filesystem equivalent to an enterprise local disk array. It makes failure explicit and prevents the node from claiming success while its guarded storage path is unsafe.

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
- `/dev/fuse` plus `docker-compose.sshfs.yml` when SSHFS recovery is enabled
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
chmod 600 secrets/*
```

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

Start with the SSHFS Compose overlay so `/dev/fuse` and the SSH secrets are available:

```bash
docker compose -f docker-compose.yml -f docker-compose.sshfs.yml up -d
```

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

Restoring CIFS does not cause automatic failback. See [Exclusive CIFS-to-SSHFS failover](docs/exclusive-transport-failover.md).

## Failure and recovery behavior

### Unexpected storage or process failure

1. Readiness changes to `503` and HAProxy withdraws the backend.
2. The active worker generation is physically network-fenced.
3. SeaweedFS shutdown is attempted in reverse dependency order.
4. Managed mounts are detached and the old generation is retired only when safe.
5. A failed transport is persisted and the next eligible exclusive transport is selected after recovery backoff.
6. Storage identity, capacity, full durability probes, SeaweedFS startup, S3 canaries, and the recovery stability window must pass.
7. Readiness returns to `200` and HAProxy reopens traffic.

The fault path is deliberately fence-first because the storage path is no longer trusted.

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
| `GET :9090/healthz` | Detailed appliance, generation, storage, sentinel, and transport state |
| `GET :9090/metrics` | Prometheus metrics |

The public endpoint is gated by guardian readiness, not merely by whether SeaweedFS has opened a socket.

## Topology and replication boundary

The bundled appliance supervises one master, one guarded volume server, one filer, and one S3 gateway. Its safe default is `default_replication = "000"`. It does not supervise a distributed SeaweedFS cluster, replicate between independent storage nodes, or make CIFS and SSHFS separate replicas.

Cross-node or cross-location redundancy belongs to a higher-level replication, orchestration, backup, or storage system. This node's responsibility is to make its own copy fail closed and report its health accurately.

## Security note

Mounting filesystems inside a container requires meaningful privilege. The supplied Compose configuration grants `CAP_SYS_ADMIN` and `CAP_NET_ADMIN`, relaxes AppArmor for mount operations, and optionally exposes `/dev/fuse`. It does not mount the Docker socket, host root filesystem, host PID namespace, or host network namespace. SeaweedFS and HAProxy run as UID/GID `10001`; the guardian alone performs privileged lifecycle work.

Protect configuration, CIFS credentials, SSH private keys, trusted host keys, S3 credentials, block devices, `/dev/fuse`, persistent state, and the detailed health endpoint.

## Documentation

- [Architecture and invariants](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Storage backends](docs/storage-backends.md)
- [Dataset sentinel format](docs/sentinel-format.md)
- [Operations and recovery](docs/operations.md)
- [Worker-generation fencing](docs/worker-generation-fencing.md)
- [Exclusive CIFS-to-SSHFS failover](docs/exclusive-transport-failover.md)
- [Security model](docs/security.md)
- [Release workflow](docs/releasing.md)

## License

Apache License 2.0. SeaweedFS is a separate project also distributed under the Apache License 2.0.

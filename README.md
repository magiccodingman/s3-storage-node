# S3 Storage Node

**S3 Storage Node turns a network share, attached filesystem, or dedicated disk into a guarded SeaweedFS S3 endpoint that fails closed and repairs itself.**

It exists for storage that is useful, inexpensive, and occasionally temperamental: SMB/CIFS shares, storage appliances, hot-swappable disks, remote storage boxes, and drives that can disappear without warning.

The dangerous failure is not simply that a drive goes offline. Linux may leave a dead network filesystem mounted for months, block I/O for minutes, or expose the ordinary local directory beneath a mount after it disappears. A storage service pointed at that pathname can then hang indefinitely—or worse, begin writing new data onto the container host while everyone believes it is still writing to the remote storage.

S3 Storage Node is built specifically to prevent that.

## What it protects against

The appliance wraps SeaweedFS with a storage guardian and an always-on S3 gate:

- **No mount, no write path.** Appliance-managed mountpoints are root-owned and mode `000` beneath the mounted filesystem. SeaweedFS runs as an unprivileged user, so it cannot create local fallback volumes if a mount disappears.
- **Storage identity is verified.** Every target has a sentinel ID. The appliance refuses to start on the wrong share, wrong disk, unexpected empty filesystem, or missing sentinel.
- **A failed target withdraws the whole S3 node.** HAProxy immediately stops forwarding requests and returns `503`. The master, volume server, filer, and S3 gateway are stopped together rather than being left half-alive.
- **The guardian cannot be trapped by dead storage I/O.** Filesystem probes run in disposable child processes with hard deadlines. A blocked CIFS syscall cannot freeze the supervisor.
- **Recovery is automatic.** The appliance detaches a stale managed mount, reconnects it with backoff, verifies a write/`fsync`/read/delete cycle, restarts SeaweedFS in order, performs an S3 canary, and only then reopens traffic.
- **Metadata and indexes may be separated.** SeaweedFS volume data can live on inexpensive bulk storage while filer metadata and volume indexes live on faster local persistent storage.
- **Health is observable.** JSON logs, readiness/liveness endpoints, Prometheus metrics, capacity checks, state transitions, failure counts, and recovery counts are included.

This does not pretend a remote filesystem is identical to a local enterprise disk array. It gives that filesystem a hard, explicit failure boundary so upstream systems receive success only while this node is actually healthy.

## Architecture

```text
                         public S3 :8333
                                │
                         ┌──────▼──────┐
                         │   HAProxy   │
                         │ fail-closed │
                         └──────┬──────┘
                                │ only while /ready = 200
       ┌────────────────────────▼─────────────────────────┐
       │ Guardian container                              │
       │                                                 │
       │ master → volume server → filer → S3 gateway    │
       │             │                 │                  │
       │             │                 └─ filer metadata │
       │             ├─ .dat volume data                 │
       │             └─ .idx volume indexes              │
       │                                                 │
       │ mount manager • probes • recovery • health API  │
       └─────────────────────────────────────────────────┘
```

The container stays alive during a storage outage. Its PID 1 guardian owns the lifecycle of the internal mounts and SeaweedFS processes.

## Current storage support

| Storage type | Status | Appliance behavior |
|---|---|---|
| SMB/CIFS | Supported | Mounted and repaired inside the container |
| CIFS with SSHFS recovery | Optional | Exclusively switches fenced worker generations to the same sentinel-protected dataset; never mounts both writers together |
| Fixed block device | Supported | UUID and filesystem verified, then mounted; never formatted |
| Existing host path | Supported | Verified and monitored; mounting remains the host's responsibility and enrollment is manual outside appliance state |

SSHFS is supported only as an optional, exclusive recovery transport for a canonical CIFS data target. The appliance still does not place rclone, WebDAV, or a union filesystem beneath live SeaweedFS volume files.

## Requirements

- Linux Docker host
- Docker Engine with Compose v2
- A kernel with CIFS support for SMB targets
- `CAP_SYS_ADMIN` for appliance-managed mounts
- `CAP_NET_ADMIN` for worker-generation network fencing
- AppArmor allowance for mounting; the included Compose file uses `apparmor=unconfined`
- `/dev/fuse` and the optional `docker-compose.sshfs.yml` overlay when SSHFS failover is enabled
- For block devices, an already formatted filesystem and an explicit Docker device mapping

The container uses Debian 13.6 slim and includes SeaweedFS, `mount.cifs`, SSHFS, HAProxy, filesystem tools, and the guardian. The image is published for `linux/amd64` and `linux/arm64`.

## Quick start: CIFS / SMB

### 1. Create the configuration and secrets

```bash
git clone https://github.com/magiccodingman/s3-storage-node.git
cd s3-storage-node

cp config/config.toml.example config/config.toml
cp secrets/cifs-credentials.example secrets/cifs-credentials
cp secrets/s3-access-key.example secrets/s3-access-key
cp secrets/s3-secret-key.example secrets/s3-secret-key
chmod 600 secrets/*
```

Edit `secrets/cifs-credentials`:

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

### 2. Configure the target

Edit `config/config.toml`:

```toml
[storage.data]
type = "cifs"
source = "//u123456.your-storagebox.de/backup"
mountpoint = "/run/s3-storage-node/mounts/data"
subdirectory = "seaweedfs"
credentials_file = "/run/secrets/cifs_credentials"
sentinel_id = "storagebox-production-01"
allow_initialize = true
```

The sentinel ID must remain stable and unique to this storage target.

`allow_initialize = true` permits creation of the sentinel and SeaweedFS directories on the first intentional startup. The sample configuration enables enrollment for the data, metadata, and index targets. After the node reaches `ONLINE`, change `allow_initialize` to `false` on **every active target** and restart the Compose project. From then on, a missing sentinel is treated as an identity failure rather than a blank target to initialize.

### 3. Start the node

```bash
docker compose up -d
```

Watch the state machine:

```bash
docker compose logs -f
```

Check readiness:

```bash
curl -i http://localhost:9090/ready
```

A healthy node returns HTTP `200` and state `ONLINE`. An unhealthy node returns `503`, and the public S3 port also returns `503` because HAProxy withdraws its backend.

### 4. Connect to S3

The default endpoint is:

```text
http://HOST:8333
```

Use the values stored in `secrets/s3-access-key` and `secrets/s3-secret-key`. Path-style S3 access works by default.

Example with AWS CLI:

```bash
AWS_ACCESS_KEY_ID="$(cat secrets/s3-access-key)" \
AWS_SECRET_ACCESS_KEY="$(cat secrets/s3-secret-key)" \
aws --endpoint-url http://localhost:8333 s3 mb s3://example
```

## Default storage layout

The sample configuration uses:

```text
Docker persistent volume
├── master/                 SeaweedFS master state
├── metadata/filer/         embedded filer metadata
└── index/volume-indexes/   fast .idx files

CIFS share
└── seaweedfs/volumes/      bulk .dat volume files
```

This is the recommended default for a remote share: filer metadata and indexes remain on fast local persistent storage, while object payload volumes live on the bulk target.

It does **not** require PostgreSQL. The embedded filer metadata store is persistent and local. PostgreSQL is optional for users who already operate it or need an external metadata service.

## Choosing metadata and index locations

All three locations use the same storage-target abstraction:

```toml
[storage.data]      # SeaweedFS .dat files
[storage.metadata]  # embedded filer metadata, when used
[storage.index]     # SeaweedFS .idx files
```

Each can be a `cifs`, `block`, or `path` target. Any active target failure takes the entire endpoint offline.

The logical mapping is configured separately:

```toml
[metadata]
backend = "embedded"
target = "metadata"
directory = "filer"

[index]
target = "index"
directory = "volume-indexes"
```

To keep everything on the bulk target, point both roles at `data`. The role-specific directories keep `.dat`, `.idx`, and filer database files separate even though they share one physical mount:

```toml
[metadata]
backend = "embedded"
target = "data"
directory = "metadata"

[index]
target = "data"
directory = "indexes"
```

To use PostgreSQL, set `metadata.backend = "postgres"` and provide the PostgreSQL settings and password secret described in [configuration documentation](docs/configuration.md).

## Direct block devices

S3 Storage Node never partitions or formats a disk. Prepare the filesystem yourself, identify it by UUID, and expose the device to the container.

Configuration:

```toml
[storage.data]
type = "block"
device = "/dev/storage-data"
expected_uuid = "11111111-2222-3333-4444-555555555555"
expected_filesystem = "xfs"
mountpoint = "/run/s3-storage-node/mounts/data"
subdirectory = "seaweedfs"
sentinel_id = "archive-disk-01"
allow_initialize = true
mount_options = ["noatime"]
```

Compose override:

```yaml
services:
  s3-storage-node:
    devices:
      - /dev/disk/by-id/your-device:/dev/storage-data
```

The guardian verifies the UUID and filesystem before mounting. A different disk at the same device path is rejected.

For externally managed `path` targets, create the sentinel deliberately before startup and keep `allow_initialize = false`. Automatic path enrollment is accepted only beneath the appliance's persistent `state_dir`; this prevents a missing host mount from being mistaken for a blank local directory.

## Authentication, authorization, and HTTPS

The default `static` authentication mode generates a SeaweedFS S3 identity from Docker secrets with administrative S3 actions.

Available modes:

- `static`: one administrator identity generated from secret files
- `config`: use a complete SeaweedFS S3 identity JSON file
- `none`: SeaweedFS's unauthenticated development behavior; not recommended on a network

Advanced IAM and audit configuration files can be passed directly through the S3 section.

TLS modes:

- `off`: HAProxy serves HTTP; use this behind a trusted proxy or private network
- `terminate`: HAProxy terminates HTTPS with a mounted PEM bundle

Every backend node should still have unique credentials even when a separate S3 orchestrator performs public authentication, encryption, compression, or replication.

The public container port must be `1024` or greater so HAProxy can run without root privileges. Publish host ports such as `443` through Docker port mapping, for example `443:8333`, rather than changing the internal port to a privileged value.

## SeaweedFS configuration

The appliance supplies conservative defaults but exposes the settings most deployments need:

- volume, metadata, and index subdirectories
- ports
- volume size limit and maximum volume count
- replication placement (only when the deployment actually has enough distinct volume servers for that placement)
- filer chunk size
- data center, rack, and disk labels
- SeaweedFS volume encryption
- S3 domains, external URL, CORS, authentication, IAM, audit logging, and TLS
- extra command-line arguments for master, volume, filer, and S3 processes

Storage path arguments are intentionally appliance-owned and cannot be overridden through the normal configuration. That invariant is what prevents SeaweedFS from bypassing the guarded mount paths.

See [Configuration](docs/configuration.md) for every setting.

### Version 1 topology and replication

The bundled version 1 appliance intentionally runs one master, one guarded volume server, one filer, and one S3 gateway. Its safe default is `default_replication = "000"`. A placement that requires additional copies cannot be satisfied by this single volume server and will make new writes fail unless the deployment has been deliberately extended with additional SeaweedFS volume servers.

S3 Storage Node does not yet supervise a distributed multi-node SeaweedFS cluster. For version 1, cross-node or cross-location redundancy belongs to the system above this endpoint, while the node's responsibility is to fail closed and accurately report whether its own copy is available.

## Failure and recovery behavior

When an active target fails:

1. Readiness changes to `503`.
2. HAProxy withdraws the SeaweedFS S3 backend.
3. New S3 requests fail immediately rather than waiting on a dead filesystem.
4. The worker generation's network path is fenced before shutdown or mount repair.
5. S3, filer, volume, and master processes stop in reverse order.
6. Appliance-managed mounts are lazily detached.
7. Recovery retries with exponential backoff; configured transport failures rotate to the next eligible exclusive transport.
8. The target source, filesystem, sentinel, free space, and durable write/read/delete cycle are verified.
9. SeaweedFS restarts in dependency order.
10. An authenticated S3 PUT/GET/DELETE canary and the recovery stability window must pass.
11. Readiness returns to `200` and HAProxy reopens traffic.

Failed S3 operations are never reported as successful by this node. An upstream orchestrator should record a replica only after receiving a completed successful response.

## Health and metrics

| Endpoint | Purpose |
|---|---|
| `GET :9090/live` | Guardian process is running |
| `GET :9090/ready` | Full node is safe to receive S3 traffic |
| `GET :9090/healthz` | Detailed state and storage information |
| `GET :9090/metrics` | Prometheus metrics |

The public S3 endpoint is gated by `/ready`, not merely by whether the SeaweedFS process has a listening socket.

## Logs

All appliance events are JSON on stdout:

```json
{"event":"appliance_offline","error":"storage probe timed out for data","level":"ERROR"}
{"event":"storage_detached","target":"data","level":"INFO"}
{"event":"appliance_online","level":"INFO"}
```

Use Docker's logging driver or any stdout collector. Failure duration and external notifications can be derived from the state events and Prometheus metrics.

## Security note

Mounting filesystems inside a container requires substantial privilege. The included Compose file grants `CAP_SYS_ADMIN` and `CAP_NET_ADMIN` and relaxes AppArmor for this container. The image does not mount the Docker socket, host root filesystem, or host PID namespace. SeaweedFS itself runs as UID/GID `10001` without mount privileges; only the small guardian runs as root.

Review the Compose file before deploying and restrict access to configuration, credentials, block devices, `/dev/fuse`, and the health endpoint.

## Documentation

- [Architecture and invariants](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Storage backends](docs/storage-backends.md)
- [Operations and recovery](docs/operations.md)
- [Worker-generation fencing](docs/worker-generation-fencing.md)
- [Exclusive CIFS-to-SSHFS failover](docs/exclusive-transport-failover.md)
- [Security model](docs/security.md)
- [Release workflow](docs/releasing.md)

## License

Apache License 2.0. SeaweedFS is a separate project also distributed under the Apache License 2.0.

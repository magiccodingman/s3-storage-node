# Configuration reference

Configuration is TOML and defaults to `/etc/s3-storage-node/config.toml`.

Validate a configuration without starting the guardian:

```bash
s3-storage-node validate --config /etc/s3-storage-node/config.toml
```

The sample file at `config/config.toml.example` is the recommended starting point.

## `[appliance]`

| Setting | Default | Meaning |
|---|---:|---|
| `name` | `s3-storage-node` | Logical appliance name used in logs and the local writer lock |
| `state_dir` | `/var/lib/s3-storage-node` | Persistent master, guardian, generation, and selector state |
| `runtime_dir` | `/run/s3-storage-node` | Ephemeral mounts, generated configuration, copied SSH keys, and namespace state |
| `uid` / `gid` | `10001` | Unprivileged SeaweedFS and HAProxy identity |
| `health_host` | `0.0.0.0` | Health API bind address |
| `health_port` | `9090` | Health and Prometheus port |
| `probe_interval_seconds` | `5` | Fast online storage-probe cadence |
| `full_probe_interval_seconds` | `60` | Full durability and S3-canary cadence while online |
| `probe_timeout_seconds` | `4` | Deadline for a storage probe subprocess |
| `startup_timeout_seconds` | `30` | Mount/helper and per-process startup deadline |
| `shutdown_grace_seconds` | `20` | One global SeaweedFS drain deadline before hard-fence fallback |
| `recovery_initial_seconds` | `5` | Initial recovery retry delay |
| `recovery_max_seconds` | `60` | Maximum exponential recovery delay |
| `recovery_stability_seconds` | `15` | Minimum healthy interval before readiness returns |
| `recovery_probe_interval_seconds` | `2` | Probe cadence during recovery certification |
| `recovery_successes_required` | `3` | Consecutive successful recovery probes required |
| `s3_canary_enabled` | `true` | Require authenticated end-to-end S3 PUT/GET/DELETE checks |
| `worker_fencing_mode` | `disabled` | `disabled` or `namespace`; the sample enables `namespace` |
| `worker_host_address` | `169.254.254.1/30` | Root side of the private worker veth |
| `worker_address` | `169.254.254.2/30` | Worker side and stable internal SeaweedFS endpoint |
| `worker_gateway` | `169.254.254.1` | Worker default gateway through the appliance namespace |

`worker_fencing_mode = "namespace"` runs the selected data mount and all SeaweedFS processes in private mount and network namespaces. It requires `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, usable IPv4 forwarding, and is mandatory for exclusive CIFS-to-SSHFS failover.

`disabled` remains the parser default for compatibility, but it does not provide generation-level physical network fencing.

## Storage targets

Targets are declared under `[storage.data]`, `[storage.metadata]`, and `[storage.index]`. `data` is required. Metadata and index targets are active when referenced by the logical `[metadata]` and `[index]` sections.

Common settings:

| Setting | Default | Meaning |
|---|---:|---|
| `type` | required | `cifs`, `block`, or `path` |
| `mountpoint` | required | Guardian-visible target root |
| `subdirectory` | empty | Role-independent base directory beneath the target |
| `sentinel_id` | required | Stable logical dataset identity |
| `sentinel_file` | `.s3-storage-node.json` | Sentinel filename at the target storage root |
| `allow_initialize` | `false` | Permit intentional first enrollment and role-directory creation |
| `min_free_bytes` | `1073741824` | Hard free-capacity floor |
| `mount_options` | `[]` | Backend-specific guarded mount options |

New enrollment writes a strict transport-independent Sentinel V2 document. Existing explicit or versionless V1 documents remain accepted and are not silently rewritten. See [Dataset sentinel format](sentinel-format.md).

After first enrollment reaches `ONLINE`, set `allow_initialize = false` on every active target and redeploy. Leaving initialization enabled makes an intentionally blank redirected target eligible for enrollment.

### CIFS settings

```toml
[storage.data]
type = "cifs"
source = "//server/share"
credentials_file = "/run/secrets/cifs_credentials"
io_failure_policy = "soft"
handle_reconnect_policy = "auto"
minimum_smb_dialect = "3.1.1"
multichannel_policy = "disabled"
max_channels = 2
require_transport_observability = false
mount_options = ["vers=3.1.1", "uid=10001", "gid=10001"]
```

| Setting | Default | Meaning |
|---|---:|---|
| `source` | required | UNC share such as `//server/share` |
| `credentials_file` | required | `mount.cifs` credential file |
| `io_failure_policy` | `soft` | Explicit `soft` or `hard` client behavior |
| `handle_reconnect_policy` | `disabled` | Reconnect/open-handle policy; `auto` enables the guarded supported profile |
| `minimum_smb_dialect` | empty | Minimum negotiated dialect required by certification |
| `multichannel_policy` | `disabled` | Required multichannel policy for certification |
| `max_channels` | `2` | Expected/configured channel ceiling when applicable |
| `require_transport_observability` | `false` | Fail startup if negotiated transport details cannot be certified |
| `mount_options` | `[]` | Additional CIFS options after validation |

`io_failure_policy` must contain the `soft` or `hard` decision. Do not duplicate it in `mount_options`. Legacy raw flags are canonicalized; duplicate or contradictory policies are rejected.

- `soft` allows failed I/O to return errors so the guardian can withdraw and repair the node.
- `hard` may retry indefinitely and can leave SeaweedFS blocked in kernel I/O. Replacement remains forbidden while an old process still owns shared local state.

`handle_reconnect_policy`, SMB dialect, multichannel, and observability settings are used during effective transport certification. Their exact usefulness depends on the client kernel and server exposing the necessary state.

Credentials use `mount.cifs` format:

```ini
username=user
password=password
domain=
```

Never place passwords in TOML or command-line mount options.

### Exclusive CIFS-to-SSHFS recovery

The canonical `[storage.data]` target must remain `type = "cifs"`. Add:

```toml
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
source = "user@storage.example:/remote/path"
identity_file = "/run/secrets/ssh_identity"
known_hosts_file = "/run/secrets/ssh_known_hosts"
port = 22
mount_options = []
```

Failover settings:

| Setting | Default | Meaning |
|---|---:|---|
| `enabled` | `false` | Enable exclusive transport selection for `storage.data` |
| `primary_name` | `cifs-primary` | Stable operator-visible name for the canonical CIFS route |
| `primary_priority` | `10` | Lower numbers are preferred during eligible automatic selection |
| `failback_policy` | `manual` | Only supported policy; a recovered primary is never selected automatically |
| `failure_cooldown_seconds` | `60` | Time before an automatically failed transport becomes eligible again |

SSHFS transport settings:

| Setting | Default | Meaning |
|---|---:|---|
| `name` | required | Unique 1–64 character transport name |
| `type` | `sshfs` | Currently only `sshfs` is supported |
| `priority` | `20 + index` | Unique non-negative selection priority |
| `source` | required | `user@host:/remote/path` |
| `identity_file` | required | Absolute path to the read-only private-key secret |
| `known_hosts_file` | required | Absolute path to trusted OpenSSH host keys |
| `port` | `22` | SSH/SFTP port |
| `mount_options` | `[]` | Performance-only options from the guarded allowlist |

The guardian owns security and correctness options including strict host-key checking, batch mode, reconnect, server-alive checks, synchronous SSHFS writes, cache behavior, FUSE permissions, UID/GID, and umask. Operator `mount_options` cannot override those controls.

Requirements:

- `appliance.worker_fencing_mode = "namespace"`;
- the canonical data target is CIFS;
- `/dev/fuse` is mapped with `docker-compose.sshfs.yml`;
- the private key and `known_hosts` secrets are mounted;
- every route exposes the same dataset root and sentinel.

See [Exclusive CIFS-to-SSHFS failover](exclusive-transport-failover.md).

### Block settings

```toml
[storage.data]
type = "block"
device = "/dev/storage-data"
expected_uuid = "filesystem-uuid"
expected_filesystem = "xfs"
mount_options = ["noatime"]
```

| Setting | Meaning |
|---|---|
| `device` | Device path mapped into the container |
| `expected_uuid` | Required filesystem UUID |
| `expected_filesystem` | Required filesystem type |

The appliance verifies and mounts an existing filesystem. It never partitions, formats, repairs, decrypts, or changes filesystem configuration.

### Path settings

```toml
[storage.metadata]
type = "path"
mountpoint = "/var/lib/s3-storage-node/metadata"
subdirectory = ""
sentinel_id = "metadata-local-01"
allow_initialize = true
```

Path targets are not mounted by the guardian. Paths beneath `appliance.state_dir` may be initialized intentionally. External paths must already exist, contain the expected sentinel, and use `allow_initialize = false`; this prevents a missing host mount from becoming an ordinary local directory enrollment.

## `[metadata]`

### Embedded metadata

```toml
[metadata]
backend = "embedded"
target = "metadata"
directory = "filer"
```

The guardian generates a SeaweedFS `leveldb2` filer configuration using the selected target and relative directory.

### PostgreSQL filer metadata

```toml
[metadata]
backend = "postgres"
postgres_host = "postgres.example.internal"
postgres_port = 5432
postgres_user = "seaweedfs"
postgres_password_file = "/run/secrets/postgres_password"
postgres_database = "seaweedfs"
postgres_schema = "public"
postgres_sslmode = "require"
```

This PostgreSQL integration stores **SeaweedFS filer metadata only**. It is not a writer lease, distributed lock, failover coordinator, or requirement for the default architecture.

Loss of the metadata database prevents the complete endpoint from passing startup or S3 canaries. Supply the password as a Docker secret:

```yaml
services:
  s3-storage-node:
    secrets:
      - postgres_password
secrets:
  postgres_password:
    file: ./secrets/postgres-password
```

### Custom filer configuration

```toml
[metadata]
backend = "custom"
custom_filer_toml = "/run/secrets/filer_toml"
```

The supplied file is copied into the generated runtime configuration. Its availability and correctness remain operator responsibilities.

## `[index]`

```toml
[index]
target = "index"
directory = "volume-indexes"
```

The selected target becomes SeaweedFS `-dir.idx`. Indexes must remain persistent. Set `target = "data"` to store them on the bulk target while retaining a separate directory.

## `[seaweed]`

| Setting | Default | Meaning |
|---|---:|---|
| `binary` | `/usr/local/bin/weed` | SeaweedFS binary path |
| `volume_directory` | `volumes` | Relative `.dat` directory beneath the data storage root |
| `master_port` | `9333` | Internal master port |
| `volume_port` | `8080` | Internal volume-server port |
| `filer_port` | `8888` | Internal filer port |
| `s3_internal_port` | `18333` | Internal S3 backend port |
| `volume_max` | `0` | Automatic based on capacity |
| `volume_size_limit_mb` | `30000` | SeaweedFS volume size ceiling |
| `default_replication` | `000` | Placement policy suitable for one supervised volume server |
| `filer_max_mb` | `16` | Filer chunking threshold |
| `data_center` | empty | SeaweedFS topology label |
| `rack` | empty | SeaweedFS topology label |
| `disk_type` | empty | SeaweedFS disk label |
| `encrypt_volume_data` | `false` | Enable SeaweedFS volume encryption |
| `volume_health_enabled` | `true` | Require the upstream volume-server `/status` check before and during readiness |
| `expected_readonly_volume_ids` | `[]` | Explicit volume IDs whose SeaweedFS `ReadOnly` state is intentionally accepted |

Raw argument arrays are available:

```toml
master_extra_args = []
volume_extra_args = []
filer_extra_args = []
s3_extra_args = []
```

Guardian-owned bind addresses, internal dependencies, data paths, index paths, filer-store paths, and other safety-critical arguments cannot be overridden through these arrays.

The bundled appliance supervises one volume server. Keep `default_replication = "000"` unless the deployment has deliberately added and independently managed enough volume servers to satisfy another placement.

The guardian consumes SeaweedFS's own `ReadOnly` result and does not reproduce its volume-full, sealed, or disk-pressure rules. An upstream read-only ID is unexpected unless it is explicitly listed. Keep the list empty for new deployments. Add an ID only after independent operator certification; never use the list to suppress an index-integrity incident.

## `[s3]`

| Setting | Default | Meaning |
|---|---:|---|
| `host` | `0.0.0.0` | Public HAProxy bind address |
| `port` | `8333` | Public container S3 port; must be 1024 or greater |
| `domain_name` | empty | Virtual-hosted-style bucket domain suffix |
| `allowed_origins` | `*` | SeaweedFS S3 CORS origins |
| `external_url` | empty | URL used for signature verification behind a proxy |
| `auth_mode` | `static` | `static`, `config`, or `none` |
| `access_key_file` | `/run/secrets/s3_access_key` | Static administrator access key |
| `secret_key_file` | `/run/secrets/s3_secret_key` | Static administrator secret |
| `auth_config_file` | empty | Complete SeaweedFS S3 identity JSON |
| `canary_access_key_file` | empty | Canary identity when using config auth |
| `canary_secret_key_file` | empty | Canary secret when using config auth |
| `iam_config_file` | empty | Advanced SeaweedFS IAM configuration |
| `audit_log_config_file` | empty | SeaweedFS audit-log configuration |
| `tls_mode` | `off` | `off` or `terminate` |
| `tls_pem_file` | empty | HAProxy PEM bundle for TLS termination |

When `auth_mode = "config"`, supply canary credentials for an identity able to create and use the health bucket. Disable the built-in canary only when another end-to-end check replaces it.

## Operator commands

```bash
s3-storage-node validate --config /etc/s3-storage-node/config.toml
s3-storage-node health --config /etc/s3-storage-node/config.toml
s3-storage-node transport-status --config /etc/s3-storage-node/config.toml
s3-storage-node transport-select --config /etc/s3-storage-node/config.toml --transport cifs-primary
```

The transport commands require `storage.data.failover.enabled = true`. A selection request is persisted and consumed only when the guardian can safely create the replacement generation.

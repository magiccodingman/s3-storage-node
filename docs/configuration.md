# Configuration reference

Configuration is TOML and defaults to `/etc/s3-storage-node/config.toml`.

## `[appliance]`

| Setting | Default | Meaning |
|---|---:|---|
| `name` | `s3-storage-node` | Logical node name used in logs |
| `state_dir` | `/var/lib/s3-storage-node` | Persistent appliance state |
| `runtime_dir` | `/run/s3-storage-node` | Temporary mount and generated-config directory |
| `uid` / `gid` | `10001` | Unprivileged SeaweedFS identity |
| `health_host` | `0.0.0.0` | Health API bind address |
| `health_port` | `9090` | Health and metrics port |
| `probe_interval_seconds` | `5` | Fast storage check cadence |
| `full_probe_interval_seconds` | `60` | Durable storage and S3 canary cadence |
| `probe_timeout_seconds` | `4` | Deadline for a storage probe subprocess |
| `startup_timeout_seconds` | `30` | Per-process startup deadline |
| `shutdown_grace_seconds` | `20` | Grace before child process SIGKILL |
| `recovery_initial_seconds` | `5` | Initial retry delay |
| `recovery_max_seconds` | `60` | Maximum exponential retry delay |
| `s3_canary_enabled` | `true` | Require end-to-end S3 PUT/GET/DELETE checks |

## Storage targets

Targets are declared under `[storage.data]`, `[storage.metadata]`, and `[storage.index]`. `data` is required. Metadata and index targets are required when referenced by their logical sections.

Common settings:

| Setting | Meaning |
|---|---|
| `type` | `cifs`, `block`, or `path` |
| `mountpoint` | Guardian-visible target root |
| `subdirectory` | Optional base directory on this physical target |
| `sentinel_id` | Stable expected identity value |
| `sentinel_file` | Sentinel filename; defaults to `.s3-storage-node.json` |
| `allow_initialize` | Permit first-time sentinel/directory creation |
| `min_free_bytes` | Hard free-space floor |

### CIFS settings

```toml
source = "//server/share"
credentials_file = "/run/secrets/cifs_credentials"
mount_options = ["vers=3.1.1", "uid=10001", "gid=10001"]
```

Credentials use `mount.cifs` format:

```ini
username=user
password=password
domain=
```

Avoid putting passwords in TOML or mount options.

### Block settings

```toml
device = "/dev/storage-data"
expected_uuid = "filesystem-uuid"
expected_filesystem = "xfs"
mount_options = ["noatime"]
```

The device must be mapped into the container. The appliance never formats it.

### Path settings

```toml
type = "path"
mountpoint = "/var/lib/s3-storage-node/metadata"
```

A path target is not mounted by the guardian. For paths beneath the appliance state directory, `allow_initialize = true` allows the guardian to create the path. External paths must already exist, must contain the expected sentinel, and must use `allow_initialize = false`. This ensures a missing host-managed mount cannot be silently enrolled as ordinary local storage.

## `[metadata]`

### Embedded metadata

```toml
[metadata]
backend = "embedded"
target = "metadata"
directory = "filer"
```

The appliance generates a SeaweedFS `leveldb2` filer configuration in the selected target. `directory` is relative to that target's base `subdirectory`.

### PostgreSQL metadata

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

Add the secret to Compose:

```yaml
services:
  s3-storage-node:
    secrets:
      - postgres_password
secrets:
  postgres_password:
    file: ./secrets/postgres-password
```

Loss of PostgreSQL causes the complete S3 endpoint to fail its startup or S3 canary and be withdrawn. Do not run destructive SeaweedFS maintenance against a filer whose metadata database is unavailable.

### Custom filer configuration

```toml
[metadata]
backend = "custom"
custom_filer_toml = "/run/secrets/filer_toml"
```

The appliance copies the supplied file into its generated runtime configuration.

## `[index]`

```toml
[index]
target = "index"
directory = "volume-indexes"
```

The selected target becomes SeaweedFS `-dir.idx`. It must remain persistent. Set `target = "data"` to keep indexes on the bulk target while retaining a distinct directory.

## `[seaweed]`

| Setting | Default |
|---|---:|
| `volume_directory` | `volumes` |
| `master_port` | `9333` |
| `volume_port` | `8080` |
| `filer_port` | `8888` |
| `s3_internal_port` | `18333` |
| `volume_max` | `0` (automatic based on capacity) |
| `volume_size_limit_mb` | `30000` |
| `default_replication` | `000` |
| `filer_max_mb` | `16` |
| `data_center` | empty |
| `rack` | empty |
| `disk_type` | empty |
| `encrypt_volume_data` | `false` |

Raw argument arrays are available:

```toml
master_extra_args = []
volume_extra_args = []
filer_extra_args = []
s3_extra_args = []
```

Do not try to override guardian-owned data, index, filer-store, bind, or internal dependency paths. Conflicting arguments may make startup fail and are unsupported.

Version 1 starts one guarded volume server. Keep `default_replication = "000"` unless additional SeaweedFS volume servers have been deliberately supplied outside this appliance. The guardian does not yet supervise a distributed SeaweedFS cluster, and replica placements that cannot be satisfied cause writes to fail.

## `[s3]`

| Setting | Default | Meaning |
|---|---:|---|
| `host` | `0.0.0.0` | Public HAProxy bind address |
| `port` | `8333` | Public container S3 port; must be 1024 or greater |
| `domain_name` | empty | Virtual-hosted-style bucket domain suffix |
| `allowed_origins` | `*` | SeaweedFS S3 CORS origins |
| `external_url` | empty | URL used for signature verification behind a proxy |
| `auth_mode` | `static` | `static`, `config`, or `none` |
| `access_key_file` | Docker secret path | Static admin access key |
| `secret_key_file` | Docker secret path | Static admin secret |
| `auth_config_file` | empty | Full SeaweedFS S3 identity JSON |
| `canary_access_key_file` | empty | Credential used by the appliance canary with config auth |
| `canary_secret_key_file` | empty | Secret used by the appliance canary with config auth |
| `iam_config_file` | empty | Advanced SeaweedFS IAM configuration |
| `audit_log_config_file` | empty | SeaweedFS audit-log configuration |
| `tls_mode` | `off` | `off` or `terminate` |
| `tls_pem_file` | empty | HAProxy PEM bundle for termination |

When `auth_mode = "config"`, provide canary credential files for an identity allowed to create and use the health bucket. Alternatively, disable the built-in canary only when another end-to-end external check replaces it.

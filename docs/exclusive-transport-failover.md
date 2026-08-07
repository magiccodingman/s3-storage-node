# Exclusive CIFS-to-SSHFS transport failover

The data target can expose one logical dataset through a preferred CIFS route and one or more exclusive SSHFS recovery routes. This is generation-level failover, not per-I/O switching. Exactly one route is writable at a time, and every replacement generation is physically network-fenced from the old one first.

## Authentication choices

SSHFS supports either password or private-key authentication. Both modes always require a pinned `known_hosts_file`; client authentication and server identity verification are separate concerns.

### Password authentication

Password mode accepts the same `username=...` and `password=...` secret format used by `mount.cifs`:

```toml
[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
primary_priority = 10
failback_policy = "manual"
failure_cooldown_seconds = 60
verify_all_transports_on_startup = true

[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
priority = 20
source = "u123456@u123456.your-storagebox.de:/home"
auth_mode = "password"
known_hosts_file = "/run/secrets/ssh_known_hosts"
port = 23
mount_options = []
```

When `credentials_file` is omitted, password mode reuses the canonical `[storage.data].credentials_file`. This directly supports providers such as Hetzner Storage Box where SMB and SSH/SFTP use the same account. A separate secret can be selected explicitly:

```toml
credentials_file = "/run/secrets/ssh_credentials"
```

The username in `source` must match the credentials file. The password is delivered only through SSHFS standard input using `password_stdin`; it is never included in TOML, process arguments, environment variables, health output, or logs.

### Key authentication

```toml
[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
priority = 20
source = "user@storage.example:/remote/path"
auth_mode = "key"
identity_file = "/run/secrets/ssh_identity"
known_hosts_file = "/run/secrets/ssh_known_hosts"
port = 22
mount_options = []
```

Existing configurations that provide `identity_file` without `auth_mode` remain key mode. Before mounting, the helper copies the read-only key into the ephemeral runtime directory with mode `0600`.

If `auth_mode` is omitted and only `credentials_file` is present, password mode is inferred. Explicit mode is recommended for new deployments.

## Host verification

`known_hosts_file` is mandatory in both modes. The guardian always enables strict host-key checking and fails closed if the server key is missing or changes. Do not use `StrictHostKeyChecking=no` for an unattended storage writer.

## Startup verification

With the default:

```toml
verify_all_transports_on_startup = true
```

the guardian sequentially certifies every configured route before first use:

1. mount and authenticate one route;
2. verify the expected SMB/SSH endpoint and shared dataset sentinel;
3. run the persistent append and temporary write/read/delete durability probes;
4. unmount it before testing the next route.

Only one route is mounted at a time. Successful verification is persisted against a SHA-256 fingerprint of the transport settings and credential, private-key, and known-hosts files. Secret contents are not stored. Changing any of those inputs invalidates the marker and forces all-route verification again.

A routine restart with unchanged verified inputs does not require every alternate route to be online. The selected serving route is still mounted, sentinel-checked, and durability-probed on every generation before readiness opens.

Set:

```toml
verify_all_transports_on_startup = false
```

when an intentionally unavailable alternate must not block initial deployment. This disables only inactive-route preflight; it never disables certification of the selected serving route.

Startup-verification failures are attributed to the route actually being tested rather than automatically condemning the selected route.

## Docker

The standard `docker-compose.yml` includes `/dev/fuse` and both SSH secret mounts, so CIFS-only, SSHFS password, and SSHFS key deployments all use the same command:

```bash
docker compose up -d
```

Create the SSH secret files before the first Compose start. CIFS-only deployments may leave them empty because the guardian reads them only when an SSHFS route is configured:

```bash
touch secrets/ssh-identity secrets/ssh-known-hosts
chmod 600 secrets/ssh-identity secrets/ssh-known-hosts
```

Password mode reusing CIFS credentials requires a populated pinned host-key file:

```text
secrets/cifs-credentials
secrets/ssh-known-hosts
```

Key mode additionally requires:

```text
secrets/ssh-identity
```

SSHFS requires a usable host `/dev/fuse`. Namespace fencing continues to use the `SYS_ADMIN` and `NET_ADMIN` capabilities already granted by the standard Compose file.

## Selection and failback

The lowest-priority-number eligible route is selected initially. A transport mount, authentication, sentinel, or durability failure withdraws readiness, fences the generation, records that route as failed, and allows the next eligible route after recovery backoff. Generic SeaweedFS or HAProxy failures do not condemn a transport.

Fallback is sticky. Recovery of CIFS does not automatically move a healthy SSHFS generation back. Request controlled failback with:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-select \
  --config /etc/s3-storage-node/config.toml \
  --transport cifs-primary
```

The guardian drains SeaweedFS, cleanly detaches the healthy route, fences the old generation, certifies the requested route in a new generation, and only then restores readiness.

Inspect transport and startup-verification state with:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-status --config /etc/s3-storage-node/config.toml
```

Credential fingerprints are never exposed.

## Integration certification

CI uses real OpenSSH and real SSHFS mounts for both password and key authentication, including sentinel validation and full durability probes. The Docker chaos harness continues to exercise CIFS failure, physical fencing, SSHFS recovery, sticky fallback, controlled failback, guardian restart, object preservation, and Sentinel V1 compatibility.

## Deliberate limitations

- No simultaneous CIFS and SSHFS writers.
- No path-layer or per-I/O fallback.
- No automatic failback.
- CIFS and SSHFS to the same provider remain one failure domain, not two replicas.
- No direct SSH-only storage profile.
- No claim that an SSHFS acknowledgement proves physical-media persistence on the remote server.

# Storage backends

S3 Storage Node supports a deliberately narrow set of storage profiles. The goal is not to make every remote protocol appear file-like; it is to preserve a small set of failure semantics that the guardian can observe, fence, repair, and certify.

## CIFS / SMB

CIFS is the primary managed network-filesystem backend. It provides kernel filesystem semantics for SeaweedFS's mutable volume files and allows the guardian to replace a stale session by fencing its generation, detaching the mount, and creating a new mount.

Recommended baseline:

```toml
[storage.data]
type = "cifs"
io_failure_policy = "soft"
handle_reconnect_policy = "auto"
minimum_smb_dialect = "3.1.1"
multichannel_policy = "disabled"
require_transport_observability = false
mount_options = [
  "vers=3.1.1",
  "uid=10001",
  "gid=10001",
  "forceuid",
  "forcegid",
  "file_mode=0660",
  "dir_mode=0770",
  "noperm",
  "noserverino",
  "iocharset=utf8",
  "echo_interval=15",
  "actimeo=1"
]
```

Adjust protocol version and server-specific options only after testing the exact client kernel, server, and outage behavior.

### I/O failure policy

The guardian models CIFS `soft` or `hard` behavior explicitly:

- `soft` is the default. Failed server operations can return errors to SeaweedFS, allowing the guardian to withdraw the endpoint and rebuild the generation.
- `hard` keeps retrying unavailable I/O and can trap SeaweedFS in blocked kernel work. The guardian will not start a replacement while an old process remains alive and still owns shared local master, filer, or index state.

Put the decision in `io_failure_policy`, not `mount_options`. Legacy raw `soft` or `hard` flags are canonicalized; contradictory or duplicate policies are rejected.

### Transport certification

The guardian does not consider a listed mount sufficient. It combines mount identity with active I/O and, when configured, negotiated transport details such as:

- effective I/O failure policy;
- reconnect/open-handle profile;
- minimum SMB dialect;
- multichannel policy and channel ceiling;
- whether the client kernel exposes enough information to certify the transport.

A mount that remains listed but times out, returns `Host is down`, fails `fsync`, exposes the wrong source, or cannot meet required transport policy is treated as offline.

## SSHFS as an exclusive recovery transport

SSHFS is supported only as an optional recovery route for the canonical CIFS **data** target. It is not a general primary backend and is not available for independent `storage.metadata` or `storage.index` targets.

```toml
[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
failback_policy = "manual"

[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
source = "user@host:/remote/path"
identity_file = "/run/secrets/ssh_identity"
known_hosts_file = "/run/secrets/ssh_known_hosts"
port = 22
```

The profile has strict boundaries:

- the canonical target remains CIFS;
- worker namespace fencing is mandatory;
- only one transport is mounted for a generation;
- CIFS and SSHFS must expose the same dataset root and sentinel;
- fallback is sticky and failback is manual;
- a restored CIFS server does not move an online SSHFS generation automatically;
- SSHFS and CIFS are one storage failure domain, not two replicas;
- no per-I/O switching or union filesystem sits beneath SeaweedFS.

The guardian supplies strict host-key checking, batch authentication, reconnect and keepalive settings, synchronous SSHFS writes, guarded cache behavior, FUSE permission controls, UID/GID mapping, and umask. Only a small performance-tuning allowlist is exposed through `mount_options`.

Teardown first verifies that SeaweedFS writers stopped, then attempts a normal FUSE unmount. Lazy detach is used only after the clean unmount fails or times out; SSHFS receives `SIGTERM` after detach and `SIGKILL` only as the final bounded escape hatch.

The private key is copied from the read-only secret into the ephemeral runtime directory with mode `0600`; the original secret is not changed. `/dev/fuse` and the SSH secrets are supplied through `docker-compose.sshfs.yml`.

SSHFS acknowledgement still does not prove remote physical-media persistence. The appliance certifies the filesystem semantics visible to its process and does not claim stronger durability than the remote service provides.

See [Exclusive CIFS-to-SSHFS failover](exclusive-transport-failover.md).

## Fixed block devices

Block mode is for an already formatted filesystem exposed to the container as a device. The appliance verifies:

- the configured device path;
- filesystem UUID;
- filesystem type;
- sentinel identity after mounting;
- capacity and active durability probes.

It does not partition, format, repair, decrypt, or change encryption configuration. Filesystem repair remains an operator action because automatic `fsck` during uncertain hardware failure is not a safe generic policy.

Use stable `/dev/disk/by-id` paths on the host and map them to a stable internal device path.

```toml
[storage.data]
type = "block"
device = "/dev/storage-data"
expected_uuid = "11111111-2222-3333-4444-555555555555"
expected_filesystem = "xfs"
mountpoint = "/run/s3-storage-node/mounts/data"
subdirectory = "seaweedfs"
sentinel_id = "archive-disk-01"
```

Namespace network fencing does not make a stuck local block writer harmless. Exclusive transport failover therefore applies only to the CIFS data profile.

## Existing paths

Path mode works for:

- a Docker named volume;
- a bind-mounted host directory;
- local appliance state;
- storage mounted and supervised outside the container.

The guardian verifies sentinel identity, free capacity, and active I/O but does not mount or remount the path.

Automatic initialization is allowed only when the path is beneath `appliance.state_dir`. Any external path must already exist, use `allow_initialize = false`, and contain its sentinel before startup. This prevents a vanished host-managed mount from being enrolled as an ordinary local directory.

For new external paths, create a Sentinel V2 document deliberately. Existing V1 documents remain supported. See [Dataset sentinel format](sentinel-format.md) for the exact schema rather than copying an incomplete ad hoc example.

## Metadata and index placement

The data, embedded metadata, and index roles use the same `cifs`, `block`, or `path` target abstraction, but only the canonical data target can have exclusive SSHFS recovery.

A common layout is:

```text
remote CIFS/SSHFS dataset
└── volume .dat files

local persistent path targets
├── embedded filer metadata
└── volume .idx files
```

Separating metadata and indexes improves latency and prevents every small metadata operation from traversing the network share. It does not create a replica: losing any active role withdraws the complete endpoint.

PostgreSQL may replace the embedded filer metadata target. That integration stores SeaweedFS filer metadata only and is unrelated to data-transport selection or distributed writer ownership.

## Unsupported layers

The appliance does not support the following beneath live SeaweedFS volume files:

- rclone mounts;
- WebDAV mounts;
- union or overlay filesystems combining transports;
- dual-mounted CIFS and SSHFS writers;
- direct SSH-only data profiles;
- local NVMe write-back or deferred remote flush queues;
- transparent per-I/O fallback.

Those designs introduce additional caching, acknowledgement, ordering, ownership, or split-brain semantics that the current guardian does not claim to fence or certify.

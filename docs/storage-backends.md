# Storage backends

## CIFS / SMB

CIFS is the primary network-filesystem backend in version 1. It provides direct kernel filesystem semantics for SeaweedFS's large mutable volume files and allows the guardian to repair a stale session by detaching and creating a fresh mount.

Recommended baseline options:

```toml
io_failure_policy = "soft"
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

Adjust protocol version and server-specific options as needed.

`mount.cifs` uses `soft` behavior by default, so omitting both `soft` and `hard` did not avoid soft semantics. The appliance now models this explicitly with `io_failure_policy` and always includes the selected policy in the mount command:

- `soft` is the default and preserves the existing runtime behavior. Server failure can return an error to SeaweedFS, allowing the guardian to withdraw the endpoint and begin recovery. An interrupted storage operation must still be treated as failed and is covered by the existing fail-closed lifecycle.
- `hard` keeps retrying I/O when the server is unavailable. This can avoid early application-visible errors, but it can also trap SeaweedFS in blocked kernel I/O until the server returns. The guardian will not start an overlapping SeaweedFS generation while an old process remains alive.

Use `hard` only after testing the exact kernel, server, and outage modes used by the deployment. Put the choice in `io_failure_policy`, not `mount_options`. A legacy configuration containing one of the raw flags is canonicalized; contradictory policies are rejected.

The configured policy is included in storage health results as `configured_io_failure_policy`.

The guardian checks both `/proc/self/mountinfo` and active I/O. A mount that remains listed but returns `Host is down`, stalls, or fails `fsync` is treated as offline.

## Fixed block devices

Block mode is for an already formatted filesystem exposed to the container as a device. The appliance verifies:

- configured device path;
- filesystem UUID;
- filesystem type;
- sentinel ID after mounting.

It does not partition, format, repair, or change encryption configuration. Filesystem repair remains an operator action because automatic `fsck` during uncertain hardware failure is not a safe generic policy.

Use stable `/dev/disk/by-id` paths on the host and map them to a stable internal path.

## Existing paths

Path mode works for:

- a Docker named volume;
- a bind-mounted host directory;
- local appliance state;
- storage mounted and supervised outside the container.

The guardian verifies sentinel, capacity, and active I/O, but does not mount or remount a path target. Self-healing of an externally managed mount remains external.

Automatic initialization is allowed only when the path is beneath `appliance.state_dir`. Any external path must already exist, must use `allow_initialize = false`, and must contain its sentinel before the appliance starts. Create the storage root and sentinel deliberately on the real backing storage:

```bash
mkdir -p /actual/storage/root
printf '%s\n' '{"sentinel_id":"external-data-01","target":"data","version":1}' \
  > /actual/storage/root/.s3-storage-node.json
```

This makes a vanished external mount fail closed: the ordinary directory beneath it does not contain the expected sentinel, and the guardian is forbidden from initializing it.

## Why SSHFS, rclone, and WebDAV are not included

SeaweedFS volume `.dat` files are long-lived, mutable, append-oriented files. A userspace layer may cache writes locally and report filesystem completion before the remote backend has durably received the new bytes. That creates a second durability boundary below SeaweedFS.

Those adapters are not rejected forever; they are excluded from the supported version 1 surface until crash, reconnect, compaction, delayed flush, and remote durability behavior is proven. The current image therefore does not install or advertise them.

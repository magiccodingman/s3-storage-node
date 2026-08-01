# Architecture and invariants

S3 Storage Node is an appliance supervisor around a single-node SeaweedFS deployment. It is intended for storage targets whose availability cannot be assumed continuously.

## Components

The container runs these processes:

1. **Guardian** — PID 1 beneath `tini`; owns configuration, mounts, probes, process order, recovery, and health state.
2. **HAProxy** — always listens on the configured public S3 port. Its backend is healthy only when the guardian's `/ready` endpoint returns `200`.
3. **SeaweedFS master** — stores topology state under the appliance persistent state directory.
4. **SeaweedFS volume server** — stores `.dat` files on the configured data target and `.idx` files on the configured index target.
5. **SeaweedFS filer** — stores namespace/object metadata using either an embedded target or PostgreSQL.
6. **SeaweedFS S3 gateway** — listens only on loopback. It is never exposed directly.

## Safety invariants

### SeaweedFS never owns mount lifecycle

SeaweedFS receives only guardian-controlled paths. It never receives a CIFS source, block device, host device identifier, or credentials.

### Missing managed mountpoints are not writable

Before mounting a CIFS share or block device, the guardian creates its mountpoint as `root:root` with mode `000`. SeaweedFS runs as UID/GID `10001`.

When the target is mounted, the mounted filesystem's permissions apply. When it is absent, the underlying local directory is inaccessible. This prevents local fallback volume creation even if guardian startup logic is faulty.

### Target identity precedes service startup

A target must pass all applicable checks:

- mounted at the expected path;
- expected filesystem type;
- expected CIFS source or block-device UUID;
- matching sentinel ID;
- configured subdirectory available;
- sufficient free capacity;
- write, file `fsync`, read-back, checksum, and delete. Directory `fsync` is also used when the backend supports it.

No SeaweedFS process starts until active targets pass.

### Any active target failure withdraws the endpoint

Version 1 uses endpoint-wide fail-closed behavior. If any active data, embedded metadata, or index target fails, the entire SeaweedFS stack is stopped. This avoids partial behavior where namespace mutations remain possible while object payload storage is unavailable.

### Readiness requires end-to-end proof

A listening S3 socket is insufficient. The guardian performs an S3 canary through SeaweedFS after startup and periodically while online. HAProxy forwards public traffic only while the guardian remains ready.

### The guardian does not synchronously trust remote I/O

Potentially blocking mount, unmount, enrollment, layout, and storage probe operations run in subprocesses with deadlines. If the kernel blocks a filesystem syscall, the parent guardian can still withdraw traffic, stop SeaweedFS, and detach the mount.

## State machine

```text
BOOTSTRAPPING
  → MOUNTING
  → VERIFYING_STORAGE
  → STARTING_SEAWEED
  → ONLINE

ONLINE failure
  → DRAINING
  → OFFLINE
  → RECOVERING
  → MOUNTING
```

A configuration or identity failure leaves the endpoint unavailable and is repeatedly logged. The guardian does not initialize an unknown target unless `allow_initialize` is explicitly enabled.

## Persistence domains

```text
/var/lib/s3-storage-node/master
    SeaweedFS master state

configured metadata target
    embedded filer database, unless PostgreSQL/custom is selected

configured index target
    SeaweedFS volume indexes

configured data target
    SeaweedFS volume data
```

The image and runtime directory are disposable. `/run/s3-storage-node` is temporary and contains generated configuration, mountpoints, HAProxy state, and runtime secrets derived from Docker secrets.

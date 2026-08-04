# Exclusive CIFS-to-SSHFS transport failover

The data target can optionally expose the same remote dataset through a preferred CIFS transport and one or more SSHFS recovery transports. This is generation-level failover, not per-I/O path switching.

## Safety invariants

- Exactly one transport is selected for a worker generation.
- An unexpected storage failure withdraws readiness and network-fences the old generation before SeaweedFS shutdown or transport replacement.
- A controlled operator switch withdraws readiness, drains the sole active generation while its transport is still healthy, then network-fences it before unmounting or selecting the replacement transport.
- No replacement generation is started until the old generation is physically fenced.
- A replacement generation is never started while an unfenced old SeaweedFS process still owns local master, filer, or index state.
- CIFS and SSHFS must expose the same sentinel and SeaweedFS volume directory.
- Two protocols to one storage service are reported as one failure domain, not two replicas.
- Failback is manual and sticky. A healthy SSHFS recovery generation remains active until an operator requests CIFS or the SSHFS transport itself fails.

SSHFS reconnect is enabled to bound ordinary interruptions, but the guardian does not treat it as transparent file-descriptor failover. Any storage error still withdraws readiness and rebuilds the worker generation.

## Shared dataset identity

CIFS and SSHFS are different routes to one logical dataset. New enrollments use the transport-independent Sentinel V2 schema, while existing V1 sentinels remain accepted. The sentinel validates the dataset ID, target role, and subdirectory without encoding a protocol, server address, or mountpoint.

See [Dataset sentinel format](sentinel-format.md).

## Configuration

The existing `[storage.data]` target remains the canonical CIFS primary. Add a nested failover table:

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

Namespace fencing is mandatory:

```toml
[appliance]
worker_fencing_mode = "namespace"
```

The guardian owns authentication, host verification, timeout, reconnect, synchronous-write, FUSE permission, UID/GID, and umask options. `mount_options` uses a narrow performance-tuning allowlist; unknown or security-sensitive SSH/FUSE options are rejected so configuration cannot silently weaken the failover profile.

The private key may be supplied as a read-only Docker secret. Before mounting, the helper copies it into the ephemeral runtime directory with mode `0600`; the source secret is never modified.

## Docker

SSHFS requires `/dev/fuse`. Start with the optional Compose overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.sshfs.yml up -d
```

Create the two secret files referenced by the overlay:

```text
secrets/ssh-identity
secrets/ssh-known-hosts
```

Populate `ssh-known-hosts` out of band from a trusted host-key source. The guardian always enables strict host-key checking and batch authentication.

## Selection behavior

On first startup, the lowest-priority-number transport is selected. A transport-related mount, enrollment, or storage-probe failure is recorded, the generation is fenced before shutdown, and the next eligible transport is selected after recovery backoff. Generic SeaweedFS or HAProxy failures do not condemn the active transport.

A successful fallback is sticky. Cooldown expiry does not automatically move traffic back to CIFS. When every configured transport is cooling down, the node remains offline until one is eligible rather than immediately recycling a known-failed path.

Inspect state:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-status --config /etc/s3-storage-node/config.toml
```

Request a controlled switch:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-select \
  --config /etc/s3-storage-node/config.toml \
  --transport cifs-primary
```

The running guardian notices the request and withdraws readiness. Because this is an explicit switch rather than an unexpected storage fault, it first drains the only active SeaweedFS generation while that transport is still healthy. It then network-fences and retires the generation, mounts the requested transport in a new generation, runs full durability probes and the S3 canary, and only then returns online. The persisted request is not consumed until the guardian has verified that no prior process blocks creation of the replacement generation.

## Combined chaos certification

CI includes a privileged Docker lab rather than relying only on mocked transport selection. A Samba container and an OpenSSH/SFTP container expose the same backing volume to the real appliance image.

The harness performs the following sequence:

1. Start the appliance on CIFS and create a Sentinel V2 document.
2. Upload and retrieve randomized signed S3 objects through HAProxy.
3. Drop port 445 packets while the node is online.
4. Verify readiness withdrawal and persistent recording of the CIFS failure.
5. Verify the old generation veth is absent before replacement.
6. Recover through SSHFS and read every pre-failure object.
7. Write more S3 objects on SSHFS.
8. Restore Samba and prove there is no automatic failback.
9. Request controlled failback and verify every object through CIFS.
10. Fail CIFS again, kill the guardian after the failure is persisted, and verify restart resumes on SSHFS.
11. Replace only the sentinel with the legacy V1 form, restart again, and prove V1 remains accepted without being rewritten.

Run it explicitly with:

```bash
RUN_TRANSPORT_CHAOS=1 PYTHONPATH=src \
  python -m pytest -q -s tests/test_transport_chaos_integration.py
```

The harness requires Docker Compose, `/dev/fuse`, and a host kernel that permits CIFS and namespace mounts inside the test container.

## Deliberate limitations

- No simultaneous CIFS and SSHFS writers.
- No path-layer or per-I/O fallback.
- No automatic failback.
- No cross-host shared-dataset ownership; one appliance owns one dataset.
- No direct SSH-only storage profile.
- No NVMe write-back, queueing, or tiered-storage semantics.
- No claim that an SSHFS acknowledgement proves physical-media persistence on the remote server.

# Exclusive CIFS-to-SSHFS transport failover

The data target can optionally expose the same remote dataset through a preferred CIFS transport and one or more SSHFS recovery transports. This is generation-level failover, not per-I/O path switching.

## Safety invariants

- Exactly one transport is selected for a worker generation.
- The old generation is withdrawn and network-fenced before SeaweedFS shutdown or transport replacement.
- A replacement generation is never started while an old SeaweedFS process still owns local master, filer, or index state.
- CIFS and SSHFS must expose the same sentinel and SeaweedFS volume directory.
- Two protocols to one storage service are reported as one failure domain, not two replicas.
- Failback is manual and sticky. A healthy SSHFS recovery generation remains active until an operator requests CIFS or the SSHFS transport itself fails.

SSHFS reconnect is enabled to bound ordinary interruptions, but the guardian does not treat it as transparent file-descriptor failover. Any storage error still withdraws readiness and rebuilds the worker generation.

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

On first startup, the lowest-priority-number transport is selected. A transport-related mount, enrollment, or storage-probe failure is recorded, the generation is fenced, and the next eligible transport is selected after recovery backoff. Generic SeaweedFS or HAProxy failures do not condemn the active transport.

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

The running guardian notices the request, withdraws readiness, fences and retires the current generation, mounts the requested transport in a new generation, runs full durability probes and the S3 canary, and only then returns online.

## Deliberate limitations

- No simultaneous CIFS and SSHFS writers.
- No path-layer or per-I/O fallback.
- No automatic failback.
- No cross-host distributed writer lease yet.
- No direct SSH-only storage profile yet.
- No NVMe write-back, queueing, or tiered-storage semantics.
- No claim that an SSHFS acknowledgement proves physical-media persistence on the remote server.

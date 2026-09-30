# Kernel-blocked transport recovery

This update separates recoverable transport failures from blocked tasks that
cannot safely be replaced. It does not weaken the single-writer rule or assume
network isolation discards old buffered writes.

## Persistent quarantine

After a verified fence, any remaining writer or storage-helper process causes
the selected transport to be quarantined in the local durable transport journal.
Quarantine survives cooldowns, supervisor/container restarts and host reboots.
Normal successful probes and ordinary transport selection do not clear it.
An unreadable/corrupt transport journal fails closed rather than silently losing
quarantine. Startup certification skips quarantined transports and explicitly
reports only those actually verified. Clearance invalidates certification.

The existing operator commands use the configured local state volume:

```sh
s3-storage-node transport-status --config /etc/s3-storage-node/config.toml
s3-storage-node transport-quarantine --config /etc/s3-storage-node/config.toml \
  --transport cifs-primary --reason 'kernel-blocked writeback; operator quarantine'
s3-storage-node transport-select --config /etc/s3-storage-node/config.toml \
  --transport sshfs-secondary
```

The manual quarantine command does not terminate a currently active writer.
It is appropriate when carrying a known prior-version incident into an upgrade.
Run all commands against the same production state volume, not a new empty volume.
Do not start a second guardian or SeaweedFS writer to run operator commands.

To deliberately make a transport eligible again:

```sh
s3-storage-node transport-clear-quarantine --config /etc/s3-storage-node/config.toml \
  --transport cifs-primary
```

Clearance does not switch transports or bypass old-task/writer fencing. Request
a controlled switch separately only after investigating and clearing the fault.
If all transports are quarantined, selection stays offline for operator review.

## Blocked recovery health

After retiring/fencing a failed generation, the guardian checks remaining tasks
without mounting or starting replacement writers. While tasks remain it reports
`HOST_RECOVERY_REQUIRED`, readiness 503, and `blocked_recovery` containing PIDs,
transport, elapsed time and the diagnostics directory. The manual-intervention
Prometheus gauge is 1. There is no automatic host reboot. If tasks later exit,
the guardian automatically resumes ordinary exclusive transport selection;
the failed transport remains quarantined. A stuck kernel task may require a
host reboot before any replacement writer can safely start.

## Before-fence evidence

Failures with an active generation collect bounded local snapshots before drain
and immediately before a hard fence. Files live under
`<state_dir>/guardian/diagnostics/<generation>-before-{drain,fence}.json`.
Snapshots contain blocked task stacks, kernel version and selected CIFS
credit/connection counters, not credentials or remote filesystem contents.
Collection has a two-second supervisor wait budget; diagnostic failures never
authorize an unsafe replacement. Do not confuse post-fence reconnect/credit
failures with proof of the initial cause. Retain relevant snapshots for diagnosis;
there is no automatic deletion of incident evidence.

## September 30 production recovery plan

The v0.3.9 incident began at approximately 2:20 AM EDT. A CIFS writeback worker
waited for SMB credits, while SeaweedFS exit waited on netfs writeback. Fencing
removed the old generation's network but surviving tasks prevented failover.
The initial trigger is not conclusively attributed to load, network, server,
or a particular upstream kernel bug.

For the authorized upgrade, preserve deployment config/image rollback references,
clear the existing host kernel state only with reboot approval, quarantine
`cifs-primary`, and explicitly select the already-configured `sshfs-secondary`
before starting the new guardian. Use the same Storage Box and data directories,
namespace fencing, durability checks and guarded index recovery. Keep the initial
one-write admission ceiling, four queued writes and three-second queue timeout.
Validate ONLINE readiness, expected read-only IDs 63/64, index certification and
authenticated public PUT/GET/DELETE. Only then retry Harbor while observing health.

SSHFS is not a separate provider or a promise of zero outages: it avoids the
specific host CIFS writeback path and supports the existing tested failover model.
Do not automatically fail back to CIFS. A future opt-in CIFS `cache=none` experiment
can use existing mount options, preserving fsync behavior; it bypasses normal
read/write caching but not mmap caching and requires separate performance and
durability validation. It is not part of this production recovery rollout.

## Verification

Tests cover persistence past cooldown/restart, corrupt journal rejection, pending
requests unable to bypass quarantine, skipped startup mounts, exact pre-fence
snapshot ordering, and a surviving-writer fault model that cannot start a new
generation. Existing real CIFS/SSHFS namespace and transport-chaos CI remain
required. A deterministic fault model is not proof of reproducing the exact
production kernel deadlock.

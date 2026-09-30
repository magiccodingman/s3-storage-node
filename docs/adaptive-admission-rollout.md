# Adaptive admission: prepared rollout, not deployed

Production must remain unchanged until the operator explicitly authorizes the swap.
The deployment is `/opt/s3-storage-node`; implementation work is separate in
`/root/s3-storage-node-dev`. Do not restart the container, rewrite its live config,
stress-test the public endpoint, or manipulate the storage mounts while preparing.

## Incident context

On September 29, 2026 (EDT), Harbor upload traffic encountered stalled writes,
queue timeouts, a failed storage probe, and a kernel CIFS nonresponse warning.
The latest guarded offline interval was 9:26:41–9:32:54 PM EDT
(September 30, 01:26:41–01:32:54 UTC). Requests were failing earlier.
Automatic generation fencing/recovery restored service without a host reboot
or container restart. Volume status reported 75 volumes and no unexpected
read-only volumes; this was not a full data-integrity audit. The causal split
between workload pressure, client CIFS behavior, network, and provider remains
unproven. Do not describe it as a confirmed provider outage.

An isolated real-HAProxy test uncovered that the previous admission ACL watched
only the per-server queue, missing requests in the backend queue. The new ACL
uses the total backend queue, including server queues. Adaptive admission and
small-operation queue priority complement the existing recovery safeguards.

Privileged CI also exposed an existing SSHFS cleanup race: an exited direct child
can remain visible in `/proc` until its parent waits, causing a false blocked-task
failure. Cleanup now attempts a nonblocking wait for that exact PID. Live or
non-child processes still retain the prior conservative refusal-to-replace guard.

## Before the authorized swap

1. Confirm PR CI, including Docker build, real HAProxy admission testing, namespace
   fencing and transport chaos tests. Merge to `main`, then publish through the
   existing `release` branch workflow. Record the published immutable image digest.
2. Preserve the current image/tag, compose configuration and config as rollback
   references. Never delete images/backups to prepare this change without review.
3. Prepare the following admission settings for deployment. The current config
   explicitly contains the old queue values, so new defaults alone will not replace
   them. Prepare this edit outside the live deployment until approval.

```toml
[s3.admission]
enabled = true
adaptive_enabled = true
slow_probe_seconds = 2
healthy_window_seconds = 300
max_active_read_requests = 16
max_active_write_requests = 1
max_queued_read_requests = 32
max_queued_write_requests = 4
queue_timeout_seconds = 3
```

The first production validation pins the ceiling to **one** write. Adaptive ramp
to two is supported and tested, but should not be enabled during the first Harbor
trial. Offline/slow/queued/stale feedback still resets the healthy window.

## After explicit deployment approval

Use the existing production compose/security settings and storage configuration;
do not instantiate a second writer against the same real volumes. Perform the
normal guarded replacement, then verify ONLINE readiness, expected volume status
(63/64 deliberately read-only), index certification, and authenticated S3 behavior.
Do not manually kill or detach blocked storage processes to speed a rollout.

Only with the operator's approval, retry a Harbor push and observe probe duration,
admission active/queue counts, 503s, control errors, and kernel CIFS reconnects.
The desired write limit is confirmed only when `admission.applied=true`.
Verify backups and small queued operations continue progressing. A retryable
503 under load is expected backpressure, not permission to restart the appliance.

If probes stall even at one write, collect request sizes/timing and storage/kernel
logs before further tuning. This would be evidence that admission alone is
insufficient, not grounds to increase the queue or concurrency. Do not promise
that throttling eliminates independent remote-storage outages.

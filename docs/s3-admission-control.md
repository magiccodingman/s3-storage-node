# Bounded S3 admission control

S3 Storage Node places HAProxy in front of the SeaweedFS S3 gateway. The admission controller uses that existing boundary to prevent a burst of client requests from driving more concurrent work into SeaweedFS than the storage path can safely sustain.

The complete sample is available in `config/config.toml.example`. Admission control is enabled by default:

```toml
[s3.admission]
enabled = true
adaptive_enabled = true
slow_probe_seconds = 2
healthy_window_seconds = 300
max_active_read_requests = 16
max_active_write_requests = 2
max_queued_read_requests = 32
max_queued_write_requests = 4
queue_timeout_seconds = 3
```

## Behavior

The limits form independent read and write budgets. `GET`, `HEAD`, and `OPTIONS` use the read pool. Mutating and metadata methods use the deliberately smaller write pool so a burst of multipart uploads cannot starve health and recovery reads.

With the defaults:

1. Up to 16 reads and initially **one write** are forwarded concurrently. Two is the adaptive write ceiling, not the startup allowance.
2. Up to 32 reads and 4 writes remain pending in their separate HAProxy queues.
3. A request that waits longer than 3 seconds receives HTTP `503 Service Unavailable`.
4. A request arriving after the bounded queue is full receives HTTP `503 Service Unavailable` immediately.
5. A client that disconnects while queued is removed before its request reaches SeaweedFS.

The write ceiling is deliberately low for remote filesystems. SeaweedFS separately caps total volume-server upload data at 32 MiB by default, approximately two configured 16 MiB filer chunks.

## Configuration reference

| Setting | Default | Meaning |
|---|---:|---|
| `enabled` | `true` | Generate HAProxy admission limits for the public S3 endpoint |
| `adaptive_enabled` | `true` | Adjust the shared write allowance through a private HAProxy runtime socket |
| `slow_probe_seconds` | `2` | Successful or still-running storage probe latency that triggers backoff |
| `healthy_window_seconds` | `300` | Continuous healthy, queue-free time required for each additional write slot |
| `max_active_read_requests` | `16` | Maximum concurrent read-pool requests |
| `max_active_write_requests` | `2` | Adaptive write ceiling; static allowance if adaptation is disabled |
| `max_queued_read_requests` | `32` | Maximum queued read-pool requests |
| `max_queued_write_requests` | `4` | Maximum queued write-pool requests |
| `queue_timeout_seconds` | `3` | Maximum time a request may wait for an active slot |

All numeric settings must be greater than zero. Set `enabled = false` only when another layer provides an equivalent hard active limit and bounded queue.

## Failure semantics

Admission overload is not a storage failure.

A queue rejection or queue timeout:

- does not mark CIFS, SSHFS, block storage, or a path target failed;
- does not trigger transport failover;
- does not fence or replace the active worker generation;
- does not change `/ready` from `200` while the guarded storage and SeaweedFS checks remain healthy.

Clients should treat the returned `503` as retryable and use exponential backoff with jitter. The client-side request timeout must be longer than the configured queue timeout plus the expected execution time of the S3 operation, otherwise the client may abandon a request before HAProxy can forward it.

Queue-full admission responses include an S3 XML `SlowDown` error and `Retry-After: 3`. HAProxy's native queue-timeout and offline responses remain retryable 503s but do not necessarily include that header. Not all SDKs honor Retry-After; clients still need bounded retries with jitter.

## Adaptive feedback and queue fairness

A separate supervisor thread samples once per second, including while a storage probe is blocked. A slow completed probe, a running probe older than two seconds, stale probe results, offline readiness, or queued writes resets the allowance to one. After five continuous healthy, queue-free minutes it increases by one, up to the configured ceiling. Every worker recovery or proxy restart starts conservatively again. Existing requests are **not cancelled** when the allowance falls; no further requests are admitted beyond the new limit until they finish.

All writes share **one** server budget. Queued Harbor paths containing `/harbor/`, multipart requests with `uploadId`, and requests with a declared Content-Length of at least 1 MiB receive lower queue priority. Other queued operations can overtake them without creating an extra write pool. This is best-effort classification, not a reserved storage partition: chunked uploads without those markers may look like small requests, an active upload cannot be preempted, and ordinary requests can still be rejected when the shared queue is full. Bulk requests can time out under sustained higher-priority traffic.

`/ready`, `/live`, and `/metrics` expose the desired write allowance, observed active/queued writes, backoff count and control errors. `admission.applied` is false when runtime control fails; the desired limit must not then be mistaken for a confirmed proxy limit. The controller attempts a fallback to one and logs errors, but a completely inaccessible runtime socket cannot enforce a reduction of a previously expanded allowance. The configured ceiling and readiness gate remain in place. The mode-0600 runtime socket is internal and must never be published.

HAProxy access logs retain response bytes and execution/queue timers and additionally capture declared Content-Length. This is a workload-size hint, not a measurement of internal filesystem work or chunked request bytes. The probe latency gauge provides the storage feedback signal.

These controls reduce workload-driven pressure; they cannot prevent a remote server/network failure or directly limit all filesystem operations inside one SeaweedFS request. Prove the production benefit with a controlled Harbor retry after deployment, watching latency, queues, 503s, and storage reconnects.

Existing explicit configuration values are preserved: upgrading does **not** replace an old queue limit of 16 or timeout of 10. For the next production swap, deliberately set the new values above. Use `max_active_write_requests = 1` to pin the ceiling to one during the first Harbor validation. Increase to two only after measuring stability. No live configuration change or restart is required to prepare these changes.

## Choosing limits

Set the active limits from load testing against the slowest active transport, not from CPU or memory alone. A remote filesystem can become I/O-bound and stop servicing health probes long before the container exhausts RAM.

Set the queue limits large enough to absorb ordinary bursts but small enough that queued work does not create unacceptable tail latency. A larger queue smooths brief spikes; it does not increase backend throughput.

Set `queue_timeout_seconds` to the maximum useful wait before the client should retry elsewhere or later. Keep upstream proxy and SDK timeouts longer than this value.

When exclusive CIFS-to-SSHFS recovery is configured, the same admission settings protect whichever transport is active. Configure the values for the least capable transport that may serve production traffic, or update and restart the node deliberately when operating with a different capacity profile.

## Generated HAProxy controls

When enabled, the generated HAProxy configuration uses:

- server `maxconn` for the active request ceiling;
- server `maxqueue` for the server-specific bounded queue;
- `timeout queue` for the maximum wait;
- an explicit queue-depth ACL that returns `503` when the bounded queue is full;
- `option abortonclose` so disconnected clients do not leave stale work queued.

The guardian readiness check remains separate from this data-plane queue. Storage and SeaweedFS failures still withdraw the backend through the normal fail-closed recovery path.

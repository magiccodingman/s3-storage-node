# Bounded S3 admission control

S3 Storage Node places HAProxy in front of the SeaweedFS S3 gateway. The admission controller uses that existing boundary to prevent a burst of client requests from driving more concurrent work into SeaweedFS than the storage path can safely sustain.

The complete sample is available in `config/config.toml.example`. Admission control is enabled by default:

```toml
[s3.admission]
enabled = true
max_active_requests = 32
max_queued_requests = 128
queue_timeout_seconds = 30
```

## Behavior

The limits form one aggregate budget shared by all S3 request methods, including reads, writes, lists, deletes, multipart operations, and metadata calls.

With the defaults:

1. Up to 32 requests are forwarded to SeaweedFS concurrently.
2. Up to 128 additional requests remain pending in HAProxy while they wait for a SeaweedFS slot.
3. A request that waits longer than 30 seconds receives HTTP `503 Service Unavailable`.
4. A request arriving after the bounded queue is full receives HTTP `503 Service Unavailable` immediately.
5. A client that disconnects while queued is removed before its request reaches SeaweedFS.

The aggregate ceiling is deliberate. Separate independent limits of 32 reads and 32 writes could expose the storage path to 64 active operations during mixed traffic. One shared active budget prevents that bypass while still applying equally to reads and writes.

## Configuration reference

| Setting | Default | Meaning |
|---|---:|---|
| `enabled` | `true` | Generate HAProxy admission limits for the public S3 endpoint |
| `max_active_requests` | `32` | Maximum aggregate requests concurrently forwarded to SeaweedFS |
| `max_queued_requests` | `128` | Maximum additional requests held in the bounded HAProxy queue |
| `queue_timeout_seconds` | `30` | Maximum time a request may wait for an active slot |

All numeric settings must be greater than zero. Set `enabled = false` only when another layer provides an equivalent hard active limit and bounded queue.

## Failure semantics

Admission overload is not a storage failure.

A queue rejection or queue timeout:

- does not mark CIFS, SSHFS, block storage, or a path target failed;
- does not trigger transport failover;
- does not fence or replace the active worker generation;
- does not change `/ready` from `200` while the guarded storage and SeaweedFS checks remain healthy.

Clients should treat the returned `503` as retryable and use exponential backoff with jitter. The client-side request timeout must be longer than the configured queue timeout plus the expected execution time of the S3 operation, otherwise the client may abandon a request before HAProxy can forward it.

## Choosing limits

Set `max_active_requests` from load testing against the slowest active transport, not from CPU or memory alone. A remote filesystem can become I/O-bound and stop servicing health probes long before the container exhausts RAM.

Set `max_queued_requests` large enough to absorb ordinary bursts but small enough that queued work does not create unacceptable tail latency. A larger queue smooths brief spikes; it does not increase backend throughput.

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

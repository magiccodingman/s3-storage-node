# Distributed PostgreSQL writer lease

S3 Storage Node always takes a local `flock` in its persistent appliance state directory. That prevents two guardians from sharing one local state directory. An optional PostgreSQL writer lease adds a second boundary for separate hosts that may point at the same remote data target.

This feature coordinates ownership. It does not replicate SeaweedFS master state, filer metadata, volume indexes, or object data, and it does not by itself turn two appliances into a complete HA cluster.

## Safety model

The PostgreSQL row identifies one active dataset writer epoch:

```text
lease name
owner session
monotonic fencing token
renewed timestamp
lease expiry
optional takeover block
```

Every successful takeover receives a strictly higher fencing token. The token is exported to the worker environment and health data, but CIFS and SSHFS do not understand fencing tokens themselves. The physical enforcement mechanism remains worker-generation network fencing: when ownership is lost, the guardian withdraws readiness and deletes the generation's root veth before another recovery attempt.

The local flock remains mandatory even with PostgreSQL enabled. PostgreSQL prevents cross-host ownership overlap; the local lock prevents two guardian processes from sharing one appliance state directory.

## Configuration

```toml
[writer_lease]
backend = "postgres"
lease_name = "storagebox-production-01"
node_id = "storage-node-fsn1-a"
postgres_dsn_file = "/run/secrets/writer_lease_postgres_dsn"
postgres_schema = "public"
postgres_table = "s3_storage_node_writer_leases"
auto_create = true

ttl_seconds = 15
renew_interval_seconds = 5
retry_interval_seconds = 1
fence_margin_seconds = 2
takeover_delay_seconds = 5
connect_timeout_seconds = 5
```

`lease_name` must identify the shared dataset, not merely the host. Using the data sentinel ID is recommended.

`node_id` identifies the appliance. Each guardian boot appends a random session ID internally, so a newly started process never impersonates an older process that may still be alive.

`auto_create = true` creates two tables in the configured schema:

- the writer lease table;
- a single-row epoch counter used to allocate monotonic fencing tokens.

Set it to `false` after provisioning when the runtime database role should not retain DDL permission.

PostgreSQL mode requires:

```toml
[appliance]
worker_fencing_mode = "namespace"
```

Without the namespace network fence, losing database ownership would not physically prevent an old CIFS or SSHFS session from reconnecting.

## DSN secret

Copy the example and restrict it:

```bash
cp secrets/writer-lease-postgres-dsn.example secrets/writer-lease-postgres-dsn
chmod 600 secrets/writer-lease-postgres-dsn
```

Start with the optional Compose overlay:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgres-lease.yml \
  up -d
```

When SSHFS failover is also enabled, include both overlays.

## Timing rules

The guardian validates that:

```text
renew interval + fence margin < lease TTL
retry interval < safe lease window
connection timeout < safe lease window
```

A successful renewal records the database server's expiry time and converts the remaining duration into a local monotonic deadline. This avoids extending the safe serving window because of later wall-clock changes on the appliance.

On the first renewal error:

1. readiness is withdrawn immediately;
2. the lease enters `WRITER_LEASE_AT_RISK`;
3. renewal retries continue inside the remaining safe window.

If renewal recovers before the fence margin, traffic can reopen without restarting SeaweedFS. If the safe deadline is reached, ownership is declared lost and the worker generation is physically fenced.

A renewal returning no matching row is treated as immediate ownership loss rather than a transient database error. That means the owner, token, lease name, non-blocked state, and unexpired lease must all still match.

## Takeover delay

A different node may acquire an expired row only after `takeover_delay_seconds` has also elapsed. This is an additional buffer after the old TTL; it is not a replacement for local fencing.

Acquisition is serialized through the PostgreSQL epoch-counter row. Two contenders cannot both observe an eligible lease and become writers. The winner updates the lease row with a newly allocated fencing token in the same transaction.

## Fence failure and takeover blocking

If the guardian cannot verify that its worker generation was network-fenced, it attempts to mark the lease row `takeover_blocked` with the exact owner and fencing token.

A blocked row cannot be acquired even after its TTL expires. This deliberately converts an uncertain local fence into an operator-visible outage instead of allowing an automatic split brain.

Inspect the configured row:

```bash
docker compose exec s3-storage-node \
  s3-storage-node writer-lease-status \
  --config /etc/s3-storage-node/config.toml
```

After the old process and network path have been independently verified dead, clear an expired block using the exact token reported by status:

```bash
docker compose exec s3-storage-node \
  s3-storage-node writer-lease-unblock \
  --config /etc/s3-storage-node/config.toml \
  --expected-token 42
```

The command refuses to clear a non-expired row or a row whose token changed. That prevents an operator command prepared for an old incident from unblocking a newer owner epoch.

If PostgreSQL is unreachable at the same time a local physical fence cannot be applied, the guardian cannot safely coordinate an automatic takeover. This compound failure requires operator intervention; the project does not pretend otherwise.

## Graceful shutdown

The guardian fences and retires the worker generation before releasing a healthy PostgreSQL lease. Release deletes only the row matching the current owner and fencing token.

A takeover-blocked row is never deleted by normal shutdown. A stale process also cannot release or renew a newer epoch because every mutation includes the exact fencing token.

## Observability

Health JSON includes:

- backend and scope;
- lease and node IDs;
- unique owner session;
- held, healthy, at-risk, lost, and takeover-blocked flags;
- fencing token;
- lease expiry and monotonic TTL remaining;
- successful renewals and renewal failures;
- the last renewal error.

Prometheus exports corresponding gauges and counters, including:

```text
s3_storage_node_writer_lease_held
s3_storage_node_writer_lease_healthy
s3_storage_node_writer_fencing_token
s3_storage_node_writer_lease_ttl_remaining_seconds
s3_storage_node_writer_lease_renewals_total
s3_storage_node_writer_lease_renewal_failures_total
s3_storage_node_writer_lease_takeover_blocked
```

The active generation health object also carries the writer lease name and fencing token so incident logs can correlate a SeaweedFS generation with its distributed ownership epoch.

## Deliberate limitations

- PostgreSQL is an ownership coordinator, not an object replica.
- The fencing token is not enforced by CIFS, SSHFS, or the remote storage service.
- A second host still needs a valid strategy for SeaweedFS master, filer, and index state before it can be considered an HA replacement.
- This feature does not allow a new local generation while an old process still owns local writable state.
- Automatic failback and NVMe write-back remain separate future designs.

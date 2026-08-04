# Operations and recovery

## Normal state

Check guardian readiness:

```bash
curl -fsS http://localhost:9090/ready | jq
```

Expected result:

```json
{"ready":true,"state":"ONLINE"}
```

Inspect detailed state:

```bash
curl -fsS http://localhost:9090/healthz | jq
```

The detailed response includes active storage probes, sentinel information, worker-generation identity and fence state, local writer-lock state, and exclusive transport state when enabled.

The public S3 endpoint is usable only while `/ready` returns `200`. HAProxy returns `503` during mounting, certification, failure, fencing, draining, and recovery.

## Logs

```bash
docker compose logs --tail=200 s3-storage-node
```

Important events include:

- `guardian_starting`
- `writer_lease_unavailable`
- `worker_generation_created`
- `storage_transport_selected`
- `storage_mounted`
- `recovery_stable`
- `appliance_online`
- `appliance_suspect`
- `worker_generation_fenced`
- `process_stopping`
- `storage_detached`
- `storage_transport_failed`
- `recovery_wait`
- `storage_transport_switch_requested`
- `storage_transport_drained`

Logs are JSON on stdout and can be collected with the Docker logging driver or any standard container log collector.

## First enrollment

1. Confirm every configured share, device, or local path is the intended target.
2. Set `allow_initialize = true` only on targets being enrolled.
3. Start the node and wait for `ONLINE`.
4. Inspect `/healthz` and confirm the expected sentinel version, dataset ID, target role, and subdirectory.
5. Confirm the sentinel physically exists on each target.
6. Change `allow_initialize = false` on every active target.
7. Redeploy and verify the node returns to `ONLINE` without changing the sentinel.

New enrollment writes Sentinel V2. Valid existing V1 sentinels remain supported and are not silently rewritten.

Never leave initialization enabled on a target that could be redirected to an empty replacement filesystem.

## Validate configuration

```bash
docker compose run --rm s3-storage-node \
  s3-storage-node validate \
  --config /etc/s3-storage-node/config.toml
```

Validation checks the normal TOML model plus exclusive transport configuration, transport names, priorities, SSH secret paths, allowed SSHFS options, and required namespace fencing.

## Inspect exclusive transport state

When `[storage.data.failover]` is enabled:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-status \
  --config /etc/s3-storage-node/config.toml | jq
```

The persisted selector state includes:

- `active` — most recently selected transport;
- `requested` — pending operator request;
- `failed` — automatically failed transports with timestamps and reasons;
- `last_success_at` — last stable transport certification;
- `updated_at` — selector-state update time.

The active transport is also exposed in `/healthz` and Prometheus output.

A failed transport remains in cooldown for `failure_cooldown_seconds`. Cooldown makes it eligible for a future selection; it does not trigger automatic failback.

## Controlled transport switch

To return from a healthy SSHFS fallback to CIFS:

1. Restore and independently verify the CIFS service.
2. Confirm the node is currently `ONLINE` on the SSHFS transport.
3. Persist the request:

```bash
docker compose exec s3-storage-node \
  s3-storage-node transport-select \
  --config /etc/s3-storage-node/config.toml \
  --transport cifs-primary
```

4. Watch logs and `/ready`.
5. Verify the node returns to `ONLINE` with `active_transport = cifs-primary`.
6. Read and write representative S3 objects through the public endpoint.

A controlled switch follows:

```text
withdraw readiness
→ drain the sole active SeaweedFS generation
→ cleanly detach the healthy active transport
→ physically fence and retire the old generation
→ mount and certify the requested transport in a new generation
→ reopen traffic
```

The request remains persisted until the guardian can safely create the replacement generation. A lingering SeaweedFS process or blocked storage helper prevents the request from being silently consumed.

Do not repeatedly issue requests while one is already pending. Inspect `transport-status` and the guardian logs first.

## Automatic transport failure

An unexpected data mount, enrollment, or storage-probe failure is treated differently from a controlled switch:

```text
withdraw readiness
→ physically fence the active generation immediately
→ attempt SeaweedFS shutdown and storage detach
→ persist the failed transport
→ select another eligible transport after recovery backoff
→ perform full storage and S3 certification
```

The unexpected fault path is fence-first because the current storage route is no longer trusted.

Generic HAProxy or SeaweedFS failures do not automatically condemn the active transport unless a data storage operation also fails.

## Simulating CIFS failure

Use a disposable test dataset, never production data.

A useful test is to drop or reject TCP port 445 while the node is online. Expected behavior:

1. `/ready` becomes `503`.
2. Public S3 returns `503`.
3. The old worker generation is physically fenced.
4. CIFS failure is persisted in selector state.
5. The node selects and mounts SSHFS if configured and eligible.
6. The same sentinel is verified through SSHFS.
7. Full durability probes, SeaweedFS startup, S3 canaries, and the stability window pass.
8. The node returns to `ONLINE` on SSHFS.
9. Restoring port 445 does not cause automatic failback.

The repository's privileged transport-chaos CI job exercises this sequence against real Samba and OpenSSH/SFTP containers, including object verification and guardian restart.

## Guardian restart during failover

Transport selector state lives beneath `state_dir/guardian` and survives container or guardian restart.

If the guardian exits after recording a CIFS failure but before finishing replacement:

1. the local writer lock is released when the process exits;
2. the restarted guardian reads persisted failure and active/requested state;
3. it creates a new generation only after lingering processes and helpers are clear;
4. it resumes selection using an eligible transport;
5. full certification still runs before readiness returns.

Restart does not bypass sentinel, durability, fencing, or recovery requirements.

## Lingering process or helper

A process can remain blocked in kernel or FUSE I/O even after `SIGTERM` and `SIGKILL`. A helper can also remain stuck in a mount or unmount syscall.

The guardian fails closed in either case:

- no replacement generation starts while an old SeaweedFS process remains alive;
- no new transport is selected while a previous storage helper remains blocked;
- a pending manual request remains pending rather than being discarded;
- health and logs expose the blocker.

Do not manually delete persistent guardian state to force progress. Restore the failed transport or reboot the host when necessary to release uninterruptible kernel tasks, then allow the guardian to recover normally.

## Manual appliance restart

```bash
docker compose restart s3-storage-node
```

Use this for a clean lifecycle after configuration or secret changes, or after the underlying host condition has been repaired. A restart does not bypass identity or durability checks.

## Capacity floor

Each target has `min_free_bytes`. Crossing the floor withdraws the complete endpoint before the filesystem reaches zero free space.

Set the floor high enough for:

- SeaweedFS volume growth and compaction;
- embedded metadata growth;
- index rebuild or expansion;
- filesystem metadata and temporary work;
- operator response time.

## Docker health

Docker marks the container unhealthy when `/ready` fails. This is observability, not the primary recovery controller. The guardian normally remains alive and performs internal recovery.

The Compose restart policy handles host reboot, guardian crash, or actual container exit.

## Changing storage

Treat replacement storage as a new logical identity:

1. Stop the node.
2. Update the source, device, UUID, and `sentinel_id` as applicable.
3. Confirm no old appliance instance can access the prior dataset.
4. Set `allow_initialize = true` only after verifying the replacement target.
5. Start and verify the new Sentinel V2 and storage layout.
6. Disable initialization and redeploy.

S3 Storage Node does not copy, replicate, or reconstruct data between targets. Migration and independent redundancy belong to the surrounding storage or orchestration system.

## Safe maintenance checklist

Before planned storage maintenance:

1. Remove the node from any upstream write routing or replication quorum.
2. Confirm no uploads are in progress.
3. Stop the appliance or deliberately switch to a certified recovery transport.
4. Perform the storage maintenance.
5. Restore the target and verify mount, sentinel, capacity, and transport policy independently.
6. Start or switch the appliance.
7. Wait for `ONLINE` and verify representative S3 operations.
8. Return the node to upstream routing only after successful application-level checks.

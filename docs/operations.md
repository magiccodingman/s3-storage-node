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

The detailed response includes active storage probes, sentinel information, worker-generation identity and fence state, bounded generation history/cause counters, index-certification state, SeaweedFS volume topology, local writer-lock state, and exclusive transport state when enabled.

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
- `seaweed_volume_status_changed_during_startup`
- `seaweed_volume_status_probe_failed`
- `seaweed_volume_status_stable`
- `appliance_online`
- `appliance_suspect`
- `worker_generation_fenced`
- `worker_generation_gracefully_drained`
- `worker_generation_hard_fence_required`
- `seaweed_indexes_certified`
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

An unexpected data mount, enrollment, or storage-probe failure follows the same ownership-safe termination controller:

```text
withdraw readiness
→ attempt SeaweedFS shutdown and clean detach under one global deadline
→ physically fence after clean detach, or immediately when the deadline/failure requires it
→ persist clean/unclean outcome, phase, cause, transport, and index trust
→ persist the failed transport
→ select another eligible transport after recovery backoff
→ perform full storage, SeaweedFS volume, and S3 certification
```

No replacement is created during the bounded drain. A hard-fenced generation leaves `indexes_certified=false` until the replacement generation passes SeaweedFS's upstream volume-status check.

Generic HAProxy or SeaweedFS failures do not automatically condemn the active transport unless a data storage operation also fails.

## Simulating CIFS failure

Use a disposable test dataset, never production data.

A useful test is to drop or reject TCP port 445 while the node is online. Expected behavior:

1. `/ready` becomes `503`.
2. Public S3 returns `503`.
3. The old worker generation drains cleanly or is physically hard-fenced when the deadline expires.
4. CIFS failure is persisted in selector state.
5. The node selects and mounts SSHFS if configured and eligible.
6. The same sentinel is verified through SSHFS.
7. Full durability probes, SeaweedFS startup, S3 canaries, and the stability window pass.
8. The node returns to `ONLINE` on SSHFS.
9. Restoring port 445 does not cause automatic failback.

The repository's privileged transport-chaos CI job exercises this sequence against real Samba and OpenSSH/SFTP containers, including object verification, upstream volume-status validation, index certification, cause history, and guardian restart.

## Volume and index health

The `seaweed_volumes` field in `/healthz` is populated directly from the volume server's `/status` response. The guardian does not infer why SeaweedFS set `ReadOnly`; it treats that upstream bit as authoritative. Any read-only ID not explicitly listed in `seaweed.expected_readonly_volume_ids` closes readiness and is reported in `unexpected_readonly_volume_ids`.

Only add an expected ID after independently proving that its read-only lifecycle is intentional. Do not add an ID to hide an index-integrity error. When `indexes_certified=false`, orphan reports may be inspected, but destructive orphan/fsck application is prohibited. `orphan_deletion_safe` becomes true only after the upstream volume check recertifies the generation.

### Automatic index reconstruction

Unexpected upstream `ReadOnly` volumes trigger reconstruction only while readiness is already withdrawn. The guardian records their exact IDs and collections, stops the complete SeaweedFS stack, verifies the writers are gone, and reconstructs each local `.idx` from its authoritative remote `.dat`. It never scans every healthy volume.

An all-volume read-only snapshot is never accepted as a broad repair target. SeaweedFS can produce this snapshot after a remote filesystem temporarily reports zero free space and retain it until the next internal disk-space refresh. The guardian therefore waits at least `seaweed.all_readonly_wait_seconds`. If the volume server settles to a volume-specific result, normal classification continues. If every volume remains read-only, the endpoint stays offline and the guardian reports that broad repair was refused. Investigate the transport, remote capacity, `statfs`, and SeaweedFS disk-space logs before authorizing any index work.

The source is fingerprinted before and after reconstruction using dataset identity, active transport, collection, volume ID, size, modification time, and bounded head/tail hashes. A changed fingerprint aborts before live installation. `weed fix` runs as the SeaweedFS UID/GID against an individually mounted read-only view; `-ignoreError` and `-remoteFile` are never used.

Persistent state is stored beneath:

```text
<index path>/.s3-storage-node-repair/
├── journal/
├── staging/
└── backups/
```

The original `.idx` and any `.sdx` are hash-verified and retained before atomic replacement. `.sdx` is derived sorted-index state and is removed after backup so SeaweedFS cannot reuse it with the rebuilt `.idx`. Repair backups are not automatically pruned.

If `weed fix` reconstructs an `.idx` whose size and SHA-256 are identical to the live index, the controller does not create a backup, replace the index, or remove `.sdx`. The transaction enters `manual_intervention_required` with `candidate_identical_to_live_index=true`, proving that index divergence does not explain the upstream read-only state.

After restart, each target must be present in `/status` with `ReadOnly=false`. A rejected or missing target causes another complete writer stop, restoration of that volume's original `.idx` and `.sdx`, and a persistent manual-intervention block. A complete record beyond the old index is normally recovered. An incomplete final record may produce no valid candidate or may still be rejected by the volume server; the appliance never truncates the `.dat` to make it load.

An interrupted transaction that never reached candidate installation can outlive the status sample that triggered it. Once the startup status has passed the configured stability gate and that exact volume is present but no longer unexpectedly read-only, the guardian marks the transaction `resolved_without_install` and removes only its transaction-owned staging directory. It does not touch the live `.idx`, `.sdx`, retained backups, or `.dat`. Candidate-installed, rolled-back, and manual-intervention transactions are excluded from this reconciliation.

If the bundled official `weed fix` succeeds but SeaweedFS still rejects the candidate because its tail entries extend beyond authoritative `.dat` EOF, leave the automatic rollback in place. Do not retry with `-ignoreError` and do not truncate the `.dat`. An operator may explicitly certify the volume as expected read-only only after verifying the rollback and retained data; document the reason next to `expected_readonly_volume_ids` in the deployment configuration.

Inspect repair state without changing it:

```bash
docker compose exec s3-storage-node \
  s3-storage-node index-repair status --config /etc/s3-storage-node/config.toml
```

Authorize one new guarded attempt after investigating a blocked fingerprint:

```bash
docker compose exec s3-storage-node \
  s3-storage-node index-repair retry --volume-id 64 \
  --config /etc/s3-storage-node/config.toml
```

Retry authorization does not run `weed fix` immediately and bypasses no checks. The next offline recovery still requires writer shutdown, read-only staging, fingerprints, backups, atomic installation, and upstream validation.

`/healthz` exposes `index_repair` state, current/pending/verified/resolved-without-install/failed IDs, transaction identity, timestamps, and the most recent persistent failure. Prometheus exposes repair detection, attempt, success, resolved-without-install, failure, rollback, pending, and current-volume metrics without permanent per-volume labels.

Never run orphan deletion during or immediately after an index incident. Corrupt indexes can make orphan conclusions unreliable, and automatic repair intentionally contains no orphan deletion path.

The durable file `state_dir/guardian/generation-history.json` keeps the last 64 completed outcomes and counters by cause. A large generation number alone is not a failure; use the cause counters, phase, transport, duration, and clean/unclean outcome to diagnose churn.

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

After a generation's network fence is verified, the guardian also resolves the selected SSHFS PID file, verifies the process command line and mountpoint, and terminates that exact SSHFS process. A replacement SSHFS mount refuses to start while that PID remains alive, and the PID file is preserved if even `SIGKILL` cannot reap it. This prevents a detached FUSE transport from accumulating across generations without weakening the network fence or PID-reuse protections.

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

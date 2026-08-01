# Operations and recovery

## Normal state

```bash
curl -fsS http://localhost:9090/ready | jq
```

Expected state:

```json
{"ready":true,"state":"ONLINE"}
```

## Inspecting logs

```bash
docker compose logs --tail=200 s3-storage-node
```

Important events include:

- `storage_mounted`
- `appliance_online`
- `appliance_offline`
- `process_stopping`
- `storage_detached`
- `recovery_wait`

## First enrollment

1. Confirm the configured share/device/path is correct.
2. Set `allow_initialize = true` only on the targets being enrolled.
3. Start the node and wait for `ONLINE`.
4. Confirm the sentinel exists on each target.
5. Change `allow_initialize = false` and redeploy.

Do not leave initialization enabled on a target whose identity may be redirected to an empty replacement.

## Simulating a failure

Use a disposable test target, never production data.

For CIFS, block the remote SMB port or temporarily disable the share. Expected behavior:

1. `/ready` becomes `503`.
2. Public S3 becomes `503`.
3. SeaweedFS child processes stop.
4. The guardian attempts lazy unmount and reconnect.
5. When connectivity returns, full storage and S3 canaries run.
6. The node returns to `ONLINE`.

## Capacity floor

Each target has `min_free_bytes`. Crossing the floor withdraws the endpoint before the filesystem reaches zero free space. Set the floor high enough for SeaweedFS compaction, temporary files, and operational headroom.

## Container health

Docker marks the container unhealthy when `/ready` fails. This is observability only. Docker does not need to restart the container; the guardian performs internal recovery while the container remains alive.

The Compose restart policy exists for host reboot, guardian crash, or an actual container exit.

## Manual recovery

The guardian should normally recover automatically. To force a clean appliance lifecycle:

```bash
docker compose restart s3-storage-node
```

A restart does not bypass identity or durability checks.

## Changing storage

Treat replacement storage as a new identity:

1. Stop the node.
2. Update source/device/UUID and sentinel ID.
3. Set `allow_initialize = true` only after confirming the replacement target.
4. Start and verify.
5. Disable initialization again.

S3 Storage Node does not copy, replicate, or reconstruct data between targets. That responsibility belongs to the storage/replication system above or beside this node.

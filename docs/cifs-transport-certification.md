# CIFS transport certification

The guardian no longer treats a successful `mount.cifs` exit as sufficient evidence that the requested transport behavior is active. Every enrollment and storage probe inspects the effective mount options and, when available, Linux CIFS runtime telemetry from `/proc/fs/cifs/DebugData` and `/proc/fs/cifs/Stats`.

## Policy settings

```toml
minimum_smb_dialect = "3.1.1"
handle_reconnect_policy = "auto"
multichannel_policy = "auto"
max_channels = 2
require_transport_observability = false
```

### `minimum_smb_dialect`

When set, the negotiated/effective dialect must be at least the configured value. Supported values are `1.0`, `2.0`, `2.1`, `3.0`, `3.02`, and `3.1.1`. The guardian fails closed when it cannot determine the effective dialect or when the negotiated dialect is below the floor.

### `handle_reconnect_policy`

- `disabled` preserves the previous behavior and does not request resilient or persistent handles.
- `auto` tries persistent handles, then resilient handles, then ordinary handles when the kernel or server rejects a stronger profile.
- `resilient` requires `resilienthandles` to be active.
- `persistent` requires `persistenthandles` to be active.

Explicit `resilient` and `persistent` policies are requirements, not preferences. The node remains offline when the effective mount state does not expose the required option.

### `multichannel_policy`

- `disabled` does not request multichannel.
- `auto` tries multichannel and falls back to one channel when the capability is rejected.
- `required` requires multichannel to remain active.

`max_channels` is valid only with `auto` or `required` and must be between 2 and 16. When CIFS debug telemetry exposes channel state, a required policy also verifies that at least two channels were allocated or connected.

### `require_transport_observability`

When false, effective mount options are authoritative and `/proc/fs/cifs` telemetry enriches health data when available. When true, the guardian also requires the configured share to be visible in `DebugData`. Use this only after confirming that the container can read the host kernel's CIFS diagnostic files.

## Legacy mount options

Existing raw options are migrated during parsing:

- `persistenthandles` and `resilienthandles` become `handle_reconnect_policy`;
- `multichannel`, `nomultichannel`, and `max_channels=N` become the multichannel policy;
- contradictions and duplicate guardian-owned options are rejected.

New configurations should keep these options out of `mount_options`. The guardian owns their ordering, fallback behavior, and certification.

## Runtime telemetry

CIFS probe results can include:

- effective SMB dialect;
- configured and effective handle mode;
- configured and effective multichannel state;
- allocated or connected channel counts;
- share status and transport-observation status;
- kernel session and share reconnect counters.

The same data is exposed through `/healthz`, `/ready`, and selected Prometheus metrics.

## Stronger durability probe

Full probes retain the temporary create/write/`fsync`/read/delete cycle and also append a checksummed record to a persistent hidden probe file. The guardian closes and reopens the file before validating the appended tail. The probe file is compacted after one MiB so repeated health checks cannot grow it without bound.

This intentionally resembles the long-lived append pattern used by SeaweedFS volume data more closely than a throwaway temporary file alone.

## Recovery behavior

Any online process or storage-probe failure moves readiness to `SUSPECT` immediately, causing HAProxy health checks to withdraw the endpoint before mount repair begins. After storage is remounted and SeaweedFS restarts, the node remains in `VERIFYING_RECOVERY` until full durability probes and the S3 canary succeed repeatedly across a stability window. Only then does it return to `ONLINE`.

Size `appliance.probe_timeout_seconds` for the certified server reconnect bound. The default is 60 seconds because remote CIFS services can legitimately pause through a reconnect interval; a shorter deadline can turn transient latency into unnecessary generation churn. Readiness still withdraws when the deadline expires, and no alternate generation starts before the old one is drained or physically fenced.

The current appliance layers this certification under worker-generation namespaces, a local writer lease, and exclusive CIFS-to-SSHFS failover. A previously blocked generation must be physically network-fenced before any replacement can reach the dataset.

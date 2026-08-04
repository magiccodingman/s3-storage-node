# Dataset sentinel format

Every configured storage target contains a small JSON identity document, normally named `.s3-storage-node.json`. The sentinel prevents a syntactically valid mount from being mistaken for the intended dataset.

## Version 2

New enrollments write the transport-independent V2 format:

```json
{
  "schema": "s3-storage-node/dataset-sentinel",
  "version": 2,
  "sentinel_id": "storagebox-production-01",
  "dataset_id": "storagebox-production-01",
  "target": "data",
  "subdirectory": "seaweedfs",
  "transport_independent": true
}
```

V2 validates all of the following before storage is considered usable:

- the schema identifier is exact;
- the version is supported;
- `sentinel_id` and `dataset_id` both match the configured logical dataset;
- the target role matches, such as `data`, `metadata`, or `index`;
- the configured subdirectory matches;
- the document explicitly declares itself transport-independent.

The document intentionally does **not** contain a CIFS source, SSH host, mountpoint, transport name, or appliance hostname. Those values describe a route to the dataset, not the dataset itself. An exclusive CIFS and SSHFS pair must therefore validate the same V2 sentinel.

Probe and health output report:

```text
sentinel_version
sentinel_schema
dataset_id
```

## Version 1 compatibility

Existing V1 sentinels remain valid:

```json
{
  "version": 1,
  "sentinel_id": "storagebox-production-01",
  "target": "data"
}
```

A missing `version` is interpreted as legacy V1 for compatibility with the earliest appliance format. V1 requires the expected `sentinel_id`; the optional `target` field remains informational because older installations did not consistently use it as a strict schema field.

The appliance does not silently rewrite a valid V1 sentinel. Existing deployments can continue operating without an unrequested mutation to their storage root. Newly initialized targets use V2.

## Fail-closed behavior

The target remains offline when:

- the JSON is malformed;
- the version is neither 1 nor 2;
- a required V2 field is missing or has the wrong type;
- the schema, dataset, role, or subdirectory does not match configuration;
- V2 does not declare `transport_independent=true`.

Unknown future versions fail closed rather than being treated as V1.

## Integration certification

The privileged transport-chaos CI job exposes one Docker volume through a real Samba server and a real OpenSSH/SFTP server. It proves that:

- CIFS creates and validates V2;
- the same V2 document validates after fenced failover to SSHFS;
- signed S3 objects written before and after failover remain readable;
- a full guardian restart accepts an existing V1 document without rewriting it.

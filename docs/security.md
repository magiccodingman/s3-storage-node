# Security model

## Privilege boundary

The guardian needs root and `CAP_SYS_ADMIN` to mount and unmount filesystems. SeaweedFS and HAProxy processes run as UID/GID `10001` with no supplementary groups. Privileged host ports should be published through Docker port mapping rather than bound inside the container.

The container does not require:

- Docker socket access;
- host PID namespace;
- host network namespace;
- host root filesystem;
- blanket `privileged: true` in the supplied Compose file.

AppArmor is relaxed for the container because the default Docker profile may deny mount operations. Deployments with a custom narrowly scoped AppArmor profile can replace `apparmor=unconfined`.

## Read-only image

The Compose file uses a read-only root filesystem. Writable locations are limited to:

- `/run` tmpfs for mounts and generated runtime configuration;
- `/tmp` tmpfs;
- `/var/lib/s3-storage-node` persistent state;
- explicitly mounted storage targets.

## Secrets

CIFS, S3, TLS, PostgreSQL, IAM, and audit credentials should be supplied with Docker secrets or read-only mounted files. They must not be placed in image layers, committed TOML, command-line mount passwords, or public logs.

Generated S3 and filer configuration is written mode `0600` beneath `/run`.

## Network exposure

Only public S3 and the guardian health API are published by the default Compose file. SeaweedFS master, volume, filer, and internal S3 ports bind to loopback inside the container.

Restrict the health API at the host firewall or reverse proxy if detailed storage state should not be public.

## TLS

`tls_mode = "terminate"` makes HAProxy terminate TLS using a PEM bundle. `tls_mode = "off"` is suitable behind a trusted TLS proxy or private encrypted network.

Transport encryption does not replace application-level encryption. Conversely, application-level encryption by an upstream S3 orchestrator does not protect credentials or requests in transit to this node.

## Upstream correctness

An upstream orchestrator should count a replica only after this endpoint returns a completed successful S3 operation. It should remove the node from routing when `/ready` fails or S3 returns `503`.

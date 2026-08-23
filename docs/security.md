# Security model

## Privilege boundary

The guardian requires root plus selected Linux capabilities to manage mounts, namespaces, veth interfaces, routing, and physical fencing. SeaweedFS and HAProxy run as UID/GID `10001` with no supplementary groups.

The supplied Compose configuration grants:

- `CAP_SYS_ADMIN` for mount and namespace operations;
- `CAP_NET_ADMIN` for worker veth, routing, firewall, and fencing work;
- relaxed AppArmor confinement because Docker's default profile may deny filesystem mounts;
- `/dev/fuse` access for the read-only single-file index-repair projection and for SSHFS recovery when configured.

The container does **not** require:

- Docker socket access;
- host PID namespace;
- host network namespace;
- host root filesystem;
- blanket `privileged: true` in the supplied production Compose file.

Deployments with a custom narrowly scoped AppArmor profile may replace `apparmor=unconfined` after testing every mount, namespace, FUSE, and recovery path.

## Namespace isolation

The guardian and HAProxy remain in the appliance namespace. A worker generation receives private mount and network namespaces plus a veth pair.

SeaweedFS listeners bind inside that private worker namespace. They are reachable from HAProxy and the guardian through the stable worker address but are not published directly on the host.

Deleting the root side of the veth is the physical remote-storage fence. It removes the retired generation's route to SMB or SSH/SFTP even when a process remains blocked on an old file descriptor.

The network fence does not isolate shared local master, filer, or index files. For that reason, a living old process still blocks replacement.

## Read-only image and writable locations

The Compose file uses a read-only root filesystem. Writable locations are limited to:

- `/run` tmpfs for mounts, namespace state, generated configuration, and runtime SSH key copies;
- `/tmp` tmpfs;
- `/var/lib/s3-storage-node` persistent master and guardian state;
- explicitly configured storage targets.

Underlying managed mountpoints are root-owned and mode `000`. When a managed filesystem is absent, the unprivileged SeaweedFS process cannot write into the ordinary directory beneath it.

## Index-repair source isolation

The guardian is privileged enough to mount storage, but the process parsing SeaweedFS volume records is not given the writable production data path. After every SeaweedFS writer stops, a disposable child mount namespace exposes only the selected authoritative `.dat` through layered read-only mounts. The source bind, permission-masking FUSE projection, and final file bind are all verified read-only, and an attempted write as UID/GID `10001` must fail.

SeaweedFS 4.44's `weed fix` uses file permission bits to choose its open mode. The FUSE projection masks write bits without chmod, copying, renaming, or otherwise changing the remote source. Source content, size, ownership, mode, and bounded fingerprints are checked across the operation. The helper namespace and FUSE mount are destroyed after each attempt.

Candidates, journals, and backups reside on the local index filesystem. Journal and backup directories are root-only; the per-transaction staging directory is writable only where required for the unprivileged SeaweedFS helper. No repair command receives credentials or a broad writable remote-data directory.

## Secrets

Supply credentials with Docker secrets or read-only mounted files. Relevant secrets include:

- CIFS username/password files;
- S3 access and secret keys;
- SSH private keys;
- trusted OpenSSH `known_hosts` files;
- TLS PEM bundles;
- optional PostgreSQL filer-metadata passwords;
- IAM, authentication, audit, and custom filer configuration.

Do not place secrets in:

- image layers;
- committed TOML;
- command-line CIFS passwords;
- untrusted environment dumps;
- public logs;
- health responses.

Generated S3 and filer configuration is written mode `0600` beneath `/run`.

For SSHFS recovery, the guardian copies the read-only private-key secret into its ephemeral runtime directory with mode `0600`. The source secret is never modified. The runtime copy disappears with the container runtime directory.

## SSH host verification

SSHFS always uses:

- `StrictHostKeyChecking=yes`;
- the configured `known_hosts_file`;
- batch authentication;
- an explicit private key;
- an explicit port;
- no interactive fallback.

Populate `known_hosts_file` out of band from a trusted host-key source. Do not fetch and trust a host key automatically during the same connection that will access production storage.

A host-key mismatch fails the transport rather than accepting a changed server identity.

## CIFS credentials and policy

Use a dedicated CIFS credentials file with restrictive host permissions. Do not embed passwords in mount options.

The guardian validates security- and correctness-sensitive CIFS policy separately from arbitrary mount tuning, including explicit I/O failure behavior, dialect requirements, reconnect profile, and transport observability where configured.

## Transport exclusivity

CIFS and SSHFS recovery routes are never mounted as simultaneous writers. Every worker generation receives one selected transport.

A controlled switch withdraws public readiness before draining and detaching the current transport. The old generation is then physically fenced before a replacement generation mounts the requested route.

An unexpected storage failure withdraws readiness immediately and receives one bounded chance to drain while the data route still exists. The route is physically fenced after clean detach or immediately when drain/detach cannot complete. Recovery never starts a replacement before that fence is verified.

The selector state and generation counter are stored under the persistent appliance state directory and protected by a local exclusive writer lock.

## PostgreSQL scope

PostgreSQL is optional only as a SeaweedFS filer metadata backend. It is not used as a distributed writer lock, transport coordinator, or cross-host ownership system.

When PostgreSQL metadata is enabled, use TLS appropriate to the deployment, least-privilege database credentials, a dedicated schema/database where practical, and normal database backup and availability controls. Loss of the metadata database withdraws the complete S3 endpoint.

## Network exposure

The default Compose project publishes only:

- the HAProxy public S3 port;
- the guardian health API.

SeaweedFS master, volume, filer, and internal S3 ports remain private to the appliance/worker namespace topology.

Restrict the health API at the host firewall or reverse proxy. `/healthz` can expose operational details such as generation IDs, transport names, storage capacity, failure reasons, and sentinel metadata.

Restrict egress from the appliance to the required storage servers, DNS/NTP infrastructure, and optional PostgreSQL service where practical. Worker traffic is routed through the appliance namespace, so host firewall policy remains part of the security boundary.

## TLS

`tls_mode = "terminate"` makes HAProxy terminate TLS with a mounted PEM bundle. `tls_mode = "off"` is suitable only behind a trusted TLS proxy or on a private encrypted network.

Transport encryption does not replace application-level encryption. Application-level encryption by an upstream S3 system does not protect credentials or requests in transit to this node.

## S3 authentication

The default `static` mode generates a SeaweedFS S3 administrator identity from secret files. `config` mode accepts a complete SeaweedFS identity configuration and requires a usable canary identity when the built-in S3 canary remains enabled.

`auth_mode = "none"` exposes SeaweedFS development behavior and should not be used on an untrusted network.

Every independent storage node should use unique S3 credentials even when a higher-level orchestrator handles public authentication.

## Upstream correctness

An upstream writer, orchestrator, or replication controller should count a replica only after this endpoint returns a completed successful S3 operation.

It should stop routing to the node when:

- `/ready` fails;
- S3 returns `503`;
- application-level checks fail;
- the node is undergoing maintenance or controlled failback.

CIFS and SSHFS routes must never be counted as separate replicas. They are alternate routes to one dataset and one storage failure domain.

## Operational hardening

- Restrict access to `state_dir`; it contains selector state, generation metadata, and the local writer-lock path.
- Restrict the transport selection CLI to trusted operators.
- Protect CIFS, SSH, S3, TLS, and PostgreSQL secrets with host permissions and secret-management controls.
- Use a trusted time source for meaningful logs and selector failure timestamps.
- Review Docker, kernel, CIFS, FUSE, OpenSSH, HAProxy, SeaweedFS, and base-image security updates.
- Test kernel and storage-server upgrades with the privileged chaos harness or an equivalent disposable environment before production rollout.
- Never bypass a fence or lingering-process failure by deleting persistent state and starting another writer manually.

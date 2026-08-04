# Worker-generation fencing

The guardian can run every SeaweedFS generation and its selected managed data transport inside dedicated Linux mount and network namespaces. Public HAProxy and the health service remain in the appliance namespace.

```text
public S3
   │
HAProxy + guardian namespace
   │
root veth 169.254.254.1
   X  deleting this link is the physical network fence
worker veth 169.254.254.2
   │
worker generation
├── private mount namespace
├── selected CIFS or SSHFS data transport
├── master
├── volume server
├── filer
└── S3 gateway
```

## Why both namespaces are required

A mount namespace prevents one generation's managed mount, lazy detach, or repair from changing another generation's pathname view.

A network namespace provides the physical remote-storage fence. Deleting the root side of the veth removes the failed generation's route to CIFS or SSH/SFTP. An old process may remain blocked in kernel or FUSE I/O, but it cannot reconnect through that generation after the fence is applied.

An open file descriptor survives pathname changes and lazy unmounts. Rebinding a mountpoint or exposing another transport beneath the same path is therefore not fencing.

## Configuration

```toml
[appliance]
worker_fencing_mode = "namespace"
worker_host_address = "169.254.254.1/30"
worker_address = "169.254.254.2/30"
worker_gateway = "169.254.254.1"
```

`disabled` remains the parser default for compatibility. The sample deployment enables `namespace` mode.

Namespace mode requires:

- `CAP_SYS_ADMIN` for namespace and mount operations;
- `CAP_NET_ADMIN` for veth, routing, and firewall work;
- IPv4 forwarding already enabled or writable by the guardian;
- an address range that does not conflict with host or container networks.

The guardian accepts an already-enabled read-only forwarding sysctl. It fails closed when forwarding is disabled and cannot be enabled.

## Lifecycle

A normal generation lifecycle is:

1. The guardian acquires the exclusive local writer lock beneath `state_dir/guardian`.
2. It verifies that no previous SeaweedFS process or storage helper is still blocked.
3. It increments the durable generation counter and creates a random generation token.
4. It creates the private mount/network namespace and veth pair.
5. It selects exactly one data transport when failover is configured.
6. Storage mount, enrollment, Sentinel validation, role-layout creation, probes, and all SeaweedFS processes run inside that generation.
7. HAProxy reaches SeaweedFS through the stable worker address while checking readiness in the appliance namespace.
8. The generation becomes public only after full storage and S3 recovery certification.

Generation ID, token identifier, namespace PID, worker address, selected transport, fence state, and local writer-lock state are exposed through health JSON and Prometheus metrics.

## Unexpected failure: fence first

An unexpected storage, process, or health failure follows:

1. withdraw readiness;
2. delete the root veth;
3. mark the generation fenced;
4. attempt SeaweedFS shutdown;
5. detach managed storage;
6. retire the namespace only when safe;
7. create a replacement generation after backoff and full certification.

The old transport is untrusted, so network fencing must precede attempts to drain or detach it.

If the physical fence cannot be verified, the guardian enters a fatal fence-failure state and does not create a replacement.

## Controlled switch: drain and detach first

A healthy operator-requested CIFS/SSHFS switch is not treated as a storage fault:

1. withdraw readiness;
2. drain the only active SeaweedFS generation while the current transport still works;
3. cleanly detach the active transport;
4. physically fence and retire the old generation;
5. select and certify the requested transport in a new generation.

There is still never more than one writer. The difference is that a trusted healthy route remains available long enough for SeaweedFS and FUSE/CIFS teardown to complete cleanly.

If shutdown or unmount leaves a blocked process/helper, replacement remains forbidden and the persisted request remains pending.

## Lingering processes

The guardian deliberately blocks a replacement while any previous SeaweedFS process remains alive.

Network fencing prevents that process from reconnecting to remote data storage, but the master, embedded filer metadata, and volume indexes may be local persistent files shared by future generations. Starting a replacement against those same paths while the old process remains alive would be unsafe.

This conservative rule favors correctness over availability:

- a fenced but living volume process still blocks replacement;
- a stale or blocked filer/master process blocks replacement;
- a storage helper trapped in mount or unmount I/O blocks new transport selection;
- health remains unavailable until ownership is unambiguous.

The appliance does not claim generation-scoped isolation for every local writable state path.

## Local ownership lock

The writer lock is a local filesystem lock tied to one persistent appliance `state_dir`. It prevents:

- duplicate guardian processes in one container;
- accidentally starting the Compose project twice against the same state volume;
- two local appliance instances sharing master, metadata, index, and selector state.

It is not a distributed lease and does not coordinate independent hosts with different state directories.

That is intentional. The supported topology is one S3 Storage Node appliance owning one dataset. Independent availability should use separate storage copies, storage-level replication, application-level replication, backups, or a higher-level routing/orchestration system—not two appliance hosts competing for one mapped network drive.

## Transport scope

Namespace mode supports:

- the canonical CIFS data target;
- an exclusive SSHFS recovery route for that same CIFS dataset.

It does not turn local path or block storage into safely replaceable remote generations, because deleting a network route cannot fence a process from a local writable device.

CIFS and SSHFS are two access routes to the same logical dataset. Their shared Sentinel proves identity; the fence proves the retired generation can no longer reach either remote route.

# Worker-generation fencing

The guardian can run every SeaweedFS generation and its guardian-managed CIFS mount inside dedicated Linux mount and network namespaces. The public HAProxy and health service remain in the appliance namespace.

```text
public S3
   |
HAProxy + guardian namespace
   |
root veth 169.254.254.1
   X  <- deleting this link is the physical fence
worker veth 169.254.254.2
   |
SeaweedFS generation + private CIFS mount
```

## Why both namespaces are required

A mount namespace prevents one generation's CIFS mount and mount repairs from changing another generation's pathname view. A network namespace gives the guardian a physical fence: deleting the root side of the veth destroys the failed generation's route to the SMB server. An old process may remain blocked in kernel I/O, but it cannot reconnect its CIFS session after the fence is applied.

An open file descriptor survives pathname changes and lazy unmounts. For that reason, changing bind mounts or exposing a fallback mount beneath SeaweedFS is not considered fencing.

## Configuration

```toml
[appliance]
worker_fencing_mode = "namespace"
worker_host_address = "169.254.254.1/30"
worker_address = "169.254.254.2/30"
worker_gateway = "169.254.254.1"
```

`disabled` preserves the previous single-namespace process lifecycle. It remains the parser default for compatibility. The sample deployment enables `namespace` mode.

Namespace mode currently requires the data target to be CIFS. Network fencing cannot make a stuck process harmless when its writable data target is a local path or block device.

The container requires `CAP_SYS_ADMIN` for mount/setns operations and `CAP_NET_ADMIN` for veth, routing, and firewall configuration. The supplied Compose file includes both.

## Lifecycle

1. The guardian acquires an exclusive local writer lease in `state_dir`.
2. It increments a durable generation counter and creates a random fencing token.
3. A private mount/network namespace and veth pair are created.
4. Storage mount, enrollment, probes, and all SeaweedFS processes run inside that generation.
5. HAProxy routes S3 traffic to the stable worker address while checking the guardian's root readiness endpoint.
6. Any unhealthy state withdraws readiness and deletes the root veth **before** SeaweedFS shutdown begins.
7. The old namespace is unmounted and retired after shutdown attempts.

Generation ID, token, namespace PID, worker address, fence state, and writer-lease state are exposed through health JSON and Prometheus metrics.

## Lingering processes

This PR deliberately continues to block a replacement while any old SeaweedFS process remains alive. The network path is physically fenced, but master state, filer metadata, and volume indexes may be local persistent files. Starting another process against those same files would not be safe merely because CIFS is disconnected.

A later architecture can permit selected quarantined generations only after every local writable state path is isolated as well. Until then, the guardian reports the fenced generation and fails closed rather than overstating what the network fence guarantees.

## Ownership scope

The writer lease in this version is local to one appliance `state_dir`. It prevents duplicate guardians on the same persistent appliance state. It is not a distributed lease across two hosts with different state directories. A PostgreSQL/etcd-backed lease can implement that separate failure domain later.

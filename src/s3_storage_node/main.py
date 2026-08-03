from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .config import ConfigError, load_config
from .storage import (
    StorageError,
    mount_target,
    prepare_role_paths,
    probe_target,
    unmount_target,
    verify_or_initialize_sentinel,
)
from .transport_failover import (
    TransportFailoverError,
    TransportSelector,
    load_exclusive_failover,
    resolve_target,
)
from .transport_guardian import run_guardian


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="s3-storage-node")
    subcommands = root.add_subparsers(dest="command")

    run = subcommands.add_parser("run", help="run the storage guardian")
    run.add_argument("--config", default="/etc/s3-storage-node/config.toml")

    for name, help_text in (
        ("mount", "mount one configured storage target"),
        ("unmount", "unmount one configured storage target"),
        ("prepare", "verify or enroll one configured storage target"),
        ("probe", "probe one configured storage target"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--config", required=True)
        command.add_argument("--target", required=True)
        command.add_argument("--transport", default="")
        if name == "probe":
            command.add_argument("--full", action="store_true")

    layout = subcommands.add_parser("prepare-layout", help="create guarded SeaweedFS role directories")
    layout.add_argument("--config", required=True)

    status = subcommands.add_parser("transport-status", help="show exclusive failover transport state")
    status.add_argument("--config", default="/etc/s3-storage-node/config.toml")

    select = subcommands.add_parser("transport-select", help="request a controlled transport switch")
    select.add_argument("--config", default="/etc/s3-storage-node/config.toml")
    select.add_argument("--transport", required=True)

    validate = subcommands.add_parser("validate", help="validate configuration")
    validate.add_argument("--config", default="/etc/s3-storage-node/config.toml")

    health = subcommands.add_parser("health", help="check the configured readiness endpoint")
    health.add_argument("--config", default="/etc/s3-storage-node/config.toml")
    return root


def _selector(config_path: str):
    config = load_config(config_path)
    failover = load_exclusive_failover(config_path, config)
    if failover is None:
        raise TransportFailoverError("storage.data.failover is not enabled")
    return config, TransportSelector(config.appliance.state_dir / "guardian", failover)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    command = args.command or "run"
    if command == "run":
        return run_guardian(args.config)
    if command == "validate":
        try:
            config = load_config(args.config)
            load_exclusive_failover(args.config, config)
        except (ConfigError, TransportFailoverError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("configuration valid")
        return 0
    if command == "health":
        try:
            config = load_config(args.config)
            host = config.appliance.health_host
            if host in {"0.0.0.0", "::", "[::]"}:
                host = "127.0.0.1"
            url = f"http://{host}:{config.appliance.health_port}/ready"
            with urllib.request.urlopen(url, timeout=2) as response:
                return 0 if response.status == 200 else 1
        except (ConfigError, OSError, urllib.error.URLError):
            return 1
    if command in {"transport-status", "transport-select"}:
        try:
            _config, selector = _selector(args.config)
            if command == "transport-select":
                selector.request(args.transport)
            print(json.dumps(selector.status(), sort_keys=True))
            return 0
        except (ConfigError, TransportFailoverError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    try:
        config = load_config(args.config)
        if command == "prepare-layout":
            paths = [config.volume_path, config.index_path]
            if config.metadata.backend == "embedded":
                paths.append(config.metadata_path)
            prepare_role_paths(paths, config.appliance.uid, config.appliance.gid)
            print(json.dumps({"prepared": True, "paths": [str(path) for path in paths]}))
            return 0

        selected_transport = args.transport or os.environ.get("S3_STORAGE_NODE_TRANSPORT", "")
        target = resolve_target(args.config, config, args.target, selected_transport)
        if command == "mount":
            mount_target(target)
            print(json.dumps({"mounted": True, "target": target.name, "transport": target.transport_name}))
            return 0
        if command == "unmount":
            unmount_target(target)
            print(json.dumps({"unmounted": True, "target": target.name, "transport": target.transport_name}))
            return 0
        if command == "prepare":
            verify_or_initialize_sentinel(target, config.appliance.uid, config.appliance.gid)
            print(json.dumps({"prepared": True, "target": target.name, "transport": target.transport_name}))
            return 0
        if command == "probe":
            print(json.dumps(probe_target(target, full=args.full), sort_keys=True))
            return 0
    except (ConfigError, TransportFailoverError, StorageError, KeyError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

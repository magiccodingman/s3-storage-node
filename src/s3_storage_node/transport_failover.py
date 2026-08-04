from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config_helpers import ConfigError
from .config_types import Config, TargetConfig


class TransportFailoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class SshfsTransportConfig:
    name: str
    priority: int
    source: str
    auth_mode: str
    known_hosts_file: str
    identity_file: str = ""
    credentials_file: str = ""
    port: int = 22
    mount_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExclusiveFailoverConfig:
    target: str
    primary_name: str
    primary_priority: int
    failback_policy: str
    failure_cooldown_seconds: int
    verify_all_transports_on_startup: bool
    transports: tuple[SshfsTransportConfig, ...]
    worker_uid: int
    worker_gid: int
    runtime_dir: Path

    @property
    def ordered_names(self) -> tuple[str, ...]:
        ordered = [(self.primary_priority, self.primary_name)]
        ordered.extend((transport.priority, transport.name) for transport in self.transports)
        return tuple(name for _priority, name in sorted(ordered, key=lambda item: (item[0], item[1])))

    def contains(self, name: str) -> bool:
        return name in self.ordered_names

    def resolve(self, base_target: TargetConfig, name: str) -> TargetConfig:
        if name == self.primary_name:
            return replace(base_target, transport_name=name)
        for transport in self.transports:
            if transport.name != name:
                continue
            runtime_key = self.runtime_dir / "generated" / f"ssh-{transport.name}.key"
            runtime_pid = self.runtime_dir / "generated" / f"ssh-{transport.name}.pid"
            options = [
                f"UserKnownHostsFile={transport.known_hosts_file}",
                "StrictHostKeyChecking=yes",
                f"port={transport.port}",
                "reconnect",
                "ServerAliveInterval=15",
                "ServerAliveCountMax=3",
                "sshfs_sync",
                "dir_cache=no",
                "allow_other",
                "default_permissions",
                f"uid={self.worker_uid}",
                f"gid={self.worker_gid}",
                "umask=007",
            ]
            if transport.auth_mode == "key":
                options[0:0] = [
                    f"IdentityFile={runtime_key}",
                    "BatchMode=yes",
                    "PreferredAuthentications=publickey",
                    "PasswordAuthentication=no",
                    "KbdInteractiveAuthentication=no",
                ]
            else:
                options[0:0] = [
                    "password_stdin",
                    "BatchMode=no",
                    "PreferredAuthentications=password",
                    "PubkeyAuthentication=no",
                    "KbdInteractiveAuthentication=no",
                    "NumberOfPasswordPrompts=1",
                ]
            options.extend(transport.mount_options)
            return replace(
                base_target,
                type="sshfs",
                source=transport.source,
                credentials_file="",
                mount_options=tuple(options),
                io_failure_policy="",
                minimum_smb_dialect="",
                handle_reconnect_policy="disabled",
                multichannel_policy="disabled",
                max_channels=2,
                require_transport_observability=False,
                transport_name=name,
                ssh_auth_mode=transport.auth_mode,
                ssh_identity_file=transport.identity_file,
                ssh_credentials_file=transport.credentials_file,
                ssh_known_hosts_file=transport.known_hosts_file,
                ssh_runtime_identity_file=str(runtime_key) if transport.auth_mode == "key" else "",
                ssh_runtime_pid_file=str(runtime_pid),
                ssh_port=transport.port,
            )
        raise TransportFailoverError(f"unknown transport {name!r} for target {self.target}")


def _require_table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _string(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value.strip()


def _integer(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    return value


def _boolean(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    return tuple(value)


_TRANSPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SSHFS_SAFE_OPTIONS = {
    "no_readahead", "max_read", "max_write", "max_background", "congestion_threshold",
    "dcache_max_size", "dcache_timeout", "dcache_stat_timeout", "dcache_link_timeout",
    "dcache_dir_timeout", "direct_io", "auto_cache", "disable_hardlink", "renamexdev",
    "truncate_workaround", "workaround",
}


def _transport_name(value: Any, name: str, default: str = "") -> str:
    result = _string(value, name, default)
    if not result or not _TRANSPORT_NAME.fullmatch(result):
        raise ConfigError(f"{name} must use 1-64 letters, numbers, dots, underscores, or hyphens")
    return result


def _absolute_file(value: Any, name: str, *, required: bool = True) -> str:
    result = _string(value, name)
    if not result and not required:
        return ""
    if not result or not Path(result).is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    if any(character in result for character in (",", "\n", "\r", "\x00")):
        raise ConfigError(f"{name} may not contain commas or control characters")
    return result


def _validate_sshfs_options(options: tuple[str, ...], name: str) -> None:
    for option in options:
        key = option.partition("=")[0].strip().lower()
        if key not in _SSHFS_SAFE_OPTIONS:
            raise ConfigError(f"{name} option {key!r} is not in the guarded SSHFS tuning allowlist")


def load_exclusive_failover(config_path: str | os.PathLike[str], config: Config) -> ExclusiveFailoverConfig | None:
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"unable to read transport failover configuration: {exc}") from exc
    storage = raw.get("storage", {})
    if not isinstance(storage, dict):
        return None
    data = storage.get("data", {})
    if not isinstance(data, dict):
        return None
    failover_raw = data.get("failover")
    if failover_raw is None:
        return None
    failover = _require_table(failover_raw, "storage.data.failover")
    if not _boolean(failover.get("enabled"), "storage.data.failover.enabled", False):
        return None
    if config.data_target.type != "cifs":
        raise ConfigError("storage.data.failover requires the canonical storage.data target to be CIFS")
    if config.appliance.worker_fencing_mode != "namespace":
        raise ConfigError("storage.data.failover requires appliance.worker_fencing_mode=namespace")

    primary_name = _transport_name(failover.get("primary_name"), "storage.data.failover.primary_name", "cifs-primary")
    primary_priority = _integer(failover.get("primary_priority"), "storage.data.failover.primary_priority", 10)
    if primary_priority < 0:
        raise ConfigError("storage.data.failover.primary_priority must be zero or greater")
    failback_policy = _string(failover.get("failback_policy"), "storage.data.failover.failback_policy", "manual").lower()
    if failback_policy != "manual":
        raise ConfigError("storage.data.failover.failback_policy currently supports only manual")
    cooldown = _integer(failover.get("failure_cooldown_seconds"), "storage.data.failover.failure_cooldown_seconds", 60)
    if cooldown <= 0:
        raise ConfigError("storage.data.failover.failure_cooldown_seconds must be greater than zero")
    verify_on_startup = _boolean(
        failover.get("verify_all_transports_on_startup"),
        "storage.data.failover.verify_all_transports_on_startup",
        True,
    )

    raw_transports = failover.get("transports", [])
    if not isinstance(raw_transports, list) or not raw_transports:
        raise ConfigError("storage.data.failover.transports must contain at least one SSHFS transport")
    transports: list[SshfsTransportConfig] = []
    names = {primary_name}
    priorities = {primary_priority}
    for index, value in enumerate(raw_transports):
        prefix = f"storage.data.failover.transports[{index}]"
        item = _require_table(value, prefix)
        transport_type = _string(item.get("type"), f"{prefix}.type", "sshfs").lower()
        if transport_type != "sshfs":
            raise ConfigError(f"{prefix}.type currently supports only sshfs")
        name = _transport_name(item.get("name"), f"{prefix}.name")
        if name in names:
            raise ConfigError(f"duplicate storage transport name: {name}")
        names.add(name)
        priority = _integer(item.get("priority"), f"{prefix}.priority", 20 + index)
        if priority < 0:
            raise ConfigError(f"{prefix}.priority must be zero or greater")
        if priority in priorities:
            raise ConfigError(f"duplicate storage transport priority: {priority}")
        priorities.add(priority)
        source = _string(item.get("source"), f"{prefix}.source")
        if (not source or source.startswith("-") or ":" not in source or "@" not in source.partition(":")[0]
                or any(character in source for character in ("\n", "\r", "\x00"))):
            raise ConfigError(f"{prefix}.source must look like user@host:/remote/path")

        configured_mode = _string(item.get("auth_mode"), f"{prefix}.auth_mode").lower()
        has_identity = bool(_string(item.get("identity_file"), f"{prefix}.identity_file"))
        has_credentials = bool(_string(item.get("credentials_file"), f"{prefix}.credentials_file"))
        auth_mode = configured_mode or ("password" if has_credentials and not has_identity else "key")
        if auth_mode not in {"key", "password"}:
            raise ConfigError(f"{prefix}.auth_mode must be key or password")
        identity_file = ""
        credentials_file = ""
        if auth_mode == "key":
            identity_file = _absolute_file(item.get("identity_file"), f"{prefix}.identity_file")
            if has_credentials:
                raise ConfigError(f"{prefix}.credentials_file is only valid with auth_mode=password")
        else:
            if has_identity:
                raise ConfigError(f"{prefix}.identity_file is only valid with auth_mode=key")
            credentials_file = _absolute_file(
                item.get("credentials_file") or config.data_target.credentials_file,
                f"{prefix}.credentials_file",
            )
        known_hosts_file = _absolute_file(item.get("known_hosts_file"), f"{prefix}.known_hosts_file")
        port = _integer(item.get("port"), f"{prefix}.port", 22)
        if not 1 <= port <= 65535:
            raise ConfigError(f"{prefix}.port must be between 1 and 65535")
        mount_options = _string_list(item.get("mount_options"), f"{prefix}.mount_options")
        _validate_sshfs_options(mount_options, f"{prefix}.mount_options")
        transports.append(SshfsTransportConfig(
            name=name, priority=priority, source=source, auth_mode=auth_mode,
            identity_file=identity_file, credentials_file=credentials_file,
            known_hosts_file=known_hosts_file, port=port, mount_options=mount_options,
        ))

    return ExclusiveFailoverConfig(
        target="data", primary_name=primary_name, primary_priority=primary_priority,
        failback_policy=failback_policy, failure_cooldown_seconds=cooldown,
        verify_all_transports_on_startup=verify_on_startup, transports=tuple(transports),
        worker_uid=config.appliance.uid, worker_gid=config.appliance.gid,
        runtime_dir=config.appliance.runtime_dir,
    )


def resolve_target(config_path: str | os.PathLike[str], config: Config, target_name: str, transport_name: str = "") -> TargetConfig:
    base = config.targets[target_name]
    failover = load_exclusive_failover(config_path, config)
    if failover is None or target_name != failover.target:
        if transport_name:
            raise TransportFailoverError(f"target {target_name} does not have transport failover configured")
        return base
    return failover.resolve(base, transport_name or failover.primary_name)


def _file_fingerprint(path: str) -> str:
    if not path:
        return ""
    try:
        content = Path(path).read_bytes()
    except OSError:
        return "missing"
    return hashlib.sha256(content).hexdigest()


def startup_verification_fingerprint(config: Config, failover: ExclusiveFailoverConfig) -> str:
    primary = config.data_target
    payload: dict[str, object] = {
        "target": failover.target,
        "primary": {
            "name": failover.primary_name, "source": primary.source,
            "credentials": _file_fingerprint(primary.credentials_file),
            "mount_options": list(primary.mount_options),
            "io_failure_policy": primary.effective_io_failure_policy,
            "minimum_smb_dialect": primary.minimum_smb_dialect,
            "handle_reconnect_policy": primary.handle_reconnect_policy,
            "multichannel_policy": primary.multichannel_policy,
            "max_channels": primary.max_channels,
        },
        "transports": [],
    }
    transports = payload["transports"]
    assert isinstance(transports, list)
    for transport in failover.transports:
        transports.append({
            "name": transport.name, "source": transport.source, "auth_mode": transport.auth_mode,
            "identity": _file_fingerprint(transport.identity_file),
            "credentials": _file_fingerprint(transport.credentials_file),
            "known_hosts": _file_fingerprint(transport.known_hosts_file),
            "port": transport.port, "mount_options": list(transport.mount_options),
        })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class TransportSelector:
    def __init__(self, state_dir: Path, config: ExclusiveFailoverConfig) -> None:
        self.config = config
        self.path = state_dir / "transports" / f"{config.target}.json"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self, *, shared: bool = False):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        failed = payload.get("failed", {})
        if not isinstance(failed, dict):
            failed = {}
        verified = payload.get("startup_verified_transports", [])
        if not isinstance(verified, list) or not all(isinstance(item, str) for item in verified):
            verified = []
        return {
            "active": payload.get("active", ""), "requested": payload.get("requested", ""),
            "failed": failed, "last_success_at": payload.get("last_success_at", 0.0),
            "startup_verification_fingerprint": payload.get("startup_verification_fingerprint", ""),
            "startup_verified_at": payload.get("startup_verified_at", 0.0),
            "startup_verified_transports": verified, "updated_at": payload.get("updated_at", 0.0),
        }

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = time.time()
        _write_json_atomic(self.path, state)

    def startup_verification_required(self, fingerprint: str) -> bool:
        with self._locked(shared=True):
            state = self._load()
        return state.get("startup_verification_fingerprint") != fingerprint

    def record_startup_verification(self, fingerprint: str, transports: tuple[str, ...]) -> None:
        with self._locked():
            state = self._load()
            state["startup_verification_fingerprint"] = fingerprint
            state["startup_verified_at"] = time.time()
            state["startup_verified_transports"] = list(transports)
            self._save(state)

    def select(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self._locked():
            state = self._load()
            requested = str(state.get("requested", ""))
            if requested:
                if not self.config.contains(requested):
                    raise TransportFailoverError(f"requested transport is no longer configured: {requested}")
                state["active"] = requested
                state["requested"] = ""
                self._save(state)
                return requested
            active = str(state.get("active", ""))
            failed = state["failed"]
            if active and self.config.contains(active) and active not in failed:
                return active
            candidates: list[str] = []
            cooling: list[tuple[float, str]] = []
            for name in self.config.ordered_names:
                failure = failed.get(name)
                if not isinstance(failure, dict):
                    candidates.append(name)
                    continue
                available_at = float(failure.get("at", 0.0)) + self.config.failure_cooldown_seconds
                if available_at <= now:
                    candidates.append(name)
                else:
                    cooling.append((available_at, name))
            if not candidates:
                available_at, name = min(cooling)
                remaining = max(1, int(available_at - now + 0.999))
                raise TransportFailoverError(
                    f"all configured transports are cooling down; next eligible transport {name} in {remaining} seconds"
                )
            selected = candidates[0]
            state["active"] = selected
            self._save(state)
            return selected

    def record_failure(self, name: str, reason: str, now: float | None = None) -> None:
        if not self.config.contains(name):
            return
        with self._locked():
            state = self._load()
            state["failed"][name] = {"at": time.time() if now is None else now, "reason": reason[:1000]}
            state["active"] = name
            self._save(state)

    def record_success(self, name: str, now: float | None = None) -> None:
        with self._locked():
            state = self._load()
            state["failed"].pop(name, None)
            state["active"] = name
            state["last_success_at"] = time.time() if now is None else now
            self._save(state)

    def request(self, name: str) -> None:
        if not self.config.contains(name):
            raise TransportFailoverError(f"unknown transport: {name}")
        with self._locked():
            state = self._load()
            state["requested"] = "" if state.get("active") == name else name
            self._save(state)

    def pending_request(self) -> str:
        with self._locked(shared=True):
            requested = str(self._load().get("requested", ""))
        if requested and not self.config.contains(requested):
            raise TransportFailoverError(f"requested transport is no longer configured: {requested}")
        return requested

    def status(self) -> dict[str, Any]:
        with self._locked(shared=True):
            state = self._load()
        state.pop("startup_verification_fingerprint", None)
        return {
            **state, "target": self.config.target, "primary": self.config.primary_name,
            "ordered_transports": list(self.config.ordered_names), "failback_policy": self.config.failback_policy,
            "failure_cooldown_seconds": self.config.failure_cooldown_seconds,
            "verify_all_transports_on_startup": self.config.verify_all_transports_on_startup,
        }

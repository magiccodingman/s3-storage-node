from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path

from .config_helpers import (
    ConfigError,
    _bool,
    _int,
    _int_list,
    _positive,
    _relative_path,
    _string,
    _string_list,
    _table,
)
from .config_target import _parse_target
from .config_types import (
    ApplianceConfig,
    Config,
    IndexConfig,
    MetadataConfig,
    S3AdmissionConfig,
    S3Config,
    SeaweedConfig,
    TargetConfig,
)


def load_config(path: str | os.PathLike[str]) -> Config:
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    appliance_raw = _table(raw, "appliance")
    runtime_dir = Path(_string(appliance_raw.get("runtime_dir"), "appliance.runtime_dir", "/run/s3-storage-node"))
    state_dir = Path(_string(appliance_raw.get("state_dir"), "appliance.state_dir", "/var/lib/s3-storage-node"))
    if not runtime_dir.is_absolute() or not state_dir.is_absolute():
        raise ConfigError("appliance.state_dir and appliance.runtime_dir must be absolute")
    if state_dir == runtime_dir or state_dir.is_relative_to(runtime_dir):
        raise ConfigError("appliance.state_dir must not be inside the ephemeral runtime_dir")

    fencing_mode = _string(appliance_raw.get("worker_fencing_mode"), "appliance.worker_fencing_mode", "disabled").lower()
    if fencing_mode not in {"disabled", "namespace"}:
        raise ConfigError("appliance.worker_fencing_mode must be disabled or namespace")
    host_address = _string(appliance_raw.get("worker_host_address"), "appliance.worker_host_address", "169.254.254.1/30")
    worker_address = _string(appliance_raw.get("worker_address"), "appliance.worker_address", "169.254.254.2/30")
    gateway = _string(appliance_raw.get("worker_gateway"), "appliance.worker_gateway", "169.254.254.1")
    try:
        host_interface = ipaddress.ip_interface(host_address)
        worker_interface = ipaddress.ip_interface(worker_address)
        gateway_address = ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise ConfigError(f"invalid worker namespace address: {exc}") from exc
    if host_interface.version != 4 or worker_interface.version != 4 or gateway_address.version != 4:
        raise ConfigError("worker namespace addresses must be IPv4")
    if host_interface.network != worker_interface.network:
        raise ConfigError("worker_host_address and worker_address must share one subnet")
    if host_interface.ip == worker_interface.ip:
        raise ConfigError("worker_host_address and worker_address must use different addresses")
    if gateway_address != host_interface.ip:
        raise ConfigError("worker_gateway must equal the worker_host_address IP")

    appliance = ApplianceConfig(
        name=_string(appliance_raw.get("name"), "appliance.name", "s3-storage-node"),
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        uid=_int(appliance_raw.get("uid"), "appliance.uid", 10001),
        gid=_int(appliance_raw.get("gid"), "appliance.gid", 10001),
        health_host=_string(appliance_raw.get("health_host"), "appliance.health_host", "0.0.0.0"),
        health_port=_int(appliance_raw.get("health_port"), "appliance.health_port", 9090),
        probe_interval_seconds=_int(appliance_raw.get("probe_interval_seconds"), "appliance.probe_interval_seconds", 5),
        full_probe_interval_seconds=_int(appliance_raw.get("full_probe_interval_seconds"), "appliance.full_probe_interval_seconds", 60),
        probe_timeout_seconds=_int(appliance_raw.get("probe_timeout_seconds"), "appliance.probe_timeout_seconds", 4),
        startup_timeout_seconds=_int(appliance_raw.get("startup_timeout_seconds"), "appliance.startup_timeout_seconds", 30),
        shutdown_grace_seconds=_int(appliance_raw.get("shutdown_grace_seconds"), "appliance.shutdown_grace_seconds", 45),
        recovery_initial_seconds=_int(appliance_raw.get("recovery_initial_seconds"), "appliance.recovery_initial_seconds", 5),
        recovery_max_seconds=_int(appliance_raw.get("recovery_max_seconds"), "appliance.recovery_max_seconds", 60),
        recovery_stability_seconds=_int(appliance_raw.get("recovery_stability_seconds"), "appliance.recovery_stability_seconds", 15),
        recovery_probe_interval_seconds=_int(appliance_raw.get("recovery_probe_interval_seconds"), "appliance.recovery_probe_interval_seconds", 2),
        recovery_successes_required=_int(appliance_raw.get("recovery_successes_required"), "appliance.recovery_successes_required", 3),
        s3_canary_enabled=_bool(appliance_raw.get("s3_canary_enabled"), "appliance.s3_canary_enabled", True),
        worker_fencing_mode=fencing_mode,
        worker_host_address=host_address,
        worker_address=worker_address,
        worker_gateway=gateway,
    )
    for field_name in (
        "health_port", "probe_interval_seconds", "full_probe_interval_seconds", "probe_timeout_seconds",
        "startup_timeout_seconds", "shutdown_grace_seconds", "recovery_initial_seconds", "recovery_max_seconds",
        "recovery_probe_interval_seconds", "recovery_successes_required",
    ):
        _positive(getattr(appliance, field_name), f"appliance.{field_name}")
    _positive(appliance.recovery_stability_seconds, "appliance.recovery_stability_seconds")
    if appliance.recovery_initial_seconds > appliance.recovery_max_seconds:
        raise ConfigError("appliance.recovery_initial_seconds may not exceed recovery_max_seconds")

    storage_raw = _table(raw, "storage")
    if "data" not in storage_raw or not isinstance(storage_raw["data"], dict):
        raise ConfigError("[storage.data] is required")
    targets: dict[str, TargetConfig] = {}
    for name in ("data", "metadata", "index"):
        if name in storage_raw:
            if not isinstance(storage_raw[name], dict):
                raise ConfigError(f"[storage.{name}] must be a table")
            targets[name] = _parse_target(name, storage_raw[name], runtime_dir)

    if appliance.worker_fencing_mode == "namespace" and targets["data"].type != "cifs":
        raise ConfigError("appliance.worker_fencing_mode=namespace currently requires storage.data.type=cifs")

    for target in targets.values():
        if target.type == "path" and target.allow_initialize:
            resolved_mountpoint = target.mountpoint.resolve(strict=False)
            resolved_state = state_dir.resolve(strict=False)
            if not resolved_mountpoint.is_relative_to(resolved_state):
                raise ConfigError(
                    f"storage.{target.name}.allow_initialize may only be true for path targets beneath "
                    f"appliance.state_dir ({state_dir}); externally managed paths must be enrolled manually "
                    "and use allow_initialize=false"
                )

    metadata_raw = _table(raw, "metadata")
    backend = _string(metadata_raw.get("backend"), "metadata.backend", "embedded").lower()
    if backend not in {"embedded", "postgres", "custom"}:
        raise ConfigError("metadata.backend must be embedded, postgres, or custom")
    metadata = MetadataConfig(
        backend=backend,
        target=_string(metadata_raw.get("target"), "metadata.target", "metadata"),
        directory=_relative_path(metadata_raw.get("directory"), "metadata.directory", "filer"),
        postgres_host=_string(metadata_raw.get("postgres_host"), "metadata.postgres_host", ""),
        postgres_port=_int(metadata_raw.get("postgres_port"), "metadata.postgres_port", 5432),
        postgres_user=_string(metadata_raw.get("postgres_user"), "metadata.postgres_user", ""),
        postgres_password_file=_string(metadata_raw.get("postgres_password_file"), "metadata.postgres_password_file", ""),
        postgres_database=_string(metadata_raw.get("postgres_database"), "metadata.postgres_database", ""),
        postgres_schema=_string(metadata_raw.get("postgres_schema"), "metadata.postgres_schema", "public"),
        postgres_sslmode=_string(metadata_raw.get("postgres_sslmode"), "metadata.postgres_sslmode", "require"),
        custom_filer_toml=_string(metadata_raw.get("custom_filer_toml"), "metadata.custom_filer_toml", ""),
    )
    if metadata.backend == "embedded" and metadata.target not in targets:
        raise ConfigError(f"metadata.target references missing storage target: {metadata.target}")
    if metadata.backend == "postgres":
        missing = [name for name, value in {
            "postgres_host": metadata.postgres_host,
            "postgres_user": metadata.postgres_user,
            "postgres_password_file": metadata.postgres_password_file,
            "postgres_database": metadata.postgres_database,
        }.items() if not value]
        if missing:
            raise ConfigError(f"metadata postgres settings missing: {', '.join(missing)}")
    if metadata.backend == "custom" and not metadata.custom_filer_toml:
        raise ConfigError("metadata.custom_filer_toml is required for custom backend")

    index_raw = _table(raw, "index")
    index = IndexConfig(
        target=_string(index_raw.get("target"), "index.target", "index"),
        directory=_relative_path(index_raw.get("directory"), "index.directory", "volume-indexes"),
    )
    if index.target not in targets:
        raise ConfigError(f"index.target references missing storage target: {index.target}")

    seaweed_raw = _table(raw, "seaweed")
    seaweed = SeaweedConfig(
        binary=_string(seaweed_raw.get("binary"), "seaweed.binary", "/usr/local/bin/weed"),
        master_port=_int(seaweed_raw.get("master_port"), "seaweed.master_port", 9333),
        volume_port=_int(seaweed_raw.get("volume_port"), "seaweed.volume_port", 8080),
        filer_port=_int(seaweed_raw.get("filer_port"), "seaweed.filer_port", 8888),
        s3_internal_port=_int(seaweed_raw.get("s3_internal_port"), "seaweed.s3_internal_port", 18333),
        volume_directory=_relative_path(seaweed_raw.get("volume_directory"), "seaweed.volume_directory", "volumes"),
        volume_max=_int(seaweed_raw.get("volume_max"), "seaweed.volume_max", 0),
        volume_size_limit_mb=_int(seaweed_raw.get("volume_size_limit_mb"), "seaweed.volume_size_limit_mb", 30000),
        default_replication=_string(seaweed_raw.get("default_replication"), "seaweed.default_replication", "000"),
        filer_max_mb=_int(seaweed_raw.get("filer_max_mb"), "seaweed.filer_max_mb", 16),
        data_center=_string(seaweed_raw.get("data_center"), "seaweed.data_center", ""),
        rack=_string(seaweed_raw.get("rack"), "seaweed.rack", ""),
        disk_type=_string(seaweed_raw.get("disk_type"), "seaweed.disk_type", ""),
        volume_extra_args=tuple(_string_list(seaweed_raw.get("volume_extra_args"), "seaweed.volume_extra_args")),
        master_extra_args=tuple(_string_list(seaweed_raw.get("master_extra_args"), "seaweed.master_extra_args")),
        filer_extra_args=tuple(_string_list(seaweed_raw.get("filer_extra_args"), "seaweed.filer_extra_args")),
        s3_extra_args=tuple(_string_list(seaweed_raw.get("s3_extra_args"), "seaweed.s3_extra_args")),
        encrypt_volume_data=_bool(seaweed_raw.get("encrypt_volume_data"), "seaweed.encrypt_volume_data", False),
        volume_health_enabled=_bool(
            seaweed_raw.get("volume_health_enabled"), "seaweed.volume_health_enabled", True,
        ),
        expected_readonly_volume_ids=tuple(
            _int_list(seaweed_raw.get("expected_readonly_volume_ids"), "seaweed.expected_readonly_volume_ids")
        ),
        auto_index_repair_enabled=_bool(
            seaweed_raw.get("auto_index_repair_enabled"), "seaweed.auto_index_repair_enabled", True,
        ),
        index_repair_concurrency=_int(
            seaweed_raw.get("index_repair_concurrency"), "seaweed.index_repair_concurrency", 1,
        ),
        index_repair_timeout_seconds=_int(
            seaweed_raw.get("index_repair_timeout_seconds"), "seaweed.index_repair_timeout_seconds", 3600,
        ),
    )
    for field_name in ("master_port", "volume_port", "filer_port", "s3_internal_port", "volume_size_limit_mb", "filer_max_mb"):
        _positive(getattr(seaweed, field_name), f"seaweed.{field_name}")
    _positive(seaweed.volume_max, "seaweed.volume_max", allow_zero=True)
    if any(volume_id <= 0 for volume_id in seaweed.expected_readonly_volume_ids):
        raise ConfigError("seaweed.expected_readonly_volume_ids must contain positive volume IDs")
    if len(seaweed.expected_readonly_volume_ids) != len(set(seaweed.expected_readonly_volume_ids)):
        raise ConfigError("seaweed.expected_readonly_volume_ids must not contain duplicates")
    _positive(seaweed.index_repair_concurrency, "seaweed.index_repair_concurrency")
    _positive(seaweed.index_repair_timeout_seconds, "seaweed.index_repair_timeout_seconds")
    if seaweed.index_repair_concurrency > 8:
        raise ConfigError("seaweed.index_repair_concurrency may not exceed 8")
    if seaweed.auto_index_repair_enabled and not seaweed.volume_health_enabled:
        raise ConfigError("seaweed.auto_index_repair_enabled requires seaweed.volume_health_enabled")

    s3_raw = _table(raw, "s3")
    auth_mode = _string(s3_raw.get("auth_mode"), "s3.auth_mode", "static").lower()
    if auth_mode not in {"static", "config", "none"}:
        raise ConfigError("s3.auth_mode must be static, config, or none")
    tls_mode = _string(s3_raw.get("tls_mode"), "s3.tls_mode", "off").lower()
    if tls_mode not in {"off", "terminate"}:
        raise ConfigError("s3.tls_mode must be off or terminate")

    admission_raw = _table(s3_raw, "admission")
    admission = S3AdmissionConfig(
        enabled=_bool(admission_raw.get("enabled"), "s3.admission.enabled", True),
        max_active_requests=_int(
            admission_raw.get("max_active_requests"),
            "s3.admission.max_active_requests",
            32,
        ),
        max_queued_requests=_int(
            admission_raw.get("max_queued_requests"),
            "s3.admission.max_queued_requests",
            128,
        ),
        queue_timeout_seconds=_int(
            admission_raw.get("queue_timeout_seconds"),
            "s3.admission.queue_timeout_seconds",
            30,
        ),
    )
    _positive(admission.max_active_requests, "s3.admission.max_active_requests")
    _positive(admission.max_queued_requests, "s3.admission.max_queued_requests")
    _positive(admission.queue_timeout_seconds, "s3.admission.queue_timeout_seconds")

    s3 = S3Config(
        host=_string(s3_raw.get("host"), "s3.host", "0.0.0.0"),
        port=_int(s3_raw.get("port"), "s3.port", 8333),
        domain_name=_string(s3_raw.get("domain_name"), "s3.domain_name", ""),
        allowed_origins=_string(s3_raw.get("allowed_origins"), "s3.allowed_origins", "*"),
        external_url=_string(s3_raw.get("external_url"), "s3.external_url", ""),
        auth_mode=auth_mode,
        access_key_file=_string(s3_raw.get("access_key_file"), "s3.access_key_file", "/run/secrets/s3_access_key"),
        secret_key_file=_string(s3_raw.get("secret_key_file"), "s3.secret_key_file", "/run/secrets/s3_secret_key"),
        auth_config_file=_string(s3_raw.get("auth_config_file"), "s3.auth_config_file", ""),
        canary_access_key_file=_string(s3_raw.get("canary_access_key_file"), "s3.canary_access_key_file", ""),
        canary_secret_key_file=_string(s3_raw.get("canary_secret_key_file"), "s3.canary_secret_key_file", ""),
        iam_config_file=_string(s3_raw.get("iam_config_file"), "s3.iam_config_file", ""),
        audit_log_config_file=_string(s3_raw.get("audit_log_config_file"), "s3.audit_log_config_file", ""),
        tls_mode=tls_mode,
        tls_pem_file=_string(s3_raw.get("tls_pem_file"), "s3.tls_pem_file", ""),
        admission=admission,
    )
    _positive(s3.port, "s3.port")
    if s3.port < 1024:
        raise ConfigError("s3.port must be 1024 or greater; publish privileged host ports through Docker port mapping")
    if s3.auth_mode == "static" and (not s3.access_key_file or not s3.secret_key_file):
        raise ConfigError("static S3 auth requires access_key_file and secret_key_file")
    if s3.auth_mode == "config" and not s3.auth_config_file:
        raise ConfigError("config S3 auth requires auth_config_file")
    if s3.auth_mode == "config" and appliance.s3_canary_enabled:
        if not s3.canary_access_key_file or not s3.canary_secret_key_file:
            raise ConfigError("config S3 auth with canary enabled requires canary_access_key_file and canary_secret_key_file")
    if s3.tls_mode == "terminate" and not s3.tls_pem_file:
        raise ConfigError("TLS termination requires s3.tls_pem_file")

    ports = [appliance.health_port, seaweed.master_port, seaweed.volume_port, seaweed.filer_port, seaweed.s3_internal_port, s3.port]
    if any(port > 65535 for port in ports):
        raise ConfigError("ports must be between 1 and 65535")
    if len(ports) != len(set(ports)):
        raise ConfigError("appliance and SeaweedFS ports must be unique")

    config = Config(appliance=appliance, targets=targets, metadata=metadata, index=index, seaweed=seaweed, s3=s3)

    active_mountpoints: dict[Path, str] = {}
    for target in config.active_targets:
        resolved = target.mountpoint.resolve(strict=False)
        previous = active_mountpoints.get(resolved)
        if previous and previous != target.name:
            raise ConfigError(
                f"active storage targets {previous} and {target.name} use the same mountpoint; "
                "reference one target from metadata/index instead"
            )
        active_mountpoints[resolved] = target.name

    role_paths = {"volume": config.volume_path.resolve(strict=False), "index": config.index_path.resolve(strict=False)}
    if config.metadata.backend == "embedded":
        role_paths["metadata"] = config.metadata_path.resolve(strict=False)
    seen_roles: dict[Path, str] = {}
    for role, role_path in role_paths.items():
        previous = seen_roles.get(role_path)
        if previous:
            raise ConfigError(f"SeaweedFS role directories {previous} and {role} resolve to the same path")
        seen_roles[role_path] = role

    return config

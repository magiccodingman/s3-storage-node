from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path


@dataclass(frozen=True)
class TargetConfig:
    name: str
    type: str
    mountpoint: Path
    subdirectory: str
    sentinel_id: str
    sentinel_file: str = ".s3-storage-node.json"
    allow_initialize: bool = False
    min_free_bytes: int = 1_073_741_824
    source: str = ""
    credentials_file: str = ""
    mount_options: tuple[str, ...] = ()
    io_failure_policy: str = ""
    minimum_smb_dialect: str = ""
    handle_reconnect_policy: str = "disabled"
    multichannel_policy: str = "disabled"
    max_channels: int = 2
    require_transport_observability: bool = False
    device: str = ""
    expected_uuid: str = ""
    expected_filesystem: str = ""
    transport_name: str = ""
    ssh_auth_mode: str = "key"
    ssh_identity_file: str = ""
    ssh_credentials_file: str = ""
    ssh_known_hosts_file: str = ""
    ssh_runtime_identity_file: str = ""
    ssh_runtime_pid_file: str = ""
    ssh_port: int = 22

    @property
    def storage_root(self) -> Path:
        return self.mountpoint / self.subdirectory if self.subdirectory else self.mountpoint

    @property
    def effective_io_failure_policy(self) -> str:
        return self.io_failure_policy or "soft"


@dataclass(frozen=True)
class ApplianceConfig:
    name: str = "s3-storage-node"
    state_dir: Path = Path("/var/lib/s3-storage-node")
    runtime_dir: Path = Path("/run/s3-storage-node")
    uid: int = 10001
    gid: int = 10001
    health_host: str = "0.0.0.0"
    health_port: int = 9090
    probe_interval_seconds: int = 5
    full_probe_interval_seconds: int = 60
    probe_timeout_seconds: int = 4
    startup_timeout_seconds: int = 30
    shutdown_grace_seconds: int = 20
    recovery_initial_seconds: int = 5
    recovery_max_seconds: int = 60
    recovery_stability_seconds: int = 15
    recovery_probe_interval_seconds: int = 2
    recovery_successes_required: int = 3
    s3_canary_enabled: bool = True
    worker_fencing_mode: str = "disabled"
    worker_host_address: str = "169.254.254.1/30"
    worker_address: str = "169.254.254.2/30"
    worker_gateway: str = "169.254.254.1"


@dataclass(frozen=True)
class MetadataConfig:
    backend: str = "embedded"
    target: str = "metadata"
    directory: str = "filer"
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password_file: str = ""
    postgres_database: str = ""
    postgres_schema: str = "public"
    postgres_sslmode: str = "require"
    custom_filer_toml: str = ""


@dataclass(frozen=True)
class IndexConfig:
    target: str = "index"
    directory: str = "volume-indexes"


@dataclass(frozen=True)
class SeaweedConfig:
    binary: str = "/usr/local/bin/weed"
    master_port: int = 9333
    volume_port: int = 8080
    filer_port: int = 8888
    s3_internal_port: int = 18333
    volume_directory: str = "volumes"
    volume_max: int = 0
    volume_size_limit_mb: int = 30000
    default_replication: str = "000"
    filer_max_mb: int = 16
    data_center: str = ""
    rack: str = ""
    disk_type: str = ""
    volume_extra_args: tuple[str, ...] = ()
    master_extra_args: tuple[str, ...] = ()
    filer_extra_args: tuple[str, ...] = ()
    s3_extra_args: tuple[str, ...] = ()
    encrypt_volume_data: bool = False


@dataclass(frozen=True)
class S3AdmissionConfig:
    enabled: bool = True
    max_active_requests: int = 32
    max_queued_requests: int = 128
    queue_timeout_seconds: int = 30


@dataclass(frozen=True)
class S3Config:
    host: str = "0.0.0.0"
    port: int = 8333
    domain_name: str = ""
    allowed_origins: str = "*"
    external_url: str = ""
    auth_mode: str = "static"
    access_key_file: str = "/run/secrets/s3_access_key"
    secret_key_file: str = "/run/secrets/s3_secret_key"
    auth_config_file: str = ""
    canary_access_key_file: str = ""
    canary_secret_key_file: str = ""
    iam_config_file: str = ""
    audit_log_config_file: str = ""
    tls_mode: str = "off"
    tls_pem_file: str = ""
    admission: S3AdmissionConfig = field(default_factory=S3AdmissionConfig)


@dataclass(frozen=True)
class Config:
    appliance: ApplianceConfig
    targets: dict[str, TargetConfig]
    metadata: MetadataConfig
    index: IndexConfig
    seaweed: SeaweedConfig
    s3: S3Config

    @property
    def data_target(self) -> TargetConfig:
        return self.targets["data"]

    @property
    def active_target_names(self) -> tuple[str, ...]:
        names = ["data", self.index.target]
        if self.metadata.backend == "embedded":
            names.append(self.metadata.target)
        return tuple(dict.fromkeys(names))

    @property
    def active_targets(self) -> list[TargetConfig]:
        return [self.targets[name] for name in self.active_target_names]

    @property
    def volume_path(self) -> Path:
        return self.data_target.storage_root / self.seaweed.volume_directory

    @property
    def metadata_path(self) -> Path:
        return self.targets[self.metadata.target].storage_root / self.metadata.directory

    @property
    def index_path(self) -> Path:
        return self.targets[self.index.target].storage_root / self.index.directory

    @property
    def worker_endpoint_host(self) -> str:
        if self.appliance.worker_fencing_mode != "namespace":
            return "127.0.0.1"
        return str(ipaddress.ip_interface(self.appliance.worker_address).ip)

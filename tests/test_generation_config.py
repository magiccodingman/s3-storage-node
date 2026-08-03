from pathlib import Path

import pytest

from s3_storage_node.config import ConfigError, load_config


def write_config(tmp_path: Path, *, data_type: str = "cifs", mode: str = "namespace", worker: str = "169.254.254.2/30") -> Path:
    state = tmp_path / "state"
    runtime = tmp_path / "run"
    if data_type == "cifs":
        data = f'''type = "cifs"\nsource = "//server/share"\ncredentials_file = "/run/secrets/cifs"\nmountpoint = "{runtime / 'mounts' / 'data'}"'''
    else:
        data = f'''type = "path"\nmountpoint = "{state / 'data'}"\nallow_initialize = true'''
    path = tmp_path / "config.toml"
    path.write_text(f'''
[appliance]
state_dir = "{state}"
runtime_dir = "{runtime}"
worker_fencing_mode = "{mode}"
worker_host_address = "169.254.254.1/30"
worker_address = "{worker}"
worker_gateway = "169.254.254.1"
recovery_stability_seconds = 1

[storage.data]
{data}
sentinel_id = "data"
min_free_bytes = 0

[metadata]
backend = "embedded"
target = "data"
directory = "metadata"

[index]
target = "data"
directory = "indexes"

[seaweed]
volume_directory = "volumes"

[s3]
auth_mode = "none"
''', encoding="utf-8")
    return path


def test_namespace_fencing_configuration(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.appliance.worker_fencing_mode == "namespace"
    assert config.worker_endpoint_host == "169.254.254.2"


def test_namespace_fencing_requires_cifs_data(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires storage.data.type=cifs"):
        load_config(write_config(tmp_path, data_type="path"))


def test_namespace_addresses_must_share_subnet(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="share one subnet"):
        load_config(write_config(tmp_path, worker="169.254.253.2/30"))

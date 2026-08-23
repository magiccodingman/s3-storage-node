from pathlib import Path

import pytest

from s3_storage_node.config import ConfigError, load_config


BASE = """
[appliance]
state_dir = "{state}"
runtime_dir = "{runtime}"

[storage.data]
type = "path"
mountpoint = "{data}"
sentinel_id = "data-1"
allow_initialize = true

[storage.metadata]
type = "path"
mountpoint = "{meta}"
sentinel_id = "meta-1"
allow_initialize = true

[storage.index]
type = "path"
mountpoint = "{index}"
sentinel_id = "index-1"
allow_initialize = true

[metadata]
backend = "embedded"
target = "metadata"

[index]
target = "index"

[seaweed]

[s3]
auth_mode = "none"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_valid_path_configuration(tmp_path: Path) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    for path_to_create in (state, runtime, state / "data", state / "meta", state / "index"):
        path_to_create.mkdir(parents=True, exist_ok=True)
    path = write_config(tmp_path, BASE.format(
        state=state, runtime=runtime, data=state / "data",
        meta=state / "meta", index=state / "index",
    ))
    config = load_config(path)
    assert config.data_target.type == "path"
    assert config.metadata.backend == "embedded"
    assert config.index.target == "index"
    assert config.volume_path == tmp_path / "state" / "data" / "volumes"
    assert config.metadata_path == tmp_path / "state" / "meta" / "filer"
    assert config.index_path == tmp_path / "state" / "index" / "volume-indexes"


def test_data_sentinel_is_required(tmp_path: Path) -> None:
    text = BASE.format(state="/tmp/state", runtime="/tmp/run", data="/tmp/state/data", meta="/tmp/state/meta", index="/tmp/state/index")
    text = text.replace('sentinel_id = "data-1"', 'sentinel_id = ""', 1)
    with pytest.raises(ConfigError, match="storage.data.sentinel_id"):
        load_config(write_config(tmp_path, text))


def test_rejects_missing_metadata_target(tmp_path: Path) -> None:
    text = BASE.format(state="/tmp/state", runtime="/tmp/run", data="/tmp/state/data", meta="/tmp/state/meta", index="/tmp/state/index")
    text = text.replace('target = "metadata"', 'target = "missing"', 1)
    with pytest.raises(ConfigError, match="missing storage target"):
        load_config(write_config(tmp_path, text))


def test_roles_can_share_one_physical_target(tmp_path: Path) -> None:
    text = f"""
[appliance]
state_dir = "{tmp_path / 'state'}"
runtime_dir = "{tmp_path / 'runtime'}"

[storage.data]
type = "path"
mountpoint = "{tmp_path / 'state' / 'bulk'}"
subdirectory = "seaweedfs"
sentinel_id = "bulk-1"
allow_initialize = true

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
"""
    config = load_config(write_config(tmp_path, text))
    assert config.active_target_names == ("data",)
    assert config.volume_path == tmp_path / "state" / "bulk" / "seaweedfs" / "volumes"
    assert config.metadata_path == tmp_path / "state" / "bulk" / "seaweedfs" / "metadata"
    assert config.index_path == tmp_path / "state" / "bulk" / "seaweedfs" / "indexes"


def test_managed_mountpoint_must_stay_in_runtime_mounts(tmp_path: Path) -> None:
    text = f"""
[appliance]
state_dir = "{tmp_path / 'state'}"
runtime_dir = "{tmp_path / 'runtime'}"

[storage.data]
type = "cifs"
source = "//server/share"
credentials_file = "/run/secrets/cifs"
mountpoint = "/dangerous"
sentinel_id = "bulk-1"

[metadata]
backend = "embedded"
target = "data"

[index]
target = "data"

[s3]
auth_mode = "none"
"""
    with pytest.raises(ConfigError, match="must be beneath"):
        load_config(write_config(tmp_path, text))


def test_external_path_cannot_auto_initialize(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        f"""
[appliance]
state_dir = "{tmp_path / 'state'}"
runtime_dir = "{tmp_path / 'run'}"

[storage.data]
type = "path"
mountpoint = "{tmp_path / 'external'}"
sentinel_id = "external"
allow_initialize = true

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
""",
    )
    with pytest.raises(ConfigError, match="externally managed paths"):
        load_config(config_path)


def test_privileged_internal_s3_port_is_rejected(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        f"""
[appliance]
state_dir = "{tmp_path / 'state'}"
runtime_dir = "{tmp_path / 'run'}"

[storage.data]
type = "path"
mountpoint = "{tmp_path / 'state' / 'data'}"
sentinel_id = "data"
allow_initialize = true

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
port = 443
auth_mode = "none"
""",
    )
    with pytest.raises(ConfigError, match="1024 or greater"):
        load_config(config_path)


def test_expected_readonly_volume_ids_are_explicit_upstream_exceptions(tmp_path: Path) -> None:
    text = BASE.format(
        state=tmp_path / "state", runtime=tmp_path / "runtime",
        data=tmp_path / "state/data", meta=tmp_path / "state/meta", index=tmp_path / "state/index",
    ).replace("[seaweed]", "[seaweed]\nexpected_readonly_volume_ids = [23, 64]")
    config = load_config(write_config(tmp_path, text))
    assert config.seaweed.expected_readonly_volume_ids == (23, 64)


@pytest.mark.parametrize("value", ["[0]", "[23, 23]", '["23"]'])
def test_expected_readonly_volume_ids_are_validated(tmp_path: Path, value: str) -> None:
    text = BASE.format(
        state=tmp_path / "state", runtime=tmp_path / "runtime",
        data=tmp_path / "state/data", meta=tmp_path / "state/meta", index=tmp_path / "state/index",
    ).replace("[seaweed]", f"[seaweed]\nexpected_readonly_volume_ids = {value}")
    with pytest.raises(ConfigError, match="expected_readonly_volume_ids"):
        load_config(write_config(tmp_path, text))

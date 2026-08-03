from __future__ import annotations

from pathlib import Path

import pytest

from s3_storage_node.config import ConfigError, load_config


def config_text(tmp_path: Path, appliance_overrides: str = "") -> str:
    state = tmp_path / "state"
    return f"""
[appliance]
state_dir = "{state}"
runtime_dir = "{tmp_path / 'run'}"
{appliance_overrides}

[storage.data]
type = "path"
mountpoint = "{state / 'data'}"
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
auth_mode = "none"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_recovery_stability_controls_are_loaded(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            config_text(
                tmp_path,
                """recovery_stability_seconds = 45
recovery_probe_interval_seconds = 5
recovery_successes_required = 6""",
            ),
        )
    )
    assert config.appliance.recovery_stability_seconds == 45
    assert config.appliance.recovery_probe_interval_seconds == 5
    assert config.appliance.recovery_successes_required == 6


@pytest.mark.parametrize(
    "setting",
    [
        "recovery_stability_seconds = 0",
        "recovery_probe_interval_seconds = 0",
        "recovery_successes_required = 0",
    ],
)
def test_recovery_stability_controls_must_be_positive(tmp_path: Path, setting: str) -> None:
    with pytest.raises(ConfigError, match="must be greater than zero"):
        load_config(write_config(tmp_path, config_text(tmp_path, setting)))

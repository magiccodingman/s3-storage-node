from __future__ import annotations

from pathlib import Path

import pytest

from s3_storage_node.config import ConfigError, load_config


def _write_config(tmp_path: Path, admission: str = "") -> Path:
    state_dir = tmp_path / "state"
    runtime_dir = tmp_path / "runtime"
    data_dir = state_dir / "data"
    state_dir.mkdir()
    runtime_dir.mkdir()
    data_dir.mkdir()

    path = tmp_path / "config.toml"
    path.write_text(
        f'''[appliance]
state_dir = "{state_dir}"
runtime_dir = "{runtime_dir}"

[storage.data]
type = "path"
mountpoint = "{data_dir}"
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

{admission}
''',
        encoding="utf-8",
    )
    return path


def test_s3_admission_defaults_are_safe_and_bounded(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    assert config.s3.admission.enabled is True
    assert config.s3.admission.max_active_requests == 32
    assert config.s3.admission.max_queued_requests == 128
    assert config.s3.admission.queue_timeout_seconds == 30


def test_s3_admission_settings_are_configurable(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            '''[s3.admission]
enabled = true
max_active_requests = 24
max_queued_requests = 96
queue_timeout_seconds = 15
''',
        )
    )

    assert config.s3.admission.enabled is True
    assert config.s3.admission.max_active_requests == 24
    assert config.s3.admission.max_queued_requests == 96
    assert config.s3.admission.queue_timeout_seconds == 15


def test_s3_admission_can_be_disabled(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            '''[s3.admission]
enabled = false
''',
        )
    )

    assert config.s3.admission.enabled is False


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("max_active_requests = 0", "s3.admission.max_active_requests"),
        ("max_queued_requests = 0", "s3.admission.max_queued_requests"),
        ("queue_timeout_seconds = 0", "s3.admission.queue_timeout_seconds"),
    ],
)
def test_s3_admission_rejects_non_positive_limits(
    tmp_path: Path,
    setting: str,
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(
            _write_config(
                tmp_path,
                f'''[s3.admission]
{setting}
''',
            )
        )

import json
from pathlib import Path

from s3_storage_node.config import TargetConfig
from s3_storage_node.storage import decode_mountinfo_path, find_mount, read_mountinfo, verify_or_initialize_sentinel


def test_mountinfo_parser(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("36 25 0:31 / /run/node\\040data rw,relatime - cifs //server/share rw\n", encoding="utf-8")
    entries = read_mountinfo(str(mountinfo))
    assert entries[0].mountpoint == "/run/node data"
    assert entries[0].filesystem == "cifs"
    assert entries[0].source == "//server/share"


def test_decode_mountinfo_path() -> None:
    assert decode_mountinfo_path("/hello\\040world") == "/hello world"


def test_path_target_enrollment(tmp_path: Path) -> None:
    target = TargetConfig(
        name="data", type="path", mountpoint=tmp_path, subdirectory="volumes",
        sentinel_id="node-1", allow_initialize=True,
    )
    verify_or_initialize_sentinel(target, 0, 0)
    payload = json.loads((tmp_path / "volumes" / ".s3-storage-node.json").read_text())
    assert payload["sentinel_id"] == "node-1"
    assert (tmp_path / "volumes").is_dir()

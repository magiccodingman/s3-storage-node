from pathlib import Path

import pytest

from s3_storage_node.ssh_credentials import SshCredentialsError, read_password_credentials, ssh_source_username


def test_reads_cifs_style_credentials_for_ssh(tmp_path: Path) -> None:
    path = tmp_path / "credentials"
    path.write_text("username=u123456\npassword=a=b=c\ndomain=\n")
    value = read_password_credentials(path)
    assert value.username == "u123456"
    assert value.password == "a=b=c"


def test_missing_password_fails_without_echoing_username_value(tmp_path: Path) -> None:
    path = tmp_path / "credentials"
    path.write_text("username=u123456\n")
    with pytest.raises(SshCredentialsError, match="missing password"):
        read_password_credentials(path)


def test_source_username_parser() -> None:
    assert ssh_source_username("u123456@u123456.your-storagebox.de:/home") == "u123456"

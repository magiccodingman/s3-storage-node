from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SshCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class PasswordCredentials:
    username: str
    password: str


def read_password_credentials(path: str | Path) -> PasswordCredentials:
    credential_path = Path(path)
    try:
        lines = credential_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SshCredentialsError(f"unable to read SSH password credentials file: {credential_path}") from exc

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip().lower()] = value

    username = values.get("username", "").strip()
    password = values.get("password", "")
    if not username:
        raise SshCredentialsError(f"SSH password credentials file is missing username: {credential_path}")
    if not password:
        raise SshCredentialsError(f"SSH password credentials file is missing password: {credential_path}")
    if any(character in username for character in ("\n", "\r", "\x00", "@", ":")):
        raise SshCredentialsError("SSH password username contains invalid characters")
    if any(character in password for character in ("\n", "\r", "\x00")):
        raise SshCredentialsError("SSH password contains invalid control characters")
    return PasswordCredentials(username=username, password=password)


def ssh_source_username(source: str) -> str:
    authority, separator, _path = source.partition(":")
    if not separator:
        raise SshCredentialsError("SSHFS source must look like user@host:/remote/path")
    username, at, host = authority.rpartition("@")
    if not at or not username or not host:
        raise SshCredentialsError("SSHFS source must include user@host")
    return username

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Config


def read_secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret is empty: {path}")
    return value


def write_atomic(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def render_filer_toml(config: Config) -> Path | None:
    runtime = config.appliance.runtime_dir / "generated"
    if config.metadata.backend == "embedded":
        path = runtime / "filer.toml"
        write_atomic(path, f"[leveldb2]\nenabled = true\ndir = {_toml_string(str(config.metadata_path))}\n")
        return path
    if config.metadata.backend == "custom":
        source = Path(config.metadata.custom_filer_toml)
        path = runtime / "filer.toml"
        write_atomic(path, source.read_text(encoding="utf-8"))
        return path

    password = read_secret(config.metadata.postgres_password_file)
    content = f'''[postgres2]
enabled = true
hostname = {_toml_string(config.metadata.postgres_host)}
port = {config.metadata.postgres_port}
username = {_toml_string(config.metadata.postgres_user)}
password = {_toml_string(password)}
database = {_toml_string(config.metadata.postgres_database)}
schema = {_toml_string(config.metadata.postgres_schema)}
sslmode = {_toml_string(config.metadata.postgres_sslmode)}
connection_max_idle = 25
connection_max_open = 50
connection_max_lifetime_seconds = 0
enableUpsert = true
createTable = """
  CREATE TABLE IF NOT EXISTS "%s" (
    dirhash BIGINT,
    name VARCHAR(65535),
    directory VARCHAR(65535),
    meta BYTEA,
    PRIMARY KEY (dirhash, name)
  );
"""
upsertQuery = """
  INSERT INTO "%[1]s" (dirhash,name,directory,meta) VALUES($1,$2,$3,$4)
  ON CONFLICT (dirhash,name) DO UPDATE SET meta = EXCLUDED.meta
  WHERE "%[1]s".meta != EXCLUDED.meta
"""
'''
    path = runtime / "filer.toml"
    write_atomic(path, content)
    return path


def render_s3_config(config: Config) -> tuple[Path | None, str | None, str | None]:
    if config.s3.auth_mode == "none":
        return None, None, None
    if config.s3.auth_mode == "config":
        source = Path(config.s3.auth_config_file)
        path = config.appliance.runtime_dir / "generated" / "s3.json"
        write_atomic(path, source.read_text(encoding="utf-8"))
        access_key = read_secret(config.s3.canary_access_key_file)
        secret_key = read_secret(config.s3.canary_secret_key_file)
        return path, access_key, secret_key

    access_key = read_secret(config.s3.access_key_file)
    secret_key = read_secret(config.s3.secret_key_file)
    payload = {
        "identities": [
            {
                "name": "s3-storage-node-admin",
                "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                "actions": ["Admin", "Read", "List", "Tagging", "Write"],
            }
        ]
    }
    path = config.appliance.runtime_dir / "generated" / "s3.json"
    write_atomic(path, json.dumps(payload, indent=2) + "\n")
    return path, access_key, secret_key


def render_haproxy(config: Config) -> Path:
    bind = f"{config.s3.host}:{config.s3.port}"
    tls = ""
    if config.s3.tls_mode == "terminate":
        tls = f" ssl crt {config.s3.tls_pem_file}"
    backend_host = config.worker_endpoint_host
    health_host = config.appliance.health_host
    if health_host in {"0.0.0.0", "::", "[::]"}:
        health_host = "127.0.0.1"
    content = f'''global
  log stdout format raw local0
  maxconn 4096
defaults
  log global
  mode http
  option httplog
  timeout connect 5s
  timeout client 5m
  timeout server 5m
  timeout http-request 30s

frontend s3_public
  bind {bind}{tls}
  default_backend seaweed_s3

backend seaweed_s3
  option httpchk GET /ready
  http-check expect status 200
  server worker_s3 {backend_host}:{config.seaweed.s3_internal_port} check addr {health_host} port {config.appliance.health_port} inter 250ms fall 1 rise 1
'''
    path = config.appliance.runtime_dir / "generated" / "haproxy.cfg"
    write_atomic(path, content, 0o644)
    return path

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _string(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def _int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    return value


def _bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    return list(value)


def _int_list(value: Any, name: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        raise ConfigError(f"{name} must be an array of integers")
    return list(value)


def _relative_path(value: Any, name: str, default: str) -> str:
    result = _string(value, name, default)
    candidate = Path(result)
    if not result or candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError(f"{name} must be a non-empty relative path without '..'")
    return result


def _positive(value: int, name: str, *, allow_zero: bool = False) -> int:
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise ConfigError(f"{name} must be {qualifier}")
    return value

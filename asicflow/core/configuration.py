"""Load and merge framework YAML without taking ownership of ORFS Make config."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml


class ConfigurationError(ValueError):
    """Invalid or incomplete ASIC flow configuration."""


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_yaml(path: Path | str, *, required: bool = False) -> dict[str, Any]:
    """Load a YAML mapping. Missing optional files produce an empty mapping."""
    source = Path(path).expanduser()
    if not source.is_file():
        if required:
            raise ConfigurationError(f"configuration file not found: {source}")
        return {}
    try:
        data = yaml.safe_load(source.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"top-level YAML value must be a mapping: {source}")
    return _expand(data)


def deep_merge(*mappings: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mappings left-to-right; nested mappings are merged recursively."""
    result: dict[str, Any] = {}
    for mapping in mappings:
        for key, value in mapping.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = deep_merge(result[key], value)
            elif isinstance(value, Mapping):
                result[key] = deep_merge(value)
            else:
                result[key] = value
    return result


def load_configuration(
    config_dir: Path | str,
    *,
    platform: str,
    design_name: str | None = None,
    design_profile: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return global, platform and design settings from one configuration root."""
    root = Path(config_dir).expanduser().resolve()
    global_settings = load_yaml(root / "flow.yaml")
    platform_settings = load_yaml(root / "platforms" / f"{platform}.yaml")
    if design_profile:
        profile_path = Path(design_profile).expanduser()
        if not profile_path.is_absolute():
            profile_path = root / profile_path
    elif design_name:
        profile_path = root / "designs" / f"{design_name}.yaml"
    else:
        profile_path = None
    design_settings = load_yaml(profile_path) if profile_path else {}
    return global_settings, platform_settings, design_settings

"""Target-platform configuration resolution and the provider interface.

Resolution order (later layers override earlier ones):

1. ``DEFAULT_PLATFORM_CONFIG`` below.
2. ``defaults`` from the JSON platforms file.
3. ``platforms.<name>`` from the JSON platforms file.
4. The mapping returned by the configured provider module.
5. Per-build request overrides received by the HTTP API.

A provider is a Python module (configured through ``build.provider``) that
exposes either ``get_platform_config(platform, context)`` or a class named
``Provider`` with that method. Providers are trusted code running inside the
service; requests from API clients cannot load providers.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable


class PlatformConfigError(RuntimeError):
    """Raised when platform configuration cannot be loaded or is invalid."""


DEFAULT_PLATFORM_CONFIG: dict[str, Any] = {
    "description": "",
    "msbuild_platform": "x64",
    "configuration": "Release",
    "platform_toolset": None,
    "windows_sdk_version": None,
    "solution": "Source/KDU.sln",
    "artifacts": {
        "kdu.exe": "Source/Hamakaze/output/{msbuild_platform}/{configuration}/kdu.exe",
        "drv64.dll": "Source/Tanikaze/output/{msbuild_platform}/{configuration}/drv64.dll",
    },
    "required_artifacts": ["kdu.exe"],
    "dist_dir": "dist/{platform}",
    "extra_properties": {},
}


class PlatformConfigProvider(ABC):
    """Formal provider interface (optional; plain functions are also accepted)."""

    @abstractmethod
    def get_platform_config(self, platform: str, context: dict[str, Any]) -> dict[str, Any]:
        """Return a partial platform configuration for *platform*.

        ``context`` keys: ``repo_root`` (str or None), ``platform``,
        ``config_path``, ``base_config`` (already resolved defaults + JSON),
        ``env`` (snapshot of ``os.environ``).
        """


def merge_config(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Recursively merge *override* into a copy of *base*."""
    if override is None:
        return copy.deepcopy(base)
    if not isinstance(override, dict):
        raise PlatformConfigError(
            f"Platform config override must be a mapping, got {type(override).__name__}"
        )
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_platform_document(config_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(config_path)
    if not path.is_file():
        raise PlatformConfigError(f"Platforms file not found: {path}")
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PlatformConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise PlatformConfigError(f"Platforms file {path} must contain a JSON object")
    if not isinstance(document.get("defaults", {}), dict):
        raise PlatformConfigError(f"'defaults' in {path} must be an object")
    if not isinstance(document.get("platforms", {}), dict):
        raise PlatformConfigError(f"'platforms' in {path} must be an object")
    document.setdefault("defaults", {})
    document.setdefault("platforms", {})
    return document


def load_provider(provider_path: str | os.PathLike[str]) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    path = Path(provider_path).resolve()
    if not path.is_file():
        raise PlatformConfigError(f"Provider module not found: {path}")

    module_name = f"kdu_platform_provider_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PlatformConfigError(f"Cannot import provider module: {path}")

    provider_dir = str(path.parent)
    if provider_dir not in sys.path:
        sys.path.insert(0, provider_dir)

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - report provider failures clearly
        sys.modules.pop(module_name, None)
        raise PlatformConfigError(f"Failed to load provider {path}: {exc}") from exc

    provider_class = getattr(module, "Provider", None)
    if provider_class is not None:
        try:
            provider = provider_class()
        except Exception as exc:  # noqa: BLE001
            raise PlatformConfigError(f"Failed to instantiate Provider from {path}: {exc}") from exc
        getter = getattr(provider, "get_platform_config", None)
    else:
        getter = getattr(module, "get_platform_config", None)

    if not callable(getter):
        raise PlatformConfigError(
            f"Provider {path} must define get_platform_config() or a Provider class"
        )
    return getter


def resolve_platform_config(
    platform: str | None,
    platforms_file: str | os.PathLike[str],
    provider: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    document = load_platform_document(platforms_file)
    platforms = document.get("platforms") or {}
    selected = platform or document.get("default_platform")

    if not selected:
        raise PlatformConfigError(
            "No platform selected and the platforms file has no 'default_platform'"
        )
    if selected not in platforms and provider is None:
        available = ", ".join(sorted(platforms)) or "<none>"
        raise PlatformConfigError(
            f"Unknown platform '{selected}'. Available platforms: {available}"
        )

    config = merge_config(DEFAULT_PLATFORM_CONFIG, document.get("defaults"))
    config = merge_config(config, platforms.get(selected) or {})

    context = {
        "repo_root": str(repo_root) if repo_root is not None else None,
        "platform": selected,
        "config_path": str(Path(platforms_file).resolve()),
        "base_config": copy.deepcopy(config),
        "env": dict(os.environ),
    }
    if provider is not None:
        config = merge_config(config, load_provider(provider)(selected, context))

    config = merge_config(config, {k: v for k, v in (overrides or {}).items() if v is not None})
    config["platform"] = selected
    validate_config(config, selected)
    return config


def validate_config(config: dict[str, Any], platform: str) -> None:
    for key in ("msbuild_platform", "configuration", "solution", "dist_dir"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PlatformConfigError(
                f"Platform '{platform}': '{key}' must be a non-empty string"
            )
    if not isinstance(config.get("artifacts"), dict):
        raise PlatformConfigError(f"Platform '{platform}': 'artifacts' must be an object")
    if not isinstance(config.get("extra_properties"), dict):
        raise PlatformConfigError(f"Platform '{platform}': 'extra_properties' must be an object")
    required = config.get("required_artifacts", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise PlatformConfigError(
            f"Platform '{platform}': 'required_artifacts' must be a list of strings"
        )
    missing = [item for item in required if item not in config["artifacts"]]
    if missing:
        raise PlatformConfigError(
            f"Platform '{platform}': required artifacts missing from 'artifacts': "
            + ", ".join(missing)
        )


def platform_summaries(platforms_file: str | os.PathLike[str]) -> list[dict[str, Any]]:
    document = load_platform_document(platforms_file)
    summaries: list[dict[str, Any]] = []
    for name, platform_config in (document.get("platforms") or {}).items():
        merged = merge_config(DEFAULT_PLATFORM_CONFIG, document.get("defaults"))
        merged = merge_config(merged, platform_config or {})
        summaries.append(
            {
                "name": name,
                "description": merged.get("description") or "",
                "msbuild_platform": merged.get("msbuild_platform"),
                "configuration": merged.get("configuration"),
                "platform_toolset": merged.get("platform_toolset"),
                "required_artifacts": merged.get("required_artifacts", []),
            }
        )
    return summaries


def default_platform(platforms_file: str | os.PathLike[str]) -> str | None:
    document = load_platform_document(platforms_file)
    value = document.get("default_platform")
    return str(value) if value else None

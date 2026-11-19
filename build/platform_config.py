"""Target-platform configuration loading for the KDU build script.

This module is the stable "interface" used to obtain platform settings.
It supports three layers, merged in this order (later wins):

1. ``DEFAULT_PLATFORM_CONFIG`` below.
2. A JSON document (``build/platforms.json`` by default): ``defaults`` and
   ``platforms.<name>``.
3. A custom provider module passed with ``--provider``. A provider is a
   Python file that exposes either a module-level function::

       def get_platform_config(platform, context): ...

   or a class named ``Provider`` with a ``get_platform_config`` method.
   The provider returns a (partial) mapping that is merged over the
   configuration resolved so far.

Command line overrides are applied after the provider. See README.md
("Automated build") for the full contract.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any


class PlatformConfigError(RuntimeError):
    """Raised when platform configuration cannot be loaded or is invalid."""


#: Baseline configuration. Every key can be overridden by a JSON file or a
#: provider. ``None`` means "do not pass this MSBuild property", so the value
#: stored in the project file is used.
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
    """Formal provider interface (optional; plain functions work too)."""

    @abstractmethod
    def get_platform_config(self, platform: str, context: dict[str, Any]) -> dict[str, Any]:
        """Return a partial platform configuration for *platform*.

        ``context`` contains:

        * ``repo_root``   - absolute path of the repository root (str or None)
        * ``platform``    - selected platform name
        * ``config_path`` - absolute path of the JSON config, if any
        * ``base_config`` - configuration resolved from defaults + JSON
        * ``env``         - a copy of ``os.environ``
        """


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into a copy of *base*.

    Dictionaries (``artifacts``, ``extra_properties``, ...) are merged
    key-by-key; every other value replaces the previous one.
    """
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


def load_config_document(config_path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Load the JSON platform document. A missing path yields an empty one."""
    if config_path is None:
        return {"default_platform": None, "defaults": {}, "platforms": {}}

    path = Path(config_path)
    if not path.is_file():
        raise PlatformConfigError(f"Platform config file not found: {path}")

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PlatformConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise PlatformConfigError(f"Platform config {path} must contain a JSON object")

    defaults = document.get("defaults", {})
    platforms = document.get("platforms", {})
    if not isinstance(defaults, dict):
        raise PlatformConfigError(f"'defaults' in {path} must be an object")
    if not isinstance(platforms, dict):
        raise PlatformConfigError(f"'platforms' in {path} must be an object")

    document["defaults"] = defaults
    document["platforms"] = platforms
    return document


def load_provider(provider_path: str | os.PathLike[str]) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Import a provider file and return its ``get_platform_config`` callable."""
    path = Path(provider_path).resolve()
    if not path.is_file():
        raise PlatformConfigError(f"Provider module not found: {path}")

    module_name = f"kdu_platform_provider_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PlatformConfigError(f"Cannot import provider module: {path}")

    # Allow the provider to import helpers that live next to it.
    provider_dir = str(path.parent)
    if provider_dir not in sys.path:
        sys.path.insert(0, provider_dir)

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PlatformConfigError(f"Failed to load provider {path}: {exc}") from exc

    provider_class = getattr(module, "Provider", None)
    if provider_class is not None:
        try:
            provider = provider_class()
        except Exception as exc:
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
    platform: str | None = None,
    config_path: str | os.PathLike[str] | None = None,
    provider_path: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve the final platform configuration.

    Precedence: defaults -> JSON defaults -> JSON platform -> provider ->
    *overrides* (typically command line flags).
    """
    document = load_config_document(config_path)
    platforms = document.get("platforms") or {}
    selected = platform or document.get("default_platform")

    if not selected:
        raise PlatformConfigError(
            "No target platform selected and the config file has no 'default_platform'"
        )
    if selected not in platforms and provider_path is None:
        available = ", ".join(sorted(platforms)) or "<none>"
        raise PlatformConfigError(
            f"Unknown platform '{selected}'. Available platforms: {available}"
        )

    config = merge_config(DEFAULT_PLATFORM_CONFIG, document.get("defaults") or {})
    config = merge_config(config, platforms.get(selected) or {})

    context = {
        "repo_root": str(repo_root) if repo_root is not None else None,
        "platform": selected,
        "config_path": str(Path(config_path).resolve()) if config_path else None,
        "base_config": copy.deepcopy(config),
        "env": dict(os.environ),
    }

    if provider_path is not None:
        provider_overrides = load_provider(provider_path)(selected, context)
        config = merge_config(config, provider_overrides)

    if overrides:
        config = merge_config(config, {k: v for k, v in overrides.items() if v is not None})

    config["platform"] = selected
    validate_config(config, selected)
    return config


def validate_config(config: dict[str, Any], platform: str) -> None:
    """Sanity-check a resolved configuration and raise on bad values."""
    for key in ("msbuild_platform", "configuration", "solution", "dist_dir"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PlatformConfigError(f"Platform '{platform}': '{key}' must be a non-empty string")

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
            f"Platform '{platform}': required artifacts not described in 'artifacts': "
            + ", ".join(missing)
        )


def list_platforms(config_path: str | os.PathLike[str] | None) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(name, resolved_config)`` pairs declared in the JSON file."""
    document = load_config_document(config_path)
    pairs: list[tuple[str, dict[str, Any]]] = []
    for name, platform_config in (document.get("platforms") or {}).items():
        merged = merge_config(DEFAULT_PLATFORM_CONFIG, document.get("defaults") or {})
        merged = merge_config(merged, platform_config or {})
        pairs.append((name, merged))
    return pairs

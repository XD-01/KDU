"""Service configuration loading and validation.

Configuration comes from a JSON file (``config/service.json`` by default) and
is then overlaid with environment variables. See README.md for the complete
reference.

Relative paths are resolved against the process working directory, which is why
the deployment documentation recommends starting the service from the
``kdu-build-server`` directory.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SettingsError(RuntimeError):
    """Raised when service configuration is missing or invalid."""


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8600,
        "api_token": "",
        "public_url": "",
        "max_request_bytes": 65536,
    },
    "storage": {
        "data_dir": "data",
        "keep_builds": 20,
    },
    "source": {
        "repo": "hfiref0x/KDU",
        "ref": "master",
        "github_token": "",
        "timeout_seconds": 180,
        "validate_files": ["Source/KDU.sln", "Source/Hamakaze/KDU.vcxproj"],
    },
    "build": {
        "msbuild": None,
        "timeout_seconds": 1800,
        "workers": 1,
        "disable_postbuild": False,
        "platforms_file": "config/platforms.json",
        "provider": None,
        "max_extra_properties": 32,
    },
    "callbacks": {
        "enabled": True,
        "timeout_seconds": 10,
    },
}


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    api_token: str
    public_url: str
    max_request_bytes: int


@dataclass(frozen=True)
class StorageSettings:
    data_dir: Path
    keep_builds: int


@dataclass(frozen=True)
class SourceSettings:
    repo: str
    ref: str
    github_token: str
    timeout_seconds: int
    validate_files: tuple[str, ...]


@dataclass(frozen=True)
class BuildSettings:
    msbuild: str | None
    timeout_seconds: int
    workers: int
    disable_postbuild: bool
    platforms_file: Path
    provider: Path | None
    max_extra_properties: int


@dataclass(frozen=True)
class CallbackSettings:
    enabled: bool
    timeout_seconds: int


@dataclass(frozen=True)
class ServiceSettings:
    server: ServerSettings
    storage: StorageSettings
    source: SourceSettings
    build: BuildSettings
    callbacks: CallbackSettings

    def describe(self) -> dict[str, Any]:
        """Return a JSON-serializable description with secrets redacted."""
        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "api_token": "<set>" if self.server.api_token else "",
                "public_url": self.server.public_url,
                "max_request_bytes": self.server.max_request_bytes,
            },
            "storage": {
                "data_dir": str(self.storage.data_dir),
                "keep_builds": self.storage.keep_builds,
            },
            "source": {
                "repo": self.source.repo,
                "ref": self.source.ref,
                "github_token": "<set>" if self.source.github_token else "",
                "timeout_seconds": self.source.timeout_seconds,
                "validate_files": list(self.source.validate_files),
            },
            "build": {
                "msbuild": self.build.msbuild,
                "timeout_seconds": self.build.timeout_seconds,
                "workers": self.build.workers,
                "disable_postbuild": self.build.disable_postbuild,
                "platforms_file": str(self.build.platforms_file),
                "provider": str(self.build.provider) if self.build.provider else None,
                "max_extra_properties": self.build.max_extra_properties,
            },
            "callbacks": {
                "enabled": self.callbacks.enabled,
                "timeout_seconds": self.callbacks.timeout_seconds,
            },
        }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _env_overlay() -> dict[str, Any]:
    """Read environment overrides. Empty variables are ignored."""
    mapping: dict[tuple[str, str], tuple[str, Any]] = {
        ("server", "host"): ("KDU_SERVICE_HOST", str),
        ("server", "port"): ("KDU_SERVICE_PORT", int),
        ("server", "api_token"): ("KDU_API_TOKEN", str),
        ("server", "public_url"): ("KDU_PUBLIC_URL", str),
        ("storage", "data_dir"): ("KDU_DATA_DIR", str),
        ("storage", "keep_builds"): ("KDU_KEEP_BUILDS", int),
        ("source", "repo"): ("KDU_SOURCE_REPO", str),
        ("source", "ref"): ("KDU_SOURCE_REF", str),
        ("source", "github_token"): ("GITHUB_TOKEN", str),
        ("build", "msbuild"): ("KDU_MSBUILD", str),
        ("build", "timeout_seconds"): ("KDU_BUILD_TIMEOUT", int),
        ("build", "workers"): ("KDU_BUILD_WORKERS", int),
        ("build", "platforms_file"): ("KDU_PLATFORMS_FILE", str),
        ("build", "provider"): ("KDU_PROVIDER", str),
    }
    overlay: dict[str, Any] = {}
    for (section, key), (variable, caster) in mapping.items():
        raw = os.environ.get(variable)
        if raw is None or raw == "":
            continue
        try:
            value: Any = caster(raw)
        except ValueError as exc:
            raise SettingsError(f"Environment variable {variable} must be a {caster.__name__}") from exc
        overlay.setdefault(section, {})[key] = value

    disable_postbuild = os.environ.get("KDU_DISABLE_POSTBUILD")
    if disable_postbuild not in (None, ""):
        overlay.setdefault("build", {})["disable_postbuild"] = disable_postbuild.lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    return overlay


def load_settings(config_path: str | os.PathLike[str] | None) -> ServiceSettings:
    raw = copy.deepcopy(DEFAULT_CONFIG)
    if config_path:
        path = Path(config_path)
        if not path.is_file():
            raise SettingsError(f"Service config file not found: {path}")
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                document = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SettingsError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise SettingsError(f"Service config {path} must contain a JSON object")
        raw = _deep_merge(raw, document)

    raw = _deep_merge(raw, _env_overlay())

    try:
        server = ServerSettings(
            host=str(raw["server"]["host"]),
            port=int(raw["server"]["port"]),
            api_token=str(raw["server"].get("api_token") or ""),
            public_url=str(raw["server"].get("public_url") or "").rstrip("/"),
            max_request_bytes=int(raw["server"]["max_request_bytes"]),
        )
        storage = StorageSettings(
            data_dir=Path(str(raw["storage"]["data_dir"])),
            keep_builds=int(raw["storage"]["keep_builds"]),
        )
        source = SourceSettings(
            repo=str(raw["source"]["repo"]),
            ref=str(raw["source"]["ref"]),
            github_token=str(raw["source"].get("github_token") or ""),
            timeout_seconds=int(raw["source"]["timeout_seconds"]),
            validate_files=tuple(str(item) for item in raw["source"]["validate_files"]),
        )
        build = BuildSettings(
            msbuild=str(raw["build"]["msbuild"]) if raw["build"].get("msbuild") else None,
            timeout_seconds=int(raw["build"]["timeout_seconds"]),
            workers=int(raw["build"]["workers"]),
            disable_postbuild=bool(raw["build"]["disable_postbuild"]),
            platforms_file=Path(str(raw["build"]["platforms_file"])),
            provider=Path(str(raw["build"]["provider"])) if raw["build"].get("provider") else None,
            max_extra_properties=int(raw["build"]["max_extra_properties"]),
        )
        callbacks = CallbackSettings(
            enabled=bool(raw["callbacks"]["enabled"]),
            timeout_seconds=int(raw["callbacks"]["timeout_seconds"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SettingsError(f"Invalid service configuration: {exc}") from exc

    settings = ServiceSettings(
        server=server,
        storage=storage,
        source=source,
        build=build,
        callbacks=callbacks,
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: ServiceSettings) -> None:
    if not (0 < settings.server.port < 65536):
        raise SettingsError("server.port must be between 1 and 65535")
    if settings.server.max_request_bytes < 1024:
        raise SettingsError("server.max_request_bytes must be at least 1024")
    if settings.server.public_url:
        parsed = urlsplit(settings.server.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SettingsError(
                "server.public_url must be an absolute http:// or https:// URL"
            )
    if settings.storage.keep_builds < 1:
        raise SettingsError("storage.keep_builds must be at least 1")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", settings.source.repo):
        raise SettingsError("source.repo must look like 'owner/repository'")
    if not settings.source.ref:
        raise SettingsError("source.ref must not be empty")
    if not settings.source.validate_files:
        raise SettingsError("source.validate_files must not be empty")
    if settings.source.timeout_seconds <= 0 or settings.build.timeout_seconds <= 0:
        raise SettingsError("timeouts must be positive")
    if settings.build.workers < 1:
        raise SettingsError("build.workers must be at least 1")
    if settings.build.max_extra_properties < 0:
        raise SettingsError("build.max_extra_properties must not be negative")
    if settings.build.provider is not None and not settings.build.provider.is_file():
        raise SettingsError(f"build.provider file not found: {settings.build.provider}")
    if not settings.build.platforms_file.is_file():
        raise SettingsError(f"build.platforms_file not found: {settings.build.platforms_file}")

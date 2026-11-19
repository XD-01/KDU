"""Validation and normalization of build requests sent over the HTTP API.

The request body is deliberately small: clients select a platform profile and
may override a handful of MSBuild settings. They cannot pass arbitrary command
line arguments, choose a provider, or affect where the service stores files.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

STRING_FIELDS = (
    "configuration",
    "msbuild_platform",
    "platform_toolset",
    "windows_sdk_version",
)
ALLOWED_FIELDS = {"platform", "extra_properties", "callback_url", *STRING_FIELDS}
PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_STRING_LENGTH = 256
MAX_CALLBACK_LENGTH = 2048


class BuildRequestError(ValueError):
    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


def parse_build_request(
    payload: Any,
    default_platform_name: str | None,
    max_extra_properties: int,
) -> dict[str, Any]:
    """Validate a request payload and return a normalized request.

    Returns ``{"platform", "overrides", "callback_url"}``.
    """
    if not isinstance(payload, dict):
        raise BuildRequestError("Request body must be a JSON object")

    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise BuildRequestError(
            "Unsupported request field(s): " + ", ".join(unknown),
            details={"allowed_fields": sorted(ALLOWED_FIELDS)},
        )

    platform = payload.get("platform") or default_platform_name
    if not isinstance(platform, str) or not platform.strip():
        raise BuildRequestError(
            "'platform' must be a non-empty string (the platforms file defines no default)"
        )
    _validate_string(platform, "platform")

    overrides: dict[str, Any] = {}
    for field in STRING_FIELDS:
        if field in payload:
            value = payload[field]
            if value is None:
                continue
            _validate_string(value, field)
            overrides[field] = value

    if "extra_properties" in payload:
        extra = payload["extra_properties"]
        if extra is None:
            extra = {}
        if not isinstance(extra, dict):
            raise BuildRequestError("'extra_properties' must be an object of string values")
        if len(extra) > max_extra_properties:
            raise BuildRequestError(
                f"'extra_properties' contains too many entries (max {max_extra_properties})"
            )
        normalized: dict[str, str] = {}
        for key, value in extra.items():
            if not isinstance(key, str) or not PROPERTY_NAME.fullmatch(key):
                raise BuildRequestError(
                    f"Invalid MSBuild property name {key!r}; expected [A-Za-z_][A-Za-z0-9_]*"
                )
            if not isinstance(value, str):
                raise BuildRequestError(f"Value of 'extra_properties.{key}' must be a string")
            if len(value) > MAX_STRING_LENGTH or any(ch in value for ch in "\r\n"):
                raise BuildRequestError(
                    f"Value of 'extra_properties.{key}' is too long or contains newlines"
                )
            normalized[key] = value
        overrides["extra_properties"] = normalized

    callback_url = payload.get("callback_url")
    if callback_url is not None:
        _validate_callback_url(callback_url)

    return {
        "platform": platform.strip(),
        "overrides": overrides,
        "callback_url": callback_url,
    }


def _validate_string(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise BuildRequestError(f"'{field}' must be a string")
    if not value.strip():
        raise BuildRequestError(f"'{field}' must not be empty")
    if len(value) > MAX_STRING_LENGTH:
        raise BuildRequestError(f"'{field}' must be at most {MAX_STRING_LENGTH} characters")
    if any(ch in value for ch in "\r\n"):
        raise BuildRequestError(f"'{field}' must not contain newlines")


def _validate_callback_url(value: Any) -> None:
    if not isinstance(value, str):
        raise BuildRequestError("'callback_url' must be a string")
    if len(value) > MAX_CALLBACK_LENGTH:
        raise BuildRequestError(
            f"'callback_url' must be at most {MAX_CALLBACK_LENGTH} characters"
        )
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BuildRequestError("'callback_url' must be an absolute http:// or https:// URL")

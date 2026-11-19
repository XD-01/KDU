"""HTTP API for the build service (Python standard library only)."""

from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import SERVICE_NAME, __version__
from .build_request import BuildRequestError
from .jobs import FAILED, QUEUED, RUNNING, SUCCEEDED
from .platform_config import PlatformConfigError
from .service import BuildService, build_service_info, serialize_job
from .settings import ServiceSettings
from .source import SourceError

JOB_PATH = re.compile(r"^/api/v1/builds/([0-9a-f]{32})(?:/(log|artifact))?$")
VALID_STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class BuildHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], service: BuildService, settings: ServiceSettings
    ) -> None:
        super().__init__(address, _handler_factory(service, settings))
        self.service = service
        self.settings = settings
        self.logger = service.logger


def _handler_factory(service: BuildService, settings: ServiceSettings):
    logger = logging.getLogger("kdu_build_service.http")

    class Handler(BaseHTTPRequestHandler):
        server_version = f"{SERVICE_NAME}/{__version__}"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ routing
        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._method_not_allowed("GET, POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._method_not_allowed("GET, POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _handle(self, method: str) -> None:
            started = datetime.now(timezone.utc)
            status = 500
            try:
                parsed = urlsplit(self.path)
                path = parsed.path or "/"
                query = parse_qs(parsed.query)
                if method == "GET" and path == "/healthz":
                    self._send_json(200, {"status": "ok", "time": _now()})
                    status = 200
                    return
                if not path.startswith("/api/v1/"):
                    raise ApiError(404, "not_found", f"No route for {path}")
                self._require_auth()
                if method == "GET":
                    status = self._route_get(path, query)
                else:
                    status = self._route_post(path, query)
            except ApiError as exc:
                status = exc.status
                self._send_error(exc.status, exc.code, exc.message, exc.details)
            except _ResponseSent:
                status = 401
                return
            except BuildRequestError as exc:
                status = 422
                self._send_error(422, "invalid_request", str(exc), exc.details)
            except PlatformConfigError as exc:
                status = 422
                self._send_error(422, "invalid_configuration", str(exc))
            except SourceError as exc:
                status = 502
                logger.warning("source error: %s", exc)
                self._send_error(502, "source_unavailable", str(exc))
            except Exception:  # noqa: BLE001
                logger.exception("unhandled API error")
                self._send_error(500, "internal_error", "Internal server error")
            finally:
                logger.info(
                    "%s %s -> %s (%.1f ms)",
                    method,
                    self.path,
                    status,
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000,
                )

        def _route_get(self, path: str, query: dict[str, list[str]]) -> int:
            if path == "/api/v1/info":
                self._send_json(200, build_service_info(service))
                return 200
            if path == "/api/v1/platforms":
                self._send_json(200, service.platforms())
                return 200
            if path == "/api/v1/source":
                self._send_json(200, service.source_status())
                return 200
            if path == "/api/v1/schema/build-request":
                self._send_json(200, build_request_schema(settings))
                return 200
            if path == "/api/v1/builds":
                limit = _int_query(query, "limit", 50, 1, 200)
                status = _str_query(query, "status")
                if status and status not in VALID_STATUSES:
                    raise ApiError(
                        422,
                        "invalid_query",
                        f"status must be one of: {', '.join(VALID_STATUSES)}",
                    )
                jobs = service.list_jobs(limit=limit, status=status)
                self._send_json(
                    200,
                    {
                        "jobs": [
                            serialize_job(job, settings.server.public_url)
                            for job in jobs
                        ]
                    },
                )
                return 200

            match = JOB_PATH.match(path)
            if not match:
                raise ApiError(404, "not_found", f"No route for {path}")
            job_id, suffix = match.group(1), match.group(2)
            job = service.get_job(job_id)
            if job is None:
                raise ApiError(404, "job_not_found", f"Job {job_id} does not exist")

            if suffix is None:
                self._send_json(200, serialize_job(job, settings.server.public_url))
                return 200
            if suffix == "log":
                log_path = job.get("log_path")
                if not log_path or not Path(log_path).is_file():
                    raise ApiError(404, "log_not_found", "No log is available yet")
                self._send_file(Path(log_path), "text/plain; charset=utf-8", None)
                return 200
            artifact_path = job.get("artifact_path")
            if job["status"] != SUCCEEDED or not artifact_path or not Path(artifact_path).is_file():
                raise ApiError(404, "artifact_not_found", "No artifact is available yet")
            result = job.get("result") or {}
            requested = _str_query(query, "file")
            if requested:
                available = {item["name"] for item in result.get("artifacts", [])}
                if requested not in available:
                    raise ApiError(
                        422,
                        "invalid_artifact",
                        f"Unknown artifact {requested!r}",
                        details={"available": sorted(available)},
                    )
                file_path = service.builds_dir / job_id / "artifacts" / requested
                if not file_path.is_file():
                    raise ApiError(404, "artifact_not_found", "The artifact file is missing")
                self._send_file(file_path, "application/octet-stream", requested)
                return 200
            filename = result.get("artifact_file") or Path(artifact_path).name
            self._send_file(Path(artifact_path), "application/zip", Path(filename).name)
            return 200

        def _route_post(self, path: str, query: dict[str, list[str]]) -> int:
            if path == "/api/v1/builds":
                payload = self._read_json()
                job = service.submit(payload)
                body = serialize_job(job, settings.server.public_url)
                location = body["links"]["self"]
                self._send_json(202, body, extra_headers={"Location": location})
                return 202
            if path == "/api/v1/source/sync":
                force = _str_query(query, "force") in ("1", "true", "yes")
                result = service.sync_source(force=force)
                self._send_json(200, result)
                return 200
            raise ApiError(404, "not_found", f"No route for {path}")

        # -------------------------------------------------------------- helpers
        def _require_auth(self) -> None:
            token = settings.server.api_token
            if not token:
                return
            header = self.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                provided = header[7:].strip()
            else:
                provided = self.headers.get("X-API-Key", "").strip()
            if not provided or not hmac.compare_digest(provided, token):
                self._send_error(
                    401,
                    "unauthorized",
                    "A valid bearer token is required",
                    extra_headers={"WWW-Authenticate": "Bearer"},
                )
                raise _ResponseSent()

        def _read_json(self) -> Any:
            content_type = self.headers.get("Content-Type", "")
            if content_type and not content_type.lower().startswith("application/json"):
                raise ApiError(415, "unsupported_media_type", "Content-Type must be application/json")
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                raise ApiError(411, "length_required", "Content-Length is required")
            try:
                length = int(length_header)
            except ValueError as exc:
                raise ApiError(400, "invalid_request", "Content-Length must be an integer") from exc
            if length <= 0:
                raise ApiError(400, "invalid_request", "Request body must not be empty")
            if length > settings.server.max_request_bytes:
                raise ApiError(
                    413,
                    "payload_too_large",
                    f"Request body exceeds {settings.server.max_request_bytes} bytes",
                )
            body = self.rfile.read(length)
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, "invalid_json", f"Request body is not valid JSON: {exc}") from exc

        def _send_json(
            self,
            status: int,
            payload: Any,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", data, extra_headers)

        def _send_error(
            self,
            status: int,
            code: str,
            message: str,
            details: Any = None,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            error: dict[str, Any] = {"code": code, "message": message}
            if details is not None:
                error["details"] = details
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps({"error": error}, indent=2, sort_keys=True).encode("utf-8"),
                extra_headers,
            )

        def _send_file(
            self, path: Path, content_type: str, download_name: str | None
        ) -> None:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ApiError(404, "not_found", f"File is no longer available: {exc}") from exc
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            if download_name:
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{download_name}"'
                )
            self.end_headers()
            self.close_connection = True
            try:
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 256)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            data: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _method_not_allowed(self, allowed: str) -> None:
            self._send_error(
                405,
                "method_not_allowed",
                "Method not allowed",
                extra_headers={"Allow": allowed},
            )

    return Handler


class _ResponseSent(Exception):
    """Internal marker used after a response has already been written."""


def build_request_schema(settings: ServiceSettings) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "KDU build request",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "platform": {
                "type": "string",
                "maxLength": 256,
                "description": "Platform profile name; defaults to the platforms file default.",
            },
            "configuration": {
                "type": "string",
                "maxLength": 256,
                "description": "Overrides MSBuild Configuration.",
            },
            "msbuild_platform": {
                "type": "string",
                "maxLength": 256,
                "description": "Overrides MSBuild Platform.",
            },
            "platform_toolset": {
                "type": "string",
                "maxLength": 256,
                "description": "Overrides MSBuild PlatformToolset.",
            },
            "windows_sdk_version": {
                "type": "string",
                "maxLength": 256,
                "description": "Overrides WindowsTargetPlatformVersion.",
            },
            "extra_properties": {
                "type": "object",
                "maxProperties": settings.build.max_extra_properties,
                "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                "additionalProperties": {"type": "string", "maxLength": 256},
                "description": "Additional MSBuild properties (KEY=VALUE).",
            },
            "callback_url": {
                "type": "string",
                "format": "uri",
                "maxLength": 2048,
                "description": "Optional absolute http(s) URL notified when the job finishes.",
            },
        },
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _str_query(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _int_query(
    query: dict[str, list[str]], key: str, default: int, minimum: int, maximum: int
) -> int:
    raw = _str_query(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(422, "invalid_query", f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ApiError(
            422, "invalid_query", f"{key} must be between {minimum} and {maximum}"
        )
    return value

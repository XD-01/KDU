"""Command line entry point.

    python -m kdu_build_server serve
    python -m kdu_build_server sync-source --force
    python -m kdu_build_server check
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import SERVICE_NAME, __version__
from .api import BuildHttpServer
from .logging_setup import setup_logging
from .platform_config import PlatformConfigError
from .service import BuildService
from .settings import SettingsError, load_settings
from .source import SourceError, SourceManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kdu-build-service",
        description="Long-running KDU build service: downloads the latest source from GitHub, "
        "builds it for a requested platform and serves kdu.exe over HTTP.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("KDU_SERVICE_CONFIG", "config/service.json"),
        help="Path to the service configuration file (default: config/service.json).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Console/file log level.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run the HTTP service (default).")
    sync_parser = subparsers.add_parser(
        "sync-source", help="Check GitHub and download the latest source, then exit."
    )
    sync_parser.add_argument(
        "--force", action="store_true", help="Re-download even if the revision is unchanged."
    )
    subparsers.add_parser("check", help="Validate the configuration and print the effective settings.")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    config_path: Path | None
    candidate = Path(args.config)
    if candidate.is_file():
        config_path = candidate
    elif args.config != "config/service.json":
        print(f"error: config file not found: {candidate}", file=sys.stderr)
        return 2
    else:
        config_path = None

    try:
        settings = load_settings(config_path)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(
        Path(settings.storage.data_dir), getattr(logging, args.log_level.upper())
    )

    if command == "check":
        return _check(settings)
    if command == "sync-source":
        return _sync_source(settings, args.force, logger)
    return _serve(settings, logger)


def _check(settings) -> int:
    from .platform_config import platform_summaries, resolve_platform_config

    try:
        platforms = platform_summaries(settings.build.platforms_file)
    except PlatformConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    resolved = []
    errors = []
    for entry in platforms:
        try:
            config = resolve_platform_config(
                platform=entry["name"],
                platforms_file=settings.build.platforms_file,
                provider=settings.build.provider,
            )
            resolved.append(
                {
                    "name": entry["name"],
                    "configuration": config["configuration"],
                    "msbuild_platform": config["msbuild_platform"],
                    "platform_toolset": config.get("platform_toolset"),
                    "artifacts": sorted(config["artifacts"]),
                    "required_artifacts": config["required_artifacts"],
                }
            )
        except (PlatformConfigError, ValueError) as exc:
            errors.append({"platform": entry["name"], "error": str(exc)})

    output = {
        "settings": settings.describe(),
        "platforms": platforms,
        "resolved_platforms": resolved,
        "errors": errors,
    }
    source = SourceManager(settings.source, Path(settings.storage.data_dir), logging.getLogger("check"))
    output["source_current"] = source.current()
    print(json.dumps(output, indent=2, sort_keys=True))
    return 2 if errors else 0


def _sync_source(settings, force: bool, logger) -> int:
    manager = SourceManager(settings.source, Path(settings.storage.data_dir), logger)
    try:
        state, changed = manager.ensure_latest(force=force, allow_stale=False)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"changed": changed, "source": state}, indent=2, sort_keys=True))
    return 0


def _serve(settings, logger) -> int:
    service: BuildService | None = None
    try:
        service = BuildService(settings, logger)
        service.start()
        httpd = BuildHttpServer(
            (settings.server.host, settings.server.port), service, settings
        )
    except Exception:
        logger.exception("failed to start the service")
        if service is not None:
            service.stop()
        return 4

    stop_event = threading.Event()
    assert service is not None

    def handle_signal(signum, frame) -> None:
        logger.info("received signal %s, shutting down", signum)
        service.request_stop()
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, signal_name, None)
        if candidate is not None:
            try:
                signal.signal(candidate, handle_signal)
            except (ValueError, OSError):
                pass

    server_thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, name="http-server", daemon=True
    )
    server_thread.start()
    logger.info(
        "%s %s listening on http://%s:%s (platforms: %s)",
        SERVICE_NAME,
        __version__,
        settings.server.host,
        settings.server.port,
        settings.build.platforms_file,
    )
    if not settings.server.api_token:
        logger.warning("server.api_token is empty; the API is unauthenticated")

    stop_event.wait()
    logger.info("stopping HTTP server")
    httpd.shutdown()
    httpd.server_close()
    service.stop()
    logger.info("service stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

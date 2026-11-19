#!/usr/bin/env python3
"""Build KDU for a target platform and collect ``kdu.exe``.

The script resolves platform settings through ``build/platform_config.py``
(baseline defaults -> ``build/platforms.json`` -> optional ``--provider`` ->
command line overrides), locates MSBuild, builds ``Source/KDU.sln`` and copies
the produced binaries into a per-platform ``dist/`` directory.

Run ``python build/build_kdu.py --help`` for all options. See README.md,
section "Automated build", for the platform configuration contract.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from platform_config import (
    PlatformConfigError,
    list_platforms,
    resolve_platform_config,
)

EXIT_CONFIG_ERROR = 2
EXIT_TOOLCHAIN_ERROR = 3
EXIT_BUILD_ERROR = 4
EXIT_ARTIFACT_ERROR = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile KDU for a target platform and produce kdu.exe.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--platform",
        help="Target platform name defined in the config file (defaults to 'default_platform').",
    )
    parser.add_argument(
        "--config",
        default=str(SCRIPT_DIR / "platforms.json"),
        help="Path to the JSON platform configuration.",
    )
    parser.add_argument(
        "--provider",
        help="Path to a Python provider module that overrides/extends the platform config.",
    )
    parser.add_argument("--configuration", help="Override MSBuild configuration (e.g. Release).")
    parser.add_argument("--msbuild-platform", help="Override MSBuild platform (e.g. x64).")
    parser.add_argument("--toolset", help="Override MSBuild PlatformToolset (e.g. v145).")
    parser.add_argument("--sdk", help="Override WindowsTargetPlatformVersion.")
    parser.add_argument("--dist-dir", help="Override the artifact output directory.")
    parser.add_argument("--msbuild", help="Explicit path to MSBuild.exe.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="MSBuild timeout in seconds (0 disables the timeout).",
    )
    parser.add_argument("--clean", action="store_true", help="Rebuild instead of incremental build.")
    parser.add_argument(
        "--no-postbuild",
        action="store_true",
        help="Disable PostBuildEvent (skips GenAsIo2Unlock; AsIO2 provider will not be usable).",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved platform configuration before building.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved configuration and MSBuild command without executing it.",
    )
    parser.add_argument(
        "--list-platforms",
        action="store_true",
        help="List platforms declared in the config file and exit.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print extra diagnostics.")
    return parser


def find_msbuild(explicit: str | None) -> str | None:
    """Locate MSBuild without failing when only a dry run is requested."""
    if explicit:
        return explicit

    for variable in ("MSBUILD_EXE_PATH", "MSBUILD"):
        value = os.environ.get(variable)
        if value:
            return value

    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        try:
            result = subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.Component.MSBuild",
                    "-find",
                    "MSBuild\\**\\Bin\\MSBuild.exe",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if candidates:
                return candidates[0]

    for name in ("msbuild", "MSBuild.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def format_values(config: dict[str, Any]) -> dict[str, str]:
    return {
        "platform": config["platform"],
        "msbuild_platform": config["msbuild_platform"],
        "configuration": config["configuration"],
    }


def expand_path(template: str, values: dict[str, str], field: str) -> Path:
    try:
        expanded = template.format(**values)
    except KeyError as exc:
        raise PlatformConfigError(
            f"{field}: unknown placeholder {exc.args[0]!r} in {template!r}"
        ) from exc
    path = Path(expanded)
    return path if path.is_absolute() else REPO_ROOT / path


def build_command(
    msbuild: str, config: dict[str, Any], no_postbuild: bool, clean: bool
) -> list[str]:
    command = [
        msbuild,
        str(expand_path(config["solution"], format_values(config), "solution")),
        "/m",
        "/nologo",
        "/verbosity:minimal",
        f"/p:Configuration={config['configuration']}",
        f"/p:Platform={config['msbuild_platform']}",
    ]
    if config.get("platform_toolset"):
        command.append(f"/p:PlatformToolset={config['platform_toolset']}")
    if config.get("windows_sdk_version"):
        command.append(f"/p:WindowsTargetPlatformVersion={config['windows_sdk_version']}")
    if no_postbuild:
        command.append("/p:PostBuildEventUseInBuild=false")

    for key in sorted(config.get("extra_properties") or {}):
        command.append(f"/p:{key}={config['extra_properties'][key]}")

    if clean:
        command.append("/t:Rebuild")
    return command


def collect_artifacts(config: dict[str, Any], dist_dir: Path) -> int:
    values = format_values(config)
    required = set(config.get("required_artifacts") or [])
    missing: list[str] = []
    copied: list[Path] = []

    for name, template in config["artifacts"].items():
        source = expand_path(template, values, f"artifacts.{name}")
        if not source.is_file():
            if name in required:
                missing.append(f"{name} ({source})")
            else:
                print(f"[build] warning: optional artifact not found: {source}")
            continue

        dist_dir.mkdir(parents=True, exist_ok=True)
        destination = dist_dir / name
        shutil.copy2(source, destination)
        copied.append(destination)
        print(f"[build] collected {name} -> {destination}")

    if missing:
        print(
            "[build] error: required artifacts were not produced: " + ", ".join(missing),
            file=sys.stderr,
        )
        return EXIT_ARTIFACT_ERROR

    print(f"[build] artifacts ready in {dist_dir} ({len(copied)} file(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.timeout < 0:
        print("[build] error: --timeout must be >= 0", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.list_platforms:
        try:
            platforms = list_platforms(args.config)
        except PlatformConfigError as exc:
            print(f"[build] error: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        if not platforms:
            print("[build] no platforms declared in the config file")
            return 0
        width = max(len(name) for name, _ in platforms)
        for name, config in platforms:
            description = config.get("description") or ""
            print(
                f"{name:<{width}}  {config['msbuild_platform']}/{config['configuration']:<8}  {description}"
            )
        print("[build] note: providers may declare additional platform names")
        return 0

    overrides = {
        "configuration": args.configuration,
        "msbuild_platform": args.msbuild_platform,
        "platform_toolset": args.toolset,
        "windows_sdk_version": args.sdk,
        "dist_dir": args.dist_dir,
    }

    try:
        config = resolve_platform_config(
            platform=args.platform,
            config_path=args.config,
            provider_path=args.provider,
            overrides=overrides,
            repo_root=REPO_ROOT,
        )
    except PlatformConfigError as exc:
        print(f"[build] error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.show_config:
        print(json.dumps(config, indent=2, sort_keys=True))

    if args.msbuild and not Path(args.msbuild).is_file():
        print(f"[build] error: MSBuild not found: {args.msbuild}", file=sys.stderr)
        return EXIT_TOOLCHAIN_ERROR

    msbuild = find_msbuild(args.msbuild)
    if msbuild is None:
        if not args.dry_run:
            print(
                "[build] error: MSBuild not found. Install Visual Studio (with the C++ "
                "workload), run from a Developer Command Prompt, or pass --msbuild.",
                file=sys.stderr,
            )
            return EXIT_TOOLCHAIN_ERROR
        msbuild = "<msbuild>"

    command = build_command(msbuild, config, args.no_postbuild, args.clean)
    display = shlex.join(command)
    print(
        f"[build] platform={config['platform']} "
        f"configuration={config['configuration']} msbuild_platform={config['msbuild_platform']}"
    )
    if args.verbose:
        print(f"[build] repository root: {REPO_ROOT}")
        print(f"[build] command: {display}")

    if args.dry_run:
        print(f"[build] dry-run command: {display}")
        values = format_values(config)
        dist_dir = expand_path(config["dist_dir"], values, "dist_dir")
        print(f"[build] dry-run artifacts would be copied to: {dist_dir}")
        return 0

    print(f"[build] running: {display}")
    try:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            check=False,
            timeout=args.timeout if args.timeout > 0 else None,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[build] error: MSBuild timed out after {args.timeout} seconds",
            file=sys.stderr,
        )
        return EXIT_BUILD_ERROR
    except OSError as exc:
        print(f"[build] error: failed to start MSBuild: {exc}", file=sys.stderr)
        return EXIT_TOOLCHAIN_ERROR

    if completed.returncode != 0:
        print(f"[build] error: MSBuild exited with code {completed.returncode}", file=sys.stderr)
        return EXIT_BUILD_ERROR

    try:
        dist_dir = expand_path(config["dist_dir"], format_values(config), "dist_dir")
    except PlatformConfigError as exc:
        print(f"[build] error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    return collect_artifacts(config, dist_dir)


if __name__ == "__main__":
    raise SystemExit(main())

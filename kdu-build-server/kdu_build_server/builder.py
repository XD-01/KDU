"""MSBuild execution and artifact collection."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .platform_config import PlatformConfigError


class BuildError(RuntimeError):
    """Raised when MSBuild fails, times out, or artifacts are missing."""


@dataclass
class BuildOutcome:
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    artifact_dir: Path | None = None
    archive: Path | None = None
    log_path: Path | None = None
    duration_seconds: float = 0.0
    command: list[str] = field(default_factory=list)

    def to_result(self, platform: str, source_sha: str) -> dict[str, Any]:
        return {
            "platform": platform,
            "source_sha": source_sha,
            "artifacts": self.artifacts,
            "artifact_file": self.archive.name if self.archive else None,
            "log_file": self.log_path.name if self.log_path else None,
            "duration_seconds": round(self.duration_seconds, 2),
        }


def find_msbuild(explicit: str | None) -> str | None:
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
            completed = subprocess.run(
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
            completed = None
        if completed is not None and completed.returncode == 0:
            candidates = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if candidates:
                return candidates[0]

    for name in ("msbuild", "MSBuild.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_command(
    msbuild: str,
    solution: Path,
    config: dict[str, Any],
    disable_postbuild: bool,
) -> list[str]:
    command = [
        msbuild,
        str(solution),
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
    if disable_postbuild:
        command.append("/p:PostBuildEventUseInBuild=false")
    for key in sorted(config.get("extra_properties") or {}):
        command.append(f"/p:{key}={config['extra_properties'][key]}")
    return command


def run_build(
    source_root: Path,
    config: dict[str, Any],
    job_dir: Path,
    msbuild: str,
    timeout_seconds: int,
    disable_postbuild: bool,
    source_sha: str,
    logger,
) -> BuildOutcome:
    values = {
        "platform": config["platform"],
        "msbuild_platform": config["msbuild_platform"],
        "configuration": config["configuration"],
    }

    solution = _expand_relative(source_root, config["solution"], values, "solution")
    if not solution.is_file():
        raise BuildError(f"solution file not found in downloaded source: {solution}")

    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "build.log"
    artifact_dir = job_dir / "artifacts"
    command = build_command(msbuild, solution, config, disable_postbuild)

    logger.info("starting MSBuild: %s", shlex.join(command))
    started = time.monotonic()
    with log_path.open("wb") as log_handle:
        log_handle.write(("COMMAND: " + shlex.join(command) + "\n\n").encode("utf-8"))
        log_handle.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(source_root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise BuildError(f"failed to start MSBuild: {exc}") from exc

        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate(process)
            log_handle.write(b"\nBUILD TIMED OUT\n")
            log_handle.flush()
            raise BuildError(f"MSBuild timed out after {timeout_seconds} seconds")

    duration = time.monotonic() - started
    if return_code != 0:
        tail = _tail(log_path)
        raise BuildError(
            f"MSBuild exited with code {return_code}"
            + (f"; last output: {tail}" if tail else "")
        )

    artifacts = _collect_artifacts(source_root, config, artifact_dir, values, logger)
    archive = _archive_artifacts(
        job_dir,
        artifacts,
        config["platform"],
        source_sha[:8],
        job_dir.name,
    )
    logger.info("build finished in %.1fs, artifacts: %s", duration, ", ".join(artifacts))
    return BuildOutcome(
        artifacts=[
            {"name": name, "size": (artifact_dir / name).stat().st_size}
            for name in artifacts
        ],
        artifact_dir=artifact_dir,
        archive=archive,
        log_path=log_path,
        duration_seconds=duration,
        command=command,
    )


def _collect_artifacts(
    source_root: Path,
    config: dict[str, Any],
    artifact_dir: Path,
    values: dict[str, str],
    logger,
) -> list[str]:
    required = set(config.get("required_artifacts") or [])
    missing: list[str] = []
    collected: list[str] = []

    for name, template in config["artifacts"].items():
        source = _expand_relative(source_root, str(template), values, f"artifacts.{name}")
        if not source.is_file():
            if name in required:
                missing.append(f"{name} ({source})")
            else:
                logger.warning("optional artifact not produced: %s", source)
            continue
        artifact_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, artifact_dir / name)
        collected.append(name)

    if missing:
        raise BuildError("required artifacts were not produced: " + ", ".join(missing))
    if not collected:
        raise BuildError("build produced no artifacts")
    return collected


def _archive_artifacts(
    job_dir: Path,
    artifacts: list[str],
    platform: str,
    sha_prefix: str,
    job_prefix: str,
) -> Path:
    archive = job_dir / f"kdu-{platform}-{sha_prefix or 'unknown'}-{job_prefix[:8]}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in artifacts:
            bundle.write(job_dir / "artifacts" / name, arcname=name)
    return archive


def _expand_relative(
    root: Path, template: str, values: dict[str, str], field: str
) -> Path:
    try:
        expanded = template.format(**values)
    except KeyError as exc:
        raise PlatformConfigError(
            f"{field}: unknown placeholder {exc.args[0]!r} in {template!r}"
        ) from exc
    path = Path(expanded)
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PlatformConfigError(f"{field}: path escapes the source tree: {template}")
    return candidate


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _tail(path: Path, limit: int = 1500) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-5:])

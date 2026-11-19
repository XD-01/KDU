"""Build orchestration: queue, workers, source updates and job lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import shutil
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import SERVICE_NAME, __version__
from .build_request import BuildRequestError, parse_build_request
from .builder import BuildError, find_msbuild, run_build
from .jobs import FAILED, QUEUED, SUCCEEDED, JobStore
from .platform_config import (
    PlatformConfigError,
    default_platform,
    platform_summaries,
    resolve_platform_config,
)
from .settings import ServiceSettings
from .source import SourceError, SourceManager


class BuildService:
    def __init__(self, settings: ServiceSettings, logger) -> None:
        self.settings = settings
        self.logger = logger
        self.data_dir = Path(settings.storage.data_dir).resolve()
        self.builds_dir = self.data_dir / "builds"
        self.builds_dir.mkdir(parents=True, exist_ok=True)
        self._default_platform = default_platform(settings.build.platforms_file)

        self.store = JobStore(self.data_dir / "jobs.sqlite3")
        self.source_manager = SourceManager(settings.source, self.data_dir, logger)
        self.queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._msbuild: str | None = None

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        interrupted = self.store.recover_interrupted()
        if interrupted:
            self.logger.warning("marked %d interrupted job(s) as failed", interrupted)

        self._msbuild = find_msbuild(self.settings.build.msbuild)
        if self._msbuild is None:
            self.logger.warning(
                "MSBuild was not found; builds will fail until Visual Studio is "
                "installed or build.msbuild is configured"
            )
        else:
            self.logger.info("using MSBuild: %s", self._msbuild)

        for job_id in self.store.queued_ids():
            self.queue.put(job_id)

        for index in range(self.settings.build.workers):
            worker = threading.Thread(
                target=self._worker,
                name=f"build-worker-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

        threading.Thread(target=self._startup_source_sync, name="source-sync", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            self.queue.put(None)
        for worker in self._workers:
            worker.join(timeout=10)
        self.store.close()

    # ------------------------------------------------------------------- API
    def platforms(self) -> dict[str, Any]:
        return {
            "default_platform": self._default_platform,
            "platforms": platform_summaries(self.settings.build.platforms_file),
        }

    def source_status(self) -> dict[str, Any]:
        current = self.source_manager.current()
        if current:
            current = {key: value for key, value in current.items() if key != "path"}
        return {
            "repo": self.settings.source.repo,
            "ref": self.settings.source.ref,
            "current": current,
        }

    def sync_source(self, force: bool = False) -> dict[str, Any]:
        state, changed = self.source_manager.ensure_latest(
            force=force, allow_stale=False
        )
        return {"changed": changed, "source": state}

    def submit(self, payload: Any) -> dict[str, Any]:
        try:
            request = parse_build_request(
                payload,
                default_platform_name=self._default_platform,
                max_extra_properties=self.settings.build.max_extra_properties,
            )
        except BuildRequestError:
            raise

        if request["callback_url"] and not self.settings.callbacks.enabled:
            raise BuildRequestError("Callbacks are disabled on this service")

        try:
            resolve_platform_config(
                platform=request["platform"],
                platforms_file=self.settings.build.platforms_file,
                provider=self.settings.build.provider,
                overrides=request["overrides"],
            )
        except PlatformConfigError as exc:
            raise BuildRequestError(str(exc)) from exc

        job = self.store.create(request, request["platform"])
        self.queue.put(job["id"])
        self.logger.info(
            "accepted job %s (platform=%s)", job["id"], request["platform"]
        )
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get(job_id)

    def list_jobs(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        return self.store.list(limit=limit, status=status)

    # ---------------------------------------------------------------- workers
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                return
            try:
                self._process(job_id)
            except Exception:  # noqa: BLE001 - never let a worker die
                self.logger.exception("unhandled error while processing job %s", job_id)

    def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job["status"] != QUEUED:
            return
        if not self.store.mark_running(job_id):
            return

        job = self.store.get(job_id) or job
        request = job["request"]
        job_dir = self.builds_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        with (job_dir / "request.json").open("w", encoding="utf-8") as handle:
            json.dump(request, handle, indent=2, sort_keys=True)
            handle.write("\n")

        log_path = job_dir / "build.log"
        self.logger.info(
            "job %s started: platform=%s overrides=%s",
            job_id,
            request["platform"],
            request["overrides"],
        )
        try:
            config = resolve_platform_config(
                platform=request["platform"],
                platforms_file=self.settings.build.platforms_file,
                provider=self.settings.build.provider,
                overrides=request["overrides"],
            )
            source_info, changed = self.source_manager.prepare_build_source(
                job_dir / "source"
            )
            source_sha = str(source_info["sha"])
            if changed:
                self.logger.info("job %s uses new source revision %s", job_id, source_sha)
            else:
                self.logger.info("job %s uses source revision %s", job_id, source_sha)

            if not self._msbuild:
                raise BuildError(
                    "MSBuild not found. Install Visual Studio with the C++ workload, "
                    "run from a Developer Command Prompt, or set build.msbuild."
                )

            outcome = run_build(
                source_root=job_dir / "source",
                config=config,
                job_dir=job_dir,
                msbuild=self._msbuild,
                timeout_seconds=self.settings.build.timeout_seconds,
                disable_postbuild=self.settings.build.disable_postbuild,
                source_sha=source_sha,
                logger=self.logger,
            )
            result = outcome.to_result(config["platform"], source_sha)
            result["configuration"] = config["configuration"]
            result["msbuild_platform"] = config["msbuild_platform"]
            result["platform_toolset"] = config.get("platform_toolset")
            result["source_ref"] = self.settings.source.ref
            assert outcome.log_path is not None
            assert outcome.archive is not None
            self.store.mark_succeeded(
                job_id,
                result,
                source_sha,
                outcome.log_path,
                outcome.archive,
            )
            self.logger.info(
                "job %s succeeded (%s)", job_id, ", ".join(item["name"] for item in result["artifacts"])
            )
            self._notify(job_id)
        except Exception as exc:  # noqa: BLE001 - record all failures on the job
            self.logger.exception("job %s failed", job_id)
            self.store.mark_failed(job_id, str(exc), log_path=log_path)
            self._notify(job_id)
        finally:
            self._prune()

    def _prune(self) -> None:
        try:
            removed = self.store.prune(self.settings.storage.keep_builds)
        except Exception:  # noqa: BLE001
            self.logger.exception("failed to prune old jobs")
            return
        for job_id in removed:
            shutil.rmtree(self.builds_dir / job_id, ignore_errors=True)

    # -------------------------------------------------------------- callbacks
    def _notify(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        callback_url = (job.get("request") or {}).get("callback_url")
        if not callback_url or not self.settings.callbacks.enabled:
            return

        payload = serialize_job(job, self.settings.server.public_url)
        payload["event"] = "build.finished"
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"{SERVICE_NAME}/{__version__}",
        }
        if self.settings.server.api_token:
            signature = hmac.new(
                self.settings.server.api_token.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            headers["X-KDU-Signature"] = f"sha256={signature}"

        request = Request(callback_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.settings.callbacks.timeout_seconds) as response:
                self.logger.info(
                    "callback for job %s delivered (HTTP %s)", job_id, response.status
                )
        except (HTTPError, URLError, OSError) as exc:
            self.logger.warning("callback for job %s failed: %s", job_id, exc)

    # ------------------------------------------------------------------ misc
    def _startup_source_sync(self) -> None:
        try:
            state, changed = self.source_manager.ensure_latest(
                force=False, allow_stale=True
            )
            self.logger.info(
                "startup source check complete: revision %s%s",
                state.get("sha"),
                " (updated)" if changed else "",
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("startup source check failed")


def serialize_job(job: dict[str, Any], public_url: str = "") -> dict[str, Any]:
    """Convert an internal job row into the public API representation."""
    base = public_url.rstrip("/")
    job_id = job["id"]
    links = {"self": f"{base}/api/v1/builds/{job_id}"}
    if job.get("log_path") and Path(job["log_path"]).is_file():
        links["log"] = f"{base}/api/v1/builds/{job_id}/log"
    if job.get("status") == SUCCEEDED and job.get("artifact_path"):
        links["artifact"] = f"{base}/api/v1/builds/{job_id}/artifact"

    payload: dict[str, Any] = {
        "id": job_id,
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "platform": job["platform"],
        "source_sha": job["source_sha"],
        "request": job.get("request") or {},
        "result": job.get("result"),
        "error": job["error"] if job["status"] == FAILED else None,
        "links": links,
    }
    return payload


def build_service_info(service: BuildService) -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "version": __version__,
        "source": service.source_status(),
        "platforms_file": str(service.settings.build.platforms_file),
    }

"""Download and update the KDU source tree from GitHub.

Update strategy (see README, "Source updates"):

1. Ask GitHub for the current commit SHA of ``source.ref``. Conditional
   requests (ETag) are used so a long-running service does not exhaust the
   unauthenticated rate limit. A rate-limited response falls back to the public
   commit Atom feed and finally to ``git ls-remote``.
2. If the SHA matches the stored one, nothing is downloaded.
3. Otherwise the archive for that SHA is downloaded to a temporary file,
   extracted and validated, then atomically moved into
   ``<data>/source/releases/<sha>``. The state file is replaced only after a
   successful extraction.
4. All other release directories and temporary files are removed, so exactly
   one source version is kept on disk.

Only ``prepare_build_source`` is used by build workers: it performs the update
check and copies the current tree into the job directory under one lock, so a
concurrent update can never delete a tree that a build is still reading.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .settings import SourceSettings

STATE_FILE = "state.json"
LOCK_FILE = "update.lock"
IGNORED_COPY_NAMES = ("output", ".vs", "__pycache__")
MAX_ARCHIVE_ENTRIES = 20000
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024


class SourceError(RuntimeError):
    """Raised when the source tree cannot be checked out or updated."""


class FileLock:
    """Small cross-platform lock file with stale lock detection."""

    def __init__(self, path: Path, timeout: float = 300.0, stale_after: float = 3600.0) -> None:
        self._path = path
        self._timeout = timeout
        self._stale_after = stale_after
        self._held = False

    def __enter__(self) -> FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                descriptor = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
            except FileExistsError:
                try:
                    age = time.time() - self._path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self._stale_after:
                    try:
                        self._path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise SourceError(f"Timed out waiting for source lock {self._path}")
                time.sleep(0.2)
                continue
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            finally:
                os.close(descriptor)
            self._held = True
            return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._held:
            try:
                self._path.unlink()
            except OSError:
                pass
            self._held = False


class SourceManager:
    def __init__(self, settings: SourceSettings, data_dir: Path, logger) -> None:
        self._settings = settings
        self._root = data_dir / "source"
        self._releases = self._root / "releases"
        self._state_file = self._root / STATE_FILE
        self._lock = FileLock(self._root / LOCK_FILE)
        self._logger = logger
        self._api_blocked_until = 0.0

    # ------------------------------------------------------------------ paths
    @property
    def releases_dir(self) -> Path:
        return self._releases

    def release_path(self, sha: str) -> Path:
        return self._releases / sha

    # ------------------------------------------------------------------ state
    def read_state(self) -> dict[str, Any] | None:
        if not self._state_file.is_file():
            return None
        try:
            with self._state_file.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning("ignoring unreadable source state: %s", exc)
            return None
        if not isinstance(state, dict):
            return None
        if state.get("repo") != self._settings.repo or state.get("ref") != self._settings.ref:
            # The service was reconfigured: never reuse a tree from another repo/ref.
            return None
        sha = state.get("sha")
        if not isinstance(sha, str) or not self.release_path(sha).is_dir():
            return None
        return state

    def current(self) -> dict[str, Any] | None:
        state = self.read_state()
        if state is None:
            return None
        result = dict(state)
        result["path"] = str(self.release_path(str(state["sha"])))
        return result

    def _write_state(self, state: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._state_file)

    # ------------------------------------------------------------------ update
    def ensure_latest(self, force: bool = False, allow_stale: bool = False) -> tuple[dict[str, Any], bool]:
        """Check for a new revision and download it if needed.

        Returns ``(state, changed)``. With ``allow_stale`` the previous source
        is used when GitHub cannot be reached (long-running resilience).
        """
        self._root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            return self._ensure_locked(force=force, allow_stale=allow_stale)

    def _ensure_locked(self, force: bool, allow_stale: bool) -> tuple[dict[str, Any], bool]:
        state = self.read_state()
        try:
            remote_sha, etag = self._remote_sha(state)
        except SourceError:
            if allow_stale and state is not None:
                self._logger.warning(
                    "source update check failed, using cached revision %s", state.get("sha")
                )
                return state, False
            raise

        if (
            not force
            and state is not None
            and state.get("sha") == remote_sha
            and self.release_path(remote_sha).is_dir()
        ):
            self._logger.debug("source is up to date at %s", remote_sha)
            return state, False

        self._logger.info(
            "updating source %s@%s: %s -> %s",
            self._settings.repo,
            self._settings.ref,
            (state or {}).get("sha", "<none>"),
            remote_sha,
        )
        archive = self._root / f"download-{uuid.uuid4().hex}.zip"
        staging = self._root / f"staging-{uuid.uuid4().hex}"
        try:
            self._download_archive(remote_sha, archive)
            archive_sha256 = _sha256_file(archive)
            source_root = self._extract(archive, staging)
            self._validate(source_root)
            destination = self.release_path(remote_sha)
            if destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_root, destination)
            new_state = {
                "repo": self._settings.repo,
                "ref": self._settings.ref,
                "sha": remote_sha,
                "downloaded_at": _utc_now(),
                "archive_sha256": archive_sha256,
                "etag": etag,
            }
            self._write_state(new_state)
            self._cleanup(keep_sha=remote_sha)
            self._logger.info("source ready at %s", destination)
            return new_state, True
        finally:
            _remove_file(archive)
            _remove_tree(staging)

    def prepare_build_source(self, destination: Path) -> tuple[dict[str, Any], bool]:
        """Ensure the latest source and copy it to *destination*.

        The update check and the copy happen while holding the update lock, so
        an old release cannot be deleted between those two operations.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            state, changed = self._ensure_locked(force=False, allow_stale=True)
            source_root = self.release_path(str(state["sha"]))
            if destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                source_root,
                destination,
                ignore=shutil.ignore_patterns(*IGNORED_COPY_NAMES),
            )
            return state, changed

    # ------------------------------------------------------------------ remote
    def _remote_sha(self, state: dict[str, Any] | None) -> tuple[str, str | None]:
        ref = self._settings.ref.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            return ref.lower(), None

        cached_sha = str(state.get("sha")) if state else None
        cached_etag = str(state.get("etag")) if state and state.get("etag") else None

        # Without a token GitHub's anonymous API limit is easy to exhaust. Once
        # it reports 403 we bypass it for a while and use the Atom feed.
        if not self._settings.github_token and time.monotonic() < self._api_blocked_until:
            self._logger.debug("skipping GitHub API (temporarily rate limited)")
        else:
            try:
                return self._remote_sha_via_api(ref, cached_etag, cached_sha)
            except SourceError as exc:
                self._logger.warning("GitHub API check failed (%s), trying fallbacks", exc)
                if "HTTP 403" in str(exc) and not self._settings.github_token:
                    self._api_blocked_until = time.monotonic() + 600

        try:
            return self._remote_sha_via_atom(ref), cached_etag
        except SourceError as exc:
            self._logger.warning("commit feed check failed (%s), trying git", exc)

        return self._remote_sha_via_git(ref), cached_etag

    def _remote_sha_via_api(
        self, ref: str, etag: str | None, cached_sha: str | None
    ) -> tuple[str, str | None]:
        url = (
            f"https://api.github.com/repos/{self._settings.repo}/commits/"
            f"{quote(ref, safe='')}"
        )
        headers = _github_headers(self._settings.github_token)
        if etag:
            headers["If-None-Match"] = etag
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                sha = payload.get("sha")
                if not isinstance(sha, str):
                    raise SourceError("GitHub API response does not contain a commit SHA")
                return sha, response.headers.get("ETag")
        except HTTPError as exc:
            if exc.code == 304 and cached_sha:
                return cached_sha, etag
            raise SourceError(f"HTTP {exc.code} for {url}") from exc
        except (URLError, socket.timeout, json.JSONDecodeError, OSError) as exc:
            raise SourceError(str(exc)) from exc

    def _remote_sha_via_atom(self, ref: str) -> str:
        url = f"https://github.com/{self._settings.repo}/commits/{quote(ref, safe='/')}.atom"
        request = Request(url, headers={"User-Agent": _user_agent()})
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:
                text = response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, socket.timeout, OSError) as exc:
            raise SourceError(str(exc)) from exc
        match = re.search(r"Grit::Commit/([0-9a-f]{40})", text)
        if not match:
            raise SourceError("could not find a commit SHA in the Atom feed")
        return match.group(1)

    def _remote_sha_via_git(self, ref: str) -> str:
        git = shutil.which("git")
        if not git:
            raise SourceError(
                "cannot check the source revision: GitHub API/feed failed and git is not installed"
            )
        try:
            completed = subprocess.run(
                [git, "ls-remote", f"https://github.com/{self._settings.repo}.git", f"refs/heads/{ref}"],
                capture_output=True,
                text=True,
                timeout=self._settings.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceError(f"git ls-remote failed: {exc}") from exc
        if completed.returncode != 0:
            raise SourceError(f"git ls-remote exited with {completed.returncode}")
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 1 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                return parts[0]
        raise SourceError(f"git ls-remote returned no revision for refs/heads/{ref}")

    def _download_archive(self, sha: str, destination: Path) -> None:
        self._logger.info("downloading source archive for %s", sha)
        headers = {
            "User-Agent": _user_agent(),
            "Accept": "application/zip, application/octet-stream",
        }
        if self._settings.github_token:
            headers["Authorization"] = f"Bearer {self._settings.github_token}"
        urls = [
            f"https://github.com/{self._settings.repo}/archive/{sha}.zip",
            f"https://codeload.github.com/{self._settings.repo}/zip/{sha}",
        ]
        if self._settings.github_token:
            urls.insert(0, f"https://api.github.com/repos/{self._settings.repo}/zipball/{sha}")

        last_error: Exception | None = None
        for url in urls:
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=self._settings.timeout_seconds) as response:
                    received = 0
                    with destination.open("wb") as handle:
                        while True:
                            chunk = response.read(1024 * 256)
                            if not chunk:
                                break
                            handle.write(chunk)
                            received += len(chunk)
                if received < 4096:
                    raise SourceError(f"archive from {url} is unexpectedly small ({received} bytes)")
                self._logger.info("downloaded %s bytes from %s", received, url)
                return
            except (HTTPError, URLError, socket.timeout, OSError, SourceError) as exc:
                last_error = exc
                self._logger.warning("download from %s failed: %s", url, exc)
                _remove_file(destination)
        raise SourceError(f"could not download the source archive: {last_error}")

    # ------------------------------------------------------------------ unpack
    def _extract(self, archive: Path, staging: Path) -> Path:
        staging.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(archive) as bundle:
            members = [item for item in bundle.infolist() if item.filename.strip("/")]
            if not members:
                raise SourceError("source archive is empty")
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise SourceError(
                    f"source archive contains too many entries ({len(members)})"
                )
            total_size = sum(item.file_size for item in members)
            if total_size > MAX_EXTRACTED_BYTES:
                raise SourceError(
                    f"source archive expands to {total_size} bytes, refusing to extract"
                )
            top_levels = {Path(item.filename).parts[0] for item in members}
            top = next(iter(top_levels)) if len(top_levels) == 1 else None

            for member in members:
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    continue
                relative = Path(member.filename)
                if top is not None and relative.parts and relative.parts[0] == top:
                    relative = Path(*relative.parts[1:])
                if not relative.parts:
                    continue
                target = (staging / relative).resolve()
                if staging.resolve() not in target.parents:
                    raise SourceError(f"unsafe path in source archive: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)

        # Members are written without their leading "<repo>-<sha>/" component,
        # so the extracted tree root is the staging directory itself.
        return staging

    def _validate(self, source_root: Path) -> None:
        missing = [
            relative
            for relative in self._settings.validate_files
            if not (source_root / relative).is_file()
        ]
        if missing:
            raise SourceError(
                "downloaded source is incomplete, missing: " + ", ".join(missing)
            )

    # ------------------------------------------------------------------ cleanup
    def _cleanup(self, keep_sha: str) -> None:
        if not self._releases.is_dir():
            return
        for child in self._releases.iterdir():
            if child.name == keep_sha:
                continue
            self._logger.info("removing old source tree %s", child)
            _remove_tree(child)

        now = time.time()
        for child in self._root.iterdir():
            if child.name in (STATE_FILE, LOCK_FILE):
                continue
            if child.name.startswith(("staging-", "download-")):
                try:
                    if now - child.stat().st_mtime > 3600:
                        _remove_tree(child)
                except OSError:
                    pass


def _user_agent() -> str:
    return "kdu-build-service/1.0 (+https://github.com/hfiref0x/KDU)"


def _github_headers(token: str) -> dict[str, str]:
    headers = {
        "User-Agent": _user_agent(),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _remove_tree(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        pass

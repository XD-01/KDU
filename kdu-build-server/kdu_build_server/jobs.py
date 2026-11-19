"""SQLite-backed job persistence.

Jobs survive service restarts. Running jobs found at startup are marked as
failed because their worker process is gone.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TERMINAL_STATUSES = (SUCCEEDED, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    platform TEXT,
    source_sha TEXT,
    request TEXT NOT NULL,
    result TEXT,
    error TEXT,
    log_path TEXT,
    artifact_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
"""


class JobStore:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=False, timeout=30
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------ write
    def create(self, request: dict[str, Any], platform: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        created_at = _utc_now()
        with self._lock:
            self._connection.execute(
                "INSERT INTO jobs (id, status, created_at, platform, request) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, QUEUED, created_at, platform, json.dumps(request, sort_keys=True)),
            )
            self._connection.commit()
        return self.get(job_id)  # type: ignore[return-value]

    def mark_running(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE id = ? AND status = ?",
                (RUNNING, _utc_now(), job_id, QUEUED),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def mark_succeeded(
        self,
        job_id: str,
        result: dict[str, Any],
        source_sha: str,
        log_path: Path,
        artifact_path: Path,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, source_sha = ?, result = ?, "
                "log_path = ?, artifact_path = ? WHERE id = ?",
                (
                    SUCCEEDED,
                    _utc_now(),
                    source_sha,
                    json.dumps(result, sort_keys=True),
                    str(log_path),
                    str(artifact_path),
                    job_id,
                ),
            )
            self._connection.commit()

    def mark_failed(
        self, job_id: str, error: str, log_path: Path | None = None
    ) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ?, log_path = ? "
                "WHERE id = ?",
                (FAILED, _utc_now(), error, str(log_path) if log_path else None, job_id),
            )
            self._connection.commit()

    def recover_interrupted(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, "
                "error = 'service restarted while the build was running' "
                "WHERE status = ?",
                (FAILED, _utc_now(), RUNNING),
            )
            self._connection.commit()
            return cursor.rowcount

    def prune(self, keep: int) -> list[str]:
        """Delete old terminal jobs beyond *keep* and return their ids."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (*TERMINAL_STATUSES, keep),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._connection.execute(
                    f"DELETE FROM jobs WHERE id IN ({placeholders})", ids
                )
                self._connection.commit()
            return ids

    # ------------------------------------------------------------------- read
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def list(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        parameters: list[Any] = []
        if status:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [_row_to_job(row) for row in rows]

    def queued_ids(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at ASC", (QUEUED,)
            ).fetchall()
        return [row["id"] for row in rows]


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["request"] = json.loads(job["request"]) if job.get("request") else {}
    job["result"] = json.loads(job["result"]) if job.get("result") else None
    return job


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

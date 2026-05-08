from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JobStatus = str
PENDING: JobStatus = "pending"
RUNNING: JobStatus = "running"
DONE: JobStatus = "done"
FAILED: JobStatus = "failed"


@dataclass(frozen=True)
class Job:
    """One durable NewsRAG job."""

    id: str
    kind: str
    status: JobStatus
    payload: dict[str, Any]
    error: str | None
    created_at: str
    updated_at: str


def create_job(
    database_path: Path,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> Job:
    """Insert a durable pending job into SQLite."""

    resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:8]}"
    payload_json = json.dumps(payload or {}, sort_keys=True)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO jobs(id, kind, status, payload_json, error)
            VALUES(?, ?, ?, ?, NULL)
            """,
            (resolved_job_id, kind, PENDING, payload_json),
        )
        connection.commit()

    return get_job(database_path, resolved_job_id)


def get_job(database_path: Path, job_id: str) -> Job:
    """Load one durable job by ID."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, kind, status, payload_json, error, created_at, updated_at
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

    if row is None:
        raise KeyError(job_id)
    return _row_to_job(row)


def list_jobs(database_path: Path) -> list[Job]:
    """Return all durable jobs ordered by creation time."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, kind, status, payload_json, error, created_at, updated_at
            FROM jobs
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    return [_row_to_job(row) for row in rows]


def claim_next_job(database_path: Path) -> Job | None:
    """Claim the next pending job and mark it running."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id
            FROM jobs
            WHERE status = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (PENDING,),
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        job_id = str(row["id"])
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (RUNNING, job_id),
        )
        connection.commit()

    return get_job(database_path, job_id)


def mark_job_done(database_path: Path, job_id: str) -> Job:
    """Mark a running job done."""

    return _set_job_status(database_path, job_id, status=DONE, error=None)


def mark_job_failed(database_path: Path, job_id: str, *, error: str) -> Job:
    """Mark a running job failed with context."""

    return _set_job_status(database_path, job_id, status=FAILED, error=error)


def set_job_status(
    database_path: Path,
    job_id: str,
    *,
    status: JobStatus,
    error: str | None = None,
) -> Job:
    """Set one job status directly.

    This is primarily useful for deterministic tests and status shaping.
    """

    return _set_job_status(database_path, job_id, status=status, error=error)


def _set_job_status(
    database_path: Path,
    job_id: str,
    *,
    status: JobStatus,
    error: str | None,
) -> Job:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, error, job_id),
        )
        connection.commit()

    return get_job(database_path, job_id)


def _row_to_job(row: sqlite3.Row) -> Job:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        payload = {}
    return Job(
        id=str(row["id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        payload=payload,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )

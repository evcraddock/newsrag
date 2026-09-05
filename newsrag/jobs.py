from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JobStatus = str
PENDING: JobStatus = "pending"
RUNNING: JobStatus = "running"
DONE: JobStatus = "done"
FAILED: JobStatus = "failed"

_DUPLICATE_IGNORED_OUTCOME = "duplicate_ignored"


class JobRetryError(Exception):
    """Raised when a durable job cannot be retried."""


@dataclass(frozen=True)
class Job:
    """One durable NewsRAG job."""

    id: str
    kind: str
    status: JobStatus
    payload: dict[str, Any]
    result: dict[str, Any] | None
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
    return create_jobs(
        database_path,
        kind=kind,
        payloads=(payload or {},),
        job_ids=(resolved_job_id,),
    )[0]


def create_jobs(
    database_path: Path,
    *,
    kind: str,
    payloads: Sequence[dict[str, Any]],
    job_ids: Sequence[str] | None = None,
) -> list[Job]:
    """Atomically insert a batch of durable pending jobs into SQLite."""

    resolved_job_ids = (
        tuple(job_ids)
        if job_ids is not None
        else tuple(f"job-{uuid.uuid4().hex[:8]}" for _ in payloads)
    )
    if len(resolved_job_ids) != len(payloads):
        raise ValueError("job_ids must contain one ID for each payload")
    if not payloads:
        return []

    rows = [
        (job_id, kind, PENDING, json.dumps(payload, sort_keys=True))
        for job_id, payload in zip(resolved_job_ids, payloads, strict=True)
    ]
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO jobs(id, kind, status, payload_json, error)
            VALUES(?, ?, ?, ?, NULL)
            """,
            rows,
        )
        connection.commit()

    return [get_job(database_path, job_id) for job_id in resolved_job_ids]


def get_job(database_path: Path, job_id: str) -> Job:
    """Load one durable job by ID."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, kind, status, payload_json, result_json, error, created_at, updated_at
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
            SELECT id, kind, status, payload_json, result_json, error, created_at, updated_at
            FROM jobs
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    return [_row_to_job(row) for row in rows]


def claim_next_job(database_path: Path, *, include_refresh: bool = True) -> Job | None:
    """Claim pending work, optionally leaving refreshes to their lock owner."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id
            FROM jobs
            WHERE status = ? AND (? OR kind != 'refresh-source')
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (PENDING, include_refresh),
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        job_id = str(row["id"])
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, result_json = NULL, error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (RUNNING, job_id),
        )
        connection.commit()

    return get_job(database_path, job_id)


def mark_job_done(
    database_path: Path,
    job_id: str,
    *,
    result: dict[str, Any] | None = None,
) -> Job:
    """Mark a running job done with an optional structured result."""

    return _set_job_status(
        database_path,
        job_id,
        status=DONE,
        result=result,
        error=None,
        discard_payload=(
            result is not None and result.get("outcome") == _DUPLICATE_IGNORED_OUTCOME
        ),
    )


def mark_job_failed(database_path: Path, job_id: str, *, error: str) -> Job:
    """Mark a running job failed with context."""

    return _set_job_status(
        database_path,
        job_id,
        status=FAILED,
        result=None,
        error=error,
        discard_payload=False,
    )


def retry_failed_job(database_path: Path, job_id: str) -> Job:
    """Move one failed job back to pending for daemon reprocessing."""

    try:
        job = get_job(database_path, job_id)
    except KeyError as exc:
        raise JobRetryError(f"Unknown job: {job_id}") from exc

    if job.status != FAILED:
        raise JobRetryError(f"Job {job_id} is {job.status}; only failed jobs can be retried")

    if job.kind == "refresh-source":
        with sqlite3.connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_refresh_job_index(connection)
            try:
                cursor = connection.execute(
                    "UPDATE jobs SET status = ?, result_json = NULL, error = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = ?",
                    (PENDING, job_id, FAILED),
                )
            except sqlite3.IntegrityError as exc:
                raise JobRetryError(
                    "Another refresh is already pending or running for this source"
                ) from exc
            if cursor.rowcount != 1:
                raise JobRetryError(f"Job {job_id} is no longer failed")
        return get_job(database_path, job_id)

    return _set_job_status(
        database_path,
        job_id,
        status=PENDING,
        result=None,
        error=None,
        discard_payload=False,
    )


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

    return _set_job_status(
        database_path,
        job_id,
        status=status,
        result=None,
        error=error,
        discard_payload=False,
    )


def _set_job_status(
    database_path: Path,
    job_id: str,
    *,
    status: JobStatus,
    result: dict[str, Any] | None,
    error: str | None,
    discard_payload: bool,
) -> Job:
    result_json = json.dumps(result, sort_keys=True) if result is not None else None
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?,
                payload_json = CASE WHEN ? THEN '{}' ELSE payload_json END,
                result_json = ?,
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND NOT (kind = 'refresh-source' AND status = 'done')
            """,
            (status, discard_payload, result_json, error, job_id),
        )
        connection.commit()

    return get_job(database_path, job_id)


def ensure_refresh_job_index(connection: sqlite3.Connection) -> None:
    """Enforce one queued or running refresh per registered source."""

    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_refresh_source "
        "ON jobs(json_extract(payload_json, '$.source_id')) "
        "WHERE kind = 'refresh-source' AND status IN ('pending', 'running')"
    )


def recover_interrupted_refresh_jobs(database_path: Path) -> None:
    """Fail unacknowledged refreshes while the caller holds the corpus worker lock.

    Successful refresh publication atomically marks its job done, so only
    genuinely interrupted work remains running once no worker owns the lock.
    """

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE kind = 'refresh-source' AND status = 'running'",
            ("refresh_interrupted: worker exited before completion; use jobs retry to resume",),
        )


def _row_to_job(row: sqlite3.Row) -> Job:
    payload = _load_json_mapping(row["payload_json"]) or {}
    result = _load_json_mapping(row["result_json"])
    return Job(
        id=str(row["id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        payload=payload,
        result=result,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _load_json_mapping(raw_value: object) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    value = json.loads(str(raw_value))
    return value if isinstance(value, dict) else None

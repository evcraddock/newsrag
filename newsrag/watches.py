from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from newsrag.ingest import INGEST_JOB_KIND
from newsrag.jobs import Job, create_job

WATCH_JOB_KIND = INGEST_JOB_KIND


@dataclass(frozen=True)
class Watch:
    """One durable watch registration."""

    id: str
    path: Path
    metadata: dict[str, Any]
    created_at: str


def add_watch(
    database_path: Path,
    *,
    path: Path,
    metadata: dict[str, Any] | None = None,
    watch_id: str | None = None,
) -> Watch:
    """Register one watched folder in durable storage."""

    resolved_path = path.expanduser().resolve()
    payload = json.dumps(metadata or {}, sort_keys=True)
    resolved_watch_id = watch_id or f"watch-{uuid.uuid4().hex[:8]}"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO watches(id, path, metadata_json)
            VALUES(?, ?, ?)
            """,
            (resolved_watch_id, str(resolved_path), payload),
        )
        connection.commit()

    return get_watch_by_path(database_path, resolved_path)


def list_watches(database_path: Path) -> list[Watch]:
    """List durable watch registrations."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, path, metadata_json, created_at
            FROM watches
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    return [_row_to_watch(row) for row in rows]


def get_watch_by_path(database_path: Path, path: Path) -> Watch:
    """Load one durable watch by its path."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, path, metadata_json, created_at
            FROM watches
            WHERE path = ?
            """,
            (str(path),),
        ).fetchone()

    if row is None:
        raise KeyError(str(path))
    return _row_to_watch(row)


def enqueue_watch_changes(
    database_path: Path,
    changes: set[tuple[object, str]],
) -> list[Job]:
    """Turn relevant watch-file changes into durable ingestion jobs."""

    watches = list_watches(database_path)
    enqueued: list[Job] = []

    for _, changed_path_text in sorted(changes, key=lambda item: item[1]):
        changed_path = Path(changed_path_text).expanduser().resolve()
        watch = _matching_watch(changed_path, watches)
        if watch is None:
            continue
        if changed_path.suffix.lower() != ".pdf":
            continue
        if not changed_path.exists() or not changed_path.is_file():
            continue

        signature = _file_signature(changed_path)
        if _seen_signature(database_path, changed_path, signature):
            continue

        job = create_job(
            database_path,
            kind=INGEST_JOB_KIND,
            payload={
                "path": str(changed_path),
                "watch_id": watch.id,
                "metadata": watch.metadata,
                "signature": signature,
                "source": "watch",
            },
        )
        _remember_signature(database_path, changed_path, watch.id, signature)
        enqueued.append(job)

    return enqueued


def _matching_watch(changed_path: Path, watches: list[Watch]) -> Watch | None:
    for watch in watches:
        if changed_path.is_relative_to(watch.path):
            return watch
    return None


def _file_signature(path: Path) -> str:
    stat_result = path.stat()
    return f"{stat_result.st_size}:{stat_result.st_mtime_ns}"


def _seen_signature(database_path: Path, path: Path, signature: str) -> bool:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT content_signature FROM watch_files WHERE path = ?",
            (str(path),),
        ).fetchone()

    if row is None:
        return False
    return str(row[0]) == signature


def _remember_signature(database_path: Path, path: Path, watch_id: str, signature: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO watch_files(path, watch_id, content_signature)
            VALUES(?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                watch_id = excluded.watch_id,
                content_signature = excluded.content_signature,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(path), watch_id, signature),
        )
        connection.commit()


def _row_to_watch(row: sqlite3.Row) -> Watch:
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata, dict):
        metadata = {}
    return Watch(
        id=str(row["id"]),
        path=Path(str(row["path"])),
        metadata=metadata,
        created_at=str(row["created_at"]),
    )

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from newsrag.acquisition import AcquisitionRequest, StagedSourceArtifact, preserve_staged_artifact
from newsrag.config import EmbeddingConfig
from newsrag.ingest import IngestionPipeline, PreparedSourceArtifact
from newsrag.ingestion_identity import register_acquired_artifact
from newsrag.jobs import Job, ensure_refresh_job_index, get_job
from newsrag.revisions import publish_revision
from newsrag.sources import HTML_MAX_SOURCE_BYTES, SOURCE_KIND_URL, build_source_identity
from newsrag.storage import initialize_storage

REFRESH_JOB_KIND = "refresh-source"


class RefreshError(Exception):
    """Raised when a source refresh cannot safely proceed."""


def enqueue_refresh(database_path: Path, source_id: str) -> Job:
    """Enqueue one known source, returning its already active refresh if any."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        _source(connection, source_id)
        ensure_refresh_job_index(connection)
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind = ? AND status IN ('pending', 'running') "
            "AND json_extract(payload_json, '$.source_id') = ?",
            (REFRESH_JOB_KIND, source_id),
        ).fetchone()
        if active is not None:
            job_id = str(active["id"])
        else:
            job_id = f"job-{uuid.uuid4().hex[:8]}"
            connection.execute(
                "INSERT INTO jobs(id, kind, status, payload_json) VALUES(?, ?, 'pending', ?)",
                (job_id, REFRESH_JOB_KIND, json.dumps({"source_id": source_id})),
            )
    return get_job(database_path, job_id)


class RefreshPipeline:
    """Refresh immutable source bytes through the existing ingestion processor."""

    def __init__(self, ingestion: IngestionPipeline) -> None:
        self.ingestion = ingestion
        self.database_path = ingestion.storage_paths.database

    async def handle_job(self, job: Job) -> dict[str, Any]:
        """Run blocking acquisition and processing outside the event loop."""

        return await asyncio.to_thread(self.process_job, job)

    def process_job(self, job: Job) -> dict[str, Any]:
        """Resume a checkpoint or acquire one new candidate and publish atomically."""

        current_job = get_job(self.database_path, job.id)
        if current_job.kind != REFRESH_JOB_KIND:
            raise RefreshError("refresh_state: expected a refresh-source job")
        if current_job.status == "done" and current_job.result is not None:
            return current_job.result
        if current_job.status != "running":
            raise RefreshError("refresh_state: job must be claimed before processing")
        payload = dict(current_job.payload)
        source_id = str(payload.get("source_id", ""))
        stage = "refresh_prepare"
        staged: StagedSourceArtifact | None = None

        def set_stage(value: str) -> None:
            nonlocal stage
            stage = value

        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                source = _source(connection, source_id)
                if "base" not in payload:
                    payload["base"] = {
                        "generation": source["publication_generation"],
                        "revision_id": source["current_revision_id"],
                        "document_id": source["document_id"],
                        "user_metadata": json.loads(source["user_metadata_json"] or "{}"),
                        "metadata_origin": source["user_metadata_origin"] or "legacy",
                        "options": json.loads(source["ingestion_options_json"]),
                    }
                    _save_payload(connection, job.id, payload)
                _check_generation(connection, source_id, payload["base"])

            if "candidate" not in payload:
                stage = "artifact_acquisition"
                reference = (
                    str(source["submitted_reference"])
                    if source["kind"] == SOURCE_KIND_URL
                    else str(source["normalized_reference"])
                )
                filename = _filename(reference)
                request = AcquisitionRequest(
                    kind="url" if source["kind"] == SOURCE_KIND_URL else "local_path",
                    reference=reference,
                    max_bytes=(
                        HTML_MAX_SOURCE_BYTES
                        if Path(filename).suffix.lower() in {".html", ".htm", ".xhtml"}
                        else None
                    ),
                )
                staged = self.ingestion.acquirer.acquire(
                    request, self.ingestion.storage_paths.artifact_staging
                )
                stage = "artifact_identity"
                result = self._existing_outcome(job.id, source_id, payload["base"], staged)
                if result is not None:
                    return result

                # Preserve even an invalid candidate before adapter processing.
                stored = self._artifact_by_hash(staged.content_hash)
                if stored is None:
                    stored_path = preserve_staged_artifact(
                        staged, self.ingestion.storage_paths.source_artifacts
                    )
                    decision = register_acquired_artifact(
                        self.database_path,
                        source_identity=build_source_identity(
                            source_path=Path(str(source["normalized_reference"])),
                            source_url=(reference if source["kind"] == SOURCE_KIND_URL else None),
                            resolved_reference=staged.resolved_reference,
                        ),
                        content_hash=staged.content_hash,
                        media_type=staged.reported_media_type or "application/octet-stream",
                        byte_size=staged.byte_size,
                        stored_path=stored_path,
                        acquired_at=staged.acquired_at,
                        reported_media_type=staged.reported_media_type,
                        provenance=staged.provenance,
                    )
                    # Recheck ownership/publication after racing ordinary ingest.
                    result = self._existing_outcome(job.id, source_id, payload["base"], staged)
                    if result is not None:
                        return result
                    artifact_id = decision.artifact_id
                else:
                    artifact_id = str(stored["id"])
                payload["candidate"] = {
                    "artifact_id": artifact_id,
                    "observed_at": staged.acquired_at,
                    "observation": staged.provenance,
                    # A local MIME guess comes from the old filename, not a
                    # server. Let fresh signatures select a changed local type.
                    "reported_media_type": (
                        staged.reported_media_type if source["kind"] == SOURCE_KIND_URL else None
                    ),
                }
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    _check_generation(connection, source_id, payload["base"])
                    _save_payload(connection, job.id, payload)

            candidate = payload["candidate"]
            stage = "artifact_integrity"
            with self._connection() as connection:
                artifact = connection.execute(
                    "SELECT * FROM source_artifacts WHERE id = ?", (candidate["artifact_id"],)
                ).fetchone()
            if artifact is None:
                raise RefreshError("Saved artifact is missing; start a fresh refresh")
            if str(artifact["source_id"]) != source_id:
                raise RefreshError(
                    "artifact_source_conflict: saved artifact belongs to another source"
                )
            _verify_artifact(artifact)
            stage = "adapter_selection"
            selected = self.ingestion.adapter_registry.select(
                artifact_path=Path(str(artifact["stored_path"])),
                source_type_hint=None,
                reported_media_type=candidate["reported_media_type"],
                filename=_filename(str(source["submitted_reference"])),
            )
            reported = str(candidate["reported_media_type"] or "")
            media_type = (
                reported
                if reported.partition(";")[0].strip().lower() in selected.accepted_media_types
                else selected.media_type
            )
            base = payload["base"]
            metadata = dict(base["user_metadata"])
            metadata["source_size_bytes"] = int(artifact["byte_size"])
            provenance = json.loads(artifact["provenance_json"])
            if "file_mtime_ns" in provenance:
                metadata["source_mtime_ns"] = provenance["file_mtime_ns"]
            source_url = None
            if source["kind"] == SOURCE_KIND_URL:
                source_url = str(source["submitted_reference"])
                metadata["source_url"] = source_url
                metadata["retrieved_at"] = str(artifact["acquired_at"])
            published_result: dict[str, Any] = {}

            def publish(connection: sqlite3.Connection, document_id: str) -> None:
                _check_generation(connection, source_id, base)
                revision = publish_revision(
                    connection,
                    document_id=document_id,
                    job_id=job.id,
                    expected_generation=int(base["generation"]),
                )
                published_result.update(
                    _result(
                        "revision_created",
                        source_id,
                        str(artifact["id"]),
                        base,
                        revision.id,
                        document_id,
                        candidate["observed_at"],
                    )
                )
                _complete(connection, job.id, published_result)

            self.ingestion.processor.process(
                PreparedSourceArtifact(
                    source_path=(
                        Path(str(artifact["stored_path"]))
                        if source_url is not None
                        else Path(str(source["normalized_reference"]))
                    ),
                    artifact_path=Path(str(artifact["stored_path"])),
                    content_hash=str(artifact["content_hash"]),
                    media_type=media_type,
                    source_url=source_url,
                    acquired_at=str(artifact["acquired_at"]),
                    work_dir=self.ingestion.storage_paths.ocr_pdfs,
                    metadata=metadata,
                    adapter_options=base["options"],
                    user_metadata=base["user_metadata"],
                    user_metadata_origin=base["metadata_origin"],
                ),
                job_id=job.id,
                adapter=selected.adapter,
                on_publish=publish,
                on_stage=set_stage,
            )
            return published_result
        except Exception as exc:
            # Commit is the success boundary, even if acknowledgement failed.
            finished = get_job(self.database_path, job.id)
            if finished.status == "done" and finished.result is not None:
                return finished.result
            if isinstance(exc, RefreshError):
                raise
            raise RefreshError(f"{stage} failed for source {source_id}: {exc}") from exc
        finally:
            if staged is not None:
                staged.staged_path.unlink(missing_ok=True)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _artifact_by_hash(self, content_hash: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_artifacts WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            return row if isinstance(row, sqlite3.Row) else None

    def _existing_outcome(
        self,
        job_id: str,
        source_id: str,
        base: dict[str, Any],
        staged: StagedSourceArtifact,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _check_generation(connection, source_id, base)
            row = connection.execute(
                "SELECT a.*, r.id AS revision_id, r.document_id "
                "FROM source_artifacts a LEFT JOIN source_revisions r "
                "ON r.document_id = (SELECT id FROM documents WHERE artifact_id = a.id) "
                "WHERE a.content_hash = ?",
                (staged.content_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["source_id"] != source_id:
                if row["revision_id"] is None:
                    raise RefreshError(
                        "artifact_source_conflict: unpublished bytes belong to another source"
                    )
                result: dict[str, Any] = {
                    "outcome": "duplicate_ignored",
                    "source_id": str(row["source_id"]),
                    "artifact_id": str(row["id"]),
                    "document_id": str(row["document_id"]),
                    "requested_source_id": source_id,
                }
                _complete(connection, job_id, result)
                return result
            if row["revision_id"] is None:
                return None
            _verify_artifact(row)
            outcome = "unchanged"
            if row["revision_id"] != base["revision_id"]:
                outcome = "revision_reactivated"
                connection.execute(
                    "UPDATE sources SET current_revision_id = ?, "
                    "publication_generation = publication_generation + 1 WHERE id = ?",
                    (row["revision_id"], source_id),
                )
            result = _result(
                outcome,
                source_id,
                str(row["id"]),
                base,
                str(row["revision_id"]),
                str(row["document_id"]),
                staged.acquired_at,
            )
            result["observation"] = staged.provenance
            _complete(connection, job_id, result)
            return result


def build_refresh_handler(
    *, data_dir: Path, embedding_config: EmbeddingConfig
) -> Callable[[Job], Awaitable[dict[str, Any]]]:
    """Build a lazy refresh handler without requiring a model at enqueue time."""

    async def handle(job: Job) -> dict[str, Any]:
        ingestion = IngestionPipeline(
            storage_paths=initialize_storage(data_dir), embedding_config=embedding_config
        )
        return await RefreshPipeline(ingestion).handle_job(job)

    return handle


def _source(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row:
    cursor = connection.execute(
        "SELECT s.*, r.document_id, d.user_metadata_json, d.user_metadata_origin, "
        "d.ingestion_options_json FROM sources s "
        "JOIN source_revisions r ON r.id = s.current_revision_id AND r.source_id = s.id "
        "JOIN documents d ON d.id = r.document_id "
        "JOIN source_artifacts a ON a.id = d.artifact_id AND a.source_id = s.id "
        "WHERE s.id = ? AND a.state = 'published'",
        (source_id,),
    )
    cursor.row_factory = sqlite3.Row
    row = cursor.fetchone()
    if not isinstance(row, sqlite3.Row):
        raise RefreshError(
            "Unknown source or no published revision; ingest or retry initial ingestion first"
        )
    return row


def _check_generation(connection: sqlite3.Connection, source_id: str, base: dict[str, Any]) -> None:
    source = _source(connection, source_id)
    if (
        source["publication_generation"] != base["generation"]
        or source["current_revision_id"] != base["revision_id"]
    ):
        raise RefreshError(
            "refresh_conflict: source changed since this attempt; start a fresh refresh"
        )


def _save_payload(connection: sqlite3.Connection, job_id: str, payload: dict[str, Any]) -> None:
    cursor = connection.execute(
        "UPDATE jobs SET payload_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND kind = ? AND status = 'running'",
        (json.dumps(payload, sort_keys=True), job_id, REFRESH_JOB_KIND),
    )
    if cursor.rowcount != 1:
        raise RefreshError("refresh_state: job is no longer running")


def _complete(connection: sqlite3.Connection, job_id: str, result: dict[str, Any]) -> None:
    cursor = connection.execute(
        "UPDATE jobs SET status = 'done', result_json = ?, error = NULL, "
        "payload_json = CASE WHEN ? THEN '{}' ELSE payload_json END, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND kind = ? AND status = 'running'",
        (
            json.dumps(result, sort_keys=True),
            result["outcome"] == "duplicate_ignored",
            job_id,
            REFRESH_JOB_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise RefreshError("refresh_state: publication requires a running job")


def _result(
    outcome: str,
    source_id: str,
    artifact_id: str,
    base: dict[str, Any],
    revision_id: str,
    document_id: str,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "source_id": source_id,
        "artifact_id": artifact_id,
        "previous_revision_id": base["revision_id"],
        "previous_document_id": base["document_id"],
        "revision_id": revision_id,
        "document_id": document_id,
        "observed_at": observed_at,
    }


def _verify_artifact(artifact: sqlite3.Row) -> None:
    path = Path(str(artifact["stored_path"]))
    try:
        with path.open("rb") as source_file:
            digest = hashlib.file_digest(source_file, "sha256").hexdigest()
        if path.stat().st_size != artifact["byte_size"] or digest != artifact["content_hash"]:
            raise RefreshError("artifact_integrity: saved artifact hash or size does not match")
    except OSError as exc:
        raise RefreshError("artifact_integrity: saved artifact is missing or unreadable") from exc


def _filename(reference: str) -> str:
    parsed = urlsplit(reference)
    return Path(parsed.path if parsed.scheme in {"http", "https"} else reference).name

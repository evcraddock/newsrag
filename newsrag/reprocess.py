"""Manual, failure-safe rebuilding from preserved evidence artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from newsrag.config import EmbeddingConfig
    from newsrag.ingest import IngestionPipeline

from newsrag.jobs import Job, ensure_reprocessing_job_index, get_job

REPROCESS_JOB_KIND = "reprocess-document"
MAX_REPROCESS_DOCUMENTS = 20


class ReprocessingError(Exception):
    """A reprocessing request cannot safely complete."""


def enqueue_reprocessing(
    database_path: Path,
    document_ids: Sequence[str],
    *,
    pdf_extractor: str | None = None,
    csv_options: dict[str, object] | None = None,
) -> list[Job]:
    """Validate and enqueue an explicit bounded batch atomically, without model access."""

    if not 1 <= len(document_ids) <= MAX_REPROCESS_DOCUMENTS:
        raise ReprocessingError("Specify between 1 and 20 explicit document IDs")
    if isinstance(document_ids, str) or any(not item.strip() for item in document_ids):
        raise ReprocessingError("Specify a list of nonempty document IDs")
    if pdf_extractor is not None and pdf_extractor not in {
        "auto",
        "pymupdf",
        "pdfplumber",
        "table",
    }:
        raise ReprocessingError("Unknown PDF extractor; use auto, pymupdf, pdfplumber, or table")
    if csv_options is not None:
        from newsrag.adapters import AdapterError
        from newsrag.csv_adapter import normalize_csv_options

        try:
            normalized_csv = normalize_csv_options(csv_options)
        except AdapterError as exc:
            raise ReprocessingError(str(exc)) from exc
        csv_options = {key: normalized_csv[key] for key in csv_options}
        if pdf_extractor is not None:
            raise ReprocessingError("CSV recipe options conflict with PDF options")
    job_ids = []
    with sqlite3.connect(database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        ensure_reprocessing_job_index(connection)
        for document_id in dict.fromkeys(document_ids):
            document = _document(connection, document_id)
            if pdf_extractor is not None and document["media_type"] != "application/pdf":
                raise ReprocessingError("--pdf-extractor applies only to PDF documents")
            if csv_options is not None and document["media_type"] not in {
                "text/csv",
                "application/csv",
            }:
                raise ReprocessingError("CSV options apply only to CSV documents")
            existing = connection.execute(
                "SELECT id, payload_json FROM jobs WHERE kind = ? "
                "AND status IN ('pending', 'running') "
                "AND json_extract(payload_json, '$.document_id') = ?",
                (REPROCESS_JOB_KIND, document_id),
            ).fetchone()
            if existing is not None:
                saved_options = json.loads(existing["payload_json"])
                if (
                    saved_options.get("pdf_extractor") != pdf_extractor
                    or saved_options.get("csv") != csv_options
                ):
                    raise ReprocessingError(
                        f"Document {document_id} already has an active job with different options"
                    )
                job_ids.append(str(existing["id"]))
                continue
            payload = {
                "document_id": document_id,
                "base_generation_id": document["current_processing_generation_id"],
                "artifact_id": document["artifact_id"],
                "pdf_extractor": pdf_extractor,
                "csv": csv_options,
                "stage": "pending",
            }
            job_id = f"job-{uuid.uuid4().hex[:8]}"
            connection.execute(
                "INSERT INTO jobs(id, kind, status, payload_json) VALUES(?, ?, 'pending', ?)",
                (job_id, REPROCESS_JOB_KIND, json.dumps(payload, sort_keys=True)),
            )
            job_ids.append(job_id)
    return [get_job(database_path, job_id) for job_id in job_ids]


class ReprocessingPipeline:
    """Reuse the shared processing pipeline without reacquisition or identity mutation."""

    def __init__(self, ingestion: IngestionPipeline) -> None:
        self.ingestion = ingestion
        self.database_path: Path = ingestion.storage_paths.database

    async def handle_job(self, job: Job) -> dict[str, Any]:
        return await asyncio.to_thread(self.process_job, job)

    def process_job(self, job: Job) -> dict[str, Any]:
        from newsrag.ingest import PreparedSourceArtifact
        from newsrag.processing_configuration import (
            configuration_fingerprint,
            processing_configuration,
        )

        current = get_job(self.database_path, job.id)
        if current.kind != REPROCESS_JOB_KIND:
            raise ReprocessingError("reprocess_state: expected a reprocess-document job")
        if current.status == "done" and current.result is not None:
            return current.result
        if current.status != "running":
            raise ReprocessingError("reprocess_state: job must be claimed before processing")
        payload = dict(current.payload)
        document_id = str(payload["document_id"])
        stage = "artifact_integrity"

        def set_stage(value: str) -> None:
            nonlocal stage
            stage = value
            payload["stage"] = value
            with self._connection() as connection:
                _save_payload(connection, job.id, payload)

        try:
            with self._connection() as connection:
                document = _document(connection, document_id)
                _check_base(document, payload)
            # A private byte-for-byte snapshot prevents an altered cache file
            # from changing the input between verification and adapter reads.
            with tempfile.TemporaryDirectory(
                prefix="reprocess-", dir=self.ingestion.storage_paths.artifact_staging
            ) as scratch:
                artifact_path = Path(scratch) / "source"
                _snapshot_artifact(document, artifact_path)
                set_stage("configuration")
                selected = self.ingestion.adapter_registry.select(
                    artifact_path=artifact_path,
                    source_type_hint=None,
                    reported_media_type=str(document["media_type"]),
                    filename="",
                )
                options = json.loads(document["ingestion_options_json"] or "{}")
                # Inherit the active generation's options, not the initial run's.
                old_config = json.loads(document["configuration_json"])
                options.update(old_config.get("options", {}))
                reported_type = str(document["reported_media_type"] or "")
                canonical_type = str(document["media_type"])
                input_media_type = str(
                    options.get("source_media_type")
                    or (
                        reported_type
                        if reported_type.partition(";")[0].strip().lower() == canonical_type
                        else canonical_type
                    )
                )
                options["source_media_type"] = input_media_type
                if payload.get("pdf_extractor") is not None:
                    options["pdf_extractor"] = payload["pdf_extractor"]
                if payload.get("csv") is not None:
                    from newsrag.csv_adapter import normalize_csv_options

                    options["csv"] = normalize_csv_options(
                        {**options.get("csv", {}), **payload["csv"]}
                    )
                target = processing_configuration(
                    adapter=selected.adapter,
                    chunker=self.ingestion.processor.chunker,
                    embedding_provider=self.ingestion.processor.embedding_provider,
                    options=options,
                )
                fingerprint = configuration_fingerprint(target)
                if "configuration" in payload and payload["configuration"] != target:
                    raise ReprocessingError(
                        "reprocess_configuration_conflict: saved target differs from this worker; "
                        "restore its processing configuration or submit a new reprocess request"
                    )
                payload["configuration"] = target
                payload["fingerprint"] = fingerprint
                payload.setdefault("generation_id", f"processing-{uuid.uuid4().hex}")
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    _check_base(_document(connection, document_id), payload)
                    _save_payload(connection, job.id, payload)
                    if fingerprint == document["fingerprint"]:
                        result = _result(
                            "unchanged",
                            document,
                            payload,
                            str(document["current_processing_generation_id"]),
                        )
                        _complete(connection, job.id, result)
                        return result

                result = {}

                def publish(connection: sqlite3.Connection, published_document_id: str) -> None:
                    if published_document_id != document_id:
                        raise ReprocessingError("Reprocessing cannot change document identity")
                    result.update(
                        _result("reprocessed", document, payload, payload["generation_id"])
                    )
                    _complete(connection, job.id, result)

                self.ingestion.processor.process(
                    PreparedSourceArtifact(
                        source_path=Path(document["source_path"] or document["stored_path"]),
                        artifact_path=artifact_path,
                        content_hash=str(document["content_hash"]),
                        media_type=input_media_type,
                        source_url=document["source_url"],
                        acquired_at=str(document["acquired_at"]),
                        work_dir=self.ingestion.storage_paths.ocr_pdfs,
                        metadata=json.loads(document["metadata_json"]),
                        adapter_options=options,
                        user_metadata=json.loads(document["user_metadata_json"] or "{}"),
                        user_metadata_origin=document["user_metadata_origin"] or "legacy",
                    ),
                    job_id=job.id,
                    adapter=selected.adapter,
                    document_id=document_id,
                    processing_generation_id=payload["generation_id"],
                    expected_generation_id=payload["base_generation_id"],
                    configuration=target,
                    on_stage=set_stage,
                    on_publish=publish,
                )
                return result
        except Exception as exc:
            receipt = get_job(self.database_path, job.id)
            if receipt.status == "done" and receipt.result is not None:
                return receipt.result
            raise ReprocessingError(f"{stage} failed for document {document_id}: {exc}") from exc

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def build_reprocessing_handler(
    *, data_dir: Path, embedding_config: EmbeddingConfig
) -> Callable[[Job], Awaitable[dict[str, Any]]]:
    """Build lazily so enqueueing/status work without an embedding configuration."""

    async def handle(job: Job) -> dict[str, Any]:
        from newsrag.ingest import IngestionPipeline
        from newsrag.storage import initialize_storage

        return await ReprocessingPipeline(
            IngestionPipeline(
                storage_paths=initialize_storage(data_dir),
                embedding_config=embedding_config,
            )
        ).handle_job(job)

    return handle


def _document(connection: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    row = cursor.execute(
        "SELECT documents.*, source_artifacts.source_id, source_artifacts.media_type, "
        "source_artifacts.stored_path, source_artifacts.content_hash, source_artifacts.byte_size, "
        "source_artifacts.acquired_at, source_artifacts.reported_media_type, "
        "source_revisions.id AS revision_id, "
        "processing_generations.fingerprint, processing_generations.configuration_json "
        "FROM documents JOIN source_artifacts ON source_artifacts.id = documents.artifact_id "
        "JOIN source_revisions ON source_revisions.document_id = documents.id "
        "JOIN processing_generations ON processing_generations.id = documents.current_processing_generation_id "
        "AND processing_generations.document_id = documents.id "
        "WHERE documents.id = ? AND source_artifacts.state = 'published'",
        (document_id,),
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise ReprocessingError(f"Unknown or unpublished document {document_id}; ingest it first")
    return row


def _check_base(document: sqlite3.Row, payload: dict[str, Any]) -> None:
    if document["current_processing_generation_id"] != payload["base_generation_id"]:
        raise ReprocessingError("reprocess_conflict: generation changed; submit a new request")
    if document["artifact_id"] != payload["artifact_id"]:
        raise ReprocessingError("reprocess_conflict: artifact identity changed")


def _snapshot_artifact(document: sqlite3.Row, output: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    path = Path(document["stored_path"])
    if not path.is_file():
        raise ReprocessingError("Saved artifact is missing or not a regular file")
    with path.open("rb") as source, output.open("xb") as destination:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            if document["byte_size"] is not None and size > document["byte_size"]:
                raise ReprocessingError("Saved artifact size changed; restore the preserved bytes")
            digest.update(chunk)
            destination.write(chunk)
    if digest.hexdigest() != document["content_hash"] or (
        document["byte_size"] is not None and size != document["byte_size"]
    ):
        raise ReprocessingError("Saved artifact hash/size mismatch; restore the preserved bytes")


def _save_payload(connection: sqlite3.Connection, job_id: str, payload: dict[str, Any]) -> None:
    cursor = connection.execute(
        "UPDATE jobs SET payload_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'running'",
        (json.dumps(payload, sort_keys=True), job_id),
    )
    if cursor.rowcount != 1:
        raise ReprocessingError("reprocess_state: running job ownership was lost")


def _complete(connection: sqlite3.Connection, job_id: str, result: dict[str, Any]) -> None:
    cursor = connection.execute(
        "UPDATE jobs SET status = 'done', result_json = ?, error = NULL, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'",
        (json.dumps(result, sort_keys=True), job_id),
    )
    if cursor.rowcount != 1:
        raise ReprocessingError("reprocess_state: cannot commit without job ownership")


def _result(
    outcome: str, document: sqlite3.Row, payload: dict[str, Any], generation_id: str
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "document_id": document["id"],
        "source_id": document["source_id"],
        "artifact_id": document["artifact_id"],
        "revision_id": document["revision_id"],
        "previous_processing_generation_id": payload["base_generation_id"],
        "processing_generation_id": generation_id,
        "fingerprint": payload["fingerprint"],
    }

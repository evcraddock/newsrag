from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit

import lancedb  # type: ignore[import-untyped]

from newsrag.acquisition import (
    SOURCE_KIND_URL,
    AcquisitionError,
    AcquisitionRequest,
    SafeSourceArtifactAcquirer,
    SourceArtifactAcquirer,
    preserve_staged_artifact,
    safe_url_reference,
    validate_url_submission,
)
from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    AdapterSelectionError,
    CanonicalSourceUnit,
    ExtractorIdentity,
    RegisteredSourceAdapter,
    SourceAdapter,
    SourceAdapterRegistry,
)
from newsrag.config import EmbeddingConfig
from newsrag.embeddings import (
    ChunkEmbedding,
    EmbeddingMetadata,
    EmbeddingProvider,
    build_embedding_provider,
)
from newsrag.html_adapter import StaticHtmlSourceAdapter
from newsrag.ingestion_identity import (
    OUTCOME_CREATED,
    OUTCOME_DUPLICATE_IGNORED,
    find_document_for_artifact,
    find_published_duplicate,
    find_stored_artifact_path,
    register_acquired_artifact,
)
from newsrag.jobs import Job, create_jobs
from newsrag.passages import build_passage_rows
from newsrag.pdf_adapter import (
    PDF_EXTRACTOR_AUTO,
    PDF_EXTRACTOR_PDFPLUMBER,
    PDF_EXTRACTOR_PYMUPDF,
    PDF_EXTRACTOR_TABLE,
    ExtractedPage,
    FallbackTextExtractor,
    OcrRunner,
    PdfExtractorMode,
    PdfPlumberTextExtractor,
    PdfSourceAdapter,
    PyMuPdfTextExtractor,
    SubprocessOcrRunner,
    TextExtractor,
)
from newsrag.pdf_adapter import (
    build_pdf_text_extractor as _build_pdf_text_extractor,
)
from newsrag.pdf_adapter import (
    normalize_pdf_extractor_mode as _normalize_pdf_extractor_mode,
)
from newsrag.search import LanceDbPassageVectorStore, PassageVectorRecord
from newsrag.sources import (
    HTML_MAX_SOURCE_BYTES,
    HTML_MEDIA_TYPES,
    PAGE_LOCATION_TYPE,
    PDF_MEDIA_TYPE,
    artifact_id_for_hash,
    build_source_identity,
    normalize_url_reference,
    source_unit_id_for_ordinal,
    source_unit_id_for_page,
)
from newsrag.storage import StoragePaths, initialize_storage

__all__ = [
    "ExtractedPage",
    "FallbackTextExtractor",
    "PDF_EXTRACTOR_AUTO",
    "PDF_EXTRACTOR_PDFPLUMBER",
    "PDF_EXTRACTOR_PYMUPDF",
    "PDF_EXTRACTOR_TABLE",
    "PreparedSourceArtifact",
    "PdfPlumberTextExtractor",
    "PyMuPdfTextExtractor",
    "SourceAdapter",
    "SourceProcessingPipeline",
    "build_pdf_text_extractor",
    "normalize_pdf_extractor_mode",
]

INGEST_JOB_KIND = "ingest-file"
DEFAULT_CHUNK_MAX_CHARS = 2000
DEFAULT_CHUNK_OVERLAP_CHARS = 200
VECTOR_TABLE_NAME = "chunk_embeddings"
SOURCE_TYPE_PDF = "pdf"
SOURCE_TYPE_HTML = "html"
SUPPORTED_SOURCE_TYPES = frozenset({SOURCE_TYPE_HTML, SOURCE_TYPE_PDF})
_PDF_EXTENSIONS = (".pdf",)
_PDF_SIGNATURES = (b"%PDF-",)
_HTML_EXTENSIONS = (".html", ".htm", ".xhtml")
_HTML_SIGNATURES = (b"<!doctype html", b"<html", b"<?xml")
_UNKNOWN_MEDIA_TYPE = "application/octet-stream"
LOGGER = logging.getLogger(__name__)


class IngestError(Exception):
    """Raised when source acquisition or ingestion cannot complete."""


@dataclass(frozen=True)
class PreparedIngestBatch:
    """Validated job payloads and scan reporting before durable enqueue."""

    payloads: tuple[dict[str, Any], ...]
    queued_by_type: dict[str, int]
    skipped_by_type: dict[str, int]


@dataclass(frozen=True)
class IngestEnqueueResult:
    """Durable jobs and grouped results for one ingestion request."""

    jobs: tuple[Job, ...]
    queued_by_type: dict[str, int]
    skipped_by_type: dict[str, int]


@dataclass(frozen=True)
class ChunkDraft:
    """One chunk ready for embedding and persistence."""

    text: str
    page_start: int
    page_end: int
    source_unit_start_ordinal: int | None = None
    source_unit_end_ordinal: int | None = None


@dataclass(frozen=True)
class DocumentRecord:
    """One durable ingested document record."""

    id: str
    source_path: str | None
    source_url: str | None
    title: str | None
    source_hash: str | None
    normalized_path: str | None
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class PageRecord:
    """One durable page record."""

    id: str
    document_id: str
    page_number: int
    text: str
    extractor: str
    created_at: str
    source_unit_id: str | None = None


@dataclass(frozen=True)
class ChunkRecord:
    """One durable chunk record."""

    id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    created_at: str
    source_unit_start_id: str | None = None
    source_unit_end_id: str | None = None


@dataclass(frozen=True)
class ChunkVectorRecord:
    """One vector-search row written to LanceDB."""

    chunk_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    vector: tuple[float, ...]
    metadata: EmbeddingMetadata
    source_unit_start_id: str | None = None
    source_unit_end_id: str | None = None


class Chunker(Protocol):
    """Protocol for canonical-source-unit chunking."""

    def chunk_units(self, units: Sequence[CanonicalSourceUnit]) -> list[ChunkDraft]:
        """Split canonical source units into searchable chunks."""


class VectorStore(Protocol):
    """Protocol for chunk-vector persistence."""

    def add_chunks(self, chunks: Sequence[ChunkVectorRecord]) -> None:
        """Persist embedded chunk vectors."""

    def delete_document(self, document_id: str) -> None:
        """Remove staged vectors for one failed document publication."""


class PassageVectorStore(Protocol):
    """Protocol for passage-vector persistence."""

    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        """Persist embedded passage vectors."""

    def delete_document(self, document_id: str) -> None:
        """Remove staged vectors for one failed document publication."""


def build_pdf_text_extractor(mode: PdfExtractorMode = PDF_EXTRACTOR_AUTO) -> TextExtractor:
    """Build the configured PDF text extraction path."""

    try:
        return _build_pdf_text_extractor(mode)
    except AdapterError as exc:
        raise IngestError(str(exc)) from exc


def normalize_pdf_extractor_mode(value: str | None) -> PdfExtractorMode:
    """Normalize and validate a PDF extractor mode option."""

    try:
        return _normalize_pdf_extractor_mode(value)
    except AdapterError as exc:
        raise IngestError(str(exc)) from exc


@dataclass(frozen=True)
class PageChunker:
    """Source-unit chunking that preserves existing page-first PDF behavior."""

    max_chars: int = DEFAULT_CHUNK_MAX_CHARS
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS

    def chunk_pages(self, pages: Sequence[ExtractedPage]) -> list[ChunkDraft]:
        """Retain the existing page-based entry point for compatibility."""

        return self.chunk_units(
            [
                CanonicalSourceUnit(
                    ordinal=page.page_number,
                    location_type=PAGE_LOCATION_TYPE,
                    location={"page_number": page.page_number},
                    human_label=f"p. {page.page_number}",
                    normalized_text=page.text,
                    structure={},
                    extractor=_extractor_identity(page.extractor),
                )
                for page in pages
            ]
        )

    def chunk_units(self, units: Sequence[CanonicalSourceUnit]) -> list[ChunkDraft]:
        chunks: list[ChunkDraft] = []
        for unit in units:
            page_number = _page_number(unit)
            text = unit.normalized_text.strip()
            if not text:
                continue
            if len(text) <= self.max_chars:
                chunks.append(
                    ChunkDraft(
                        text=text,
                        page_start=page_number,
                        page_end=page_number,
                        source_unit_start_ordinal=unit.ordinal,
                        source_unit_end_ordinal=unit.ordinal,
                    )
                )
                continue

            start = 0
            while start < len(text):
                end = min(start + self.max_chars, len(text))
                chunk_text = text[start:end].strip()
                if chunk_text:
                    chunks.append(
                        ChunkDraft(
                            text=chunk_text,
                            page_start=page_number,
                            page_end=page_number,
                            source_unit_start_ordinal=unit.ordinal,
                            source_unit_end_ordinal=unit.ordinal,
                        )
                    )
                if end >= len(text):
                    break
                start = max(0, end - self.overlap_chars)
        return chunks


@dataclass(frozen=True)
class SourceUnitChunker:
    """Chunk page units compatibly and non-page units by stable source ordinal."""

    page_chunker: PageChunker = PageChunker()
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS

    def chunk_units(self, units: Sequence[CanonicalSourceUnit]) -> list[ChunkDraft]:
        if all(unit.location_type == PAGE_LOCATION_TYPE for unit in units):
            return self.page_chunker.chunk_units(units)
        if any(unit.location_type == PAGE_LOCATION_TYPE for unit in units):
            raise IngestError("Cannot chunk mixed page and non-page source units")

        chunks: list[ChunkDraft] = []
        for unit in units:
            text = unit.normalized_text.strip()
            if not text:
                continue
            start = 0
            while start < len(text):
                end = min(start + self.max_chars, len(text))
                chunk_text = text[start:end].strip()
                if chunk_text:
                    chunks.append(
                        ChunkDraft(
                            text=chunk_text,
                            page_start=unit.ordinal,
                            page_end=unit.ordinal,
                            source_unit_start_ordinal=unit.ordinal,
                            source_unit_end_ordinal=unit.ordinal,
                        )
                    )
                if end >= len(text):
                    break
                start = max(0, end - self.overlap_chars)
        return chunks


@dataclass(frozen=True)
class LanceDbVectorStore:
    """Vector persistence backed by LanceDB."""

    lancedb_path: Path
    table_name: str = VECTOR_TABLE_NAME

    def add_chunks(self, chunks: Sequence[ChunkVectorRecord]) -> None:
        records = [
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "source_unit_start_id": chunk.source_unit_start_id,
                "source_unit_end_id": chunk.source_unit_end_id,
                "text": chunk.text,
                "vector": list(chunk.vector),
                "provider": chunk.metadata.provider,
                "model": chunk.metadata.model,
                "version": chunk.metadata.version,
            }
            for chunk in chunks
        ]
        if not records:
            return

        self.lancedb_path.mkdir(parents=True, exist_ok=True)
        database = lancedb.connect(self.lancedb_path)
        try:
            table = database.open_table(self.table_name)
        except ValueError:
            database.create_table(self.table_name, data=records)
            return

        table.add(records)

    def delete_document(self, document_id: str) -> None:
        database = lancedb.connect(self.lancedb_path)
        try:
            table = database.open_table(self.table_name)
        except ValueError:
            return
        escaped_document_id = document_id.replace("'", "''")
        table.delete(f"document_id = '{escaped_document_id}'")


@dataclass(frozen=True)
class PreparedSourceArtifact:
    """One acquired artifact ready for source-neutral processing."""

    source_path: Path
    artifact_path: Path
    content_hash: str
    media_type: str
    source_url: str | None
    acquired_at: str
    work_dir: Path
    metadata: dict[str, Any]
    adapter_options: dict[str, object]


class SourceProcessingPipeline:
    """Shared adapter, indexing, and atomic publication pipeline."""

    def __init__(
        self,
        *,
        storage_paths: StoragePaths,
        adapter: SourceAdapter,
        chunker: Chunker,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        passage_vector_store: PassageVectorStore,
    ) -> None:
        self.storage_paths = storage_paths
        self.adapter = adapter
        self.chunker = chunker
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.passage_vector_store = passage_vector_store

    def process(
        self,
        artifact: PreparedSourceArtifact,
        *,
        job_id: str,
        adapter: SourceAdapter | None = None,
    ) -> str:
        resolved_adapter = adapter or self.adapter
        with _ingest_stage(job_id, "adapter_extraction", artifact.source_path):
            adapter_result = self._extract_source_units(artifact, adapter=resolved_adapter)
        LOGGER.info(
            "ingest_units_ready job_id=%s source_path=%r units=%d extractor=%s",
            job_id,
            str(artifact.source_path),
            len(adapter_result.units),
            adapter_result.extractor.name,
        )

        with _ingest_stage(job_id, "chunking", artifact.source_path):
            chunks = self.chunker.chunk_units(adapter_result.units)
        LOGGER.info(
            "ingest_chunks_ready job_id=%s source_path=%r chunks=%d",
            job_id,
            str(artifact.source_path),
            len(chunks),
        )

        with _ingest_stage(job_id, "chunk_embeddings", artifact.source_path):
            chunk_embeddings = self.embedding_provider.embed_chunks(
                [chunk.text for chunk in chunks]
            )
            if len(chunk_embeddings) != len(chunks):
                raise IngestError(
                    f"Embedded {len(chunk_embeddings)} chunks for {len(chunks)} chunk drafts"
                )

        document_id = f"document-{uuid.uuid4().hex[:8]}"
        artifact_id = artifact_id_for_hash(artifact.content_hash)
        resolved_metadata = dict(adapter_result.metadata_candidates)
        resolved_metadata.update(artifact.metadata)
        document_metadata = _build_document_metadata(
            resolved_metadata,
            artifact.source_path,
            artifact.artifact_path,
        )
        page_rows, source_unit_rows, source_unit_ids = _build_source_unit_rows(
            document_id,
            artifact_id,
            adapter_result.units,
        )
        chunk_rows, vector_rows = _build_chunk_and_vector_rows(
            document_id,
            chunks,
            chunk_embeddings,
            source_unit_ids=source_unit_ids,
        )
        passage_rows = _build_passage_rows_from_chunks(chunk_rows)
        with _ingest_stage(job_id, "passage_embeddings", artifact.source_path):
            passage_embeddings = self.embedding_provider.embed_chunks(
                [passage_row[8] for passage_row in passage_rows]
            )
            if len(passage_embeddings) != len(passage_rows):
                raise IngestError(
                    f"Embedded {len(passage_embeddings)} passages for "
                    f"{len(passage_rows)} passage rows"
                )
        passage_vector_rows = _build_passage_vector_rows(
            passage_rows,
            passage_embeddings,
        )

        with _ingest_stage(job_id, "publication", artifact.source_path):
            _publish_document_bundle(
                self.storage_paths.database,
                document_id=document_id,
                artifact_id=artifact_id,
                media_type=adapter_result.media_type,
                source_path=artifact.source_path,
                source_url=artifact.source_url,
                title=_resolve_document_title(resolved_metadata, artifact.source_path),
                source_hash=artifact.content_hash,
                normalized_path=adapter_result.derived_artifact_path,
                metadata=document_metadata,
                source_units=source_unit_rows,
                pages=page_rows,
                chunks=chunk_rows,
                chunk_embeddings=chunk_embeddings,
                passages=passage_rows,
                passage_embeddings=passage_embeddings,
                chunk_vectors=vector_rows,
                passage_vectors=passage_vector_rows,
                vector_store=self.vector_store,
                passage_vector_store=self.passage_vector_store,
            )
        LOGGER.info(
            "ingest_published job_id=%s source_path=%r document_id=%s pages=%d chunks=%d "
            "passages=%d",
            job_id,
            str(artifact.source_path),
            document_id,
            len(page_rows),
            len(chunk_rows),
            len(passage_rows),
        )
        return document_id

    def _extract_source_units(
        self,
        artifact: PreparedSourceArtifact,
        *,
        adapter: SourceAdapter,
    ) -> AdapterResult:
        adapter_input = AdapterInput(
            artifact_path=artifact.artifact_path,
            content_hash=artifact.content_hash,
            media_type=artifact.media_type,
            work_dir=artifact.work_dir,
            options=artifact.adapter_options,
        )
        try:
            result = adapter.extract(adapter_input)
        except AdapterError as exc:
            raise IngestError(f"Source adapter failed for {artifact.source_path}: {exc}") from exc
        _validate_adapter_result(result, adapter=adapter)
        return result


class IngestionPipeline:
    """Job front end for the shared source-processing pipeline."""

    def __init__(
        self,
        *,
        storage_paths: StoragePaths,
        embedding_config: EmbeddingConfig,
        acquirer: SourceArtifactAcquirer | None = None,
        adapter: SourceAdapter | None = None,
        adapter_registry: SourceAdapterRegistry | None = None,
        ocr_runner: OcrRunner | None = None,
        text_extractor: TextExtractor | None = None,
        chunker: Chunker | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        passage_vector_store: PassageVectorStore | None = None,
    ) -> None:
        self.storage_paths = storage_paths
        self.acquirer = acquirer or SafeSourceArtifactAcquirer()
        resolved_adapter = adapter or PdfSourceAdapter(
            ocr_runner=ocr_runner or SubprocessOcrRunner(),
            text_extractor=text_extractor,
        )
        self.adapter_registry = adapter_registry or SourceAdapterRegistry(
            (
                RegisteredSourceAdapter(
                    source_type=SOURCE_TYPE_PDF,
                    media_type=PDF_MEDIA_TYPE,
                    extensions=_PDF_EXTENSIONS,
                    signatures=_PDF_SIGNATURES,
                    adapter=resolved_adapter,
                ),
                RegisteredSourceAdapter(
                    source_type=SOURCE_TYPE_HTML,
                    media_type=HTML_MEDIA_TYPES[0],
                    media_type_aliases=HTML_MEDIA_TYPES[1:],
                    extensions=_HTML_EXTENSIONS,
                    signatures=_HTML_SIGNATURES,
                    adapter=StaticHtmlSourceAdapter(),
                ),
            )
        )
        self.processor = SourceProcessingPipeline(
            storage_paths=storage_paths,
            adapter=resolved_adapter,
            chunker=chunker or SourceUnitChunker(),
            embedding_provider=embedding_provider or build_embedding_provider(embedding_config),
            vector_store=vector_store or LanceDbVectorStore(storage_paths.lancedb),
            passage_vector_store=passage_vector_store
            or LanceDbPassageVectorStore(storage_paths.lancedb),
        )

    async def handle_job(self, job: Job) -> dict[str, object]:
        return await asyncio.to_thread(self.process_job, job)

    def process_job(self, job: Job) -> dict[str, object]:
        request = _payload_acquisition_request(job.payload)
        safe_reference = _safe_acquisition_reference(request)
        metadata = _payload_metadata(job.payload)
        staged_artifact = None

        try:
            with _ingest_stage(job.id, "artifact_acquisition", safe_reference):
                staged_artifact = self.acquirer.acquire(
                    request,
                    self.storage_paths.artifact_staging,
                )

            with _ingest_stage(job.id, "artifact_preparation", safe_reference):
                duplicate = find_published_duplicate(
                    self.storage_paths.database,
                    staged_artifact.content_hash,
                )
                if duplicate is not None:
                    return self._completed_identity_result(job, duplicate.result())

                selected_adapter: RegisteredSourceAdapter | None = None
                selection_error: AdapterSelectionError | None = None
                try:
                    selected_adapter = self.adapter_registry.select(
                        artifact_path=staged_artifact.staged_path,
                        source_type_hint=_payload_source_type_hint(job.payload),
                        reported_media_type=staged_artifact.reported_media_type,
                        filename=_source_filename(staged_artifact.submitted_reference),
                    )
                except AdapterSelectionError as exc:
                    selection_error = exc

                stored_path = find_stored_artifact_path(
                    self.storage_paths.database,
                    staged_artifact.content_hash,
                )
                if stored_path is None:
                    stored_path = preserve_staged_artifact(
                        staged_artifact,
                        self.storage_paths.source_artifacts,
                    )

                source_url = (
                    staged_artifact.submitted_reference
                    if staged_artifact.source_kind == SOURCE_KIND_URL
                    else _metadata_source_url(metadata)
                )
                resolved_reference = (
                    staged_artifact.resolved_reference
                    if source_url is None or staged_artifact.source_kind == SOURCE_KIND_URL
                    else source_url
                )
                decision = register_acquired_artifact(
                    self.storage_paths.database,
                    source_identity=build_source_identity(
                        source_path=Path(staged_artifact.submitted_reference),
                        source_url=source_url,
                        resolved_reference=resolved_reference,
                    ),
                    content_hash=staged_artifact.content_hash,
                    media_type=(
                        selected_adapter.media_type
                        if selected_adapter is not None
                        else _UNKNOWN_MEDIA_TYPE
                    ),
                    byte_size=staged_artifact.byte_size,
                    stored_path=stored_path,
                    acquired_at=staged_artifact.acquired_at,
                    reported_media_type=staged_artifact.reported_media_type,
                    provenance=staged_artifact.provenance,
                )
                if decision.action == "complete":
                    return self._completed_identity_result(job, decision.result())
                if selection_error is not None:
                    raise IngestError(
                        f"Source adapter selection failed for {safe_reference}: {selection_error}"
                    ) from selection_error
                if selected_adapter is None:
                    raise IngestError(f"Source adapter selection failed for {safe_reference}")

            document_metadata = dict(metadata)
            document_metadata["source_size_bytes"] = staged_artifact.byte_size
            file_mtime_ns = staged_artifact.provenance.get("file_mtime_ns")
            if isinstance(file_mtime_ns, int):
                document_metadata["source_mtime_ns"] = file_mtime_ns
            if staged_artifact.source_kind == SOURCE_KIND_URL:
                document_metadata["source_url"] = staged_artifact.submitted_reference
                document_metadata["retrieved_at"] = staged_artifact.acquired_at

            try:
                document_id = self.processor.process(
                    PreparedSourceArtifact(
                        source_path=decision.document_source_path,
                        artifact_path=decision.artifact_path,
                        content_hash=staged_artifact.content_hash,
                        media_type=_adapter_input_media_type(
                            selected_adapter,
                            staged_artifact.reported_media_type,
                        ),
                        source_url=decision.document_source_url,
                        acquired_at=decision.acquired_at,
                        work_dir=self.storage_paths.ocr_pdfs,
                        metadata=document_metadata,
                        adapter_options={"pdf_extractor": _payload_pdf_extractor_mode(job.payload)},
                    ),
                    job_id=job.id,
                    adapter=selected_adapter.adapter,
                )
            except sqlite3.IntegrityError as exc:
                published_document_id = find_document_for_artifact(
                    self.storage_paths.database,
                    decision.artifact_id,
                )
                if "documents.artifact_id" in str(exc) and published_document_id is not None:
                    return self._completed_identity_result(
                        job,
                        decision.result(
                            outcome=OUTCOME_DUPLICATE_IGNORED,
                            document_id=published_document_id,
                        ),
                    )
                raise
            return decision.result(outcome=OUTCOME_CREATED, document_id=document_id)
        except AcquisitionError as exc:
            raise IngestError(str(exc)) from exc
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(f"Failed ingesting {safe_reference}: {exc}") from exc
        finally:
            if staged_artifact is not None:
                staged_artifact.staged_path.unlink(missing_ok=True)

    def _completed_identity_result(
        self,
        job: Job,
        result: dict[str, object],
    ) -> dict[str, object]:
        LOGGER.info(
            "ingest_identity_decided job_id=%s outcome=%s artifact_id=%s document_id=%s",
            job.id,
            result["outcome"],
            result["artifact_id"],
            result.get("document_id", "-"),
        )
        return result


def normalize_source_type_hint(value: str | None) -> str | None:
    """Normalize an optional source-type hint accepted by the current registry."""

    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_SOURCE_TYPES:
        raise IngestError(
            f"Unsupported source type {value!r}; expected one of: "
            + ", ".join(sorted(SUPPORTED_SOURCE_TYPES))
        )
    return normalized


def prepare_ingest_source(
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
    source_type: str | None = None,
    pdf_extractor: str | None = None,
    origin: str = "cli",
    base_dir: Path | None = None,
    require_existing: bool = False,
) -> PreparedIngestBatch:
    """Validate one source and prepare job payloads without writing them."""

    reference = source.strip()
    if not reference:
        raise IngestError("Source must not be empty")
    normalized_source_type = normalize_source_type_hint(source_type)
    base_payload: dict[str, Any] = {
        "metadata": dict(metadata or {}),
        "source": origin,
    }
    if normalized_source_type is not None:
        base_payload["source_type"] = normalized_source_type
    if pdf_extractor is not None:
        base_payload["pdf_extractor"] = normalize_pdf_extractor_mode(pdf_extractor)

    try:
        parsed = urlsplit(reference)
        _ = parsed.port
    except ValueError as exc:
        raise IngestError("Source URL is malformed") from exc
    if parsed.scheme.lower() in {"http", "https"}:
        try:
            submitted_url = validate_url_submission(reference)
        except AcquisitionError as exc:
            raise IngestError(str(exc)) from exc
        queued_type = (
            normalized_source_type or _source_type_for_extension(Path(parsed.path)) or "auto"
        )
        return PreparedIngestBatch(
            payloads=({**base_payload, "url": submitted_url},),
            queued_by_type={queued_type: 1},
            skipped_by_type={},
        )
    if parsed.scheme:
        raise IngestError(
            f"Unsupported source scheme {parsed.scheme!r}; expected HTTP(S) or a local path"
        )

    submitted_path = Path(os.path.expanduser(reference))
    if not submitted_path.is_absolute():
        submitted_path = (base_dir or Path.cwd()) / submitted_path
    absolute_path = Path(os.path.abspath(submitted_path))
    if require_existing and not absolute_path.exists():
        raise IngestError(f"Local source path does not exist: {absolute_path}")

    if absolute_path.is_dir() and not absolute_path.is_symlink():
        paths, queued_by_type, skipped_by_type = _scan_ingest_directory(
            absolute_path,
            source_type=normalized_source_type,
        )
        return PreparedIngestBatch(
            payloads=tuple({**base_payload, "path": str(path)} for path in paths),
            queued_by_type=queued_by_type,
            skipped_by_type=skipped_by_type,
        )

    queued_type = normalized_source_type or _source_type_for_extension(absolute_path) or "auto"
    return PreparedIngestBatch(
        payloads=({**base_payload, "path": str(absolute_path)},),
        queued_by_type={queued_type: 1},
        skipped_by_type={},
    )


def enqueue_prepared_ingest_batches(
    database_path: Path,
    batches: Sequence[PreparedIngestBatch],
) -> IngestEnqueueResult:
    """Atomically enqueue fully prepared ingestion batches."""

    payloads = tuple(payload for batch in batches for payload in batch.payloads)
    seen_references: set[tuple[str, str]] = set()
    for payload in payloads:
        identity = _job_payload_identity(payload)
        if identity in seen_references:
            raise IngestError("Ingestion batch contains a duplicate source")
        seen_references.add(identity)
    jobs = create_jobs(database_path, kind=INGEST_JOB_KIND, payloads=payloads)
    queued_by_type: Counter[str] = Counter()
    skipped_by_type: Counter[str] = Counter()
    for batch in batches:
        queued_by_type.update(batch.queued_by_type)
        skipped_by_type.update(batch.skipped_by_type)
    return IngestEnqueueResult(
        jobs=tuple(jobs),
        queued_by_type=dict(sorted(queued_by_type.items())),
        skipped_by_type=dict(sorted(skipped_by_type.items())),
    )


def enqueue_ingest_source(
    database_path: Path,
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
    source_type: str | None = None,
    pdf_extractor: str | None = None,
) -> IngestEnqueueResult:
    """Validate and enqueue one URL, local file, or local directory."""

    batch = prepare_ingest_source(
        source=source,
        metadata=metadata,
        source_type=source_type,
        pdf_extractor=pdf_extractor,
    )
    return enqueue_prepared_ingest_batches(database_path, (batch,))


def build_ingest_handler(
    *,
    data_dir: Path,
    embedding_config: EmbeddingConfig,
    acquirer: SourceArtifactAcquirer | None = None,
    adapter: SourceAdapter | None = None,
    adapter_registry: SourceAdapterRegistry | None = None,
    ocr_runner: OcrRunner | None = None,
    text_extractor: TextExtractor | None = None,
    chunker: Chunker | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    passage_vector_store: PassageVectorStore | None = None,
) -> Callable[[Job], Awaitable[dict[str, object]]]:
    """Build an async handler for safely acquired source-ingest jobs."""

    pipeline = IngestionPipeline(
        storage_paths=initialize_storage(data_dir),
        embedding_config=embedding_config,
        acquirer=acquirer,
        adapter=adapter,
        adapter_registry=adapter_registry,
        ocr_runner=ocr_runner,
        text_extractor=text_extractor,
        chunker=chunker,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        passage_vector_store=passage_vector_store,
    )
    return pipeline.handle_job


def list_documents(database_path: Path) -> list[DocumentRecord]:
    """List durable documents ordered by creation time."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                id,
                source_path,
                source_url,
                title,
                source_hash,
                normalized_path,
                metadata_json,
                created_at
            FROM documents
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    return [_row_to_document(row) for row in rows]


def get_document_by_source_hash(database_path: Path, source_hash: str) -> DocumentRecord | None:
    """Return one document by content hash when present."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                id,
                source_path,
                source_url,
                title,
                source_hash,
                normalized_path,
                metadata_json,
                created_at
            FROM documents
            WHERE source_hash = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (source_hash,),
        ).fetchone()

    if row is None:
        return None
    return _row_to_document(row)


def list_pages(database_path: Path) -> list[PageRecord]:
    """List durable pages ordered by page number."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, document_id, page_number, text, extractor, created_at, source_unit_id
            FROM pages
            ORDER BY document_id ASC, page_number ASC, id ASC
            """
        ).fetchall()

    return [
        PageRecord(
            id=str(row["id"]),
            document_id=str(row["document_id"]),
            page_number=int(row["page_number"]),
            text=str(row["text"]),
            extractor=str(row["extractor"]),
            created_at=str(row["created_at"]),
            source_unit_id=(
                str(row["source_unit_id"]) if row["source_unit_id"] is not None else None
            ),
        )
        for row in rows
    ]


def list_chunks(database_path: Path) -> list[ChunkRecord]:
    """List durable chunks ordered by page span."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                id,
                document_id,
                page_start,
                page_end,
                text,
                created_at,
                source_unit_start_id,
                source_unit_end_id
            FROM chunks
            ORDER BY document_id ASC, page_start ASC, id ASC
            """
        ).fetchall()

    return [
        ChunkRecord(
            id=str(row["id"]),
            document_id=str(row["document_id"]),
            page_start=int(row["page_start"]),
            page_end=int(row["page_end"]),
            text=str(row["text"]),
            created_at=str(row["created_at"]),
            source_unit_start_id=(
                str(row["source_unit_start_id"])
                if row["source_unit_start_id"] is not None
                else None
            ),
            source_unit_end_id=(
                str(row["source_unit_end_id"]) if row["source_unit_end_id"] is not None else None
            ),
        )
        for row in rows
    ]


def list_chunk_vectors(
    lancedb_path: Path,
    *,
    table_name: str = VECTOR_TABLE_NAME,
) -> list[dict[str, Any]]:
    """List persisted chunk vectors for tests and inspection."""

    database = lancedb.connect(lancedb_path)
    try:
        table = database.open_table(table_name)
    except ValueError:
        return []

    rows = table.to_arrow().to_pylist()
    return [dict(row) for row in rows if isinstance(row, dict)]


def _scan_ingest_directory(
    directory: Path,
    *,
    source_type: str | None,
) -> tuple[tuple[Path, ...], dict[str, int], dict[str, int]]:
    queued_paths: list[Path] = []
    queued_by_type: Counter[str] = Counter()
    skipped_by_type: Counter[str] = Counter()

    def raise_scan_error(error: OSError) -> None:
        raise IngestError(
            f"Could not scan directory {directory} ({type(error).__name__})"
        ) from error

    for root, directory_names, filenames in os.walk(
        directory,
        topdown=True,
        onerror=raise_scan_error,
        followlinks=False,
    ):
        root_path = Path(root)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = root_path / name
            if candidate.is_symlink():
                skipped_by_type["symlink"] += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in sorted(filenames):
            candidate = root_path / name
            if candidate.is_symlink():
                skipped_by_type["symlink"] += 1
                continue
            if not candidate.is_file():
                skipped_by_type["special"] += 1
                continue
            selected_type = source_type or _source_type_for_extension(candidate)
            if selected_type is None:
                skipped_by_type[_unsupported_extension_label(candidate)] += 1
                continue
            queued_paths.append(Path(os.path.abspath(candidate)))
            queued_by_type[selected_type] += 1

    return (
        tuple(queued_paths),
        dict(sorted(queued_by_type.items())),
        dict(sorted(skipped_by_type.items())),
    )


def _source_type_for_extension(path: Path) -> str | None:
    extension = path.suffix.lower()
    if extension in _PDF_EXTENSIONS:
        return SOURCE_TYPE_PDF
    if extension in _HTML_EXTENSIONS:
        return SOURCE_TYPE_HTML
    return None


def _unsupported_extension_label(path: Path) -> str:
    extension = path.suffix.lower().lstrip(".")
    return extension or "no-extension"


def _job_payload_identity(payload: dict[str, Any]) -> tuple[str, str]:
    raw_url = payload.get("url")
    if isinstance(raw_url, str) and raw_url.strip():
        return "url", normalize_url_reference(raw_url)
    raw_path = payload.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return "path", str(Path(os.path.abspath(Path(raw_path).expanduser())))
    raise IngestError("Ingest job payload is missing a valid URL or path")


def _payload_acquisition_request(payload: dict[str, Any]) -> AcquisitionRequest:
    raw_url = payload.get("url")
    raw_path = payload.get("path")
    if isinstance(raw_url, str) and raw_url.strip():
        if isinstance(raw_path, str) and raw_path.strip():
            raise IngestError("Ingest job payload cannot contain both a URL and path")
        reference = raw_url.strip()
        return AcquisitionRequest(
            kind=SOURCE_KIND_URL,
            reference=reference,
            max_bytes=_payload_source_max_bytes(payload, Path(urlsplit(reference).path)),
            apply_reported_media_limit=_payload_source_type_hint(payload) is None,
        )
    if isinstance(raw_path, str) and raw_path.strip():
        return AcquisitionRequest(
            kind="local_path",
            reference=raw_path,
            max_bytes=_payload_source_max_bytes(payload, Path(raw_path)),
            apply_reported_media_limit=_payload_source_type_hint(payload) is None,
        )
    raise IngestError("Ingest job payload is missing a valid URL or path")


def _payload_source_type_hint(payload: dict[str, Any]) -> str | None:
    value = payload.get("source_type")
    if value is None:
        return None
    if not isinstance(value, str):
        raise IngestError("Ingest job payload source_type must be a string")
    return normalize_source_type_hint(value)


def _payload_source_max_bytes(payload: dict[str, Any], path: Path) -> int | None:
    source_type = _payload_source_type_hint(payload) or _source_type_for_extension(path)
    return HTML_MAX_SOURCE_BYTES if source_type == SOURCE_TYPE_HTML else None


def _adapter_input_media_type(
    registration: RegisteredSourceAdapter,
    reported_media_type: str | None,
) -> str:
    normalized_reported_type = (reported_media_type or "").partition(";")[0].strip().lower()
    if normalized_reported_type in registration.accepted_media_types:
        return str(reported_media_type)
    return registration.media_type


def _source_filename(reference: str) -> str:
    parsed = urlsplit(reference)
    if parsed.scheme.lower() in {"http", "https"}:
        return Path(parsed.path).name
    return Path(reference).name


def _safe_acquisition_reference(request: AcquisitionRequest) -> str:
    if request.kind == SOURCE_KIND_URL:
        return safe_url_reference(request.reference)
    return str(Path(os.path.abspath(Path(request.reference).expanduser())))


def _payload_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _payload_pdf_extractor_mode(payload: dict[str, Any]) -> PdfExtractorMode:
    value = payload.get("pdf_extractor")
    if value is None:
        return PDF_EXTRACTOR_AUTO
    if not isinstance(value, str):
        raise IngestError("Ingest job payload pdf_extractor must be a string")
    return normalize_pdf_extractor_mode(value)


def _metadata_source_url(metadata: dict[str, Any]) -> str | None:
    source_url = metadata.get("source_url")
    if isinstance(source_url, str) and source_url.strip():
        return source_url.strip()
    return None


@contextmanager
def _ingest_stage(job_id: str, stage: str, source_reference: Path | str) -> Iterator[None]:
    started_at = perf_counter()
    LOGGER.info(
        "ingest_stage_started job_id=%s stage=%s source_reference=%r",
        job_id,
        stage,
        str(source_reference),
    )
    try:
        yield
    except Exception:
        LOGGER.error(
            "ingest_stage_failed job_id=%s stage=%s source_reference=%r elapsed_ms=%d",
            job_id,
            stage,
            str(source_reference),
            round((perf_counter() - started_at) * 1000),
        )
        raise
    LOGGER.info(
        "ingest_stage_completed job_id=%s stage=%s source_reference=%r elapsed_ms=%d",
        job_id,
        stage,
        str(source_reference),
        round((perf_counter() - started_at) * 1000),
    )


def _validate_adapter_result(result: AdapterResult, *, adapter: SourceAdapter) -> None:
    if result.media_type not in adapter.media_types:
        raise IngestError(f"Source adapter returned unsupported media type {result.media_type!r}")
    if not result.extractor.name.strip():
        raise IngestError("Source adapter returned no extractor identity")
    ordinals = [unit.ordinal for unit in result.units]
    if ordinals != list(range(1, len(result.units) + 1)):
        raise IngestError("Source adapter units must use contiguous one-based ordinals")
    for unit in result.units:
        if not unit.location_type.strip():
            raise IngestError(f"Source adapter unit {unit.ordinal} has no location type")
        if not unit.human_label.strip():
            raise IngestError(f"Source adapter unit {unit.ordinal} has no human label")
        if not unit.extractor.name.strip():
            raise IngestError(f"Source adapter unit {unit.ordinal} has no extractor identity")


def _page_number(unit: CanonicalSourceUnit) -> int:
    page_number = _optional_page_number(unit)
    if page_number is None:
        raise IngestError(
            f"Page-first chunking cannot process source-unit type {unit.location_type!r}"
        )
    return page_number


def _optional_page_number(unit: CanonicalSourceUnit) -> int | None:
    if unit.location_type != PAGE_LOCATION_TYPE:
        return None
    page_number = unit.location.get("page_number")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise IngestError(f"Source adapter unit {unit.ordinal} has an invalid PDF page location")
    return page_number


def _extractor_identity(name: str) -> ExtractorIdentity:
    return ExtractorIdentity(name=name)


def _build_document_metadata(
    metadata: dict[str, Any],
    source_path: Path,
    stored_source_path: Path,
) -> dict[str, Any]:
    source_stat = stored_source_path.stat()
    combined = dict(metadata)
    combined.setdefault("source_filename", source_path.name)
    combined.setdefault("source_size_bytes", source_stat.st_size)
    combined.setdefault("source_mtime_ns", source_stat.st_mtime_ns)
    combined.setdefault("stored_source_path", str(stored_source_path))
    return combined


def _resolve_document_title(metadata: dict[str, Any], source_path: Path) -> str:
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return source_path.stem


def _build_source_unit_rows(
    document_id: str,
    artifact_id: str,
    units: Sequence[CanonicalSourceUnit],
) -> tuple[
    list[tuple[str, str, int, str, str, str]],
    list[tuple[str, str, str, int, str, str, str, str, str, str, str | None]],
    dict[int, str],
]:
    page_rows: list[tuple[str, str, int, str, str, str]] = []
    source_unit_rows: list[tuple[str, str, str, int, str, str, str, str, str, str, str | None]] = []
    source_unit_ids: dict[int, str] = {}

    for unit in units:
        page_number = _optional_page_number(unit)
        if page_number is not None:
            page_id = f"page-{uuid.uuid4().hex[:8]}"
            unit_id = source_unit_id_for_page(page_id)
            page_rows.append(
                (
                    page_id,
                    document_id,
                    page_number,
                    unit_id,
                    unit.normalized_text,
                    unit.extractor.name,
                )
            )
        else:
            unit_id = source_unit_id_for_ordinal(document_id, unit.ordinal)

        source_unit_ids[unit.ordinal] = unit_id
        source_unit_rows.append(
            (
                unit_id,
                artifact_id,
                document_id,
                unit.ordinal,
                unit.location_type,
                json.dumps(unit.location, sort_keys=True),
                unit.human_label,
                unit.normalized_text,
                json.dumps(unit.structure, sort_keys=True),
                unit.extractor.name,
                unit.extractor.version,
            )
        )

    return page_rows, source_unit_rows, source_unit_ids


def _build_chunk_and_vector_rows(
    document_id: str,
    chunks: Sequence[ChunkDraft],
    embeddings: Sequence[ChunkEmbedding],
    *,
    source_unit_ids: dict[int, str],
) -> tuple[list[tuple[str, str, int, int, str, str, str]], list[ChunkVectorRecord]]:
    chunk_rows: list[tuple[str, str, int, int, str, str, str]] = []
    vector_rows: list[ChunkVectorRecord] = []

    for chunk, embedding in zip(chunks, embeddings, strict=True):
        chunk_id = f"chunk-{uuid.uuid4().hex[:8]}"
        start_ordinal = chunk.source_unit_start_ordinal or chunk.page_start
        end_ordinal = chunk.source_unit_end_ordinal or chunk.page_end
        try:
            source_unit_start_id = source_unit_ids[start_ordinal]
            source_unit_end_id = source_unit_ids[end_ordinal]
        except KeyError as exc:
            raise IngestError(
                f"Chunk references missing source unit {exc.args[0]} for document {document_id}"
            ) from exc
        chunk_rows.append(
            (
                chunk_id,
                document_id,
                chunk.page_start,
                chunk.page_end,
                source_unit_start_id,
                source_unit_end_id,
                chunk.text,
            )
        )
        vector_rows.append(
            ChunkVectorRecord(
                chunk_id=chunk_id,
                document_id=document_id,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                vector=embedding.vector,
                metadata=embedding.metadata,
                source_unit_start_id=source_unit_start_id,
                source_unit_end_id=source_unit_end_id,
            )
        )

    return chunk_rows, vector_rows


def _build_passage_rows_from_chunks(
    chunks: Sequence[tuple[str, str, int, int, str, str, str]],
) -> list[tuple[str, str, str, int, int, str, str, int, str]]:
    passage_rows: list[tuple[str, str, str, int, int, str, str, int, str]] = []
    for (
        chunk_id,
        document_id,
        page_start,
        page_end,
        source_unit_start_id,
        source_unit_end_id,
        text,
    ) in chunks:
        for passage in build_passage_rows(
            chunk_id=chunk_id,
            document_id=document_id,
            page_start=page_start,
            page_end=page_end,
            text=text,
        ):
            passage_rows.append(
                (
                    passage.id,
                    passage.chunk_id,
                    passage.document_id,
                    passage.page_start,
                    passage.page_end,
                    source_unit_start_id,
                    source_unit_end_id,
                    passage.ordinal,
                    passage.text,
                )
            )
    return passage_rows


def _build_passage_vector_rows(
    passages: Sequence[tuple[str, str, str, int, int, str, str, int, str]],
    embeddings: Sequence[ChunkEmbedding],
) -> list[PassageVectorRecord]:
    return [
        PassageVectorRecord(
            passage_id=passage_id,
            document_id=document_id,
            page_start=page_start,
            page_end=page_end,
            text=text,
            vector=embedding.vector,
            metadata=embedding.metadata,
            source_unit_start_id=source_unit_start_id,
            source_unit_end_id=source_unit_end_id,
        )
        for (
            passage_id,
            _,
            document_id,
            page_start,
            page_end,
            source_unit_start_id,
            source_unit_end_id,
            _,
            text,
        ), embedding in zip(passages, embeddings, strict=True)
    ]


def _publish_document_bundle(
    database_path: Path,
    *,
    document_id: str,
    artifact_id: str,
    media_type: str,
    source_path: Path,
    source_url: str | None,
    title: str,
    source_hash: str,
    normalized_path: Path | None,
    metadata: dict[str, Any],
    source_units: Sequence[tuple[str, str, str, int, str, str, str, str, str, str, str | None]],
    pages: Sequence[tuple[str, str, int, str, str, str]],
    chunks: Sequence[tuple[str, str, int, int, str, str, str]],
    chunk_embeddings: Sequence[ChunkEmbedding],
    passages: Sequence[tuple[str, str, str, int, int, str, str, int, str]],
    passage_embeddings: Sequence[ChunkEmbedding],
    chunk_vectors: Sequence[ChunkVectorRecord],
    passage_vectors: Sequence[PassageVectorRecord],
    vector_store: VectorStore,
    passage_vector_store: PassageVectorStore,
) -> None:
    metadata_json = json.dumps(metadata, sort_keys=True)

    with _publication_transaction(
        database_path,
        document_id=document_id,
        vector_store=vector_store,
        passage_vector_store=passage_vector_store,
    ) as connection:
        connection.execute(
            """
            INSERT INTO documents(
                id,
                source_path,
                source_url,
                title,
                source_hash,
                normalized_path,
                metadata_json,
                artifact_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                str(source_path),
                source_url,
                title,
                source_hash,
                str(normalized_path) if normalized_path is not None else None,
                metadata_json,
                artifact_id,
            ),
        )
        connection.execute(
            "UPDATE source_artifacts SET state = 'published', media_type = ? WHERE id = ?",
            (media_type, artifact_id),
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id,
                artifact_id,
                document_id,
                ordinal,
                location_type,
                location_json,
                human_label,
                normalized_text,
                structure_json,
                extractor,
                extractor_version
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            source_units,
        )
        connection.executemany(
            """
            INSERT INTO pages(id, document_id, page_number, source_unit_id, text, extractor)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            pages,
        )
        connection.executemany(
            """
            INSERT INTO chunks(
                id,
                document_id,
                page_start,
                page_end,
                source_unit_start_id,
                source_unit_end_id,
                text
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            chunks,
        )
        connection.executemany(
            """
            INSERT INTO chunks_fts(chunk_id, text)
            VALUES(?, ?)
            """,
            [(chunk_id, text) for chunk_id, _, _, _, _, _, text in chunks],
        )
        connection.executemany(
            """
            INSERT INTO passages(
                id,
                chunk_id,
                document_id,
                page_start,
                page_end,
                source_unit_start_id,
                source_unit_end_id,
                ordinal,
                text
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            passages,
        )
        connection.executemany(
            """
            INSERT INTO passages_fts(passage_id, text)
            VALUES(?, ?)
            """,
            [(passage_id, text) for passage_id, _, _, _, _, _, _, _, text in passages],
        )
        _insert_embedding_rows(
            connection,
            source_kind="chunk",
            source_rows=chunks,
            embeddings=chunk_embeddings,
            source_key_index=0,
            source_unit_start_index=4,
            source_unit_end_index=5,
        )
        _insert_embedding_rows(
            connection,
            source_kind="passage",
            source_rows=passages,
            embeddings=passage_embeddings,
            source_key_index=0,
            source_unit_start_index=5,
            source_unit_end_index=6,
        )
        vector_store.add_chunks(chunk_vectors)
        passage_vector_store.add_passages(passage_vectors)


@contextmanager
def _publication_transaction(
    database_path: Path,
    *,
    document_id: str,
    vector_store: VectorStore,
    passage_vector_store: PassageVectorStore,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except Exception as exc:
        connection.rollback()
        cleanup_errors: list[str] = []
        for name, store in (
            ("chunk vectors", vector_store),
            ("passage vectors", passage_vector_store),
        ):
            try:
                store.delete_document(document_id)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"{name}: {cleanup_exc}")
        if cleanup_errors:
            detail = "; ".join(cleanup_errors)
            raise IngestError(
                f"Document publication failed for {document_id}: {exc}; "
                f"vector cleanup also failed: {detail}"
            ) from exc
        raise
    finally:
        connection.close()


def _insert_embedding_rows(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_rows: Sequence[tuple[object, ...]],
    embeddings: Sequence[ChunkEmbedding],
    source_key_index: int,
    source_unit_start_index: int,
    source_unit_end_index: int,
) -> None:
    connection.executemany(
        """
        INSERT INTO embedding_records(
            id,
            source_kind,
            source_key,
            provider,
            model,
            version,
            dimensions,
            source_unit_start_id,
            source_unit_end_id
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                f"embedding-{uuid.uuid4().hex[:8]}",
                source_kind,
                str(source_row[source_key_index]),
                embedding.metadata.provider,
                embedding.metadata.model,
                embedding.metadata.version,
                len(embedding.vector),
                str(source_row[source_unit_start_index]),
                str(source_row[source_unit_end_index]),
            )
            for source_row, embedding in zip(source_rows, embeddings, strict=True)
        ],
    )


def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata, dict):
        metadata = {}

    return DocumentRecord(
        id=str(row["id"]),
        source_path=str(row["source_path"]) if row["source_path"] is not None else None,
        source_url=str(row["source_url"]) if row["source_url"] is not None else None,
        title=str(row["title"]) if row["title"] is not None else None,
        source_hash=str(row["source_hash"]) if row["source_hash"] is not None else None,
        normalized_path=(
            str(row["normalized_path"]) if row["normalized_path"] is not None else None
        ),
        metadata=metadata,
        created_at=str(row["created_at"]),
    )

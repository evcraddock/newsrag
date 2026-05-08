from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import fitz  # type: ignore[import-untyped]
import lancedb  # type: ignore[import-untyped]

from newsrag.config import EmbeddingConfig
from newsrag.embeddings import (
    ChunkEmbedding,
    EmbeddingMetadata,
    EmbeddingProvider,
    build_embedding_provider,
    create_embedding_record,
)
from newsrag.jobs import Job, create_job
from newsrag.storage import StoragePaths, initialize_storage

INGEST_JOB_KIND = "ingest-file"
DEFAULT_EMBEDDING_PROVIDER = "ollama"
DEFAULT_CHUNK_MAX_CHARS = 2000
DEFAULT_CHUNK_OVERLAP_CHARS = 200
VECTOR_TABLE_NAME = "chunk_embeddings"


class IngestError(Exception):
    """Raised when local PDF ingestion cannot complete."""


@dataclass(frozen=True)
class ExtractedPage:
    """Canonical page text extracted from one PDF page."""

    page_number: int
    text: str


@dataclass(frozen=True)
class ChunkDraft:
    """One chunk ready for embedding and persistence."""

    text: str
    page_start: int
    page_end: int


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
    created_at: str


@dataclass(frozen=True)
class ChunkRecord:
    """One durable chunk record."""

    id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    created_at: str


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


class OcrRunner(Protocol):
    """Protocol for OCR normalization."""

    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        """Create an OCR-normalized PDF artifact."""


class TextExtractor(Protocol):
    """Protocol for page-text extraction."""

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        """Extract canonical page text from one PDF."""


class Chunker(Protocol):
    """Protocol for page-to-chunk conversion."""

    def chunk_pages(self, pages: Sequence[ExtractedPage]) -> list[ChunkDraft]:
        """Split extracted pages into searchable chunks."""


class VectorStore(Protocol):
    """Protocol for vector persistence."""

    def add_chunks(self, chunks: Sequence[ChunkVectorRecord]) -> None:
        """Persist embedded chunk vectors."""


@dataclass(frozen=True)
class SubprocessOcrRunner:
    """OCR normalization backed by the `ocrmypdf` CLI."""

    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "ocrmypdf",
                    "--skip-text",
                    "--quiet",
                    str(source_path),
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise IngestError(f"ocrmypdf failed for {source_path}: {detail}") from exc


@dataclass(frozen=True)
class PyMuPdfTextExtractor:
    """Text extraction backed by PyMuPDF."""

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        pages: list[ExtractedPage] = []
        with fitz.open(pdf_path) as document:
            for index, page in enumerate(document, start=1):
                pages.append(ExtractedPage(page_number=index, text=page.get_text().strip()))
        return pages


@dataclass(frozen=True)
class PageChunker:
    """Page-first chunking with simple overlap for long pages."""

    max_chars: int = DEFAULT_CHUNK_MAX_CHARS
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS

    def chunk_pages(self, pages: Sequence[ExtractedPage]) -> list[ChunkDraft]:
        chunks: list[ChunkDraft] = []
        for page in pages:
            text = page.text.strip()
            if not text:
                continue
            if len(text) <= self.max_chars:
                chunks.append(
                    ChunkDraft(text=text, page_start=page.page_number, page_end=page.page_number)
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
                            page_start=page.page_number,
                            page_end=page.page_number,
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


class IngestionPipeline:
    """End-to-end local PDF ingestion pipeline."""

    def __init__(
        self,
        *,
        storage_paths: StoragePaths,
        embedding_config: EmbeddingConfig,
        ocr_runner: OcrRunner | None = None,
        text_extractor: TextExtractor | None = None,
        chunker: Chunker | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.storage_paths = storage_paths
        self.ocr_runner = ocr_runner or SubprocessOcrRunner()
        self.text_extractor = text_extractor or PyMuPdfTextExtractor()
        self.chunker = chunker or PageChunker()
        self.embedding_provider = embedding_provider or build_embedding_provider(
            _resolve_embedding_config(embedding_config)
        )
        self.vector_store = vector_store or LanceDbVectorStore(storage_paths.lancedb)

    async def handle_job(self, job: Job) -> None:
        await asyncio.to_thread(self.process_job, job)

    def process_job(self, job: Job) -> None:
        source_path = _payload_path(job.payload)
        metadata = _payload_metadata(job.payload)

        try:
            source_hash = _hash_file(source_path)
            existing_document = get_document_by_source_hash(
                self.storage_paths.database, source_hash
            )
            if existing_document is not None:
                return

            source_copy_path = _copy_source_pdf(self.storage_paths, source_path, source_hash)
            normalized_path = self.storage_paths.ocr_pdfs / f"{source_hash}.pdf"
            self.ocr_runner.normalize_pdf(source_copy_path, normalized_path)

            pages = self.text_extractor.extract_pages(normalized_path)
            chunks = self.chunker.chunk_pages(pages)
            chunk_embeddings = self.embedding_provider.embed_chunks(
                [chunk.text for chunk in chunks]
            )
            if len(chunk_embeddings) != len(chunks):
                raise IngestError(
                    f"Embedded {len(chunk_embeddings)} chunks for {len(chunks)} chunk drafts"
                )

            document_id = f"document-{uuid.uuid4().hex[:8]}"
            document_metadata = _build_document_metadata(metadata, source_path, source_copy_path)
            page_rows = _build_page_rows(document_id, pages)
            chunk_rows, vector_rows = _build_chunk_and_vector_rows(
                document_id,
                chunks,
                chunk_embeddings,
            )

            self.vector_store.add_chunks(vector_rows)
            _persist_document_bundle(
                self.storage_paths.database,
                document_id=document_id,
                source_path=source_path,
                title=_resolve_document_title(metadata, source_path),
                source_hash=source_hash,
                normalized_path=normalized_path,
                metadata=document_metadata,
                pages=page_rows,
                chunks=chunk_rows,
                chunk_embeddings=chunk_embeddings,
            )
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(f"Failed ingesting {source_path}: {exc}") from exc


def enqueue_ingest_jobs(
    database_path: Path,
    *,
    source_path: Path,
    metadata: dict[str, Any] | None = None,
) -> list[Job]:
    """Enqueue local-PDF ingestion jobs for one file or directory."""

    payload_metadata = dict(metadata or {})
    jobs: list[Job] = []
    for pdf_path in _iter_pdf_inputs(source_path):
        jobs.append(
            create_job(
                database_path,
                kind=INGEST_JOB_KIND,
                payload={
                    "path": str(pdf_path),
                    "metadata": payload_metadata,
                    "source": "cli",
                },
            )
        )
    return jobs


def build_ingest_handler(
    *,
    data_dir: Path,
    embedding_config: EmbeddingConfig,
    ocr_runner: OcrRunner | None = None,
    text_extractor: TextExtractor | None = None,
    chunker: Chunker | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
) -> Callable[[Job], Awaitable[None]]:
    """Build an async handler for local-PDF ingest jobs."""

    pipeline = IngestionPipeline(
        storage_paths=initialize_storage(data_dir),
        embedding_config=embedding_config,
        ocr_runner=ocr_runner,
        text_extractor=text_extractor,
        chunker=chunker,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
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
            SELECT id, document_id, page_number, text, created_at
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
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


def list_chunks(database_path: Path) -> list[ChunkRecord]:
    """List durable chunks ordered by page span."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, document_id, page_start, page_end, text, created_at
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


def _iter_pdf_inputs(source_path: Path) -> tuple[Path, ...]:
    resolved_path = source_path.expanduser().resolve()
    if not resolved_path.exists():
        raise IngestError(f"Input path does not exist: {resolved_path}")

    if resolved_path.is_file():
        if resolved_path.suffix.lower() != ".pdf":
            raise IngestError(f"Input file is not a PDF: {resolved_path}")
        return (resolved_path,)

    pdf_paths = tuple(
        sorted(
            candidate.resolve()
            for candidate in resolved_path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() == ".pdf"
        )
    )
    if pdf_paths:
        return pdf_paths

    raise IngestError(f"No PDF files found under {resolved_path}")


def _resolve_embedding_config(embedding_config: EmbeddingConfig) -> EmbeddingConfig:
    if embedding_config.provider is not None:
        return embedding_config

    return EmbeddingConfig(
        provider=DEFAULT_EMBEDDING_PROVIDER,
        base_url=embedding_config.base_url,
        model=embedding_config.model,
        api_key_env=embedding_config.api_key_env,
    )


def _payload_path(payload: dict[str, Any]) -> Path:
    raw_path = payload.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise IngestError("Ingest job payload is missing a valid path")

    source_path = Path(raw_path).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise IngestError(f"Source PDF not found: {source_path}")
    if source_path.suffix.lower() != ".pdf":
        raise IngestError(f"Source file is not a PDF: {source_path}")
    return source_path


def _payload_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_source_pdf(storage_paths: StoragePaths, source_path: Path, source_hash: str) -> Path:
    destination = storage_paths.source_pdfs / f"{source_hash}-{source_path.name}"
    if source_path.resolve() == destination.resolve():
        return source_path

    shutil.copy2(source_path, destination)
    return destination


def _build_document_metadata(
    metadata: dict[str, Any],
    source_path: Path,
    stored_source_path: Path,
) -> dict[str, Any]:
    source_stat = source_path.stat()
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


def _build_page_rows(
    document_id: str, pages: Sequence[ExtractedPage]
) -> list[tuple[str, str, int, str]]:
    return [
        (f"page-{uuid.uuid4().hex[:8]}", document_id, page.page_number, page.text) for page in pages
    ]


def _build_chunk_and_vector_rows(
    document_id: str,
    chunks: Sequence[ChunkDraft],
    embeddings: Sequence[ChunkEmbedding],
) -> tuple[list[tuple[str, str, int, int, str]], list[ChunkVectorRecord]]:
    chunk_rows: list[tuple[str, str, int, int, str]] = []
    vector_rows: list[ChunkVectorRecord] = []

    for chunk, embedding in zip(chunks, embeddings, strict=True):
        chunk_id = f"chunk-{uuid.uuid4().hex[:8]}"
        chunk_rows.append((chunk_id, document_id, chunk.page_start, chunk.page_end, chunk.text))
        vector_rows.append(
            ChunkVectorRecord(
                chunk_id=chunk_id,
                document_id=document_id,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                vector=embedding.vector,
                metadata=embedding.metadata,
            )
        )

    return chunk_rows, vector_rows


def _persist_document_bundle(
    database_path: Path,
    *,
    document_id: str,
    source_path: Path,
    title: str,
    source_hash: str,
    normalized_path: Path,
    metadata: dict[str, Any],
    pages: Sequence[tuple[str, str, int, str]],
    chunks: Sequence[tuple[str, str, int, int, str]],
    chunk_embeddings: Sequence[ChunkEmbedding],
) -> None:
    metadata_json = json.dumps(metadata, sort_keys=True)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO documents(
                id,
                source_path,
                source_url,
                title,
                source_hash,
                normalized_path,
                metadata_json
            )
            VALUES(?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                document_id,
                str(source_path),
                title,
                source_hash,
                str(normalized_path),
                metadata_json,
            ),
        )
        connection.executemany(
            """
            INSERT INTO pages(id, document_id, page_number, text)
            VALUES(?, ?, ?, ?)
            """,
            pages,
        )
        connection.executemany(
            """
            INSERT INTO chunks(id, document_id, page_start, page_end, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            chunks,
        )
        connection.commit()

    for chunk_row, embedding in zip(chunks, chunk_embeddings, strict=True):
        create_embedding_record(
            database_path,
            source_kind="chunk",
            source_key=chunk_row[0],
            embedding=embedding,
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

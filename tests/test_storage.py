from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import lancedb  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.revisions import publish_revision
from newsrag.storage import (
    REQUIRED_TABLES,
    StorageError,
    _existing_tables,
    build_storage_paths,
    initialize_storage,
)
from newsrag.watches import add_watch

runner = CliRunner()


def test_initialize_storage_creates_layout_and_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    paths = initialize_storage(data_dir)

    assert paths.data_dir == data_dir
    assert paths.source_pdfs.is_dir()
    assert paths.downloaded_pdfs.is_dir()
    assert paths.ocr_pdfs.is_dir()
    assert paths.lancedb.is_dir()
    assert paths.logs.is_dir()
    assert paths.artifacts.is_dir()
    assert paths.source_artifacts.is_dir()
    assert paths.artifact_staging.is_dir()
    assert paths.database.is_file()
    assert REQUIRED_TABLES.issubset(_existing_tables(paths.database))


def test_initialize_storage_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    first_paths = initialize_storage(data_dir)
    second_paths = initialize_storage(data_dir)

    assert first_paths == second_paths
    assert REQUIRED_TABLES == REQUIRED_TABLES.intersection(_existing_tables(second_paths.database))


def test_schema_upgrade_resets_only_regenerable_discovery_data(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-1', 'source-1', 'application/pdf', 'hash-1',
                '/tmp/report.pdf', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, source_path, title, source_hash, metadata_json, artifact_id
            )
            VALUES(
                'document-1', '/tmp/report.pdf', 'Report', 'hash-1', '{}', 'artifact-1'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, human_label,
                normalized_text, extractor
            )
            VALUES(
                'unit-1', 'artifact-1', 'document-1', 1, 'page', 'p. 1',
                'quote', 'pdf-text'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO chunks(
                id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, text
            )
            VALUES('chunk-1', 'document-1', 1, 1, 'unit-1', 'unit-1', 'quote')
            """
        )
        connection.execute(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, ordinal, text
            )
            VALUES(
                'passage-1', 'chunk-1', 'document-1', 1, 1,
                'unit-1', 'unit-1', 1, 'quote'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO embedding_records(
                id, source_kind, source_key, provider, model, version, dimensions,
                source_unit_start_id, source_unit_end_id
            )
            VALUES(
                'embedding-1', 'passage', 'passage-1', 'test', 'test', '1', 1,
                'unit-1', 'unit-1'
            )
            """
        )
        connection.execute("INSERT INTO chunks_fts(chunk_id, text) VALUES('chunk-1', 'quote')")
        connection.execute(
            "INSERT INTO passages_fts(passage_id, text) VALUES('passage-1', 'quote')"
        )
        connection.execute(
            """
            INSERT INTO document_profiles(
                id, document_id, source_type, extent_type, extent_count, text_length,
                extraction_quality_json, extractor
            )
            VALUES('profile-1', 'document-1', 'pdf', 'pages', 1, 10, '{}', 'rules')
            """
        )
        connection.execute(
            """
            INSERT INTO document_briefs(id, document_id, summary, extractor)
            VALUES('brief-1', 'document-1', 'summary', 'rules')
            """
        )
        connection.execute(
            """
            INSERT INTO discovery_items(id, document_id, item_type, label, extractor)
            VALUES('item-1', 'document-1', 'topic', 'budget', 'rules')
            """
        )
        connection.execute(
            """
            INSERT INTO discovery_evidence(
                id, item_id, document_id, source_unit_start_id, source_unit_end_id,
                location_type, location_label, quote, validation_status
            )
            VALUES(
                'evidence-1', 'item-1', 'document-1', 'unit-1', 'unit-1',
                'page', 'p. 1', 'quote', 'validated'
            )
            """
        )
        connection.execute(
            "INSERT INTO document_briefs_fts(brief_id, summary) VALUES('brief-1', 'summary')"
        )
        connection.execute(
            "INSERT INTO discovery_items_fts(item_id, label) VALUES('item-1', 'budget')"
        )
        connection.execute("UPDATE metadata SET value = '4' WHERE key = 'schema_version'")
        connection.commit()

    initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        preserved_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "source_artifacts",
                "documents",
                "source_units",
                "chunks",
                "chunks_fts",
                "passages",
                "passages_fts",
                "embedding_records",
            )
        }
        derived_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "document_profiles",
                "document_briefs",
                "discovery_items",
                "discovery_evidence",
                "document_briefs_fts",
                "discovery_items_fts",
            )
        }
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]

    assert preserved_counts == {
        "sources": 1,
        "source_artifacts": 1,
        "documents": 1,
        "source_units": 1,
        "chunks": 1,
        "chunks_fts": 1,
        "passages": 1,
        "passages_fts": 1,
        "embedding_records": 1,
    }
    assert derived_counts == {
        "document_profiles": 0,
        "document_briefs": 0,
        "discovery_items": 0,
        "discovery_evidence": 0,
        "document_briefs_fts": 0,
        "discovery_items_fts": 0,
    }
    assert schema_version == "6"


def test_initialize_storage_backfills_passages_from_existing_chunks(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                "document-1",
                "/tmp/report.pdf",
                "Report",
                "hash-1",
                "/tmp/report-ocr.pdf",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES('page-1', 'document-1', 10, 'City agenda', 'pymupdf')
            """
        )
        connection.execute(
            """
            INSERT INTO chunks(id, document_id, page_start, page_end, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "chunk-1",
                "document-1",
                10,
                10,
                "City Manager's Report\nMay 1, 2026\nPage 10 of 16\n\n• Paperbacks & Playdates Book Club – A low-pressure book club.\n\n• Brown Bag Book Club – Bring your own lunch.",
            ),
        )
        connection.commit()

    initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        passage_rows = connection.execute(
            """
            SELECT id, text, source_unit_start_id, source_unit_end_id
            FROM passages
            ORDER BY ordinal ASC
            """
        ).fetchall()
        fts_rows = connection.execute(
            "SELECT passage_id, text FROM passages_fts ORDER BY passage_id ASC"
        ).fetchall()

    assert len(passage_rows) == 2
    assert "Paperbacks & Playdates Book Club" in passage_rows[0][1]
    assert "Brown Bag Book Club" in passage_rows[1][1]
    assert passage_rows[0][2] is not None
    assert passage_rows[0][2] == passage_rows[0][3]
    assert passage_rows[1][2] == passage_rows[0][2]
    assert passage_rows[1][3] == passage_rows[0][3]
    assert len(fts_rows) == 2


def test_initialize_storage_migrates_pdf_records_to_source_units(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "report.pdf"
    source_bytes = b"%PDF-1.4\nlegacy"
    source_pdf.write_bytes(source_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    paths = build_storage_paths(data_dir)
    paths.data_dir.mkdir(parents=True)
    paths.lancedb.mkdir()

    with sqlite3.connect(paths.database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata(key, value) VALUES('schema_version', '1');

            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                source_path TEXT,
                source_url TEXT,
                title TEXT,
                source_hash TEXT,
                normalized_path TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE pages (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                extractor TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page_start INTEGER NOT NULL,
                page_end INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE passages (
                id TEXT PRIMARY KEY,
                chunk_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                page_start INTEGER NOT NULL,
                page_end INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE embedding_records (
                id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                version TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                "document-1",
                str(source_pdf),
                "Report",
                source_hash,
                "/tmp/report-ocr.pdf",
                json.dumps(
                    {
                        "source_size_bytes": len(source_bytes),
                        "stored_source_path": str(source_pdf),
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            ("page-1", "document-1", 1, "Council agenda", "pymupdf"),
        )
        connection.execute(
            """
            INSERT INTO chunks(id, document_id, page_start, page_end, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            ("chunk-1", "document-1", 1, 1, "Council agenda"),
        )
        connection.execute(
            """
            INSERT INTO passages(id, chunk_id, document_id, page_start, page_end, ordinal, text)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            ("passage-1", "chunk-1", "document-1", 1, 1, 1, "Council agenda"),
        )
        connection.execute(
            """
            INSERT INTO embedding_records(
                id, source_kind, source_key, provider, model, version, dimensions
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            ("embedding-1", "passage", "passage-1", "test", "model", "v1", 2),
        )
        connection.commit()

    lance_database = lancedb.connect(paths.lancedb)
    lance_database.create_table(
        "chunk_embeddings",
        data=[
            {
                "chunk_id": "chunk-1",
                "document_id": "document-1",
                "page_start": 1,
                "page_end": 1,
                "text": "Council agenda",
                "vector": [0.1, 0.2],
            }
        ],
    )
    lance_database.create_table(
        "passage_embeddings",
        data=[
            {
                "passage_id": "passage-1",
                "document_id": "document-1",
                "page_start": 1,
                "page_end": 1,
                "text": "Council agenda",
                "vector": [0.1, 0.2],
            }
        ],
    )

    initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute("SELECT * FROM sources").fetchone()
        artifact = connection.execute("SELECT * FROM source_artifacts").fetchone()
        document = connection.execute(
            "SELECT artifact_id FROM documents WHERE id = 'document-1'"
        ).fetchone()
        unit = connection.execute("SELECT * FROM source_units").fetchone()
        page = connection.execute("SELECT source_unit_id FROM pages WHERE id = 'page-1'").fetchone()
        chunk = connection.execute(
            "SELECT source_unit_start_id, source_unit_end_id FROM chunks WHERE id = 'chunk-1'"
        ).fetchone()
        passage = connection.execute(
            "SELECT source_unit_start_id, source_unit_end_id FROM passages WHERE id = 'passage-1'"
        ).fetchone()
        embedding = connection.execute(
            """
            SELECT source_unit_start_id, source_unit_end_id
            FROM embedding_records
            WHERE id = 'embedding-1'
            """
        ).fetchone()
    chunk_vector = lance_database.open_table("chunk_embeddings").to_arrow().to_pylist()[0]
    passage_vector = lance_database.open_table("passage_embeddings").to_arrow().to_pylist()[0]

    assert source is not None
    assert source["kind"] == "local_path"
    assert source["submitted_reference"] == str(source_pdf)
    assert source["normalized_reference"] == str(source_pdf.resolve())
    assert artifact is not None
    assert artifact["source_id"] == source["id"]
    assert artifact["media_type"] == "application/pdf"
    assert artifact["byte_size"] == len(source_bytes)
    assert artifact["content_hash"] == source_hash
    assert artifact["stored_path"] == str(source_pdf)
    assert artifact["state"] == "published"
    assert artifact["reported_media_type"] is None
    assert artifact["provenance_json"] == "{}"
    assert document is not None
    assert document["artifact_id"] == artifact["id"]
    assert unit is not None
    assert unit["artifact_id"] == artifact["id"]
    assert unit["document_id"] == "document-1"
    assert unit["ordinal"] == 1
    assert unit["location_type"] == "page"
    assert json.loads(unit["location_json"]) == {"page_number": 1}
    assert unit["human_label"] == "p. 1"
    assert unit["normalized_text"] == "Council agenda"
    assert unit["extractor"] == "pymupdf"
    assert page is not None
    assert page["source_unit_id"] == unit["id"]
    assert chunk is not None
    assert tuple(chunk) == (unit["id"], unit["id"])
    assert passage is not None
    assert tuple(passage) == (unit["id"], unit["id"])
    assert embedding is not None
    assert tuple(embedding) == (unit["id"], unit["id"])
    assert chunk_vector["source_unit_start_id"] == unit["id"]
    assert chunk_vector["source_unit_end_id"] == unit["id"]
    assert passage_vector["source_unit_start_id"] == unit["id"]
    assert passage_vector["source_unit_end_id"] == unit["id"]


def test_initialize_storage_consolidates_legacy_exact_hash_documents(
    tmp_path: Path,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        connection.execute("DROP INDEX idx_documents_unique_artifact")
        connection.execute("UPDATE metadata SET value = '2' WHERE key = 'schema_version'")
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/packet.pdf', '/tmp/packet.pdf')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at
            )
            VALUES(
                'artifact-1', 'source-1', 'application/pdf', 10, 'same-hash',
                '/tmp/packet.pdf', CURRENT_TIMESTAMP
            )
            """
        )
        for suffix in ("a", "b"):
            document_id = f"document-{suffix}"
            unit_id = f"unit-{suffix}"
            chunk_id = f"chunk-{suffix}"
            passage_id = f"passage-{suffix}"
            connection.execute(
                """
                INSERT INTO documents(id, source_path, title, source_hash, metadata_json, artifact_id)
                VALUES(?, '/tmp/packet.pdf', ?, 'same-hash', '{}', 'artifact-1')
                """,
                (document_id, f"Packet {suffix.upper()}"),
            )
            connection.execute(
                """
                INSERT INTO source_units(
                    id, artifact_id, document_id, ordinal, location_type, location_json,
                    human_label, normalized_text, structure_json, extractor
                )
                VALUES(?, 'artifact-1', ?, 1, 'page', '{"page_number": 1}', 'p. 1', ?, '{}', 'test')
                """,
                (unit_id, document_id, f"Agenda {suffix}"),
            )
            connection.execute(
                """
                INSERT INTO pages(id, document_id, page_number, source_unit_id, text, extractor)
                VALUES(?, ?, 1, ?, ?, 'test')
                """,
                (f"page-{suffix}", document_id, unit_id, f"Agenda {suffix}"),
            )
            connection.execute(
                """
                INSERT INTO chunks(
                    id, document_id, page_start, page_end, source_unit_start_id,
                    source_unit_end_id, text
                )
                VALUES(?, ?, 1, 1, ?, ?, ?)
                """,
                (chunk_id, document_id, unit_id, unit_id, f"Agenda {suffix}"),
            )
            connection.execute(
                "INSERT INTO chunks_fts(chunk_id, text) VALUES(?, ?)",
                (chunk_id, f"Agenda {suffix}"),
            )
            connection.execute(
                """
                INSERT INTO passages(
                    id, chunk_id, document_id, page_start, page_end, source_unit_start_id,
                    source_unit_end_id, ordinal, text
                )
                VALUES(?, ?, ?, 1, 1, ?, ?, 1, ?)
                """,
                (passage_id, chunk_id, document_id, unit_id, unit_id, f"Agenda {suffix}"),
            )
            connection.execute(
                "INSERT INTO passages_fts(passage_id, text) VALUES(?, ?)",
                (passage_id, f"Agenda {suffix}"),
            )
            connection.execute(
                """
                INSERT INTO embedding_records(
                    id, source_kind, source_key, provider, model, version, dimensions,
                    source_unit_start_id, source_unit_end_id
                )
                VALUES(?, 'passage', ?, 'test', 'test', 'v1', 2, ?, ?)
                """,
                (f"embedding-{suffix}", passage_id, unit_id, unit_id),
            )
        connection.commit()

    lance_database = lancedb.connect(paths.lancedb)
    for table_name, key_name, key_prefix in (
        ("chunk_embeddings", "chunk_id", "chunk"),
        ("passage_embeddings", "passage_id", "passage"),
    ):
        lance_database.create_table(
            table_name,
            data=[
                {
                    key_name: f"{key_prefix}-{suffix}",
                    "document_id": f"document-{suffix}",
                    "vector": [0.1, 0.2],
                }
                for suffix in ("a", "b")
            ],
        )

    initialize_storage(paths.data_dir)

    with sqlite3.connect(paths.database) as connection:
        documents = connection.execute("SELECT id FROM documents ORDER BY id").fetchall()
        units = connection.execute("SELECT document_id FROM source_units").fetchall()
        pages = connection.execute("SELECT document_id FROM pages").fetchall()
        chunks = connection.execute("SELECT document_id FROM chunks").fetchall()
        passages = connection.execute("SELECT document_id FROM passages").fetchall()
        embeddings = connection.execute("SELECT source_key FROM embedding_records").fetchall()
        chunk_fts = connection.execute("SELECT chunk_id FROM chunks_fts").fetchall()
        passage_fts = connection.execute("SELECT passage_id FROM passages_fts").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO documents(id, source_hash, metadata_json, artifact_id)
                VALUES('document-c', 'same-hash', '{}', 'artifact-1')
                """
            )
    chunk_vectors = lance_database.open_table("chunk_embeddings").to_arrow().to_pylist()
    passage_vectors = lance_database.open_table("passage_embeddings").to_arrow().to_pylist()

    assert documents == [("document-a",)]
    assert units == [("document-a",)]
    assert pages == [("document-a",)]
    assert chunks == [("document-a",)]
    assert passages == [("document-a",)]
    assert embeddings == [("passage-a",)]
    assert chunk_fts == [("chunk-a",)]
    assert passage_fts == [("passage-a",)]
    assert [row["document_id"] for row in chunk_vectors] == ["document-a"]
    assert [row["document_id"] for row in passage_vectors] == ["document-a"]


def test_source_unit_locations_can_represent_non_page_units(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")

    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-html', 'url', 'https://example.test/update', 'https://example.test/update')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at
            )
            VALUES('artifact-html', 'source-html', 'text/html', 10, 'hash-html', '/tmp/update.html', CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, title, source_hash, metadata_json, artifact_id)
            VALUES('document-html', 'Update', 'hash-html', '{}', 'artifact-html')
            """
        )
        connection.execute(
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
                extractor
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "source-unit-html-1",
                "artifact-html",
                "document-html",
                1,
                "html_block",
                json.dumps({"block_number": 1, "heading_path": ["Budget"]}),
                "Budget — block 1",
                "Proposed budget",
                json.dumps({"content_kind": "paragraph"}),
                "static-html-v1",
            ),
        )
        connection.commit()

        row = connection.execute(
            """
            SELECT location_type, location_json, human_label, structure_json
            FROM source_units
            WHERE id = 'source-unit-html-1'
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "html_block"
    assert json.loads(row[1]) == {"block_number": 1, "heading_path": ["Budget"]}
    assert row[2] == "Budget — block 1"
    assert json.loads(row[3]) == {"content_kind": "paragraph"}


def test_initialize_storage_migrates_revision_and_legacy_metadata(
    tmp_path: Path,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-1', 'source-1', 'application/pdf', 'hash-1',
                '/tmp/report.pdf', '2026-01-02 03:04:05', 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, title, metadata_json, artifact_id, created_at
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "document-1",
                "Budget Packet",
                json.dumps(
                    {
                        "body": "City Council",
                        "meeting_date": "2026-01-01",
                        "source_filename": "report.pdf",
                        "source_size_bytes": 100,
                        "source_mtime_ns": 123,
                        "stored_source_path": "/tmp/report.pdf",
                        "retrieved_at": "2026-01-02T03:04:05Z",
                    }
                ),
                "artifact-1",
                "2026-01-02 03:04:06",
            ),
        )
        connection.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")
        connection.commit()

    initialize_storage(paths.data_dir)
    initialize_storage(paths.data_dir)

    with sqlite3.connect(paths.database) as connection:
        connection.row_factory = sqlite3.Row
        document = connection.execute(
            """
            SELECT user_metadata_json, user_metadata_origin, ingestion_options_json
            FROM documents WHERE id = 'document-1'
            """
        ).fetchone()
        revision = connection.execute(
            "SELECT * FROM source_revisions WHERE document_id = 'document-1'"
        ).fetchone()
        source = connection.execute(
            """
            SELECT current_revision_id, publication_generation
            FROM sources WHERE id = 'source-1'
            """
        ).fetchone()
        revision_count = connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]

    assert document is not None
    assert json.loads(document["user_metadata_json"]) == {
        "body": "City Council",
        "meeting_date": "2026-01-01",
        "title": "Budget Packet",
    }
    assert document["user_metadata_origin"] == "legacy"
    assert document["ingestion_options_json"] == "{}"
    assert revision is not None
    assert revision["source_id"] == "source-1"
    assert revision["revision_number"] == 1
    assert revision["published_at"] == "2026-01-02 03:04:06"
    assert revision["job_id"] is None
    assert source is not None
    assert tuple(source) == (revision["id"], 1)
    assert revision_count == 1


def test_initialize_storage_rejects_ambiguous_legacy_source_history(
    tmp_path: Path,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
            """
        )
        for number in (1, 2):
            connection.execute(
                """
                INSERT INTO source_artifacts(
                    id, source_id, media_type, content_hash, stored_path, acquired_at, state
                )
                VALUES(?, 'source-1', 'application/pdf', ?, ?, CURRENT_TIMESTAMP, 'published')
                """,
                (f"artifact-{number}", f"hash-{number}", f"/tmp/report-{number}.pdf"),
            )
            connection.execute(
                """
                INSERT INTO documents(id, metadata_json, artifact_id)
                VALUES(?, '{}', ?)
                """,
                (f"document-{number}", f"artifact-{number}"),
            )
        connection.commit()

    with pytest.raises(StorageError, match="multiple published documents are ambiguous"):
        initialize_storage(paths.data_dir)

    with sqlite3.connect(paths.database) as connection:
        document_ids = connection.execute("SELECT id FROM documents ORDER BY id").fetchall()
        revision_count = connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]
    assert document_ids == [("document-1",), ("document-2",)]
    assert revision_count == 0


def test_schema_six_reinitialization_preserves_revision_history(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
            """
        )
        for number in (1, 2):
            connection.execute(
                """
                INSERT INTO source_artifacts(
                    id, source_id, media_type, content_hash, stored_path, acquired_at, state
                )
                VALUES(?, 'source-1', 'application/pdf', ?, ?, CURRENT_TIMESTAMP, 'published')
                """,
                (f"artifact-{number}", f"hash-{number}", f"/tmp/report-{number}.pdf"),
            )
            connection.execute(
                """
                INSERT INTO documents(id, metadata_json, artifact_id)
                VALUES(?, '{}', ?)
                """,
                (f"document-{number}", f"artifact-{number}"),
            )
            if number == 1:
                publish_revision(connection, document_id="document-1", job_id=None)
            else:
                publish_revision(
                    connection,
                    document_id="document-2",
                    job_id="job-2",
                    expected_generation=1,
                )

    initialize_storage(paths.data_dir)

    with sqlite3.connect(paths.database) as connection:
        documents = connection.execute("SELECT id FROM documents ORDER BY id").fetchall()
        revisions = connection.execute(
            """
            SELECT document_id, revision_number
            FROM source_revisions ORDER BY revision_number
            """
        ).fetchall()
        source = connection.execute(
            """
            SELECT current_revision_id, publication_generation
            FROM sources WHERE id = 'source-1'
            """
        ).fetchone()
        current_document = connection.execute(
            "SELECT document_id FROM source_revisions WHERE id = ?", (source[0],)
        ).fetchone()[0]

    assert documents == [("document-1",), ("document-2",)]
    assert revisions == [("document-1", 1), ("document-2", 2)]
    assert source[1] == 2
    assert current_document == "document-2"


def test_initialize_storage_rejects_invalid_revision_ownership(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        for number in (1, 2):
            connection.execute(
                """
                INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
                VALUES(?, 'local_path', ?, ?)
                """,
                (
                    f"source-{number}",
                    f"/tmp/report-{number}.pdf",
                    f"/tmp/report-{number}.pdf",
                ),
            )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-1', 'source-1', 'application/pdf', 'hash-1',
                '/tmp/report-1.pdf', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, metadata_json, artifact_id)
            VALUES('document-1', '{}', 'artifact-1')
            """
        )
        connection.execute(
            """
            INSERT INTO source_revisions(
                id, source_id, document_id, revision_number, published_at
            )
            VALUES(
                'revision-bad', 'source-2', 'document-1', 1, CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()

    with pytest.raises(StorageError, match="Invalid revision ownership.*revision-bad"):
        initialize_storage(paths.data_dir)


def test_initialize_storage_rejects_file_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-a-directory"
    data_dir.write_text("x", encoding="utf-8")

    with pytest.raises(StorageError):
        initialize_storage(data_dir)


def test_initialize_storage_rejects_unwritable_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "nested" / ".newsrag"

    def fake_access(path: object, mode: int) -> bool:
        del mode
        candidate = Path(str(path))
        if candidate == tmp_path:
            return False
        return True

    monkeypatch.setattr("newsrag.storage.os.access", fake_access)

    with pytest.raises(StorageError):
        initialize_storage(data_dir)


def test_status_command_reports_storage_health(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    first_result = runner.invoke(app, ["--data-dir", str(data_dir), "status"])
    second_result = runner.invoke(app, ["--data-dir", str(data_dir), "status", "--initialize"])
    third_result = runner.invoke(app, ["--data-dir", str(data_dir), "status"])

    assert first_result.exit_code == 0
    assert "NewsRAG Status" in first_result.stdout
    assert "summary: warn" in first_result.stdout
    assert f"data_dir: {data_dir}" in first_result.stdout

    assert second_result.exit_code == 0
    assert "summary: ok" in second_result.stdout

    assert third_result.exit_code == 0
    assert "database: ok" in third_result.stdout
    assert "source_pdfs: ok" in third_result.stdout
    assert "jobs: ok" in third_result.stdout
    assert "watcher: ok" in third_result.stdout
    assert "summary: ok" in third_result.stdout


def test_status_reports_actionable_watcher_health_failures(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    missing_watch_dir = tmp_path / "missing"
    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=missing_watch_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "status"])

    assert result.exit_code == 0
    assert "watcher: warn" in result.stdout
    assert "missing watched folder" in result.stdout
    assert "remove/re-add the watch" in result.stdout
    assert "summary: warn" in result.stdout

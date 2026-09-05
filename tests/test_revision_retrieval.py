from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.discovery_browse import TOPIC_ITEM_TYPES, list_browse_items
from newsrag.documents import get_document_detail, get_document_versions
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.jobs import Job
from newsrag.packets import load_packet_source_provenance
from newsrag.search import (
    PassageVectorRecord,
    SearchCandidate,
    SearchEngine,
    format_search_results,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


@dataclass(frozen=True)
class _EmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        provider="test",
        model="test-model",
        version="1",
    )

    def embed_query(self, text: str) -> QueryEmbedding:
        return QueryEmbedding(text=text, vector=(0.1, 0.2), metadata=self.metadata)

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        raise AssertionError(f"published fixture embeddings should exist: {texts}")


@dataclass
class _VectorStore:
    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        raise AssertionError(f"published fixture embeddings should exist: {passages}")


@dataclass
class _VectorSearcher:
    candidates: list[SearchCandidate]
    before_search: Callable[[], None] | None = None
    limits: list[int] | None = None

    def search(self, query_embedding: QueryEmbedding, *, limit: int) -> list[SearchCandidate]:
        del query_embedding
        if self.limits is not None:
            self.limits.append(limit)
        if self.before_search is not None:
            callback = self.before_search
            self.before_search = None
            callback()
        return self.candidates[:limit]


def test_current_scope_excludes_history_and_unpublished_candidates(tmp_path: Path) -> None:
    database_path = _seed_revision_corpus(tmp_path)
    engine = _engine(database_path, _VectorSearcher([]))

    current = engine.search("shared evidence", limit=50)
    history = engine.search("shared evidence", limit=50, include_history=True)

    assert {result.document_id for result in current} == {"document-current"}
    assert {result.document_id for result in history} == {
        "document-current",
        "document-old",
    }
    assert all(result.document_id != "document-staged" for result in history)
    old_result = next(result for result in history if result.document_id == "document-old")
    assert old_result.source_id == "source-history"
    assert old_result.revision_id == "revision-old"
    assert old_result.revision_number == 1
    assert old_result.is_current_snapshot is False
    output = format_search_results(history, include_history=True)
    assert "revision: 1 (historical)" in output
    assert "revision: 2 (current)" in output


def test_historical_vector_hits_cannot_starve_current_candidates(tmp_path: Path) -> None:
    database_path = _seed_revision_corpus(tmp_path)
    limits: list[int] = []
    historical_candidates = [
        SearchCandidate(
            passage_id=f"passage-old-{index}",
            document_id="document-old",
            page_start=1,
            page_end=1,
            text=f"historical semantic evidence {index}",
            title=None,
            meeting_date=None,
            vector_score=float(index) / 100,
        )
        for index in range(25)
    ]
    current_candidate = SearchCandidate(
        passage_id="passage-current",
        document_id="document-current",
        page_start=1,
        page_end=1,
        text="current semantic evidence",
        title=None,
        meeting_date=None,
        vector_score=0.9,
    )
    engine = _engine(
        database_path,
        _VectorSearcher(historical_candidates + [current_candidate], limits=limits),
    )

    results = engine.search("semantic-only", limit=5)

    assert [result.document_id for result in results] == ["document-current"]
    assert limits == [20, 40]


def test_search_snapshot_survives_concurrent_current_pointer_change(tmp_path: Path) -> None:
    database_path = _seed_revision_corpus(tmp_path)

    def reactivate_old_revision() -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                UPDATE sources
                SET current_revision_id = 'revision-old',
                    publication_generation = publication_generation + 1
                WHERE id = 'source-history'
                """
            )

    vector = SearchCandidate(
        passage_id="passage-current",
        document_id="document-current",
        page_start=1,
        page_end=1,
        text="current semantic evidence",
        title=None,
        meeting_date=None,
        vector_score=0.1,
    )
    engine = _engine(
        database_path,
        _VectorSearcher([vector], before_search=reactivate_old_revision),
    )

    results = engine.search("semantic-only")

    assert [result.document_id for result in results] == ["document-current"]
    assert results[0].revision_id == "revision-current"
    assert results[0].is_current_snapshot is True
    with sqlite3.connect(database_path) as connection:
        pointer = connection.execute(
            "SELECT current_revision_id FROM sources WHERE id = 'source-history'"
        ).fetchone()
    assert pointer == ("revision-old",)


def test_packet_provenance_uses_exact_selected_revision_after_pointer_change(
    tmp_path: Path,
) -> None:
    database_path = _seed_revision_corpus(tmp_path)
    engine = _engine(database_path, _VectorSearcher([]))
    result = engine.search("shared evidence")[0]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE sources SET current_revision_id = 'revision-old' WHERE id = 'source-history'"
        )

    provenance = load_packet_source_provenance(database_path, [result])[result.document_id]

    assert provenance.source_id == "source-history"
    assert provenance.revision_id == "revision-current"
    assert provenance.revision_number == 2
    assert provenance.is_current_snapshot is True
    assert provenance.artifact_id == "artifact-current"
    assert provenance.artifact_hash == "hash-current"
    assert provenance.acquired_at == "2026-02-01T00:00:00+00:00"


def test_document_inventory_resolves_historical_detail_and_ordered_versions(
    tmp_path: Path,
) -> None:
    database_path = _seed_revision_corpus(tmp_path)

    detail = get_document_detail(database_path, "document-old")
    history = get_document_versions(database_path, "document-old")

    assert detail.revision_id == "revision-old"
    assert detail.is_current is False
    assert [version.document_id for version in history.versions] == [
        "document-old",
        "document-current",
    ]
    assert [version.revision_number for version in history.versions] == [1, 2]
    assert history.versions[0].artifact_hash == "hash-old"
    assert history.versions[1].is_current is True

    cli_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "documents",
            "versions",
            "document-old",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.stdout
    assert "revision 1 (historical)" in cli_result.stdout
    assert "revision 2 (current)" in cli_result.stdout
    assert "artifact_sha256=hash-old" in cli_result.stdout


def test_discovery_browse_defaults_current_and_can_include_history(tmp_path: Path) -> None:
    database_path = _seed_revision_corpus(tmp_path)

    current = list_browse_items(database_path, item_types=TOPIC_ITEM_TYPES)
    history = list_browse_items(
        database_path,
        item_types=TOPIC_ITEM_TYPES,
        include_history=True,
    )

    assert [item.item.id for item in current.items] == ["topic-current"]
    assert {item.item.id for item in history.items} == {"topic-current", "topic-old"}
    assert all(item.item.id != "topic-staged" for item in history.items)

    cli_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "topics",
            "list",
            "--include-history",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.stdout
    assert "revision=1 (historical)" in cli_result.stdout
    assert "revision=2 (current)" in cli_result.stdout


def test_jobs_list_explains_cross_source_refresh_duplicate(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO jobs(id, kind, status, payload_json, result_json)
            VALUES(
                'job-duplicate', 'refresh-source', 'done', '{}',
                '{"outcome": "duplicate_ignored", "requested_source_id": "source-requested", "document_id": "document-existing", "artifact_id": "artifact-existing"}'
            )
            """
        )

    result = runner.invoke(app, ["--data-dir", str(data_dir), "jobs", "list"])

    assert result.exit_code == 0, result.stdout
    line = next(line for line in result.stdout.splitlines() if "job-duplicate" in line)
    explanation = "no revision published for requested source source-requested"
    assert explanation in line
    assert line.index(explanation) < line.index("document_id=document-existing")


def test_refresh_command_enqueues_without_embedding_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".newsrag"
    expected_job = Job(
        id="job-refresh",
        kind="refresh-source",
        status="pending",
        payload={"source_id": "source-history"},
        result=None,
        error=None,
        created_at="2026-09-05T00:00:00+00:00",
        updated_at="2026-09-05T00:00:00+00:00",
    )
    monkeypatch.setattr("newsrag.refresh.enqueue_refresh", lambda **_: expected_job)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "refresh", "source-history"],
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == (
        "Enqueued refresh job job-refresh for source source-history; status=pending"
    )


def _engine(database_path: Path, vector_searcher: _VectorSearcher) -> SearchEngine:
    return SearchEngine(
        database_path=database_path,
        vector_searcher=vector_searcher,
        vector_store=_VectorStore(),
        embedding_provider=_EmbeddingProvider(),
    )


def _seed_revision_corpus(tmp_path: Path) -> Path:
    database_path = initialize_storage(tmp_path / ".newsrag").database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(
                id, kind, submitted_reference, normalized_reference,
                current_revision_id, publication_generation
            )
            VALUES(
                'source-history', 'local_path', '/tmp/source.pdf', '/tmp/source.pdf',
                NULL, 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path,
                acquired_at, state, provenance_json
            )
            VALUES(?, 'source-history', 'application/pdf', 10, ?, ?, ?, ?, '{}')
            """,
            (
                (
                    "artifact-old",
                    "hash-old",
                    "/tmp/old.pdf",
                    "2026-01-01T00:00:00+00:00",
                    "published",
                ),
                (
                    "artifact-current",
                    "hash-current",
                    "/tmp/current.pdf",
                    "2026-02-01T00:00:00+00:00",
                    "published",
                ),
                (
                    "artifact-staged",
                    "hash-staged",
                    "/tmp/staged.pdf",
                    "2026-03-01T00:00:00+00:00",
                    "processing",
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO documents(
                id, source_path, title, source_hash, metadata_json, artifact_id, created_at
            )
            VALUES(?, ?, 'Council Packet', ?, '{"body": "City Council"}', ?, ?)
            """,
            (
                (
                    "document-old",
                    "/tmp/old.pdf",
                    "hash-old",
                    "artifact-old",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "document-current",
                    "/tmp/current.pdf",
                    "hash-current",
                    "artifact-current",
                    "2026-02-01T00:00:00+00:00",
                ),
                (
                    "document-staged",
                    "/tmp/staged.pdf",
                    "hash-staged",
                    "artifact-staged",
                    "2026-03-01T00:00:00+00:00",
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO source_revisions(
                id, source_id, document_id, revision_number, published_at
            )
            VALUES(?, 'source-history', ?, ?, ?)
            """,
            (
                (
                    "revision-old",
                    "document-old",
                    1,
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "revision-current",
                    "document-current",
                    2,
                    "2026-02-01T00:00:00+00:00",
                ),
            ),
        )
        connection.execute(
            """
            UPDATE sources
            SET current_revision_id = 'revision-current', publication_generation = 2
            WHERE id = 'source-history'
            """
        )
        passage_rows = [
            (
                "passage-current",
                "chunk-current",
                "document-current",
                "shared evidence current revision",
            ),
            (
                "passage-staged",
                "chunk-staged",
                "document-staged",
                "shared evidence staged candidate",
            ),
        ] + [
            (
                f"passage-old-{index}",
                f"chunk-old-{index}",
                "document-old",
                f"shared evidence historical revision {index}",
            )
            for index in range(25)
        ]
        connection.executemany(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end, ordinal, text
            )
            VALUES(?, ?, ?, 1, 1, 1, ?)
            """,
            passage_rows,
        )
        connection.executemany(
            "INSERT INTO passages_fts(passage_id, text) VALUES(?, ?)",
            ((row[0], row[3]) for row in passage_rows),
        )
        connection.executemany(
            """
            INSERT INTO embedding_records(
                id, source_kind, source_key, provider, model, version, dimensions
            )
            VALUES(?, 'passage', ?, 'test', 'test-model', '1', 2)
            """,
            ((f"embedding-{row[0]}", row[0]) for row in passage_rows),
        )
        connection.executemany(
            """
            INSERT INTO discovery_items(
                id, document_id, item_type, label, summary, extractor
            )
            VALUES(?, ?, 'topic', ?, '', 'test')
            """,
            (
                ("topic-old", "document-old", "Old topic"),
                ("topic-current", "document-current", "Current topic"),
                ("topic-staged", "document-staged", "Staged topic"),
            ),
        )
        connection.commit()
    return database_path

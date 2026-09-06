from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import lancedb  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.discovery import DiscoveryEvidenceDraft, create_discovery_item
from newsrag.discovery_browse import TOPIC_ITEM_TYPES, list_browse_items
from newsrag.documents import (
    format_document_generations,
    get_document_detail,
    get_document_generations,
)
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.enrichment import EnrichmentRequest, enrich_document
from newsrag.facts import extract_document_facts
from newsrag.jobs import Job
from newsrag.packets import load_packet_source_provenance
from newsrag.search import (
    LanceDbPassageVectorSearcher,
    LanceDbPassageVectorStore,
    PassageVectorRecord,
    SearchCandidate,
    SearchEngine,
)
from newsrag.source_locations import resolve_source_range
from newsrag.storage import initialize_storage
from newsrag.vector_tables import add_vector_records, search_vector_records

runner = CliRunner()


@dataclass(frozen=True)
class _EmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata("test", "current-model", "1")

    def embed_query(self, text: str) -> QueryEmbedding:
        return QueryEmbedding(text, (0.0, 0.0), self.metadata)

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        raise AssertionError(f"explicit generations must not be re-embedded lazily: {texts}")


@dataclass
class _NoOpVectorStore:
    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        raise AssertionError(f"explicit generations must not be backfilled: {passages}")


@dataclass
class _ActiveGenerationEnrichmentProvider:
    name = "test"
    model = "test-model"

    def enrich(self, request: EnrichmentRequest) -> str:
        texts = {context.text for context in request.evidence_contexts}
        assert all("Old evidence" not in text for text in texts)
        assert any("Current evidence" in text for text in texts)
        return json.dumps(
            {
                "summary": "Current parks contract evidence.",
                "summary_evidence": [
                    {
                        "source_unit_start_id": "unit-new",
                        "quote": "Current evidence approved a $200 parks contract.",
                    }
                ],
                "notable_actions": [],
                "story_leads": [],
                "open_questions": [],
            }
        )


@dataclass
class _PointerChangingSearcher:
    database_path: Path

    def search(self, query_embedding: QueryEmbedding, *, limit: int) -> list[SearchCandidate]:
        del query_embedding, limit
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE documents
                SET current_processing_generation_id = 'generation-old'
                WHERE id = 'document-1'
                """
            )
        return []


def test_search_snapshots_active_generation_and_packet_keeps_exact_provenance(
    tmp_path: Path,
) -> None:
    database_path = _seed_reprocessed_document(tmp_path)
    engine = SearchEngine(
        database_path=database_path,
        vector_searcher=_PointerChangingSearcher(database_path),
        vector_store=_NoOpVectorStore(),
        embedding_provider=_EmbeddingProvider(),
    )

    results = engine.search("current evidence")

    assert [result.passage_id for result in results] == ["passage-new"]
    assert results[0].processing_generation_id == "generation-new"
    assert results[0].citation == "Council Update — Current — block 1"
    provenance = load_packet_source_provenance(database_path, results)["document-1"]
    assert provenance.processing_generation_id == "generation-new"
    assert provenance.processing_fingerprint == "fingerprint-new"
    assert provenance.normalized_path == "/tmp/new-normalized.html"


def test_explicit_old_source_range_does_not_cross_repeated_generation_ordinals(
    tmp_path: Path,
) -> None:
    database_path = _seed_reprocessed_document(tmp_path)
    with sqlite3.connect(database_path) as connection:
        resolved = resolve_source_range(
            connection,
            document_id="document-1",
            source_unit_start_id="unit-old",
            source_unit_end_id="unit-old",
            processing_generation_id="generation-old",
        )

    assert resolved.processing_generation_id == "generation-old"
    assert resolved.text == "Old evidence approved a $100 road contract."
    assert resolved.location_label == "Council Update — Old — block 1"


def test_document_inventory_counts_only_active_rows_and_lists_retained_generations(
    tmp_path: Path,
) -> None:
    database_path = _seed_reprocessed_document(tmp_path)

    detail = get_document_detail(database_path, "document-1")
    history = get_document_generations(database_path, "document-1")
    output = format_document_generations(history)
    cli_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "documents",
            "generations",
            "document-1",
        ],
    )

    assert detail.extent_count == 1
    assert detail.processing_generation_id == "generation-new"
    assert [generation.id for generation in history.generations] == [
        "generation-old",
        "generation-new",
    ]
    assert [generation.is_active for generation in history.generations] == [False, True]
    assert "generation generation-old (retained)" in output
    assert "generation generation-new (active)" in output
    assert '"pdf_extractor": "auto"' in output
    assert "normalized_artifact: /tmp/new-normalized.html" in output
    assert cli_result.exit_code == 0, cli_result.stdout
    assert "generation generation-old (retained)" in cli_result.stdout
    assert "generation generation-new (active)" in cli_result.stdout


def test_new_fact_reads_and_discovery_browse_use_active_generation_only(
    tmp_path: Path,
) -> None:
    database_path = _seed_reprocessed_document(tmp_path)
    create_discovery_item(
        database_path,
        document_id="document-1",
        item_id="topic-old",
        item_type="topic",
        label="old roads",
        extractor="test",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-1",
                source_unit_start_id="unit-old",
                source_unit_end_id="unit-old",
                quote="Old evidence approved a $100 road contract.",
                validation_status="validated",
            ),
        ),
    )
    create_discovery_item(
        database_path,
        document_id="document-1",
        item_id="topic-new",
        item_type="topic",
        label="current parks",
        extractor="test",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-1",
                source_unit_start_id="unit-new",
                source_unit_end_id="unit-new",
                quote="Current evidence approved a $200 parks contract.",
                validation_status="validated",
            ),
        ),
    )

    fact_result = extract_document_facts(database_path, "document-1", persist=False)
    current = list_browse_items(database_path, item_types=TOPIC_ITEM_TYPES)
    history = list_browse_items(
        database_path,
        item_types=TOPIC_ITEM_TYPES,
        include_history=True,
    )

    assert {draft.label for draft in fact_result.drafts if draft.item_type == "money"} == {"$200"}
    assert [item.item.id for item in current.items] == ["topic-new"]
    assert {item.item.id for item in history.items} == {"topic-old", "topic-new"}
    old = next(item for item in history.items if item.item.id == "topic-old")
    assert old.processing_generation_id == "generation-old"
    assert not old.is_current_processing_generation


def test_enrichment_request_uses_only_active_generation_sources(tmp_path: Path) -> None:
    database_path = _seed_reprocessed_document(tmp_path)

    result = enrich_document(
        database_path,
        "document-1",
        provider=_ActiveGenerationEnrichmentProvider(),
    )

    assert result.brief.summary == "Current parks contract evidence."
    summary = next(item for item in result.items if item.item_type == "summary")
    assert summary.evidence[0].source_unit_start_id == "unit-new"


def test_real_lancedb_partitions_dimensions_and_filters_model_identity(tmp_path: Path) -> None:
    lancedb_path = tmp_path / "vectors"
    base = {
        "document_id": "document-1",
        "page_start": 1,
        "page_end": 1,
        "source_unit_start_id": None,
        "source_unit_end_id": None,
        "text": "evidence",
    }
    add_vector_records(
        lancedb_path,
        "passage_embeddings",
        [
            {
                **base,
                "passage_id": "passage-model-a",
                "vector": [0.0, 0.0],
                "provider": "test",
                "model": "model-a",
                "version": "1",
            }
        ],
    )
    add_vector_records(
        lancedb_path,
        "passage_embeddings",
        [
            {
                **base,
                "passage_id": "passage-model-b",
                "vector": [0.0, 0.0, 0.0],
                "provider": "test",
                "model": "model-b",
                "version": "2",
            }
        ],
    )
    add_vector_records(
        lancedb_path,
        "passage_embeddings",
        [
            {
                **base,
                "passage_id": "passage-model-c",
                "vector": [1.0, 1.0],
                "provider": "test",
                "model": "model-c",
                "version": "1",
            }
        ],
    )

    model_b = search_vector_records(
        lancedb_path,
        "passage_embeddings",
        key="passage_id",
        vector=(0.0, 0.0, 0.0),
        provider="test",
        model="model-b",
        version="2",
        limit=5,
    )
    model_c = search_vector_records(
        lancedb_path,
        "passage_embeddings",
        key="passage_id",
        vector=(1.0, 1.0),
        provider="test",
        model="model-c",
        version="1",
        limit=5,
    )
    table_names = lancedb.connect(lancedb_path).list_tables().tables

    assert [row["passage_id"] for row in model_b] == ["passage-model-b"]
    assert [row["passage_id"] for row in model_c] == ["passage-model-c"]
    assert "passage_embeddings" in table_names
    assert "passage_embeddings__dim_3" in table_names

    store = LanceDbPassageVectorStore(lancedb_path)
    store.delete_passages(["passage-model-b"])
    assert (
        search_vector_records(
            lancedb_path,
            "passage_embeddings",
            key="passage_id",
            vector=(0.0, 0.0, 0.0),
            provider="test",
            model="model-b",
            version="2",
            limit=5,
        )
        == []
    )


def test_real_vector_search_refills_past_retained_old_candidates(tmp_path: Path) -> None:
    database_path = _seed_reprocessed_document(tmp_path)
    lancedb_path = tmp_path / "lancedb"
    records = []
    retained_passages = []
    for index in range(25):
        passage_id = f"old-vector-{index:02d}"
        retained_passages.append(
            (
                passage_id,
                index + 2,
                f"retained vector evidence {index}",
            )
        )
        records.append(
            {
                "passage_id": passage_id,
                "document_id": "document-1",
                "page_start": 1,
                "page_end": 1,
                "source_unit_start_id": "unit-old",
                "source_unit_end_id": "unit-old",
                "text": "retained vector evidence",
                "vector": [index / 1000, index / 1000],
                "provider": "test",
                "model": "current-model",
                "version": "1",
            }
        )
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, processing_generation_id,
                page_start, page_end, source_unit_start_id,
                source_unit_end_id, ordinal, text
            )
            VALUES(
                ?, 'chunk-old', 'document-1', 'generation-old',
                1, 1, 'unit-old', 'unit-old', ?, ?
            )
            """,
            retained_passages,
        )
    records.append(
        {
            "passage_id": "passage-new",
            "document_id": "document-1",
            "page_start": 1,
            "page_end": 1,
            "source_unit_start_id": "unit-new",
            "source_unit_end_id": "unit-new",
            "text": "current evidence approved a $200 parks contract",
            "vector": [0.9, 0.9],
            "provider": "test",
            "model": "current-model",
            "version": "1",
        }
    )
    add_vector_records(lancedb_path, "passage_embeddings", records)

    engine = SearchEngine(
        database_path=database_path,
        vector_searcher=LanceDbPassageVectorSearcher(
            lancedb_path,
            max_vector_distance=None,
        ),
        vector_store=_NoOpVectorStore(),
        embedding_provider=_EmbeddingProvider(),
    )

    results = engine.search("semantic only", limit=5)

    assert [result.passage_id for result in results] == ["passage-new"]
    assert results[0].processing_generation_id == "generation-new"


def test_reprocess_cli_accepts_batches_without_embedding_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)
    captured: list[tuple[Path, list[str], str | None]] = []

    def fake_enqueue(
        database_path: Path,
        document_ids: Sequence[str],
        *,
        pdf_extractor: str | None = None,
        csv_options: dict[str, object] | None = None,
    ) -> list[Job]:
        assert csv_options is None
        captured.append((database_path, list(document_ids), pdf_extractor))
        return [
            Job(
                id=f"job-{index}",
                kind="reprocess-document",
                status="pending",
                payload={"document_id": document_id, "stage": "pending"},
                result=None,
                error=None,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
            for index, document_id in enumerate(document_ids)
        ]

    monkeypatch.setattr("newsrag.reprocess.enqueue_reprocessing", fake_enqueue)
    result = runner.invoke(
        app,
        [
            "--config-path",
            str(tmp_path / "missing.yaml"),
            "--data-dir",
            str(data_dir),
            "reprocess",
            "document-a",
            "document-b",
            "--pdf-extractor",
            "table",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Enqueued 2 reprocessing job(s)" in result.stdout
    assert "document_id=document-a status=pending stage=pending" in result.stdout
    assert captured == [
        (
            data_dir / "newsrag.sqlite3",
            ["document-a", "document-b"],
            "table",
        )
    ]


def test_jobs_output_includes_reprocessing_progress_generation_and_retry_target(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO jobs(
                id, kind, status, payload_json, result_json, error
            )
            VALUES(?, 'reprocess-document', ?, ?, ?, ?)
            """,
            (
                (
                    "job-done",
                    "done",
                    '{"document_id": "document-a", "stage": "publication"}',
                    '{"outcome": "reprocessed", "processing_generation_id": "generation-new", "previous_processing_generation_id": "generation-old"}',
                    None,
                ),
                (
                    "job-failed",
                    "failed",
                    '{"document_id": "document-b", "stage": "passage_embeddings", "base_generation_id": "generation-base"}',
                    None,
                    "embedding failed",
                ),
            ),
        )

    result = runner.invoke(app, ["--data-dir", str(data_dir), "jobs", "list"])

    assert result.exit_code == 0, result.stdout
    done_line = next(line for line in result.stdout.splitlines() if "job-done" in line)
    failed_line = next(line for line in result.stdout.splitlines() if "job-failed" in line)
    assert "stage=publication" in done_line
    assert "outcome=reprocessed" in done_line
    assert "processing_generation_id=generation-new" in done_line
    assert "previous_processing_generation_id=generation-old" in done_line
    assert "stage=passage_embeddings" in failed_line
    assert "retry_target=document_id:document-b" in failed_line
    assert "retry_hint=newsrag jobs retry job-failed" in failed_line


def _seed_reprocessed_document(tmp_path: Path) -> Path:
    database_path = initialize_storage(tmp_path / ".newsrag").database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/update.html', '/tmp/update.html')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path,
                acquired_at, state, provenance_json
            )
            VALUES(
                'artifact-1', 'source-1', 'text/html', 10, 'artifact-hash',
                '/tmp/update.html', '2026-01-01T00:00:00+00:00', 'published', '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, metadata_json, artifact_id)
            VALUES('document-1', '/tmp/update.html', 'Council Update', '{}', 'artifact-1')
            """
        )
        configuration = json.dumps(
            {
                "adapter": "html",
                "embedding": {
                    "provider": "test",
                    "model": "current-model",
                    "version": "1",
                    "endpoint_fingerprint": "endpoint",
                },
                "options": {"pdf_extractor": "auto"},
            },
            sort_keys=True,
        )
        connection.executemany(
            """
            INSERT INTO processing_generations(
                id, document_id, fingerprint, configuration_json, normalized_path,
                job_id, created_at
            )
            VALUES(?, 'document-1', ?, ?, ?, ?, ?)
            """,
            (
                (
                    "generation-old",
                    "fingerprint-old",
                    configuration,
                    "/tmp/old-normalized.html",
                    "job-old",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "generation-new",
                    "fingerprint-new",
                    configuration,
                    "/tmp/new-normalized.html",
                    "job-new",
                    "2026-02-01T00:00:00+00:00",
                ),
            ),
        )
        connection.execute(
            """
            UPDATE documents
            SET current_processing_generation_id = 'generation-new'
            WHERE id = 'document-1'
            """
        )
        connection.execute(
            """
            INSERT INTO source_revisions(
                id, source_id, document_id, revision_number, published_at
            )
            VALUES('revision-1', 'source-1', 'document-1', 1, CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            UPDATE sources
            SET current_revision_id = 'revision-1', publication_generation = 1
            WHERE id = 'source-1'
            """
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, processing_generation_id, ordinal,
                location_type, location_json, human_label, normalized_text,
                structure_json, extractor
            )
            VALUES(
                ?, 'artifact-1', 'document-1', ?, 1, 'html_block',
                '{"block_number": 1}', 'block 1', ?, ?, 'static-html'
            )
            """,
            (
                (
                    "unit-old",
                    "generation-old",
                    "Old evidence approved a $100 road contract.",
                    '{"heading_path": ["Council Update", "Old"]}',
                ),
                (
                    "unit-new",
                    "generation-new",
                    "Current evidence approved a $200 parks contract.",
                    '{"heading_path": ["Council Update", "Current"]}',
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO chunks(
                id, document_id, processing_generation_id, page_start, page_end,
                source_unit_start_id, source_unit_end_id, text
            )
            VALUES(?, 'document-1', ?, 1, 1, ?, ?, ?)
            """,
            (
                (
                    "chunk-old",
                    "generation-old",
                    "unit-old",
                    "unit-old",
                    "old evidence approved a $100 road contract",
                ),
                (
                    "chunk-new",
                    "generation-new",
                    "unit-new",
                    "unit-new",
                    "current evidence approved a $200 parks contract",
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, processing_generation_id, page_start,
                page_end, source_unit_start_id, source_unit_end_id, ordinal, text
            )
            VALUES(?, ?, 'document-1', ?, 1, 1, ?, ?, 1, ?)
            """,
            (
                (
                    "passage-old",
                    "chunk-old",
                    "generation-old",
                    "unit-old",
                    "unit-old",
                    "old evidence approved a $100 road contract",
                ),
                (
                    "passage-new",
                    "chunk-new",
                    "generation-new",
                    "unit-new",
                    "unit-new",
                    "current evidence approved a $200 parks contract",
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO passages_fts(passage_id, text) VALUES(?, ?)",
            (
                ("passage-old", "old evidence approved a $100 road contract"),
                ("passage-new", "current evidence approved a $200 parks contract"),
            ),
        )
        connection.commit()
    return database_path

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import test_text_ingestion as text_support
from docx_fixtures import REL_NS, make_docx, paragraph

from newsrag.acquisition import SafeSourceArtifactAcquirer
from newsrag.cli import app
from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    create_discovery_item,
    create_document_profile,
)
from newsrag.embeddings import EmbeddingMetadata
from newsrag.ingest import list_documents
from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.refresh import enqueue_refresh
from newsrag.reprocess import enqueue_reprocessing
from newsrag.search import LanceDbPassageVectorStore, SearchFilters
from newsrag.sources import DOCX_MAX_SOURCE_BYTES, DOCX_MEDIA_TYPE

_HEADING_STYLE = (
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>'
)
_EXTERNAL_HYPERLINK = (
    f'<Relationship Id="public-record" Type="{REL_NS}/hyperlink" '
    'Target="https://records.example.gov/notice" TargetMode="External"/>'
)


def _structured_docx(path: Path, marker: str) -> Path:
    heading = paragraph(
        "Council Update",
        '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>',
    )
    evidence = paragraph(
        f"{marker} stormwater contract approved.",
        runs=(
            '<w:hyperlink r:id="public-record"><w:r><w:t> Public record.</w:t></w:r>'
            '</w:hyperlink><w:r><w:footnoteReference w:id="1"/></w:r>'
        ),
    )
    table = (
        "<w:tbl>"
        f"<w:tr><w:tc>{paragraph('Fund')}</w:tc><w:tc>{paragraph('Amount')}</w:tc></w:tr>"
        f"<w:tr><w:tc>{paragraph('Water')}</w:tc><w:tc>{paragraph('$250,000')}</w:tc></w:tr>"
        "</w:tbl>"
    )
    footnotes = f'<w:footnote w:id="1">{paragraph(f"{marker} funding is restricted.")}</w:footnote>'
    return make_docx(
        path,
        heading + evidence + table,
        styles=_HEADING_STYLE,
        footnotes=footnotes,
        relationships=_EXTERNAL_HYPERLINK,
    )


def _docx_bytes(tmp_path: Path, name: str, marker: str) -> bytes:
    return _structured_docx(tmp_path / name, marker).read_bytes()


def _identity(
    corpus: text_support.Corpus,
    document_id: str,
) -> tuple[str, str, str, str]:
    row = corpus.rows(
        "SELECT documents.id, source_artifacts.source_id, documents.artifact_id, "
        "source_revisions.id FROM documents "
        "JOIN source_artifacts ON source_artifacts.id = documents.artifact_id "
        "JOIN source_revisions ON source_revisions.document_id = documents.id "
        "WHERE documents.id = ?",
        (document_id,),
    )[0]
    return str(row[0]), str(row[1]), str(row[2]), str(row[3])


def test_local_docx_ingestion_preserves_blocks_and_drives_typed_evidence(
    tmp_path: Path,
) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = _structured_docx(tmp_path / "council-minutes.docx", "Localdocx")

    completed = corpus.ingest(
        str(source),
        metadata={
            "title": "Approved Minutes",
            "body": "City Council",
            "meeting_date": "2026-08-01",
        },
    )

    assert completed.status == "done", completed.error
    assert completed.result is not None and completed.result["outcome"] == "created"
    document_id = str(completed.result["document_id"])
    document = list_documents(corpus.paths.database)[0]
    assert document.id == document_id
    assert corpus.rows("SELECT media_type, reported_media_type FROM source_artifacts") == [
        (DOCX_MEDIA_TYPE, DOCX_MEDIA_TYPE)
    ]
    options = json.loads(corpus.rows("SELECT ingestion_options_json FROM documents")[0][0])
    assert options["source_media_type"] == DOCX_MEDIA_TYPE

    rows = corpus.rows(
        "SELECT ordinal, location_type, location_json, human_label, normalized_text, "
        "structure_json FROM source_units ORDER BY ordinal"
    )
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    assert {row[1] for row in rows} == {"docx_block"}
    locations = [json.loads(row[2]) for row in rows]
    structures = [json.loads(row[5]) for row in rows]
    assert locations == [
        {"block_number": 1, "paragraph_number": 1},
        {"block_number": 2, "paragraph_number": 2},
        {"block_number": 3, "footnote_id": "1", "paragraph_number": 1},
        {"block_number": 4, "row_number": 1, "table_number": 1},
        {"block_number": 5, "row_number": 2, "table_number": 1},
    ]
    assert [structure["kind"] for structure in structures] == [
        "heading",
        "paragraph",
        "paragraph",
        "table_row",
        "table_row",
    ]
    assert all(structure["heading_path"] == ["Council Update"] for structure in structures)
    assert "https://records.example.gov" not in "\n".join(str(row[4]) for row in rows)
    assert rows[1][4] == ("Localdocx stormwater contract approved. Public record. [footnote ID 1]")
    assert rows[2][4] == "Localdocx funding is restricted."
    assert rows[4][4] == "Water\t$250,000"
    assert len(text_support._lance_rows(corpus.paths.lancedb, "chunk_embeddings")) == 5
    assert len(text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings")) == 5

    results = text_support._keyword_results(
        corpus.paths.database,
        "Localdocx stormwater",
        filters=SearchFilters(source_type="docx", body="City Council"),
    )
    assert len(results) == 1
    assert results[0].source_type == "docx"
    assert results[0].citation == ("Approved Minutes — 2026-08-01 — Council Update — paragraph 2")
    unit_id = str(
        corpus.rows(
            "SELECT id FROM source_units WHERE document_id = ? AND ordinal = 2",
            (document_id,),
        )[0][0]
    )
    profile = create_document_profile(
        corpus.paths.database,
        document_id=document_id,
        text_length=sum(len(str(row[4])) for row in rows),
        extractor="integration-test",
    )
    item = create_discovery_item(
        corpus.paths.database,
        document_id=document_id,
        item_type="action",
        label="Approved stormwater contract",
        extractor="integration-test",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id=document_id,
                source_unit_start_id=unit_id,
                source_unit_end_id=unit_id,
                quote="stormwater contract approved",
                validation_status="verified",
            ),
        ),
    )
    provenance = load_packet_source_provenance(corpus.paths.database, results)
    packet = format_source_packet(
        query="Localdocx stormwater",
        results=results,
        source_provenance=provenance,
    )

    assert profile.source_type == "docx"
    assert profile.extent_type == "blocks"
    assert profile.extent_count == 5
    assert item.evidence[0].location_type == "docx_block"
    assert item.evidence[0].location_label == "Council Update — paragraph 2"
    assert "source type: docx" in packet
    assert "Council Update — paragraph 2" in packet
    assert "page 2" not in packet
    assert provenance[document_id].artifact_hash == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("reported_media_type", "reference", "source_type"),
    [
        (DOCX_MEDIA_TYPE, "https://example.gov/minutes.docx", None),
        ("application/zip", "https://example.gov/export", "docx"),
        ("application/octet-stream", "https://example.gov/download", "docx"),
    ],
)
def test_public_docx_url_accepts_canonical_or_explicit_generic_headers(
    tmp_path: Path,
    reported_media_type: str,
    reference: str,
    source_type: str | None,
) -> None:
    content = _docx_bytes(tmp_path, "remote-source.docx", "Remotedocx")
    response = text_support.HttpResponse(
        200,
        {
            "content-type": reported_media_type,
            "content-length": str(len(content)),
        },
        (content[:71], content[71:]),
    )
    transport = text_support.HttpTransport([response])
    corpus = text_support._corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=text_support._public_resolver,
            transport=transport,
        ),
    )

    completed = corpus.ingest(reference, source_type=source_type)

    assert completed.status == "done", completed.error
    assert list_documents(corpus.paths.database)[0].source_url == reference
    assert corpus.rows("SELECT media_type, reported_media_type FROM source_artifacts") == [
        (DOCX_MEDIA_TYPE, reported_media_type)
    ]
    assert len(text_support._keyword_results(corpus.paths.database, "Remotedocx")) == 2
    assert transport.requests == [(reference, "93.184.216.34", 30.0)]
    assert response.closed


@pytest.mark.parametrize(
    ("filename", "reported_media_type"),
    [
        ("minutes.docm", "application/octet-stream"),
        ("minutes.doc", "application/octet-stream"),
        ("slides.pptx", "application/zip"),
        (
            "export",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("minutes.docx", "application/vnd.ms-word.document.macroenabled.12"),
    ],
)
def test_explicit_docx_rejects_contradictory_office_filename_or_media_type(
    tmp_path: Path,
    filename: str,
    reported_media_type: str,
) -> None:
    content = _docx_bytes(tmp_path, "valid-package.docx", "Rejected")
    response = text_support.HttpResponse(
        200,
        {"content-type": reported_media_type, "content-length": str(len(content))},
        (content,),
    )
    corpus = text_support._corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=text_support._public_resolver,
            transport=text_support.HttpTransport([response]),
        ),
    )

    failed = corpus.ingest(f"https://example.gov/{filename}", source_type="docx")

    assert failed.status == "failed"
    assert "DOCX selection conflicts" in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(0,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_units") == [(0,)]
    assert text_support._lance_rows(corpus.paths.lancedb, "chunk_embeddings") == []
    assert text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings") == []
    assert response.closed


def test_zip_magic_alone_never_selects_docx(tmp_path: Path) -> None:
    archive = tmp_path / "ordinary.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
        output.writestr("notes.txt", "A ZIP archive is not a Word document.")
    corpus = text_support._corpus(tmp_path / ".newsrag")

    failed = corpus.ingest(str(archive))

    assert failed.status == "failed"
    assert "Unsupported source type" in str(failed.error)
    assert list_documents(corpus.paths.database) == []
    assert text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings") == []


def test_invalid_docx_archive_failure_is_atomic(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.docx"
    with ZipFile(invalid, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<not-a-docx/>")
    corpus = text_support._corpus(tmp_path / ".newsrag")

    failed = corpus.ingest(str(invalid))

    assert failed.status == "failed"
    assert "missing required part" in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM source_artifacts") == [(1,)]
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(0,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_units") == [(0,)]
    assert corpus.rows("SELECT COUNT(*) FROM chunks") == [(0,)]
    assert corpus.rows("SELECT COUNT(*) FROM passages") == [(0,)]
    assert text_support._lance_rows(corpus.paths.lancedb, "chunk_embeddings") == []
    assert text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings") == []


def test_directory_and_manifest_ingest_mixed_docx_sources(tmp_path: Path) -> None:
    directory_data = tmp_path / "directory-corpus"
    sources = tmp_path / "sources"
    nested = sources / "nested"
    nested.mkdir(parents=True)
    _structured_docx(sources / "minutes.DOCX", "Directorydocx")
    (nested / "notice.txt").write_text("Directory text evidence.\n", encoding="utf-8")
    with ZipFile(sources / "unrelated.zip", "w") as archive:
        archive.writestr("record.txt", "not a DOCX")

    queued = text_support._RUNNER.invoke(
        app,
        ["--data-dir", str(directory_data), "ingest", str(sources)],
    )
    directory_corpus = text_support._corpus(directory_data)
    completed = directory_corpus.drain()

    assert queued.exit_code == 0, queued.stdout
    assert "Queued by type: docx=1, text=1" in queued.stdout
    assert "Skipped by type: zip=1" in queued.stdout
    assert len(completed) == 2 and all(job.status == "done" for job in completed)
    assert set(directory_corpus.rows("SELECT media_type FROM source_artifacts")) == {
        (DOCX_MEDIA_TYPE,),
        ("text/plain",),
    }

    manifest_data = tmp_path / "manifest-corpus"
    explicit_docx = tmp_path / "minutes.data"
    explicit_docx.write_bytes((sources / "minutes.DOCX").read_bytes())
    notes = tmp_path / "notes.txt"
    notes.write_text("Manifest text evidence.\n", encoding="utf-8")
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        f"""documents:
  - source: {explicit_docx.name}
    type: docx
  - source: {notes.name}
    type: text
""",
        encoding="utf-8",
    )

    manifest_queued = text_support._RUNNER.invoke(
        app,
        ["--data-dir", str(manifest_data), "ingest-manifest", str(manifest)],
    )
    manifest_corpus = text_support._corpus(manifest_data)
    manifest_jobs = manifest_corpus.drain()

    assert manifest_queued.exit_code == 0, manifest_queued.stdout
    assert len(manifest_jobs) == 2 and all(job.status == "done" for job in manifest_jobs)
    assert set(manifest_corpus.rows("SELECT media_type FROM source_artifacts")) == {
        (DOCX_MEDIA_TYPE,),
        ("text/plain",),
    }


def test_explicit_extensionless_docx_url_refresh_retains_type_and_25_mib_cap(
    tmp_path: Path,
) -> None:
    original = _docx_bytes(tmp_path, "original.docx", "Originaldocx")
    revised = _docx_bytes(tmp_path, "revised.docx", "Reviseddocx")
    responses = [
        text_support.HttpResponse(
            200,
            {"content-type": "application/octet-stream"},
            (original,),
        ),
        text_support.HttpResponse(
            200,
            {"content-type": "application/octet-stream"},
            (revised,),
        ),
        text_support.HttpResponse(
            200,
            {
                "content-type": "application/octet-stream",
                "content-length": str(DOCX_MAX_SOURCE_BYTES + 1),
            },
            (),
        ),
    ]
    transport = text_support.HttpTransport(responses)
    corpus = text_support._corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=text_support._public_resolver,
            transport=transport,
        ),
    )
    reference = "https://example.gov/export"
    ingested = corpus.ingest(reference, source_type="docx")
    assert ingested.status == "done", ingested.error
    assert ingested.result is not None
    source_id = str(ingested.result["source_id"])

    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert refreshed.status == "done", refreshed.error
    assert refreshed.result is not None and refreshed.result["outcome"] == "revision_created"
    assert refreshed.payload["base"]["source_type"] == "docx"
    assert len(text_support._keyword_results(corpus.paths.database, "Reviseddocx")) == 2
    assert (
        len(
            text_support._keyword_results(
                corpus.paths.database,
                "Originaldocx",
                include_history=True,
                filters=SearchFilters(source_type="docx"),
            )
        )
        == 2
    )

    oversized = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert DOCX_MAX_SOURCE_BYTES == 25 * 1024 * 1024
    assert oversized.status == "failed"
    assert f"exceeds {DOCX_MAX_SOURCE_BYTES} compressed bytes" in str(oversized.error)
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_artifacts") == [(2,)]
    assert all(response.closed for response in responses)


def test_docx_duplicate_noop_revision_reactivation_and_history(tmp_path: Path) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = tmp_path / "budget.docx"
    original = _docx_bytes(tmp_path, "original-budget.docx", "Historicdocx")
    revised = _docx_bytes(tmp_path, "revised-budget.docx", "Reviseddocx")
    source.write_bytes(original)
    first = corpus.ingest(
        str(source),
        metadata={"title": "Budget Minutes", "body": "City Council"},
    )
    assert first.result is not None
    source_id = str(first.result["source_id"])
    first_document_id = str(first.result["document_id"])

    duplicate = corpus.ingest(str(source))
    unchanged = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    source.write_bytes(revised)
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    source.write_bytes(original)
    reactivated = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert duplicate.result is not None and duplicate.result["outcome"] == "duplicate_ignored"
    assert unchanged.result is not None and unchanged.result["outcome"] == "unchanged"
    assert refreshed.status == "done", refreshed.error
    assert refreshed.result is not None and refreshed.result["outcome"] == "revision_created"
    assert reactivated.status == "done", reactivated.error
    assert reactivated.result is not None
    assert reactivated.result["outcome"] == "revision_reactivated"
    assert reactivated.result["document_id"] == first_document_id
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]
    assert corpus.rows("SELECT publication_generation FROM sources") == [(3,)]
    current = text_support._keyword_results(
        corpus.paths.database,
        "Historicdocx stormwater",
        filters=SearchFilters(source_type="docx"),
    )
    revised_history = text_support._keyword_results(
        corpus.paths.database,
        "Reviseddocx stormwater",
        include_history=True,
        filters=SearchFilters(source_type="docx"),
    )
    assert len(current) == 1 and current[0].document_id == first_document_id
    assert current[0].is_current_snapshot is True
    assert len(revised_history) == 1
    assert revised_history[0].revision_number == 2
    assert revised_history[0].is_current_snapshot is False


def test_missing_original_docx_reprocess_noop_then_rebuild_retains_history_and_identity(
    tmp_path: Path,
) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = _structured_docx(tmp_path / "preserved.docx", "Generationdocx")
    completed = corpus.ingest(
        str(source),
        metadata={"title": "Preserved Minutes", "meeting_date": "2026-08-02"},
    )
    assert completed.result is not None
    document_id = str(completed.result["document_id"])
    original_identity = _identity(corpus, document_id)
    old_generation = str(
        corpus.rows(
            "SELECT current_processing_generation_id FROM documents WHERE id = ?",
            (document_id,),
        )[0][0]
    )
    old_unit_ids = {
        str(row[0])
        for row in corpus.rows(
            "SELECT id FROM source_units WHERE document_id = ?",
            (document_id,),
        )
    }
    old_results = text_support._keyword_results(
        corpus.paths.database,
        "Generationdocx stormwater",
        filters=SearchFilters(source_type="docx"),
    )
    old_provenance = load_packet_source_provenance(corpus.paths.database, old_results)
    old_packet = format_source_packet(
        query="Generationdocx stormwater",
        results=old_results,
        source_provenance=old_provenance,
    )
    source.unlink()

    noop = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])

    assert noop.status == "done", noop.error
    assert noop.result is not None and noop.result["outcome"] == "unchanged"
    assert noop.result["processing_generation_id"] == old_generation
    assert not source.exists()
    assert _identity(corpus, document_id) == original_identity

    corpus.embeddings.metadata = EmbeddingMetadata("fake", "docx-lifecycle", "2")
    rebuilt = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])

    assert rebuilt.status == "done", rebuilt.error
    assert rebuilt.result is not None and rebuilt.result["outcome"] == "reprocessed"
    new_generation = str(rebuilt.result["processing_generation_id"])
    assert new_generation != old_generation
    assert _identity(corpus, document_id) == original_identity
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(1,)]
    assert corpus.rows("SELECT COUNT(*) FROM processing_generations") == [(2,)]
    assert old_unit_ids < {
        str(row[0])
        for row in corpus.rows(
            "SELECT id FROM source_units WHERE document_id = ?",
            (document_id,),
        )
    }
    assert corpus.rows(
        "SELECT COUNT(*) FROM source_units WHERE document_id = ? AND processing_generation_id = ?",
        (document_id, old_generation),
    ) == [(5,)]
    retained_provenance = load_packet_source_provenance(corpus.paths.database, old_results)
    retained_packet = format_source_packet(
        query="Generationdocx stormwater",
        results=old_results,
        source_provenance=retained_provenance,
    )
    assert retained_packet == old_packet
    assert retained_provenance[document_id].processing_generation_id == old_generation
    current_results = text_support._keyword_results(
        corpus.paths.database,
        "Generationdocx stormwater",
        filters=SearchFilters(source_type="docx"),
    )
    assert len(current_results) == 1
    assert current_results[0].processing_generation_id == new_generation


def test_failed_docx_publication_keeps_current_revision_and_indexes(tmp_path: Path) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = _structured_docx(tmp_path / "safe-refresh.docx", "Currentdocx")
    completed = corpus.ingest(str(source), metadata={"title": "Safe Minutes"})
    assert completed.result is not None
    source_id = str(completed.result["source_id"])
    document_id = str(completed.result["document_id"])
    vectors_before = text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings")
    corpus.ingestion.processor.passage_vector_store = text_support.FailingPassageIndex(
        LanceDbPassageVectorStore(corpus.paths.lancedb)
    )
    _structured_docx(source, "Unpublisheddocx")

    failed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert failed.status == "failed"
    assert "injected passage index failure" in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(1,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(1,)]
    assert corpus.rows(
        "SELECT source_revisions.document_id FROM sources "
        "JOIN source_revisions ON source_revisions.id = sources.current_revision_id"
    ) == [(document_id,)]
    assert text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings") == vectors_before
    assert len(text_support._keyword_results(corpus.paths.database, "Currentdocx")) == 2
    assert text_support._keyword_results(corpus.paths.database, "Unpublisheddocx") == []

from __future__ import annotations

import hashlib
import json
import socket
from itertools import pairwise
from pathlib import Path

import pytest
import test_text_ingestion as text_support

from newsrag.acquisition import SafeSourceArtifactAcquirer
from newsrag.adapters import AdapterError, AdapterInput, AdapterResult, CanonicalSourceUnit
from newsrag.cli import app
from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    create_discovery_item,
    create_document_profile,
)
from newsrag.embeddings import EmbeddingMetadata
from newsrag.ingest import list_documents
from newsrag.markdown_adapter import MarkdownSourceAdapter
from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.refresh import enqueue_refresh
from newsrag.reprocess import enqueue_reprocessing
from newsrag.search import (
    LanceDbPassageVectorSearcher,
    LanceDbPassageVectorStore,
    SearchFilters,
    merge_search_candidates,
)
from newsrag.sources import TEXT_MAX_SOURCE_BYTES


def _extract_markdown(
    tmp_path: Path,
    content: bytes,
    *,
    media_type: str = "text/markdown",
) -> AdapterResult:
    source = tmp_path / "source.md"
    source.write_bytes(content)
    return MarkdownSourceAdapter().extract(
        AdapterInput(
            artifact_path=source,
            content_hash=hashlib.sha256(content).hexdigest(),
            media_type=media_type,
            work_dir=tmp_path / "work",
        )
    )


def _line_range(unit: CanonicalSourceUnit) -> tuple[int, int]:
    line_start = unit.location["line_start"]
    line_end = unit.location["line_end"]
    assert isinstance(line_start, int) and not isinstance(line_start, bool)
    assert isinstance(line_end, int) and not isinstance(line_end, bool)
    return line_start, line_end


def test_markdown_adapter_preserves_raw_blocks_structure_and_inert_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("Markdown extraction attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    content = """# Council Update

Opening **summary** spans
two source lines with a [linked report](https://other.example/report).

## Budget

- General fund
  - Capital project

> Public funds require public records.

| Fund | Amount |
| --- | ---: |
| Roads | $10 |

```python
print("literal source")
```

<div><script src="https://other.example/app.js">literal script</script></div>
"""

    first = _extract_markdown(tmp_path, content.encode())
    second = _extract_markdown(tmp_path, content.encode())

    assert first == second
    assert first.media_type == "text/markdown"
    assert first.metadata_candidates == {"text_encoding": "utf-8"}
    assert first.derived_artifact_path is None
    assert [unit.ordinal for unit in first.units] == list(range(1, len(first.units) + 1))
    assert all(unit.location_type == "markdown_block" for unit in first.units)

    by_kind: dict[str, list[CanonicalSourceUnit]] = {}
    for unit in first.units:
        by_kind.setdefault(str(unit.structure["kind"]), []).append(unit)
        assert set(unit.structure) >= {"kind", "heading_path", "containers"}

    headings = by_kind["heading"]
    assert [unit.normalized_text for unit in headings] == ["# Council Update", "## Budget"]
    assert [unit.location for unit in headings] == [
        {"line_start": 1, "line_end": 1},
        {"line_start": 6, "line_end": 6},
    ]
    assert headings[0].structure["heading_path"] == ["Council Update"]
    assert headings[1].structure["heading_path"] == ["Council Update", "Budget"]
    assert headings[1].structure["heading_level"] == 2

    paragraphs = by_kind["paragraph"]
    assert paragraphs[0].normalized_text == (
        "Opening **summary** spans\n"
        "two source lines with a [linked report](https://other.example/report)."
    )
    assert paragraphs[0].location == {"line_start": 3, "line_end": 4}
    assert paragraphs[0].structure["heading_path"] == ["Council Update"]
    assert paragraphs[1].normalized_text == "- General fund"
    assert paragraphs[1].structure["containers"] == ["bullet_list", "list_item"]
    assert paragraphs[2].normalized_text == "  - Capital project"
    assert paragraphs[2].structure["containers"] == [
        "bullet_list",
        "list_item",
        "bullet_list",
        "list_item",
    ]
    assert paragraphs[3].normalized_text == "> Public funds require public records."
    assert paragraphs[3].structure["containers"] == ["blockquote"]

    table = by_kind["table"][0]
    assert table.normalized_text == "| Fund | Amount |\n| --- | ---: |\n| Roads | $10 |"
    assert table.location == {"line_start": 13, "line_end": 15}
    code = by_kind["code_block"][0]
    assert code.normalized_text == '```python\nprint("literal source")\n```'
    assert code.location == {"line_start": 17, "line_end": 19}
    assert code.structure["info"] == "python"
    assert code.structure["fenced"] is True
    html = by_kind["html_block"][0]
    assert html.normalized_text == (
        '<div><script src="https://other.example/app.js">literal script</script></div>'
    )
    assert html.location == {"line_start": 21, "line_end": 21}

    ranges = [_line_range(unit) for unit in first.units]
    assert ranges[0][0] == 1
    assert ranges[-1][1] == len(content.splitlines())
    assert all(current[0] == previous[1] + 1 for previous, current in pairwise(ranges))


@pytest.mark.parametrize(
    ("constant", "limit", "content", "error"),
    [
        ("MAX_TEXT_BYTES", 4, b"12345", "byte limit"),
        ("MAX_TEXT_CHARS", 4, b"12345", "character limit"),
        ("MAX_TEXT_LINES", 3, b"one\ntwo\nthree\nfour", "line limit"),
    ],
)
def test_markdown_reuses_strict_text_decoding_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    content: bytes,
    error: str,
) -> None:
    monkeypatch.setattr(f"newsrag.text_adapter.{constant}", limit)

    with pytest.raises(AdapterError, match=error):
        _extract_markdown(tmp_path, content)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"", "empty"),
        (b" \n\t", "non-whitespace"),
        (b"GIF89a printable", "non-text file signature"),
        (b"approved\x00contract", "unsupported control characters"),
        (b"caf\xe9", "not valid utf-8"),
        (b"%PDF-1.4\nprintable", "contradictory"),
    ],
)
def test_invalid_markdown_never_publishes_or_indexes(
    tmp_path: Path,
    content: bytes,
    error: str,
) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = tmp_path / "invalid.md"
    source.write_bytes(content)

    failed = corpus.ingest(str(source))

    assert failed.status == "failed"
    assert error in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(0,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_units") == [(0,)]
    assert text_support._lance_rows(corpus.paths.lancedb, "chunk_embeddings") == []
    assert text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings") == []


def test_local_markdown_ingestion_uses_canonical_media_and_real_indexes(tmp_path: Path) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = tmp_path / "council-notes.md"
    content = b"# Council Notes\r\n\r\n## Transit\r\nCouncil approved **rail funding**.\r\n"
    source.write_bytes(content)

    completed = corpus.ingest(
        str(source),
        metadata={"title": "Approved Notes", "body": "City Council"},
    )

    assert completed.status == "done", completed.error
    assert completed.result is not None and completed.result["outcome"] == "created"
    document = list_documents(corpus.paths.database)[0]
    assert document.title == "Approved Notes"
    assert document.metadata["text_encoding"] == "utf-8"
    assert document.metadata["source_size_bytes"] == len(content)
    assert corpus.rows("SELECT media_type, reported_media_type FROM source_artifacts") == [
        ("text/markdown", "text/markdown")
    ]
    options = json.loads(corpus.rows("SELECT ingestion_options_json FROM documents")[0][0])
    assert options["source_media_type"] == "text/markdown"
    assert corpus.rows(
        "SELECT normalized_text, location_type, location_json "
        "FROM source_units WHERE normalized_text <> '' ORDER BY ordinal"
    ) == [
        ("# Council Notes", "markdown_block", '{"line_end": 1, "line_start": 1}'),
        ("## Transit", "markdown_block", '{"line_end": 3, "line_start": 3}'),
        (
            "Council approved **rail funding**.",
            "markdown_block",
            '{"line_end": 4, "line_start": 4}',
        ),
    ]
    assert len(text_support._lance_rows(corpus.paths.lancedb, "chunk_embeddings")) == 3
    assert len(text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings")) == 3


def test_markdown_url_charset_and_explicit_plain_text_hint_selection(tmp_path: Path) -> None:
    explicit_content = "# Budget\nCouncil’s café allocation.\n".encode("cp1252")
    plain_content = b"# Literal text heading\nNo Markdown auto-selection.\n"
    declared_content = b"# Parks\nDeclared Markdown response.\n"
    responses = [
        text_support.HttpResponse(
            200,
            {
                "content-type": "text/plain; charset=windows-1252",
                "content-length": str(len(explicit_content)),
            },
            (explicit_content,),
        ),
        text_support.HttpResponse(
            200,
            {"content-type": "text/plain", "content-length": str(len(plain_content))},
            (plain_content,),
        ),
        text_support.HttpResponse(
            200,
            {"content-type": "text/markdown", "content-length": str(len(declared_content))},
            (declared_content,),
        ),
    ]
    corpus = text_support._corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=text_support._public_resolver,
            transport=text_support.HttpTransport(responses),
        ),
    )
    explicit_url = "https://example.gov/export"
    plain_url = "https://example.gov/plain"
    declared_url = "https://example.gov/notice"

    explicit = corpus.ingest(explicit_url, source_type="markdown")
    plain = corpus.ingest(plain_url)
    declared = corpus.ingest(declared_url)

    assert explicit.status == declared.status == plain.status == "done"
    media_by_url = dict(
        corpus.rows(
            "SELECT sources.submitted_reference, source_artifacts.media_type "
            "FROM sources JOIN source_artifacts ON source_artifacts.source_id = sources.id "
            "WHERE source_artifacts.state = 'published'"
        )
    )
    assert media_by_url == {
        explicit_url: "text/markdown",
        plain_url: "text/plain",
        declared_url: "text/markdown",
    }
    documents = {
        document.source_url: document for document in list_documents(corpus.paths.database)
    }
    assert documents[explicit_url].metadata["text_encoding"] == "cp1252"
    assert corpus.rows(
        "SELECT source_units.normalized_text, source_units.location_type "
        "FROM source_units JOIN documents ON documents.id = source_units.document_id "
        "WHERE documents.source_url = ? ORDER BY source_units.ordinal",
        (plain_url,),
    ) == [
        ("# Literal text heading", "text_line"),
        ("No Markdown auto-selection.", "text_line"),
    ]
    assert corpus.rows(
        "SELECT reported_media_type FROM source_artifacts "
        "JOIN sources ON sources.id = source_artifacts.source_id "
        "WHERE sources.submitted_reference = ?",
        (explicit_url,),
    ) == [("text/plain; charset=windows-1252",)]


def test_known_markdown_url_refresh_retains_type_for_plain_text_and_html_signatures(
    tmp_path: Path,
) -> None:
    original = b"# Original\n\nOriginal Markdown evidence.\n"
    plain_revision = "# Revised\n\nCouncil’s café evidence.\n".encode("cp1252")
    html_revision = b"<html><body>Literal embedded markup evidence.</body></html>\n"
    transport = text_support.HttpTransport(
        [
            text_support.HttpResponse(
                200,
                {"content-type": "text/plain", "content-length": str(len(original))},
                (original,),
            ),
            text_support.HttpResponse(
                200,
                {
                    "content-type": "text/plain; charset=windows-1252",
                    "content-length": str(len(plain_revision)),
                },
                (plain_revision,),
            ),
            text_support.HttpResponse(
                200,
                {
                    "content-type": "application/octet-stream",
                    "content-length": str(len(html_revision)),
                },
                (html_revision,),
            ),
        ]
    )
    corpus = text_support._corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=text_support._public_resolver,
            transport=transport,
        ),
    )
    reference = "https://example.gov/markdown-export"
    ingested = corpus.ingest(reference, source_type="markdown")
    assert ingested.result is not None
    source_id = str(ingested.result["source_id"])

    plain = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert plain.status == "done", plain.error
    assert plain.result is not None and plain.result["outcome"] == "revision_created"
    plain_document_id = str(plain.result["document_id"])
    plain_document = next(
        document
        for document in list_documents(corpus.paths.database)
        if document.id == plain_document_id
    )
    assert plain_document.metadata["text_encoding"] == "cp1252"
    assert corpus.rows(
        "SELECT media_type, reported_media_type FROM source_artifacts WHERE id = ?",
        (plain.result["artifact_id"],),
    ) == [("text/markdown", "text/plain; charset=windows-1252")]

    html = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert html.status == "done", html.error
    assert html.result is not None and html.result["outcome"] == "revision_created"
    assert corpus.rows(
        "SELECT media_type, reported_media_type FROM source_artifacts WHERE id = ?",
        (html.result["artifact_id"],),
    ) == [("text/markdown", "application/octet-stream")]
    assert corpus.rows(
        "SELECT normalized_text, location_type FROM source_units "
        "WHERE document_id = ? AND normalized_text <> ''",
        (html.result["document_id"],),
    ) == [(html_revision.decode().strip(), "markdown_block")]


def test_directory_and_manifest_ingest_mixed_markdown_types(tmp_path: Path) -> None:
    directory_data = tmp_path / "directory-corpus"
    sources = tmp_path / "sources"
    nested = sources / "nested"
    nested.mkdir(parents=True)
    (sources / "agenda.pdf").write_bytes(b"%PDF-1.4\nfixture")
    (nested / "notice.html").write_text(
        "<!doctype html><html><body><p>HTML zoning.</p></body></html>", encoding="utf-8"
    )
    (nested / "minutes.txt").write_text("Text transit.", encoding="utf-8")
    (sources / "README.MD").write_text("# Markdown\nParks evidence.\n", encoding="utf-8")

    queued = text_support._RUNNER.invoke(
        app, ["--data-dir", str(directory_data), "ingest", str(sources)]
    )
    directory_corpus = text_support._corpus(directory_data)
    completed = directory_corpus.drain()

    assert queued.exit_code == 0, queued.stdout
    assert "Queued by type: html=1, markdown=1, pdf=1, text=1" in queued.stdout
    assert len(completed) == 4 and all(job.status == "done" for job in completed)
    assert set(directory_corpus.rows("SELECT media_type FROM source_artifacts")) == {
        ("application/pdf",),
        ("text/html",),
        ("text/markdown",),
        ("text/plain",),
    }

    manifest_data = tmp_path / "manifest-corpus"
    markdown_data = tmp_path / "minutes.data"
    markdown_data.write_text("# Explicit Markdown\nWater evidence.\n", encoding="utf-8")
    text_path = tmp_path / "notes.txt"
    text_path.write_text("Plain text evidence.\n", encoding="utf-8")
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        f"""documents:
  - source: {markdown_data.name}
    type: markdown
  - source: {text_path.name}
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
        ("text/markdown",),
        ("text/plain",),
    }


def test_markdown_typed_retrieval_discovery_and_packet_provenance(tmp_path: Path) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    markdown = tmp_path / "contract.md"
    content = (
        "# Council Contract\n\n"
        "## Stormwater\n\n"
        "Council approved a $250,000 stormwater contract with ABC Construction.\n"
    )
    markdown.write_text(content, encoding="utf-8")
    plain = tmp_path / "contract.txt"
    plain.write_text("Unrelated plain text contract record.\n", encoding="utf-8")
    completed = corpus.ingest(
        str(markdown),
        metadata={
            "title": "Contract Notes",
            "body": "City Council",
            "meeting_date": "2026-06-01",
        },
    )
    assert corpus.ingest(str(plain)).status == "done"
    assert completed.result is not None
    document_id = str(completed.result["document_id"])

    keyword = text_support._keyword_results(
        corpus.paths.database,
        "stormwater",
        filters=SearchFilters(source_type="markdown", body="City Council"),
    )
    vector_candidates = LanceDbPassageVectorSearcher(
        corpus.paths.lancedb, max_vector_distance=None
    ).search(corpus.embeddings.embed_query("stormwater"), limit=20)
    vector = merge_search_candidates(
        (),
        vector_candidates,
        database_path=corpus.paths.database,
        limit=20,
        keyword_weight=0.0,
        vector_weight=1.0,
        filters=SearchFilters(source_type="markdown"),
    )

    assert keyword
    assert all(result.document_id == document_id for result in keyword)
    assert all(result.source_type == "markdown" for result in keyword)
    assert any(result.document_id == document_id for result in vector)
    assert all(result.source_type == "markdown" for result in vector)

    profile = create_document_profile(
        corpus.paths.database,
        document_id=document_id,
        text_length=len(content),
        extractor="integration-test",
    )
    unit_id = str(
        corpus.rows(
            "SELECT id FROM source_units WHERE document_id = ? "
            "AND normalized_text LIKE '%stormwater contract%'",
            (document_id,),
        )[0][0]
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
                quote="stormwater contract",
                validation_status="verified",
            ),
        ),
    )
    provenance = load_packet_source_provenance(corpus.paths.database, keyword)
    packet = format_source_packet(
        query="stormwater",
        results=keyword,
        source_provenance=provenance,
    )

    assert profile.source_type == "markdown"
    assert profile.extent_type == "lines"
    assert profile.extent_count == len(content.splitlines())
    assert item.evidence[0].location_type == "markdown_block"
    assert item.evidence[0].source_unit_start_id == unit_id
    source_provenance = provenance[document_id]
    assert source_provenance.source_type == "markdown"
    assert source_provenance.source_kind == "local_path"
    assert source_provenance.submitted_reference == str(markdown)
    assert source_provenance.artifact_hash == hashlib.sha256(markdown.read_bytes()).hexdigest()
    assert source_provenance.processing_generation_id == keyword[0].processing_generation_id
    assert "source type: markdown" in packet
    assert source_provenance.artifact_hash in packet


def test_markdown_duplicate_refresh_reactivation_and_saved_byte_reprocessing(
    tmp_path: Path,
) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = tmp_path / "budget.md"
    original = b"# Budget\n\n## Stormwater\n\nHistoricbudget approved.\n"
    revised = b"# Budget\n\n## Transit\n\nRevisedbudget approved.\n"
    source.write_bytes(original)
    first = corpus.ingest(str(source), metadata={"title": "Budget Notes", "body": "Council"})
    assert first.result is not None
    source_id = str(first.result["source_id"])
    first_document_id = str(first.result["document_id"])

    duplicate = corpus.ingest(str(source))
    unchanged = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    source.write_bytes(revised)
    detected = corpus.ingest(str(source))
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    source.write_bytes(original)
    reactivated = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert duplicate.result is not None and duplicate.result["outcome"] == "duplicate_ignored"
    assert unchanged.result is not None and unchanged.result["outcome"] == "unchanged"
    assert detected.result is not None
    assert detected.result["outcome"] == "change_detected_artifact_saved"
    assert refreshed.status == "done", refreshed.error
    assert refreshed.result is not None and refreshed.result["outcome"] == "revision_created"
    assert reactivated.result is not None
    assert reactivated.result["outcome"] == "revision_reactivated"
    assert reactivated.result["document_id"] == first_document_id
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]
    assert (
        len(
            text_support._keyword_results(
                corpus.paths.database,
                "Revisedbudget",
                include_history=True,
                filters=SearchFilters(source_type="markdown"),
            )
        )
        == 1
    )

    source.unlink()
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "markdown-lifecycle", "2")
    reprocessed = corpus.run(enqueue_reprocessing(corpus.paths.database, [first_document_id])[0])

    assert reprocessed.status == "done", reprocessed.error
    assert reprocessed.result is not None and reprocessed.result["outcome"] == "reprocessed"
    assert not source.exists()
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM processing_generations") == [(3,)]
    current = text_support._keyword_results(
        corpus.paths.database,
        "Historicbudget",
        filters=SearchFilters(source_type="markdown"),
    )
    assert len(current) == 1 and current[0].document_id == first_document_id


def test_failed_markdown_candidate_never_replaces_published_revision(tmp_path: Path) -> None:
    corpus = text_support._corpus(tmp_path / ".newsrag")
    source = tmp_path / "safe-refresh.md"
    source.write_text("# Parks\n\nOriginalevidence approved.\n", encoding="utf-8")
    completed = corpus.ingest(str(source))
    assert completed.result is not None
    source_id = str(completed.result["source_id"])
    document_id = str(completed.result["document_id"])
    vectors_before = text_support._lance_rows(corpus.paths.lancedb, "passage_embeddings")
    corpus.ingestion.processor.passage_vector_store = text_support.FailingPassageIndex(
        LanceDbPassageVectorStore(corpus.paths.lancedb)
    )
    source.write_text("# Roads\n\nUnpublishedcandidate approved.\n", encoding="utf-8")

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
    assert len(text_support._keyword_results(corpus.paths.database, "Originalevidence")) == 1
    assert text_support._keyword_results(corpus.paths.database, "Unpublishedcandidate") == []


def test_extensionless_explicit_markdown_refresh_keeps_type_and_size_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"# Original\n\nOriginalmarkdown notice.\n"
    revised = b"# Revised\n\nRevisedmarkdown notice.\n"
    oversized = b"x" * 65
    transport = text_support.HttpTransport(
        [
            text_support.HttpResponse(
                200, {"content-type": "application/octet-stream"}, (original,)
            ),
            text_support.HttpResponse(
                200, {"content-type": "application/octet-stream"}, (revised,)
            ),
            text_support.HttpResponse(
                200,
                {"content-type": "application/octet-stream", "content-length": str(len(oversized))},
                (oversized,),
            ),
        ]
    )
    corpus = text_support._corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=text_support._public_resolver,
            transport=transport,
        ),
    )
    reference = "https://example.gov/export"
    ingested = corpus.ingest(reference, source_type="markdown")
    assert ingested.status == "done", ingested.error
    assert ingested.result is not None
    source_id = str(ingested.result["source_id"])

    monkeypatch.setattr("newsrag.refresh.TEXT_MAX_SOURCE_BYTES", 64)
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert refreshed.status == "done", refreshed.error
    assert refreshed.result is not None and refreshed.result["outcome"] == "revision_created"
    assert refreshed.payload["base"]["source_type"] == "markdown"
    assert len(text_support._keyword_results(corpus.paths.database, "Revisedmarkdown")) == 1
    assert (
        len(
            text_support._keyword_results(
                corpus.paths.database, "Originalmarkdown", include_history=True
            )
        )
        == 1
    )

    oversized_job = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert TEXT_MAX_SOURCE_BYTES == 10 * 1024 * 1024
    assert oversized_job.status == "failed"
    assert "size_limit" in str(oversized_job.error)
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_artifacts") == [(2,)]

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.packets import (
    PacketError,
    PacketSourceProvenance,
    format_source_packet,
    load_packet_source_provenance,
    write_source_packet,
)
from newsrag.search import SearchFilters, SearchResult
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_source_packet_contains_required_sections_and_cited_evidence() -> None:
    content = format_source_packet(query="stormwater", results=[_search_result()])

    assert "# Source Packet: stormwater" in content
    assert "## Key Evidence" in content
    assert "## Timeline" in content
    assert "## Open Questions" in content
    assert "## Source List" in content
    assert "**Stormwater Report — 2026-05-01 — p. 3**" in content
    assert "> downtown stormwater improvements" in content


def test_source_packet_source_list_includes_available_metadata() -> None:
    content = format_source_packet(query="stormwater", results=[_search_result()])

    assert "title: Stormwater Report" in content
    assert "body: Planning Commission" in content
    assert "meeting date: 2026-05-01" in content
    assert "page 3" in content
    assert "source file: /tmp/stormwater.pdf" in content


def test_mixed_source_packet_uses_typed_citations_and_immutable_provenance() -> None:
    pdf_result = _search_result()
    html_result = _html_search_result()
    provenance = {
        "document-a": PacketSourceProvenance(
            document_id="document-a",
            source_type="pdf",
            source_kind="url",
            submitted_reference="https://example.test/stormwater.pdf",
            resolved_reference="https://cdn.example.test/stormwater.pdf",
            retrieved_at="2026-05-02T10:30:00+00:00",
            artifact_hash="pdf-sha256",
        ),
        "document-html": PacketSourceProvenance(
            document_id="document-html",
            source_type="html",
            source_kind="local_path",
            submitted_reference="/tmp/budget.html",
            resolved_reference="/tmp/budget.html",
            retrieved_at=None,
            artifact_hash="html-sha256",
        ),
    }

    content = format_source_packet(
        query="budget",
        results=[pdf_result, html_result],
        source_provenance=provenance,
    )

    assert "**Stormwater Report — 2026-05-01 — p. 3**" in content
    assert "**Council Update — Budget — block 4**" in content
    source_lines = [line for line in content.splitlines() if line.startswith("- ")]
    pdf_line = next(line for line in source_lines if "pdf-sha256" in line)
    html_line = next(line for line in source_lines if "html-sha256" in line)
    assert "source type: pdf" in pdf_line
    assert "supplied URL: https://example.test/stormwater.pdf" in pdf_line
    assert "final URL: https://cdn.example.test/stormwater.pdf" in pdf_line
    assert "retrieved at: 2026-05-02T10:30:00+00:00" in pdf_line
    assert "artifact SHA-256: pdf-sha256" in pdf_line
    assert "page 3" in pdf_line
    assert "source type: html" in html_line
    assert "supplied path: /tmp/budget.html" in html_line
    assert "artifact SHA-256: html-sha256" in html_line
    assert "page 4" not in html_line
    assert "retrieved at:" not in html_line


def test_source_packet_rejects_incomplete_provenance() -> None:
    with pytest.raises(PacketError, match="Missing source provenance"):
        format_source_packet(
            query="budget",
            results=[_html_search_result()],
            source_provenance={},
        )


def test_load_packet_source_provenance_uses_published_artifacts(tmp_path: Path) -> None:
    database_path = initialize_storage(tmp_path / ".newsrag").database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(
                id, kind, submitted_reference, normalized_reference, resolved_reference
            )
            VALUES(
                'source-html', 'url', 'https://example.test/update',
                'https://example.test/update', 'https://stale.example.test/update.html'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, content_hash, stored_path, acquired_at, state,
                provenance_json
            )
            VALUES(
                'artifact-html', 'source-html', 'text/html', 'artifact-hash',
                '/tmp/artifact.html', '2026-05-02T10:00:00+00:00', 'published',
                '{"submitted_url": "https://example.test/update", "resolved_url": "https://cdn.example.test/update.html", "retrieved_at": "2026-05-02T10:30:00+00:00"}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, title, metadata_json, artifact_id)
            VALUES('document-html', 'Council Update', '{}', 'artifact-html')
            """
        )
        _publish_fixture_revisions(connection, (("source-html", "document-html"),))
        connection.commit()

    loaded = load_packet_source_provenance(database_path, [_html_search_result()])

    assert loaded == {
        "document-html": PacketSourceProvenance(
            document_id="document-html",
            source_type="html",
            source_kind="url",
            submitted_reference="https://example.test/update",
            resolved_reference="https://cdn.example.test/update.html",
            retrieved_at="2026-05-02T10:30:00+00:00",
            artifact_hash="artifact-hash",
            source_id="source-html",
            revision_id="revision-document-html",
            revision_number=1,
            is_current_snapshot=None,
            artifact_id="artifact-html",
            acquired_at="2026-05-02T10:00:00+00:00",
        )
    }


def test_load_packet_source_provenance_rejects_unpublished_results(tmp_path: Path) -> None:
    database_path = initialize_storage(tmp_path / ".newsrag").database

    with pytest.raises(PacketError, match="immutable source provenance"):
        load_packet_source_provenance(database_path, [_search_result()])


def test_write_source_packet_refuses_existing_output_without_overwrite(tmp_path: Path) -> None:
    output_path = tmp_path / "packet.md"
    output_path.write_text("existing", encoding="utf-8")

    with pytest.raises(PacketError, match="Use --overwrite"):
        write_source_packet(output_path, "replacement")

    assert output_path.read_text(encoding="utf-8") == "existing"


def test_write_source_packet_allows_explicit_overwrite(tmp_path: Path) -> None:
    output_path = tmp_path / "packet.md"
    output_path.write_text("existing", encoding="utf-8")

    write_source_packet(output_path, "replacement", overwrite=True)

    assert output_path.read_text(encoding="utf-8") == "replacement"


def test_packet_command_writes_markdown_from_mocked_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "packet.md"
    captured_filters: list[SearchFilters] = []

    class FakeSearchEngine:
        def search(
            self,
            query: str,
            *,
            filters: SearchFilters | None = None,
            include_history: bool = False,
        ) -> list[SearchResult]:
            assert query == "stormwater"
            assert not include_history
            assert filters is not None
            captured_filters.append(filters)
            return [_search_result()]

    monkeypatch.setattr("newsrag.search.build_search_engine", lambda **_: FakeSearchEngine())
    monkeypatch.setattr(
        "newsrag.packets.load_packet_source_provenance",
        lambda *_: {"document-a": _pdf_source_provenance()},
    )

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "packet",
            "stormwater",
            "--out",
            str(output_path),
            "--body",
            "Planning Commission",
            "--since",
            "2025-01-01",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == f"Wrote source packet to {output_path}"
    assert output_path.exists()
    assert "# Source Packet: stormwater" in output_path.read_text(encoding="utf-8")
    assert captured_filters == [SearchFilters(body="Planning Commission", since="2025-01-01")]


def test_packet_command_writes_mixed_source_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / ".newsrag"
    output_path = tmp_path / "mixed-packet.md"
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO sources(
                id, kind, submitted_reference, normalized_reference, resolved_reference
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (
                    "source-pdf",
                    "url",
                    "https://example.test/stormwater.pdf",
                    "https://example.test/stormwater.pdf",
                    "https://cdn.example.test/stormwater.pdf",
                ),
                (
                    "source-html",
                    "local_path",
                    "/tmp/budget.html",
                    "/tmp/budget.html",
                    "/tmp/budget.html",
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, content_hash, stored_path, acquired_at, state
            )
            VALUES(?, ?, ?, ?, ?, '2026-05-02T10:30:00+00:00', 'published')
            """,
            [
                (
                    "artifact-pdf",
                    "source-pdf",
                    "application/pdf",
                    "pdf-sha256",
                    "/tmp/artifact.pdf",
                ),
                (
                    "artifact-html",
                    "source-html",
                    "text/html",
                    "html-sha256",
                    "/tmp/artifact.html",
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO documents(id, title, metadata_json, artifact_id)
            VALUES(?, ?, '{}', ?)
            """,
            [
                ("document-a", "Stormwater Report", "artifact-pdf"),
                ("document-html", "Council Update", "artifact-html"),
            ],
        )
        _publish_fixture_revisions(
            connection,
            (
                ("source-pdf", "document-a"),
                ("source-html", "document-html"),
            ),
        )
        connection.commit()

    class FakeSearchEngine:
        def search(
            self,
            query: str,
            *,
            filters: SearchFilters | None = None,
            include_history: bool = False,
        ) -> list[SearchResult]:
            assert query == "budget"
            assert not include_history
            return [_search_result(), _html_search_result()]

    monkeypatch.setattr("newsrag.search.build_search_engine", lambda **_: FakeSearchEngine())

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "packet",
            "budget",
            "--out",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    content = output_path.read_text(encoding="utf-8")
    assert "Stormwater Report — 2026-05-01 — p. 3" in content
    assert "Council Update — Budget — block 4" in content
    assert "artifact SHA-256: pdf-sha256" in content
    assert "artifact SHA-256: html-sha256" in content


def test_packet_command_requires_overwrite_for_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "packet.md"
    output_path.write_text("existing", encoding="utf-8")

    class FakeSearchEngine:
        def search(
            self,
            query: str,
            *,
            filters: SearchFilters | None = None,
            include_history: bool = False,
        ) -> list[SearchResult]:
            return [_search_result()]

    monkeypatch.setattr("newsrag.search.build_search_engine", lambda **_: FakeSearchEngine())
    monkeypatch.setattr(
        "newsrag.packets.load_packet_source_provenance",
        lambda *_: {"document-a": _pdf_source_provenance()},
    )

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "packet",
            "stormwater",
            "--out",
            str(output_path),
        ],
    )

    assert result.exit_code == 1
    assert "Use --overwrite to replace it" in result.stdout
    assert output_path.read_text(encoding="utf-8") == "existing"

    overwrite_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "packet",
            "stormwater",
            "--out",
            str(output_path),
            "--overwrite",
        ],
    )

    assert overwrite_result.exit_code == 0
    assert "# Source Packet: stormwater" in output_path.read_text(encoding="utf-8")


def _publish_fixture_revisions(
    connection: sqlite3.Connection,
    memberships: tuple[tuple[str, str], ...],
) -> None:
    for source_id, document_id in memberships:
        revision_id = f"revision-{document_id}"
        connection.execute(
            """
            INSERT INTO source_revisions(
                id, source_id, document_id, revision_number, published_at
            )
            VALUES(?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (revision_id, source_id, document_id),
        )
        connection.execute(
            """
            UPDATE sources
            SET current_revision_id = ?, publication_generation = 1
            WHERE id = ?
            """,
            (revision_id, source_id),
        )


def _pdf_source_provenance() -> PacketSourceProvenance:
    return PacketSourceProvenance(
        document_id="document-a",
        source_type="pdf",
        source_kind="local_path",
        submitted_reference="/tmp/stormwater.pdf",
        resolved_reference="/tmp/stormwater.pdf",
        retrieved_at=None,
        artifact_hash="pdf-sha256",
    )


def _html_search_result() -> SearchResult:
    return SearchResult(
        passage_id="passage-html",
        document_id="document-html",
        page_start=4,
        page_end=4,
        text="Council approved the downtown budget amendment.",
        citation="Council Update — Budget — block 4",
        score=0.9,
        keyword_score=0.2,
        vector_score=0.1,
        title="Council Update",
        body="City Council",
        source_path="/tmp/budget.html",
        source_type="html",
        source_unit_start_id="unit-html-4",
        source_unit_end_id="unit-html-4",
    )


def _search_result() -> SearchResult:
    return SearchResult(
        passage_id="passage-a",
        document_id="document-a",
        page_start=3,
        page_end=3,
        text="downtown stormwater improvements",
        citation="Stormwater Report — 2026-05-01 — p. 3",
        score=1.0,
        keyword_score=0.1,
        vector_score=0.2,
        title="Stormwater Report",
        meeting_date="2026-05-01",
        body="Planning Commission",
        document_type="staff_report",
        jurisdiction="Example City",
        source_url="https://example.test/stormwater.pdf",
        source_path="/tmp/stormwater.pdf",
    )

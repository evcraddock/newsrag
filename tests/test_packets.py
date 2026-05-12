from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.packets import PacketError, format_source_packet, write_source_packet
from newsrag.search import SearchFilters, SearchResult

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
        ) -> list[SearchResult]:
            assert query == "stormwater"
            assert filters is not None
            captured_filters.append(filters)
            return [_search_result()]

    monkeypatch.setattr("newsrag.search.build_search_engine", lambda **_: FakeSearchEngine())

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
        ) -> list[SearchResult]:
            return [_search_result()]

    monkeypatch.setattr("newsrag.search.build_search_engine", lambda **_: FakeSearchEngine())

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

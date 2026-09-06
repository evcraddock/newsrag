from __future__ import annotations

import codecs
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from newsrag.adapters import AdapterError, AdapterInput, AdapterResult
from newsrag.markdown_adapter import MarkdownSourceAdapter


def extract(tmp_path: Path, data: bytes, media_type: str = "text/markdown") -> AdapterResult:
    path = tmp_path / "source.md"
    path.write_bytes(data)
    return MarkdownSourceAdapter().extract(AdapterInput(path, "hash", media_type, tmp_path))


def test_structure_line_ranges_and_heading_paths_preserve_all_original_lines(
    tmp_path: Path,
) -> None:
    text = "# Budget\r\n\r\nOpening paragraph\r\ncontinued.\r\n\r\n## Roads\r\n- Repair\r\n  - Paving\r\n\r\n> Quote\r\n> continued\r\n\r\n| A | B |\r\n|---|---|\r\n| C | D |\r\n\r\n```python\r\nprint('literal')\r\n```\r\n\r\n"
    result = extract(tmp_path, text.encode())
    units = result.units
    lines = text.replace("\r\n", "\n").split("\n")[:-1]
    assert "\n".join(u.normalized_text for u in units) == "\n".join(lines)
    assert [u.ordinal for u in units] == list(range(1, len(units) + 1))
    cursor = 1
    for unit in units:
        assert unit.location_type == "markdown_block"
        assert unit.location["line_start"] == cursor
        start, end = int(str(unit.location["line_start"])), int(str(unit.location["line_end"]))
        assert unit.normalized_text == "\n".join(lines[start - 1 : end])
        cursor = end + 1
    assert cursor == len(lines) + 1
    paragraphs = [u for u in units if u.structure["kind"] == "paragraph"]
    assert paragraphs[0].location == {"line_start": 3, "line_end": 4}
    assert paragraphs[0].structure["heading_path"] == ["Budget"]
    assert paragraphs[2].structure["containers"] == [
        "bullet_list",
        "list_item",
        "bullet_list",
        "list_item",
    ]
    assert paragraphs[3].structure["containers"] == ["blockquote"]
    table = next(u for u in units if u.structure["kind"] == "table")
    assert table.location == {"line_start": 13, "line_end": 15}
    assert table.human_label == "Budget — Roads — lines 13–15"
    code = next(u for u in units if u.structure["kind"] == "code_block")
    assert code.structure["info"] == "python"
    assert code.structure["fenced"] is True
    assert code.normalized_text.startswith("```python")
    assert result.metadata_candidates == {"text_encoding": "utf-8"}
    assert result.derived_artifact_path is None


def test_setext_headings_ordered_lists_references_and_unclosed_fences(tmp_path: Path) -> None:
    text = "Council\n=======\n\n1. Vote\n2. Approve\n\n[record]: https://example.gov/record\n\n~~~shell\ncurl https://example.gov/never\n"
    units = extract(tmp_path, text.encode()).units
    assert units[0].location == {"line_start": 1, "line_end": 2}
    assert units[0].structure["heading_path"] == ["Council"]
    assert any(u.structure["containers"] == ["ordered_list", "list_item"] for u in units)
    assert "\n".join(u.normalized_text for u in units) == text.rstrip("\n")
    assert units[-1].structure["kind"] == "code_block"
    assert units[-1].location == {"line_start": 9, "line_end": 10}


def test_markup_and_resources_remain_literal_and_renderer_is_never_called(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("Markdown rendering is forbidden")

    monkeypatch.setattr(MarkdownIt, "render", fail)
    monkeypatch.setattr(MarkdownIt, "renderInline", fail)
    text = '<html><script>alert(1)</script></html>\n\n# Notice\n\n![image](https://example.gov/image.png)\n[local](file:///etc/passwd) <iframe src="https://example.gov"></iframe>\n'
    result = extract(tmp_path, text.encode())
    assert result.units[0].structure["kind"] == "html_block"
    assert "\n".join(u.normalized_text for u in result.units) == text.rstrip("\n")


@pytest.mark.parametrize(
    "data,media,encoding",
    [
        (b"# ASCII", "text/markdown; charset=ascii", "ascii"),
        (codecs.BOM_UTF8 + b"# UTF8", "text/markdown", "utf-8"),
        ("# caf\u00e9".encode("utf-16"), "text/markdown; charset=utf-16", "utf-16-le"),
        (b"# caf\xe9", "text/markdown; charset=windows-1252", "cp1252"),
    ],
)
def test_shared_encoding_policy(tmp_path: Path, data: bytes, media: str, encoding: str) -> None:
    assert extract(tmp_path, data, media).metadata_candidates == {"text_encoding": encoding}


@pytest.mark.parametrize(
    "data,media,error",
    [
        (b"", "text/markdown", "empty"),
        (b" \n ", "text/markdown", "non-whitespace"),
        (b"\x89PNG\r\n", "text/markdown", "non-text file signature"),
        (b"%PDF-1.4", "text/markdown", "contradictory"),
        (b"# a\x00b", "text/markdown", "control characters"),
        (b"# caf\xe9", "text/markdown", "not valid utf-8"),
        (codecs.BOM_UTF8 + b"# Head", "text/markdown; charset=ascii", "conflicts"),
        (b"# Head", "text/markdown; charset=utf-7", "unsupported charset"),
        (b"# Head", "text/html", "requires text/markdown"),
    ],
)
def test_invalid_inputs_rejected(tmp_path: Path, data: bytes, media: str, error: str) -> None:
    with pytest.raises(AdapterError, match=error):
        extract(tmp_path, data, media)


@pytest.mark.parametrize(
    "constant,data,error",
    [
        ("MAX_TEXT_BYTES", b"12345", "byte limit"),
        ("MAX_TEXT_CHARS", b"12345", "character limit"),
        ("MAX_TEXT_LINES", b"1\n\n3\n4\n5", "line limit"),
    ],
)
def test_shared_limits_fail_without_truncating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    data: bytes,
    error: str,
) -> None:
    monkeypatch.setattr(f"newsrag.text_adapter.{constant}", 4)
    with pytest.raises(AdapterError, match=error):
        extract(tmp_path, data)


def test_nested_heading_paths_reset_without_losing_blank_lines(tmp_path: Path) -> None:
    result = extract(tmp_path, b"# A\n\n### Deep\n\n## B\n\n# C\n\n")
    headings = [u for u in result.units if u.structure["kind"] == "heading"]
    assert [u.structure["heading_path"] for u in headings] == [
        ["A"],
        ["A", "Deep"],
        ["A", "B"],
        ["C"],
    ]
    assert result.units[-1].location["line_end"] == 8

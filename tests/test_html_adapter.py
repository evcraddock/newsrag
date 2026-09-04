from __future__ import annotations

import socket
from pathlib import Path

import pytest

import newsrag.html_adapter as html_adapter
from newsrag.adapters import AdapterError, AdapterInput
from newsrag.html_adapter import HTML_EXTRACTOR, StaticHtmlSourceAdapter
from newsrag.sources import HTML_BLOCK_LOCATION_TYPE

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "html"


def test_article_extraction_is_deterministic_and_preserves_structure(tmp_path: Path) -> None:
    adapter = StaticHtmlSourceAdapter()
    artifact = _adapter_input(FIXTURE_DIRECTORY / "article.html", tmp_path)

    first = adapter.extract(artifact)
    second = adapter.extract(artifact)

    assert first == second
    assert first.media_type == "text/html"
    assert first.extractor == HTML_EXTRACTOR
    assert first.metadata_candidates == {
        "language": "en",
        "title": "Council Update",
        "author": "City Clerk",
        "publication_time": "2026-09-03T14:30:00Z",
    }
    assert [unit.normalized_text for unit in first.units] == [
        "Council Update",
        "Read the linked report for details.",
        "Budget",
        "General fund",
        "Capital projects",
        "Utility fund",
        "Public funds require public records.",
        "first line\nsecond line",
        "Revenue forecast",
        "Proposed allocations",
        "Fund | Amount",
        "General | $10",
    ]
    assert [unit.structure["element_kind"] for unit in first.units] == [
        "heading",
        "paragraph",
        "heading",
        "list_item",
        "list_item",
        "list_item",
        "quotation",
        "preformatted",
        "caption",
        "caption",
        "table_row",
        "table_row",
    ]
    assert first.units[0].structure == {
        "element_kind": "heading",
        "source_tag": "h1",
        "heading_path": ["Council Update"],
        "heading_level": 1,
    }
    assert first.units[1].structure["heading_path"] == ["Council Update"]
    assert first.units[2].structure["heading_path"] == ["Council Update", "Budget"]
    assert first.units[-1].structure["heading_path"] == ["Council Update", "Budget"]
    assert [unit.ordinal for unit in first.units] == list(range(1, 13))
    assert all(unit.location_type == HTML_BLOCK_LOCATION_TYPE for unit in first.units)
    assert [unit.location for unit in first.units] == [
        {"block_number": block_number} for block_number in range(1, 13)
    ]
    assert first.units[0].human_label == "Council Update — block 1"
    assert first.units[2].human_label == "Council Update — Budget — block 3"
    assert all(unit.extractor == HTML_EXTRACTOR for unit in first.units)

    extracted_text = " ".join(unit.normalized_text for unit in first.units)
    for excluded_text in (
        "Site banner",
        "Navigation text",
        "Outside article text",
        "script text",
        "Form text",
        "Sidebar text",
        "Frame text",
        "Article footer",
        "Main fallback must not be selected",
        "Remote chart",
    ):
        assert excluded_text not in extracted_text


@pytest.mark.parametrize(
    ("fixture_name", "expected_units"),
    [
        ("main.html", ["Planning Notice", "Main content."]),
        ("role-main.html", ["Transportation Notice", "Role main content."]),
        ("body.html", ["Body Notice", "Body fallback content."]),
    ],
)
def test_main_role_main_and_body_fallback_fixtures(
    tmp_path: Path,
    fixture_name: str,
    expected_units: list[str],
) -> None:
    result = StaticHtmlSourceAdapter().extract(
        _adapter_input(FIXTURE_DIRECTORY / fixture_name, tmp_path)
    )

    assert [unit.normalized_text for unit in result.units] == expected_units


def test_xhtml_media_type_and_declared_encoding_are_supported(tmp_path: Path) -> None:
    result = StaticHtmlSourceAdapter().extract(
        _adapter_input(
            FIXTURE_DIRECTORY / "xhtml.xhtml",
            tmp_path,
            media_type="application/xhtml+xml",
        )
    )

    assert result.media_type == "application/xhtml+xml"
    assert result.metadata_candidates == {"language": "en-US", "title": "XHTML Notice"}
    assert [unit.normalized_text for unit in result.units] == [
        "XHTML Notice",
        "Strict source text.",
    ]


def test_declared_windows_1252_encoding_is_decoded_strictly(tmp_path: Path) -> None:
    path = tmp_path / "windows.html"
    path.write_bytes(
        b'<!doctype html><html><head><meta charset="windows-1252"></head>'
        b"<body><p>Caf\xe9 notice.</p></body></html>"
    )

    result = StaticHtmlSourceAdapter().extract(_adapter_input(path, tmp_path))

    assert result.units[0].normalized_text == "Café notice."


@pytest.mark.parametrize(
    ("content", "media_type", "error"),
    [
        (b"", "text/html", "is empty"),
        (b"plain text", "text/html", "document signature"),
        (b"<!doctype html><html><body>\xff</body></html>", "text/html", "valid utf-8"),
        (
            b'<!doctype html><html><head><meta charset="utf-7"></head><body><p>x</p></body></html>',
            "text/html",
            "unsupported encoding",
        ),
        (
            b"<!doctype html><html><body><p>broken</div></body></html>",
            "text/html",
            "malformed",
        ),
        (
            b"<!doctype html><html><body><script>only active text</script></body></html>",
            "text/html",
            "no evidentiary content",
        ),
        (
            b"<!doctype html><html><body><article><p>one</p></article>"
            b"<article><p>two</p></article></body></html>",
            "text/html",
            "multiple article",
        ),
        (
            b"<!doctype html><html><body><main><p>one</p></main>"
            b'<div role="main"><p>two</p></div></body></html>',
            "text/html",
            "multiple main",
        ),
        (
            b'<!DOCTYPE html SYSTEM "https://other.example/schema.dtd">'
            b"<html><body><p>unsafe</p></body></html>",
            "text/html",
            "unsafe external declaration",
        ),
        (
            b"<!doctype html><html><body><p>text</p></body></html>",
            "application/pdf",
            "does not accept media type",
        ),
    ],
)
def test_invalid_html_fails_clearly(
    tmp_path: Path,
    content: bytes,
    media_type: str,
    error: str,
) -> None:
    path = tmp_path / "invalid.html"
    path.write_bytes(content)

    with pytest.raises(AdapterError, match=error):
        StaticHtmlSourceAdapter().extract(_adapter_input(path, tmp_path, media_type=media_type))


def test_input_element_nesting_and_text_limits_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StaticHtmlSourceAdapter()
    path = tmp_path / "limited.html"
    path.write_text(
        "<!doctype html><html><body><div><p>evidence text</p></div></body></html>",
        encoding="utf-8",
    )

    monkeypatch.setattr(html_adapter, "MAX_HTML_BYTES", 10)
    with pytest.raises(AdapterError, match="input limit"):
        adapter.extract(_adapter_input(path, tmp_path))

    monkeypatch.setattr(html_adapter, "MAX_HTML_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(html_adapter, "MAX_HTML_ELEMENTS", 3)
    with pytest.raises(AdapterError, match="element limit"):
        adapter.extract(_adapter_input(path, tmp_path))

    monkeypatch.setattr(html_adapter, "MAX_HTML_ELEMENTS", 100_000)
    monkeypatch.setattr(html_adapter, "MAX_HTML_NESTING_DEPTH", 3)
    with pytest.raises(AdapterError, match="nesting limit"):
        adapter.extract(_adapter_input(path, tmp_path))

    monkeypatch.setattr(html_adapter, "MAX_HTML_NESTING_DEPTH", 256)
    monkeypatch.setattr(html_adapter, "MAX_EXTRACTED_TEXT_CHARS", 4)
    with pytest.raises(AdapterError, match="text limit"):
        adapter.extract(_adapter_input(path, tmp_path))


def test_extraction_does_not_execute_or_fetch_embedded_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("HTML extraction attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    path = tmp_path / "offline.html"
    path.write_text(
        """<!doctype html>
        <html><head>
          <link rel="stylesheet" href="https://other.example/site.css">
          <script src="https://other.example/site.js">script content</script>
        </head><body><article>
          <p>Keep <a href="https://other.example/page">visible link text</a>.</p>
          <img src="https://other.example/image.png" alt="remote image">
          <iframe src="https://other.example/frame">frame content</iframe>
        </article></body></html>""",
        encoding="utf-8",
    )

    result = StaticHtmlSourceAdapter().extract(_adapter_input(path, tmp_path))

    assert [unit.normalized_text for unit in result.units] == ["Keep visible link text."]


def _adapter_input(
    artifact_path: Path,
    work_dir: Path,
    *,
    media_type: str = "text/html",
) -> AdapterInput:
    return AdapterInput(
        artifact_path=artifact_path,
        content_hash="html-hash",
        media_type=media_type,
        work_dir=work_dir,
    )

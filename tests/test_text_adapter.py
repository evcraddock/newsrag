from __future__ import annotations

import codecs
import hashlib
from pathlib import Path

import pytest

from newsrag.adapters import AdapterError, AdapterInput
from newsrag.text_adapter import PlainTextSourceAdapter


def extract(tmp_path: Path, data: bytes, media_type: str = "text/plain") -> object:
    path = tmp_path / "source.txt"
    path.write_bytes(data)
    return PlainTextSourceAdapter().extract(
        AdapterInput(
            artifact_path=path,
            content_hash=hashlib.sha256(data).hexdigest(),
            media_type=media_type,
            work_dir=tmp_path / "work",
        )
    )


def test_physical_lines_preserve_blank_lines_spaces_tabs_and_original_numbers(
    tmp_path: Path,
) -> None:
    from newsrag.adapters import AdapterResult

    result = extract(tmp_path, b"  first\tline\r\n\r\nthird\rfourth\n\n")
    assert isinstance(result, AdapterResult)
    assert [unit.normalized_text for unit in result.units] == [
        "  first\tline",
        "",
        "third",
        "fourth",
        "",
    ]
    assert [unit.ordinal for unit in result.units] == [1, 2, 3, 4, 5]
    assert all(
        unit.location == {"line_start": unit.ordinal, "line_end": unit.ordinal}
        for unit in result.units
    )
    assert all(unit.location_type == "text_line" for unit in result.units)
    assert result.metadata_candidates == {"text_encoding": "utf-8"}
    assert result.derived_artifact_path is None


@pytest.mark.parametrize(
    "data,media_type,encoding",
    [
        (b"ASCII", "text/plain", "utf-8"),
        (b"ASCII", "text/plain; charset=US-ASCII", "ascii"),
        ("caf\u00e9".encode(), "text/plain", "utf-8"),
        (codecs.BOM_UTF8 + "caf\u00e9".encode(), 'text/plain; charset="UTF8"', "utf-8"),
        ("caf\u00e9".encode("utf-16"), "text/plain; charset=utf-16", "utf-16-le"),
        (codecs.BOM_UTF16_BE + "caf\u00e9".encode("utf-16-be"), "text/plain", "utf-16-be"),
        (b"caf\xe9", "text/plain; charset=iso-8859-1", "iso8859-1"),
        (b"\x93quoted\x94", "text/plain; charset=windows-1252", "cp1252"),
        (b"literal", "text/plain; charset=utf8; charset=UTF-8", "utf-8"),
    ],
)
def test_supported_declared_or_bom_encodings(
    tmp_path: Path, data: bytes, media_type: str, encoding: str
) -> None:
    from newsrag.adapters import AdapterResult

    result = extract(tmp_path, data, media_type)
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 1
    assert result.metadata_candidates["text_encoding"] == encoding
    assert not result.units[0].normalized_text.startswith("\ufeff")


@pytest.mark.parametrize(
    "data,media_type,error",
    [
        (b"", "text/plain", "empty"),
        (b" \r\n\t ", "text/plain", "non-whitespace"),
        (b"caf\xe9", "text/plain", "not valid utf-8"),
        (b"hello", "text/plain; charset=utf-7", "unsupported charset"),
        (b"hello", "text/plain; charset=unknown", "unsupported charset"),
        (b"hello", "text/plain; charset=", "invalid charset"),
        (b"hello", "text/plain; charset=utf8; charset=ascii", "conflicting charsets"),
        (codecs.BOM_UTF8 + b"hello", "text/plain; charset=cp1252", "conflicts"),
        (codecs.BOM_UTF16_BE + b"\x00a", "text/plain; charset=utf-16-le", "conflicts"),
        (codecs.BOM_UTF16_LE + b"a\x00", "text/plain; charset=utf-16-be", "conflicts"),
        (b"a\x00", "text/plain; charset=utf-16-le", "require a byte-order mark"),
        (codecs.BOM_UTF32_LE + b"a\x00\x00\x00", "text/plain", "UTF-32"),
        (b"a\x00b", "text/plain", "control characters"),
        (b"a\x1b[31mb", "text/plain", "control characters"),
        ("a\u202eb".encode(), "text/plain", "control characters"),
        (b"PK\x03\x04whatever", "text/plain", "non-text file signature"),
        (b"GIF89a printable", "text/plain", "non-text file signature"),
        (b"%PDF-1.4\nprintable", "text/plain", "contradictory"),
        (b"<!doctype html><html>hello</html>", "text/plain", "contradictory"),
        (b"<?xml version='1.0'?><data>hello</data>", "text/plain", "contradictory"),
        (b"plain", "application/pdf", "requires text/plain"),
    ],
)
def test_invalid_text_fails_without_silent_fallback(
    tmp_path: Path, data: bytes, media_type: str, error: str
) -> None:
    with pytest.raises(AdapterError, match=error):
        extract(tmp_path, data, media_type)


@pytest.mark.parametrize(
    "constant,data,error",
    [
        ("MAX_TEXT_BYTES", b"12345", "byte limit"),
        ("MAX_TEXT_CHARS", b"12345", "character limit"),
        ("MAX_TEXT_LINES", b"1\n\n3\n4\n5", "line limit"),
    ],
)
def test_size_and_line_limits_fail_without_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str, data: bytes, error: str
) -> None:
    monkeypatch.setattr(f"newsrag.text_adapter.{constant}", 4)
    with pytest.raises(AdapterError, match=error):
        extract(tmp_path, data)


def test_missing_and_nonregular_files_fail_clearly(tmp_path: Path) -> None:
    for path in (tmp_path / "missing", tmp_path):
        with pytest.raises(AdapterError, match="regular file"):
            PlainTextSourceAdapter().extract(AdapterInput(path, "hash", "text/plain", tmp_path))


@pytest.mark.parametrize(
    "hint,media_type,filename,expected",
    [
        (None, "text/plain", "", True),
        (None, None, "notes.txt", True),
        (None, "application/octet-stream", "notes.txt", True),
        ("text", None, "unknown", True),
        (None, None, "unknown", False),
        (None, None, "notes.md", False),
        ("text", "image/png", "notes.txt", False),
        (None, "application/pdf", "notes.txt", False),
    ],
)
def test_text_selection_requires_evidence_and_rejects_contradictory_mime(
    tmp_path: Path,
    hint: str | None,
    media_type: str | None,
    filename: str,
    expected: bool,
) -> None:
    from newsrag.adapters import (
        AdapterSelectionError,
        RegisteredSourceAdapter,
        SourceAdapterRegistry,
    )

    path = tmp_path / "artifact"
    path.write_bytes(b"ordinary printable text")
    registry = SourceAdapterRegistry(
        (
            RegisteredSourceAdapter(
                source_type="text",
                media_type="text/plain",
                extensions=(".txt",),
                signatures=(),
                adapter=PlainTextSourceAdapter(),
            ),
        )
    )
    if expected:
        assert (
            registry.select(
                artifact_path=path,
                source_type_hint=hint,
                reported_media_type=media_type,
                filename=filename,
            ).source_type
            == "text"
        )
    else:
        with pytest.raises(AdapterSelectionError):
            registry.select(
                artifact_path=path,
                source_type_hint=hint,
                reported_media_type=media_type,
                filename=filename,
            )


def test_plain_text_does_not_parse_markdown_or_infer_civic_metadata(tmp_path: Path) -> None:
    from newsrag.adapters import AdapterResult

    result = extract(tmp_path, b"# Council\n**Budget** [link](https://example.com)\n2026-01-01")
    assert isinstance(result, AdapterResult)
    assert result.units[0].normalized_text == "# Council"
    assert result.units[1].normalized_text.startswith("**Budget**")
    assert result.metadata_candidates == {"text_encoding": "utf-8"}

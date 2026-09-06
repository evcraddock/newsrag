from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from newsrag.adapters import AdapterError, AdapterInput, AdapterResult
from newsrag.csv_adapter import CsvSourceAdapter, normalize_csv_options


def extract(
    tmp_path: Path,
    data: bytes,
    *,
    media: str = "text/csv",
    options: dict[str, object] | None = None,
) -> AdapterResult:
    path = tmp_path / "source"
    path.write_bytes(data)
    return CsvSourceAdapter().extract(AdapterInput(path, "hash", media, tmp_path, options or {}))


def test_logical_records_literal_values_and_blank_origins(tmp_path: Path) -> None:
    result = extract(tmp_path, b'A,A,\r\nx,0012,"a\r\nb"\r\n\r\nA,A,\r\n,,\r\n')
    table = result.tables[0]
    assert len(result.units) == 5
    assert table.row_end == 5 and table.column_end == 3
    assert [c.value for c in table.cells[3:6]] == ["x", "0012", "a\nb"]
    assert [c.kind for c in table.cells[6:9]] == ["blank"] * 3
    assert [c.presence for c in table.cells[6:9]] == ["blank-record"] * 3
    assert [c.value for c in table.cells[12:15]] == [""] * 3
    assert result.units[1].location["row_start"] == 2
    assert table.metadata["interpretation"] == {
        "encoding": "utf-8",
        "delimiter": "comma",
        "header": "present",
        "media_type": "text/csv",
    }


@pytest.mark.parametrize(
    "delimiter,byte", [("comma", b","), ("semicolon", b";"), ("tab", b"\t"), ("pipe", b"|")]
)
def test_explicit_delimiters_and_no_header(tmp_path: Path, delimiter: str, byte: bytes) -> None:
    result = extract(
        tmp_path,
        b"\n  a  " + byte + b'"=1+1"\n\n',
        options={"csv": {"delimiter": delimiter, "header": "absent"}},
    )
    assert result.tables[0].header_row is None
    assert result.tables[0].row_end == 3
    assert [cell.value for cell in result.tables[0].cells[2:4]] == ["  a  ", "=1+1"]


@pytest.mark.parametrize(
    "data",
    [
        b"a,b\nx\n",
        b"a,b\nx,y,z",
        b'a,b\nx,"y',
        b'a,b\nx,y"z',
        b'a,b\nx,"y" z',
        b"\na,b\nx,y",
        b"a,b\n",
        b"\n\n",
        b"a\n\x00",
        b"%PDF-x",
        b"PK\x03\x04",
        b"<html>not csv",
        b"a\n\xff",
    ],
)
def test_invalid_inputs_fail_closed(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(AdapterError):
        extract(tmp_path, data)


@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("cp1252", b""),
        ("iso8859-1", b""),
    ],
)
def test_supported_encodings(tmp_path: Path, encoding: str, bom: bytes) -> None:
    result = extract(
        tmp_path, bom + "Name\nCafé\n".encode(encoding), media=f"text/csv; charset={encoding}"
    )
    assert result.tables[0].cells[1].value == "Café"


@pytest.mark.parametrize(
    "media,options,data",
    [
        ("text/csv; charset=latin-1", {"encoding": "utf-8"}, b"a\nb"),
        ("text/csv; charset=utf-16", {}, b"a\nb"),
        ("text/csv; charset=utf-8", {}, codecs.BOM_UTF16_LE + "a\nb".encode("utf-16-le")),
        ("text/csv; header=absent", {}, b"a\nb"),
        ("text/csv; header=invalid", {}, b"a\nb"),
        ("text/csv; header=present; header=absent", {}, b"a\nb"),
        ("application/pdf", {}, b"a\nb"),
    ],
)
def test_contradictory_declarations(
    tmp_path: Path, media: str, options: dict[str, object], data: bytes
) -> None:
    with pytest.raises(AdapterError):
        extract(tmp_path, data, media=media, options={"csv": options})


@pytest.mark.parametrize(
    "options",
    [
        {"delimiter": ","},
        {"header": True},
        {"encoding": "utf-32"},
        {"sniff": True},
        {"delimiter": None},
    ],
)
def test_invalid_recipes(options: dict[str, object]) -> None:
    with pytest.raises(AdapterError):
        normalize_csv_options(options)


def test_explicit_utf16_endianness_agrees_with_generic_http_charset(tmp_path: Path) -> None:
    result = extract(
        tmp_path,
        codecs.BOM_UTF16_LE + "Name\nRoads\n".encode("utf-16-le"),
        media="text/csv; charset=utf-16",
        options={"csv": {"encoding": "utf-16-le"}},
    )
    assert result.tables[0].cells[1].value == "Roads"


def test_serialized_cell_limit_and_width_limit(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="8192"):
        extract(tmp_path, b"a\n" + b"x" * 8193)
    with pytest.raises(AdapterError, match="column"):
        extract(tmp_path, b",".join([b"a"] * 257) + b"\n" + b",".join([b"b"] * 257))

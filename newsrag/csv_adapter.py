"""Strict deterministic CSV parsing; all fields remain literal strings."""

from __future__ import annotations

import codecs
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.sources import CSV_MEDIA_ALIASES, CSV_MEDIA_TYPE
from newsrag.tabular import MAX_CELLS, MAX_VALUE_CHARS, Cell, Table, TableError, render_cells
from newsrag.text_adapter import read_text_lines

CSV_EXTRACTOR = ExtractorIdentity("strict-csv", "1")
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}
ENCODINGS = {"ascii", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "iso8859-1"}
MAX_RECORDS = 100_000
MAX_COLUMNS = 256


def normalize_csv_options(value: object) -> dict[str, str]:
    """Validate a requested recipe before enqueueing, without inspecting source bytes."""
    if not isinstance(value, Mapping) or set(value) - {"delimiter", "header", "encoding"}:
        raise AdapterError("CSV options must contain only delimiter, header, and encoding")
    result = {"delimiter": "comma", "header": "present", "encoding": "auto"}
    for key, setting in value.items():
        if not isinstance(setting, str):
            raise AdapterError(f"CSV {key} must be a string")
        result[key] = setting
    if result["delimiter"] not in DELIMITERS:
        raise AdapterError("CSV delimiter must be comma, semicolon, tab, or pipe")
    if result["header"] not in {"present", "absent"}:
        raise AdapterError("CSV header must be present or absent")
    if result["encoding"] != "auto":
        try:
            encoding = codecs.lookup(result["encoding"]).name
        except LookupError as exc:
            raise AdapterError("CSV encoding is unsupported") from exc
        if encoding not in ENCODINGS:
            raise AdapterError("CSV encoding is unsupported")
        result["encoding"] = encoding
    return result


@dataclass(frozen=True)
class CsvSourceAdapter:
    """Decode and parse a bounded rectangular CSV plane without dialect sniffing."""

    format_version: str = "1"

    @property
    def media_types(self) -> Sequence[str]:
        return (CSV_MEDIA_TYPE, *CSV_MEDIA_ALIASES)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        if artifact.media_type.partition(";")[0].strip().lower() not in self.media_types:
            raise AdapterError("CSV adapter requires a CSV media type")
        recipe = normalize_csv_options(artifact.options.get("csv", {}))
        message = Message()
        message["content-type"] = artifact.media_type
        for key, value in (message.get_params() or [])[1:]:
            if key.lower() == "header" and value != recipe["header"]:
                raise AdapterError(
                    "CSV HTTP header parameter conflicts with the recipe; supply --csv-header present or absent"
                )
        decoding_type = artifact.media_type
        if recipe["encoding"] != "auto":
            decoding_type += f'; charset="{recipe["encoding"]}"'
        lines, encoding = read_text_lines(artifact.artifact_path, decoding_type, allow_blank=True)
        # A final terminator does not create a record; joining physical lines retains
        # real blank records, including one empty record at the end of the list.
        text = "\n".join(lines)
        records: list[list[str]] = []
        observed_width = 0
        for record in _records(text, DELIMITERS[recipe["delimiter"]], physical_lines=len(lines)):
            if record and not observed_width:
                observed_width = len(record)
            if record and len(record) != observed_width:
                raise AdapterError(
                    f"CSV logical record {len(records) + 1} has {len(record)} fields; expected {observed_width}"
                )
            if observed_width * (len(records) + 1) > MAX_CELLS:
                raise AdapterError("CSV exceeds the 1000000 rectangular-position limit")
            records.append(record)
        if not records or not any(records):
            raise AdapterError("CSV artifact contains no nonempty logical record")
        if recipe["header"] == "present" and not records[0]:
            raise AdapterError("CSV header requires a nonblank first logical record")
        width = len(next(record for record in records if record))
        if width > MAX_COLUMNS:
            raise AdapterError("CSV exceeds the 256-column limit")
        if width * len(records) > MAX_CELLS:
            raise AdapterError("CSV exceeds the 1000000 rectangular-position limit")
        cells: list[Cell] = []
        units: list[CanonicalSourceUnit] = []
        for row, values in enumerate(records, 1):
            if values and len(values) != width:
                raise AdapterError(
                    f"CSV logical record {row} has {len(values)} fields; expected {width}"
                )
            row_cells = (
                tuple(
                    Cell(row, column, value=value, raw=value)
                    for column, value in enumerate(values, 1)
                )
                if values
                else tuple(
                    Cell(row, column, kind="blank", presence="blank-record")
                    for column in range(1, width + 1)
                )
            )
            cells.extend(row_cells)
            units.append(
                CanonicalSourceUnit(
                    ordinal=row,
                    location_type="table_row",
                    location={
                        "table_id": "sheet-1",
                        "sheet_index": 1,
                        "row_start": row,
                        "row_end": row,
                        "column_start": 1,
                        "column_end": width,
                    },
                    human_label=f"row {row}",
                    normalized_text=render_cells(row_cells),
                    structure={"kind": "table_row", "blank_record": not values},
                    extractor=CSV_EXTRACTOR,
                )
            )
        table = Table(
            "sheet-1",
            1,
            "csv",
            1,
            len(records),
            1,
            width,
            tuple(cells),
            header_row=1 if recipe["header"] == "present" else None,
            metadata={
                "requested_recipe": recipe,
                "interpretation": {
                    "encoding": encoding,
                    "delimiter": recipe["delimiter"],
                    "header": recipe["header"],
                    "media_type": artifact.media_type,
                },
            },
        )
        try:
            table.validate()
        except TableError as exc:
            raise AdapterError(str(exc)) from exc
        if not any(cell.searchable and cell.row != table.header_row for cell in cells):
            raise AdapterError("CSV artifact contains no searchable data rows")
        return AdapterResult(
            media_type=CSV_MEDIA_TYPE,
            units=tuple(units),
            extractor=CSV_EXTRACTOR,
            tables=(table,),
            metadata_candidates={"csv_encoding": encoding},
        )


def _records(text: str, delimiter: str, *, physical_lines: int) -> Iterator[list[str]]:
    """Parse strict quoting with bounded fields; Python csv's permissive quotes are unsuitable."""
    fields: list[str] = []
    value: list[str] = []
    state = "start"
    count = 0
    touched = False
    for character in text:
        if state == "quoted":
            if character == '"':
                state = "closed"
            else:
                value.append(character)
        elif state == "closed" and character == '"':
            value.append('"')
            state = "quoted"
        elif character == delimiter or character == "\n":
            if character == delimiter or touched or fields:
                fields.append("".join(value))
            value = []
            if len(fields) > MAX_COLUMNS:
                raise AdapterError("CSV exceeds the 256-column limit")
            if character == "\n":
                count += 1
                if count > MAX_RECORDS:
                    raise AdapterError("CSV exceeds the 100000 logical-record limit")
                yield fields
                fields = []
                touched = False
            else:
                touched = True
            state = "start"
            continue
        elif state == "closed":
            raise AdapterError("CSV has text after a closing quote")
        elif character == '"':
            if state != "start":
                raise AdapterError("CSV has a quote inside an unquoted field")
            state = "quoted"
        else:
            value.append(character)
            state = "unquoted"
        touched = True
        if len(value) > MAX_VALUE_CHARS:
            raise AdapterError("CSV field exceeds the 8192-character limit")
    if state == "quoted":
        raise AdapterError("CSV contains an unclosed quoted field")
    if touched or fields:
        fields.append("".join(value))
    # lines includes real trailing blanks, but never the phantom line after a
    # terminator. Even empty text represents a record if a physical line exists.
    if touched or fields or physical_lines:
        count += 1
        if count > MAX_RECORDS:
            raise AdapterError("CSV exceeds the 100000 logical-record limit")
        if len(fields) > MAX_COLUMNS:
            raise AdapterError("CSV exceeds the 256-column limit")
        yield fields

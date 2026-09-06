"""Bounded SpreadsheetML extraction, without a spreadsheet runtime or formula evaluation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from lxml import etree  # type: ignore[import-untyped]

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.tabular import (
    MAX_CELLS,
    MAX_METADATA_BYTES,
    MAX_TOTAL_VALUE_CHARS,
    MAX_VALUE_CHARS,
    RENDERER_VERSION,
    Cell,
    Table,
    TableError,
    TableRegion,
    evidence_cells,
    header_at,
    interval_overlaps,
    render_cells,
    serialized,
)
from newsrag.xlsx_package import (
    REL_NS,
    XLSX_NS,
    XLSX_PACKAGE_VERSION,
    XlsxPackage,
    load_xlsx_package,
)
from newsrag.xlsx_structure import validate_structure

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_EXTRACTOR = ExtractorIdentity("xlsx-xml", "1")
S = "{" + XLSX_NS + "}"
R = "{" + REL_NS + "}"
MAX_SHEETS = 32
MAX_ROWS = 200_000
MAX_NATIVE_TABLES = 128
MAX_MERGES = 10_000
EXCEL_ROWS = 1_048_576
EXCEL_COLUMNS = 16_384
_COORDINATE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")
_ERROR_VALUES = frozenset(
    {"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA"}
)


def normalize_xlsx_options(value: object) -> dict[str, object]:
    """Validate a header-map recipe before acquisition; names resolve in the worker."""
    if not isinstance(value, Mapping) or set(value) - {"header_rows"}:
        raise AdapterError("XLSX options must contain only header_rows")
    rows = value.get("header_rows", {})
    if not isinstance(rows, Mapping):
        raise AdapterError("XLSX header_rows must be an object mapping exact sheet names to rows")
    if len(rows) > MAX_SHEETS:
        raise AdapterError("XLSX header_rows exceeds the 32-sheet limit")
    result: dict[str, int] = {}
    for name, row in rows.items():
        _sheet_name(name)
        if type(row) is not int or not 1 <= row <= EXCEL_ROWS:
            raise AdapterError("XLSX header rows must be positive native worksheet row numbers")
        result[name] = row
    return {"header_rows": result}


def _sheet_name(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 31
        or any(char in value for char in "[]:*?/\\")
        or value.startswith("'")
        or value.endswith("'")
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    ):
        raise AdapterError("XLSX has an invalid worksheet name")


def _integer(value: str | None, label: str, *, minimum: int = 0, maximum: int = 2**32 - 1) -> int:
    if value is None or re.fullmatch(r"[0-9]+", value) is None or len(value) > 10:
        raise AdapterError(f"XLSX {label} must be an integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise AdapterError(f"XLSX {label} is outside its supported bounds")
    return number


def _boolean(value: str | None, label: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if value not in {"0", "1", "true", "false"}:
        raise AdapterError(f"XLSX {label} must be a boolean")
    return value in {"1", "true"}


def _coordinate(value: str | None) -> tuple[int, int]:
    match = _COORDINATE.fullmatch(value or "")
    if match is None:
        raise AdapterError("XLSX has an invalid native cell coordinate")
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    row = int(match[2])
    if row > EXCEL_ROWS or column > EXCEL_COLUMNS:
        raise AdapterError("XLSX coordinate exceeds native Excel limits")
    return row, column


def _bounds(value: str | None) -> tuple[int, int, int, int]:
    pieces = (value or "").split(":")
    if len(pieces) not in {1, 2}:
        raise AdapterError("XLSX requires one rectangular reference")
    first, last = _coordinate(pieces[0]), _coordinate(pieces[-1])
    if last[0] < first[0] or last[1] < first[1]:
        raise AdapterError("XLSX rectangle is reversed")
    return first[0], last[0], first[1], last[1]


def _inside(bounds: tuple[int, int, int, int], row: int, column: int) -> bool:
    return bounds[0] <= row <= bounds[1] and bounds[2] <= column <= bounds[3]


def _overlaps(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    return (
        left[0] <= right[1] and right[0] <= left[1] and left[2] <= right[3] and right[2] <= left[3]
    )


def _region(index: int, bounds: tuple[int, int, int, int]) -> dict[str, Any]:
    return TableRegion(f"sheet-{index}", index, "cells", *bounds).to_dict()


def _children(element: etree._Element, allowed: set[str]) -> None:
    if (element.text or "").strip() or any((child.tail or "").strip() for child in element):
        raise AdapterError("XLSX structural element contains unsupported mixed text")
    for child in element:
        if child.tag not in {S + name for name in allowed}:
            raise AdapterError(f"XLSX {etree.QName(element).localname} has an unsupported element")


def _one(element: etree._Element, name: str, *, required: bool = False) -> etree._Element | None:
    matches = element.findall(S + name)
    if len(matches) > 1 or (required and not matches):
        raise AdapterError(f"XLSX requires {'exactly one' if required else 'at most one'} {name}")
    return matches[0] if matches else None


def _count(element: etree._Element, label: str) -> None:
    if element.get("count") is not None and _integer(element.get("count"), label) != len(element):
        raise AdapterError(f"XLSX {label} count does not match stored entries")


def _literal(value: str, *, escaped: bool = False) -> str:
    if escaped:
        value = _ESCAPE.sub(lambda match: chr(int(match[1], 16)), value)
        # OOXML encodes UTF-16 code units; combine valid surrogate pairs, reject lone units.
        try:
            value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
        except UnicodeError as exc:
            raise AdapterError("XLSX string has an invalid escaped surrogate") from exc
    if len(value) > MAX_VALUE_CHARS:
        raise AdapterError("XLSX value or formula exceeds the 8192-character limit")
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\t\n\r" for char in value):
        raise AdapterError("XLSX text contains an unsupported control character")
    return value


@dataclass
class _Budget:
    values: int = 0
    rows: int = 0
    positions: int = 0
    metadata: int = 0
    merges: int = 0
    native_tables: int = 0

    def text(self, value: str) -> None:
        self.values += len(value)
        if self.values > MAX_TOTAL_VALUE_CHARS:
            raise AdapterError("XLSX exceeds the aggregate value/formula character budget")

    def geometry(self, bounds: tuple[int, int, int, int]) -> None:
        rows, columns = bounds[1] - bounds[0] + 1, bounds[3] - bounds[2] + 1
        if rows > 100_000 or columns > 256:
            raise AdapterError("XLSX rectangle exceeds row/column span limits")
        self.rows += rows
        self.positions += rows * columns
        if self.rows > MAX_ROWS or self.positions > MAX_CELLS:
            raise AdapterError("XLSX exceeds workbook row/rectangular-position limits")

    def record(self, value: object) -> None:
        self.metadata += len(serialized(value).encode("utf-8"))
        if self.metadata > MAX_METADATA_BYTES:
            raise AdapterError("XLSX exceeds the serialized metadata budget")


def _rich_string(element: etree._Element) -> tuple[str, str]:
    _children(element, {"t", "r", "rPh", "phoneticPr"})
    if element.findall(S + "t") and element.findall(S + "r"):
        raise AdapterError("XLSX string mixes plain and rich text representations")
    _one(element, "t")
    pieces: list[str] = []
    for child in element:
        if child.tag == S + "t":
            if len(child):
                raise AdapterError("XLSX string text cannot contain elements")
            pieces.append(child.text or "")
        elif child.tag == S + "r":
            _children(child, {"rPr", "t"})
            text = _one(child, "t", required=True)
            assert text is not None
            if len(text):
                raise AdapterError("XLSX rich string text cannot contain elements")
            _one(child, "rPr")
            pieces.append(text.text or "")
    raw = _literal("".join(pieces))
    return _literal(raw, escaped=True), raw


def _related(package: XlsxPackage, source: str, suffix: str) -> list[str]:
    return [
        rel.target_part
        for rel in package.relationships.get(source, {}).values()
        if rel.relationship_type == REL_NS + "/" + suffix and rel.target_part is not None
    ]


def _single_related(package: XlsxPackage, suffix: str) -> etree._Element | None:
    parts = _related(package, package.workbook_part, suffix)
    if len(parts) > 1:
        raise AdapterError(f"XLSX workbook has duplicate {suffix} parts")
    return package.xml_parts[parts[0]] if parts else None


def _shared_strings(package: XlsxPackage, budget: _Budget) -> tuple[tuple[str, str], ...]:
    root = _single_related(package, "sharedStrings")
    if root is None:
        return ()
    validate_structure(root, coordinate=_coordinate, bounds=_bounds)
    _children(root, {"si"})
    if root.get("uniqueCount") is not None and _integer(
        root.get("uniqueCount"), "shared strings"
    ) != len(root):
        raise AdapterError("XLSX shared-string uniqueCount does not match stored entries")
    if root.get("count") is not None:
        if _integer(root.get("count"), "shared-string references") < len(root):
            raise AdapterError("XLSX shared-string count is smaller than uniqueCount")
    result = []
    for item in root:
        value, raw = _rich_string(item)
        budget.text(value)
        budget.record({"value": value, "raw": raw})
        result.append((value, raw))
    return tuple(result)


def _styles(package: XlsxPackage, budget: _Budget) -> tuple[dict[str, Any], ...]:
    root = _single_related(package, "styles")
    if root is None:
        return ()
    validate_structure(root, coordinate=_coordinate, bounds=_bounds)
    names = {
        "numFmts",
        "fonts",
        "fills",
        "borders",
        "cellStyleXfs",
        "cellXfs",
        "cellStyles",
        "dxfs",
        "tableStyles",
        "colors",
    }
    _children(root, names)
    containers = {name: _one(root, name) for name in names}
    expected = {
        "numFmts": "numFmt",
        "fonts": "font",
        "fills": "fill",
        "borders": "border",
        "cellStyleXfs": "xf",
        "cellXfs": "xf",
        "cellStyles": "cellStyle",
        "dxfs": "dxf",
        "tableStyles": "tableStyle",
    }
    for name, child_name in expected.items():
        container = containers[name]
        if container is not None:
            _children(container, {child_name})
            _count(container, name)
    formats: dict[int, str] = {}
    numfmts = containers["numFmts"]
    if numfmts is not None:
        for item in numfmts:
            identifier = _integer(item.get("numFmtId"), "number format ID", maximum=65535)
            if identifier in formats or item.get("formatCode") is None:
                raise AdapterError("XLSX has duplicate or missing number format definitions")
            code = _literal(item.get("formatCode"), escaped=True)
            formats[identifier] = code
            budget.text(code)
    for name in ("cellStyleXfs", "cellXfs"):
        container = containers[name]
        if container is None:
            continue
        for item in container:
            for attribute, target in (
                ("fontId", "fonts"),
                ("fillId", "fills"),
                ("borderId", "borders"),
                ("xfId", "cellStyleXfs"),
            ):
                if item.get(attribute) is not None:
                    index = _integer(item.get(attribute), attribute)
                    destination = containers[target]
                    if destination is None or index >= len(destination):
                        raise AdapterError(f"XLSX style references an invalid {attribute}")
            number_id = _integer(item.get("numFmtId", "0"), "number format ID", maximum=65535)
            if number_id >= 164 and number_id not in formats:
                raise AdapterError("XLSX style references an undefined custom number format")
    cell_styles = containers["cellStyles"]
    if cell_styles is not None:
        for item in cell_styles:
            index = _integer(item.get("xfId"), "cell style xfId")
            style_target = containers["cellStyleXfs"]
            if style_target is None or index >= len(style_target):
                raise AdapterError("XLSX named style references an invalid xfId")
    # Retain bounded style definitions without using formatting to change stored types/values.
    if len(etree.tostring(root, encoding="utf-8")) > MAX_METADATA_BYTES:
        raise AdapterError("XLSX style definitions exceed the metadata budget")
    xfs = containers["cellXfs"]
    if xfs is None:
        return ()
    return tuple(
        {
            "index": index,
            "num_fmt_id": _integer(item.get("numFmtId", "0"), "number format ID"),
            "format_code": formats.get(_integer(item.get("numFmtId", "0"), "number format ID")),
            "attributes": dict(item.attrib),
            "definition": etree.tostring(item, encoding="unicode"),
        }
        for index, item in enumerate(xfs)
    )


@dataclass(frozen=True)
class XlsxSourceAdapter:
    """Retain exact workbook values and coordinates; never evaluate stored expressions."""

    format_version: str = "1"
    package_version: str = XLSX_PACKAGE_VERSION
    renderer_version: str = RENDERER_VERSION

    @property
    def media_types(self) -> Sequence[str]:
        return (XLSX_MEDIA_TYPE,)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        if artifact.media_type.partition(";")[0].strip().lower() != XLSX_MEDIA_TYPE:
            raise AdapterError("XLSX adapter requires the XLSX media type")
        recipe = normalize_xlsx_options(artifact.options.get("xlsx", {}))
        package = load_xlsx_package(artifact.artifact_path)
        try:
            return _Workbook(package, recipe).extract()
        except TableError as exc:
            raise AdapterError(f"XLSX canonical evidence is invalid: {exc}") from exc


@dataclass
class _Workbook:
    package: XlsxPackage
    recipe: dict[str, object]
    budget: _Budget = field(default_factory=_Budget)
    table_ids: set[int] = field(default_factory=set)
    table_names: set[str] = field(default_factory=set)
    differential_styles: int = 0

    def extract(self) -> AdapterResult:
        root = self.package.xml_parts[self.package.workbook_part]
        validate_structure(root, coordinate=_coordinate, bounds=_bounds)
        _children(
            root,
            {
                "fileVersion",
                "workbookPr",
                "workbookProtection",
                "bookViews",
                "sheets",
                "definedNames",
                "calcPr",
            },
        )
        for name in {etree.QName(child).localname for child in root}:
            _one(root, name)
        properties = _one(root, "workbookPr")
        date_system = (
            "1904"
            if properties is not None and _boolean(properties.get("date1904"), "date1904")
            else "1900"
        )
        sheets = _one(root, "sheets", required=True)
        assert sheets is not None
        _children(sheets, {"sheet"})
        if not 1 <= len(sheets) <= MAX_SHEETS:
            raise AdapterError("XLSX workbook must contain between 1 and 32 sheets")
        names: set[str] = set()
        ids: set[int] = set()
        targets: set[str] = set()
        descriptors: list[tuple[str, str, str, str]] = []
        for sheet in sheets:
            if len(sheet) or (sheet.text or "").strip():
                raise AdapterError("XLSX sheet declaration cannot contain nested content")
            name = sheet.get("name")
            _sheet_name(name)
            identifier = _integer(sheet.get("sheetId"), "sheet ID", minimum=1)
            state = sheet.get("state", "visible")
            if state not in {"visible", "hidden", "veryHidden"}:
                raise AdapterError("XLSX sheet has an invalid visibility state")
            relationship = self.package.relationships.get(self.package.workbook_part, {}).get(
                sheet.get(R + "id", "")
            )
            if (
                relationship is None
                or relationship.relationship_type != REL_NS + "/worksheet"
                or relationship.target_part is None
            ):
                raise AdapterError("XLSX sheet requires its own worksheet relationship")
            if name.casefold() in names or identifier in ids or relationship.target_part in targets:
                raise AdapterError("XLSX workbook has duplicate sheet names, IDs, or targets")
            names.add(name.casefold())
            ids.add(identifier)
            targets.add(relationship.target_part)
            descriptors.append((name, str(identifier), state, relationship.target_part))
        if targets != set(_related(self.package, self.package.workbook_part, "worksheet")):
            raise AdapterError("XLSX workbook has an undeclared worksheet relationship")
        overrides = self.recipe["header_rows"]
        assert isinstance(overrides, dict)
        if set(overrides) - {entry[0] for entry in descriptors}:
            raise AdapterError("XLSX header_rows contains an unknown exact sheet name")
        defined_names = self._defined_names(root, len(sheets))
        strings, styles = (
            _shared_strings(self.package, self.budget),
            _styles(self.package, self.budget),
        )
        style_root = _single_related(self.package, "styles")
        differential = _one(style_root, "dxfs") if style_root is not None else None
        self.differential_styles = len(differential) if differential is not None else 0
        if style_root is not None:
            self._differential_references(style_root)
        tables: list[Table] = []
        units: list[CanonicalSourceUnit] = []
        for index, (name, sheet_identifier, state, target) in enumerate(descriptors, 1):
            metadata: dict[str, Any] = {
                "sheet_id": sheet_identifier,
                "relationship_target": target,
                "sheet_state": state,
                "date_system": date_system,
                "requested_recipe": self.recipe,
                "interpretation": {"header_row": overrides.get(name), "date_system": date_system},
            }
            if index == 1:
                metadata["defined_names"] = defined_names
                metadata["workbook_properties"] = (
                    dict(properties.attrib) if properties is not None else {}
                )
                metadata["style_definitions"] = (
                    etree.tostring(style_root, encoding="unicode")
                    if style_root is not None
                    else None
                )
            table = self._sheet(
                index,
                name,
                target,
                state == "visible",
                overrides.get(name),
                metadata,
                strings,
                styles,
            )
            table.validate()
            self.budget.record(table.descriptor())
            for cell in table.cells:
                self.budget.record(asdict(cell))
            tables.append(table)
            if table.row_start is None or table.row_end is None:
                continue
            visible_rows: dict[int, list[Cell]] = {}
            # Project merge/cache provenance once, not once per row/merge pair.
            for projected_cell in evidence_cells(table, table.cells):
                visible_rows.setdefault(projected_cell.row, []).append(projected_cell)
            for row in range(table.row_start, table.row_end + 1):
                row_cells = tuple(visible_rows.get(row, ()))
                text = render_cells(row_cells)
                unit = CanonicalSourceUnit(
                    ordinal=len(units) + 1,
                    location_type="table_row",
                    location={
                        "table_id": table.table_id,
                        "sheet_index": index,
                        "row_start": row,
                        "row_end": row,
                        "column_start": table.column_start,
                        "column_end": table.column_end,
                    },
                    human_label=f"sheet {index} {name!r} — row {row}",
                    normalized_text=text,
                    structure={
                        "kind": "table_row",
                        "hidden": not row_cells,
                    },
                    extractor=XLSX_EXTRACTOR,
                )
                self.budget.record(asdict(unit))
                units.append(unit)
        # Full bounded passage construction is performed once by the shared processing path.
        if not any(
            table.visible
            and cell.searchable
            and cell.row != header_at(table, cell.row, cell.column)
            for table in tables
            for cell in table.cells
        ):
            raise AdapterError("XLSX workbook contains no searchable data evidence")
        return AdapterResult(
            media_type=XLSX_MEDIA_TYPE,
            units=tuple(units),
            extractor=XLSX_EXTRACTOR,
            tables=tuple(tables),
            metadata_candidates={},
        )

    def _defined_names(self, workbook: etree._Element, sheet_count: int) -> list[dict[str, Any]]:
        root = _one(workbook, "definedNames")
        if root is None:
            return []
        _children(root, {"definedName"})
        seen: set[tuple[str, int | None]] = set()
        result: list[dict[str, Any]] = []
        for item in root:
            name = _literal(item.get("name", ""))
            if not name or len(item):
                raise AdapterError("XLSX defined name is invalid")
            scope = (
                None
                if item.get("localSheetId") is None
                else _integer(
                    item.get("localSheetId"), "defined-name sheet index", maximum=sheet_count - 1
                )
            )
            if (name.casefold(), scope) in seen:
                raise AdapterError("XLSX has duplicate defined names")
            seen.add((name.casefold(), scope))
            expression = _literal(item.text or "")
            self.budget.text(expression)
            result.append(
                {
                    "name": name,
                    "local_sheet_id": scope,
                    "expression": expression,
                    "attributes": dict(item.attrib),
                }
            )
        return result

    def _sheet(
        self,
        index: int,
        name: str,
        target: str,
        visible: bool,
        header_row: int | None,
        metadata: dict[str, Any],
        strings: tuple[tuple[str, str], ...],
        styles: tuple[dict[str, Any], ...],
    ) -> Table:
        root = self.package.xml_parts[target]
        validate_structure(root, coordinate=_coordinate, bounds=_bounds)
        allowed = {
            "sheetPr",
            "dimension",
            "sheetViews",
            "sheetFormatPr",
            "cols",
            "sheetData",
            "sheetProtection",
            "autoFilter",
            "sortState",
            "mergeCells",
            "phoneticPr",
            "printOptions",
            "pageMargins",
            "pageSetup",
            "headerFooter",
            "rowBreaks",
            "colBreaks",
            "ignoredErrors",
            "hyperlinks",
            "tableParts",
        }
        _children(root, allowed)
        for child_name in allowed:
            _one(root, child_name)
        dimension = _one(root, "dimension")
        if dimension is not None:
            _bounds(dimension.get("ref"))  # Validate, but never allocate from this producer hint.
        hidden_columns, column_metadata = self._columns(root, styles)
        metadata["hidden_columns"] = hidden_columns
        metadata["column_metadata"] = column_metadata
        rows = _one(root, "sheetData", required=True)
        assert rows is not None
        _children(rows, {"row"})
        stored: dict[tuple[int, int], Cell] = {}
        formulas: dict[tuple[int, int], dict[str, Any]] = {}
        occupied: list[tuple[int, int, int, int]] = []
        hidden_rows: list[list[int]] = []
        row_metadata: list[dict[str, Any]] = []
        previous_row = 0
        for row in rows:
            row_number = _integer(row.get("r"), "row coordinate", minimum=1, maximum=EXCEL_ROWS)
            if row_number <= previous_row:
                raise AdapterError("XLSX row coordinates must be unique and ordered")
            previous_row = row_number
            hidden = _boolean(row.get("hidden"), "row hidden")
            if hidden:
                if hidden_rows and hidden_rows[-1][1] + 1 == row_number:
                    hidden_rows[-1][1] = row_number
                else:
                    hidden_rows.append([row_number, row_number])
            if row.get("s") is not None:
                self._style(row.get("s"), styles)
            spans = row.get("spans")
            if spans:
                for span in spans.split():
                    split = span.split(":")
                    if len(split) != 2:
                        raise AdapterError("XLSX row spans are invalid")
                    left = _integer(split[0], "row span", minimum=1, maximum=EXCEL_COLUMNS)
                    right = _integer(split[1], "row span", minimum=1, maximum=EXCEL_COLUMNS)
                    if right < left:
                        raise AdapterError("XLSX row span is reversed")
            if set(row.attrib) - {"r"}:
                row_metadata.append(dict(row.attrib))
            _children(row, {"c"})
            previous_column = 0
            for element in row:
                coordinate = _coordinate(element.get("r"))
                if coordinate[0] != row_number or coordinate[1] <= previous_column:
                    raise AdapterError(
                        "XLSX cell coordinates are duplicated, unordered, or in the wrong row"
                    )
                previous_column = coordinate[1]
                cell_visible = (
                    visible
                    and not hidden
                    and not interval_overlaps(hidden_columns, coordinate[1], coordinate[1])
                )
                cell, formula = self._cell(element, coordinate, cell_visible, strings, styles)
                stored[coordinate] = cell
                if cell.kind != "blank" or formula is not None:
                    occupied.append((coordinate[0], coordinate[0], coordinate[1], coordinate[1]))
                if formula is not None:
                    formulas[coordinate] = formula
                    if formula["bounds"] is not None:
                        occupied.append(formula["bounds"])
        metadata["hidden_rows"] = hidden_rows
        metadata["row_metadata"] = row_metadata
        merges = self._merges(root)
        native_regions = self._native_regions(root, target, index)
        occupied.extend(merges)
        occupied.extend(_selector_bounds(region["region"]) for region in native_regions)
        metadata["merges"] = [_region(index, bounds) for bounds in merges]
        metadata["native_regions"] = native_regions
        self._inert_references(root, target)
        if not occupied:
            if header_row is not None:
                raise AdapterError("XLSX header override cannot reference an empty sheet")
            metadata["formula_groups"] = []
            return Table(
                f"sheet-{index}",
                index,
                "xlsx",
                None,
                None,
                None,
                None,
                sheet_name=name,
                visible=visible,
                metadata=metadata,
            )
        bounds = (
            min(b[0] for b in occupied),
            max(b[1] for b in occupied),
            min(b[2] for b in occupied),
            max(b[3] for b in occupied),
        )
        self.budget.geometry(bounds)
        if header_row is not None and (
            not visible
            or not bounds[0] <= header_row <= bounds[1]
            or interval_overlaps(hidden_rows, header_row, header_row)
        ):
            raise AdapterError(
                "XLSX header override must name a visible row within the occupied extent"
            )
        formula_metadata = self._formula_groups(index, formulas, stored, bounds)
        metadata["formula_groups"] = formula_metadata[1]
        covered: dict[tuple[int, int], tuple[int, int]] = {}
        for merge in merges:
            anchor = merge[0], merge[2]
            for row in range(merge[0], merge[1] + 1):
                for column in range(merge[2], merge[3] + 1):
                    coordinate = row, column
                    if coordinate in covered:
                        raise AdapterError("XLSX merged rectangles overlap")
                    covered[coordinate] = anchor
                    if coordinate != anchor and (
                        coordinate in formulas
                        or (coordinate in stored and stored[coordinate].kind != "blank")
                        or coordinate in formula_metadata[0]
                    ):
                        raise AdapterError(
                            "XLSX merged covered cell contains a conflicting value or formula"
                        )
        cells: list[Cell] = []
        for row in range(bounds[0], bounds[1] + 1):
            row_visible = visible and not interval_overlaps(hidden_rows, row, row)
            for column in range(bounds[2], bounds[3] + 1):
                coordinate = row, column
                cell_visible = row_visible and not interval_overlaps(hidden_columns, column, column)
                cell = stored.get(
                    coordinate,
                    Cell(row, column, kind="blank", presence="absent", visible=cell_visible),
                )
                attributes = dict(cell.metadata)
                formula = formula_metadata[0].get(coordinate)
                if formula is not None:
                    attributes["formula"] = formula
                    if not formula["cache_present"]:
                        cell = Cell(
                            row,
                            column,
                            kind="unavailable",
                            presence=cell.presence,
                            visible=cell_visible,
                            metadata=attributes,
                        )
                merge_anchor = covered.get(coordinate)
                if merge_anchor is not None and coordinate != merge_anchor:
                    attributes["merge_anchor"] = {"row": merge_anchor[0], "column": merge_anchor[1]}
                    cell = Cell(
                        row,
                        column,
                        kind="merged-covered",
                        presence="merged-covered",
                        visible=cell_visible,
                        metadata=attributes,
                    )
                else:
                    cell = Cell(
                        row,
                        column,
                        value=cell.value,
                        kind=cell.kind,
                        presence=cell.presence,
                        raw=cell.raw,
                        visible=cell_visible,
                        metadata=attributes,
                    )
                cells.append(cell)
        return Table(
            f"sheet-{index}",
            index,
            "xlsx",
            *bounds,
            tuple(cells),
            header_row=header_row,
            sheet_name=name,
            visible=visible,
            metadata=metadata,
        )

    def _style(self, value: str | None, styles: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        index = _integer(value, "style index")
        if index >= len(styles):
            raise AdapterError("XLSX cell/row/column references an invalid style index")
        return {key: styles[index][key] for key in ("index", "num_fmt_id", "format_code")}

    def _columns(
        self, root: etree._Element, styles: tuple[dict[str, Any], ...]
    ) -> tuple[list[list[int]], list[dict[str, Any]]]:
        container = _one(root, "cols")
        if container is None:
            return [], []
        _children(container, {"col"})
        hidden: list[list[int]] = []
        metadata: list[dict[str, Any]] = []
        previous = 0
        for col in container:
            start = _integer(col.get("min"), "column interval", minimum=1, maximum=EXCEL_COLUMNS)
            end = _integer(col.get("max"), "column interval", minimum=1, maximum=EXCEL_COLUMNS)
            if start <= previous or end < start:
                raise AdapterError("XLSX column intervals overlap or are unordered/reversed")
            previous = end
            if _boolean(col.get("hidden"), "column hidden"):
                hidden.append([start, end])
            if col.get("style") is not None:
                self._style(col.get("style"), styles)
            metadata.append(dict(col.attrib))
        return hidden, metadata

    def _cell(
        self,
        element: etree._Element,
        coordinate: tuple[int, int],
        visible: bool,
        strings: tuple[tuple[str, str], ...],
        styles: tuple[dict[str, Any], ...],
    ) -> tuple[Cell, dict[str, Any] | None]:
        _children(element, {"f", "v", "is"})
        if element.get("cm") is not None or element.get("vm") is not None:
            raise AdapterError("XLSX cell metadata/value metadata is unsupported")
        formula_element, value_element, inline = (
            _one(element, "f"),
            _one(element, "v"),
            _one(element, "is"),
        )
        cell_type = element.get("t", "n")
        if cell_type not in {"n", "b", "d", "e", "s", "str", "inlineStr"}:
            raise AdapterError("XLSX cell has an unsupported stored type")
        if inline is not None and (
            cell_type != "inlineStr" or value_element is not None or formula_element is not None
        ):
            raise AdapterError("XLSX cell has contradictory inline/value/formula representations")
        if cell_type == "inlineStr" and (
            inline is None or value_element is not None or formula_element is not None
        ):
            raise AdapterError("XLSX inline string requires an exclusive inline value")
        if value_element is not None and len(value_element):
            raise AdapterError("XLSX stored value cannot contain elements")
        attributes: dict[str, Any] = {
            "stored_type": cell_type,
            "stored_value_present": value_element is not None,
        }
        if element.get("s") is not None:
            attributes["style"] = self._style(element.get("s"), styles)
        formula: dict[str, Any] | None = None
        if formula_element is not None:
            if len(formula_element):
                raise AdapterError("XLSX formula expression cannot contain elements")
            kind = formula_element.get("t", "normal")
            if kind not in {"normal", "shared", "array"}:
                raise AdapterError("XLSX data-table or unknown formula kind is unsupported")
            expression = _literal(formula_element.text or "")
            self.budget.text(expression)
            group = formula_element.get("si")
            if kind == "shared":
                group = str(_integer(group, "shared formula group ID"))
            elif group is not None:
                raise AdapterError("XLSX non-shared formula cannot declare a shared group ID")
            bounds = (
                _bounds(formula_element.get("ref"))
                if formula_element.get("ref") is not None
                else None
            )
            if kind == "normal" and (not expression or bounds is not None):
                raise AdapterError("XLSX normal formula requires an expression and no group range")
            if kind == "array" and (not expression or bounds is None):
                raise AdapterError("XLSX array formula requires an expression and range")
            formula = {
                "kind": kind,
                "expression": expression or None,
                "group_id": group,
                "bounds": bounds,
                "cache_present": value_element is not None,
            }
        raw = value_element.text or "" if value_element is not None else None
        value: str | bool | None
        presence = "stored"
        if inline is not None:
            value, raw = _rich_string(inline)
            kind = "string"
        elif value_element is None:
            value, kind, presence = (
                None,
                "blank",
                "stored" if formula is not None else "explicit-empty",
            )
        elif cell_type == "s":
            if formula is not None:
                raise AdapterError("XLSX formula cache cannot use a shared-string index")
            string_index = _integer(raw, "shared string index")
            if string_index >= len(strings):
                raise AdapterError("XLSX shared string index is out of bounds")
            value = strings[string_index][0]
            attributes["shared_string_index"] = string_index
            kind = "string"
        elif cell_type == "b":
            if raw not in {"0", "1"}:
                raise AdapterError("XLSX stored boolean must be 0 or 1")
            value, kind = raw == "1", "boolean"
        elif cell_type == "e":
            if raw not in _ERROR_VALUES:
                raise AdapterError("XLSX has an invalid stored error token")
            value, kind = raw, "error"
        elif cell_type == "str":
            value, kind = _literal(raw or "", escaped=True), "string"
        elif cell_type == "d":
            value, kind = raw, "date"
        elif raw == "" and formula is None:
            value, kind, presence = None, "blank", "explicit-empty"
        else:
            value, kind = raw, "number"
        if isinstance(value, str):
            _literal(value)
            self.budget.text(value)
        if raw is not None:
            _literal(raw)
        if formula is not None:
            formula["cache_kind"] = kind if formula["cache_present"] else "unavailable"
        cell = Cell(
            *coordinate,
            value=value,
            kind=kind,
            presence=presence,
            raw=raw,
            visible=visible,
            metadata=attributes,
        )
        cell.validate()
        return cell, formula

    def _merges(self, root: etree._Element) -> list[tuple[int, int, int, int]]:
        container = _one(root, "mergeCells")
        if container is None:
            return []
        _children(container, {"mergeCell"})
        _count(container, "mergeCells")
        self.budget.merges += len(container)
        if self.budget.merges > MAX_MERGES:
            raise AdapterError("XLSX exceeds the 10000-merge limit")
        return [_bounds(item.get("ref")) for item in container]

    def _native_regions(
        self, root: etree._Element, source: str, index: int
    ) -> list[dict[str, Any]]:
        container = _one(root, "tableParts")
        related = _related(self.package, source, "table")
        if container is None:
            if related:
                raise AdapterError("XLSX worksheet has undeclared native table relationships")
            return []
        _children(container, {"tablePart"})
        _count(container, "tableParts")
        self.budget.native_tables += len(container)
        if self.budget.native_tables > MAX_NATIVE_TABLES:
            raise AdapterError("XLSX exceeds the 128-native-table limit")
        result: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        for reference in container:
            relationship = self.package.relationships.get(source, {}).get(
                reference.get(R + "id", "")
            )
            if (
                relationship is None
                or relationship.relationship_type != REL_NS + "/table"
                or relationship.target_part is None
                or relationship.target_part in seen_targets
            ):
                raise AdapterError("XLSX native table requires a unique table relationship")
            seen_targets.add(relationship.target_part)
            table = self.package.xml_parts[relationship.target_part]
            validate_structure(table, coordinate=_coordinate, bounds=_bounds)
            self._differential_references(table)
            _children(table, {"autoFilter", "sortState", "tableColumns", "tableStyleInfo"})
            for name in ("autoFilter", "sortState", "tableColumns", "tableStyleInfo"):
                _one(table, name)
            if (
                table.get("tableType", "worksheet") != "worksheet"
                or table.get("connectionId") is not None
            ):
                raise AdapterError("XLSX connected/query/XML native tables are unsupported")
            identifier = _integer(table.get("id"), "native table ID", minimum=1)
            name, display_name = (
                table.get("name", table.get("displayName", "")),
                table.get("displayName", ""),
            )
            for label in {name, display_name}:
                if (
                    not label
                    or len(label) > 255
                    or not (label[0].isalpha() or label[0] in "_\\")
                    or any(not (char.isalnum() or char in "_.\\") for char in label)
                    or label.casefold() in {"r", "c"}
                ):
                    raise AdapterError("XLSX native table name is invalid")
                if _COORDINATE.fullmatch(label.upper()):
                    try:
                        _coordinate(label.upper())
                    except AdapterError:
                        pass  # A name beyond native column bounds is not a cell reference.
                    else:
                        raise AdapterError("XLSX native table name cannot be a cell reference")
            if (
                identifier in self.table_ids
                or name.casefold() in self.table_names
                or display_name.casefold() in self.table_names
            ):
                raise AdapterError("XLSX native table IDs and names must be unique")
            self.table_ids.add(identifier)
            self.table_names.update({name.casefold(), display_name.casefold()})
            bounds = _bounds(table.get("ref"))
            if any(_overlaps(bounds, _selector_bounds(other["region"])) for other in result):
                raise AdapterError("XLSX native table regions overlap")
            headers = _integer(table.get("headerRowCount", "1"), "native header count", maximum=1)
            totals = _integer(table.get("totalsRowCount", "0"), "native totals count", maximum=1)
            if headers + totals > bounds[1] - bounds[0] + 1:
                raise AdapterError("XLSX native header/totals extent is contradictory")
            columns = _one(table, "tableColumns", required=True)
            assert columns is not None
            _children(columns, {"tableColumn"})
            _count(columns, "native table columns")
            if len(columns) != bounds[3] - bounds[2] + 1:
                raise AdapterError("XLSX native column count does not match its rectangle")
            column_ids: set[int] = set()
            column_names: list[str] = []
            column_metadata: list[dict[str, Any]] = []
            for column in columns:
                column_id = _integer(column.get("id"), "native column ID", minimum=1)
                if column_id in column_ids or column.get("name") is None:
                    raise AdapterError("XLSX native columns have duplicate IDs or missing names")
                column_ids.add(column_id)
                column_name = _literal(column.get("name"), escaped=True)
                self.budget.text(column_name)
                column_names.append(column_name)
                _children(column, {"calculatedColumnFormula", "totalsRowFormula"})
                expressions: dict[str, str] = {}
                for tag in ("calculatedColumnFormula", "totalsRowFormula"):
                    formula = _one(column, tag)
                    if formula is not None:
                        if len(formula):
                            raise AdapterError(
                                "XLSX native formula has an unsupported representation"
                            )
                        expression = _literal(formula.text or "")
                        self.budget.text(expression)
                        expressions[tag] = expression
                column_metadata.append(
                    {"attributes": dict(column.attrib), "expressions": expressions}
                )
            for filter_name in ("autoFilter", "sortState"):
                filter_element = _one(table, filter_name)
                if filter_element is not None:
                    filter_bounds = _bounds(filter_element.get("ref"))
                    if not _inside(bounds, filter_bounds[0], filter_bounds[2]) or not _inside(
                        bounds, filter_bounds[1], filter_bounds[3]
                    ):
                        raise AdapterError("XLSX native filter/sort range is outside its table")
            result.append(
                {
                    "id": str(identifier),
                    "name": name,
                    "display_name": display_name,
                    "region": _region(index, bounds),
                    "header_row_count": headers,
                    "totals_row_count": totals,
                    "column_names": column_names,
                    "columns": column_metadata,
                }
            )
        if seen_targets != set(related):
            raise AdapterError("XLSX worksheet has undeclared native tables")
        return result

    def _formula_groups(
        self,
        index: int,
        formulas: dict[tuple[int, int], dict[str, Any]],
        stored: dict[tuple[int, int], Cell],
        extent: tuple[int, int, int, int],
    ) -> tuple[dict[tuple[int, int], dict[str, Any]], list[dict[str, Any]]]:
        del extent  # Geometry was budgeted before this method materializes membership.
        groups: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        assignments: dict[tuple[int, int], dict[str, Any]] = {}
        for coordinate, formula in formulas.items():
            if formula["kind"] == "normal":
                assignments[coordinate] = _formula_metadata(formula, coordinate, None, None)
            elif formula["kind"] == "shared":
                if formula["expression"] is not None:
                    if formula["bounds"] is None or formula["group_id"] in groups:
                        raise AdapterError(
                            "XLSX shared formula requires one unique anchor with a range"
                        )
                    groups[formula["group_id"]] = coordinate, formula
                elif formula["bounds"] is not None:
                    raise AdapterError("XLSX shared formula follower cannot declare a range")
            else:
                groups[f"array-{coordinate[0]}-{coordinate[1]}"] = coordinate, formula
        regions: list[dict[str, Any]] = []
        covered: set[tuple[int, int]] = set()
        for group_id, (anchor, formula) in groups.items():
            bounds = formula["bounds"]
            if not _inside(bounds, *anchor) or (
                formula["kind"] == "array" and anchor != (bounds[0], bounds[2])
            ):
                raise AdapterError("XLSX formula anchor is outside or inconsistent with its range")
            region = _region(index, bounds)
            regions.append(
                {
                    "kind": formula["kind"],
                    "group_id": group_id,
                    "anchor": {"row": anchor[0], "column": anchor[1]},
                    "expression": formula["expression"],
                    "region": region,
                }
            )
            for row in range(bounds[0], bounds[1] + 1):
                for column in range(bounds[2], bounds[3] + 1):
                    coordinate = row, column
                    if coordinate in covered:
                        raise AdapterError("XLSX formula groups overlap")
                    covered.add(coordinate)
                    member = formulas.get(coordinate)
                    if formula["kind"] == "shared":
                        if (
                            member is None
                            or member["kind"] != "shared"
                            or member["group_id"] != group_id
                        ):
                            raise AdapterError(
                                "XLSX shared formula range has missing or conflicting members"
                            )
                    elif coordinate != anchor:
                        if member is not None:
                            raise AdapterError("XLSX array formula overlaps another formula")
                        cell = stored.get(coordinate)
                        if cell is not None and cell.metadata.get("stored_type") in {
                            "s",
                            "inlineStr",
                        }:
                            raise AdapterError(
                                "XLSX array formula cache has an unsupported string representation"
                            )
                        cache_present = cell is not None and cell.metadata["stored_value_present"]
                        if cache_present and cell is not None and cell.kind == "blank":
                            raise AdapterError(
                                "XLSX array formula has an invalid empty numeric cache"
                            )
                        member = {
                            "kind": "array",
                            "expression": None,
                            "cache_present": bool(cache_present),
                            "cache_kind": cell.kind
                            if cache_present and cell is not None
                            else "unavailable",
                        }
                    assert member is not None
                    assignments[coordinate] = _formula_metadata(member, anchor, group_id, region)
        if set(formulas) - set(assignments):
            raise AdapterError("XLSX shared formula has a missing anchor or out-of-range follower")
        return assignments, regions

    def _differential_references(self, root: etree._Element) -> None:
        for element in root.iter():
            for name, value in element.attrib.items():
                if name == "dxfId" or name.endswith("DxfId"):
                    if _integer(value, "differential style index") >= self.differential_styles:
                        raise AdapterError("XLSX references an invalid differential style index")

    def _inert_references(self, root: etree._Element, source: str) -> None:
        links = _one(root, "hyperlinks")
        if links is not None:
            _children(links, {"hyperlink"})
            for link in links:
                _bounds(link.get("ref"))
                relationship_id = link.get(R + "id")
                if relationship_id is not None:
                    relationship = self.package.relationships.get(source, {}).get(relationship_id)
                    if (
                        relationship is None
                        or relationship.relationship_type != REL_NS + "/hyperlink"
                    ):
                        raise AdapterError("XLSX hyperlink references an incompatible relationship")
                elif not link.get("location"):
                    raise AdapterError(
                        "XLSX hyperlink requires a relationship or internal location"
                    )
        for name in ("autoFilter", "sortState"):
            element = _one(root, name)
            if element is not None:
                _bounds(element.get("ref"))
        for name in ("rowBreaks", "colBreaks"):
            element = _one(root, name)
            if element is not None:
                _children(element, {"brk"})
                _count(element, name)
                manual = 0
                for item in element:
                    _integer(
                        item.get("id"),
                        "page break",
                        maximum=EXCEL_ROWS if name == "rowBreaks" else EXCEL_COLUMNS,
                    )
                    manual += _boolean(item.get("man"), "manual page break")
                if (
                    element.get("manualBreakCount") is not None
                    and _integer(element.get("manualBreakCount"), "manual breaks") != manual
                ):
                    raise AdapterError("XLSX manual page break count is inconsistent")


def _selector_bounds(region: dict[str, Any]) -> tuple[int, int, int, int]:
    return region["row_start"], region["row_end"], region["column_start"], region["column_end"]


def _formula_metadata(
    formula: dict[str, Any],
    anchor: tuple[int, int],
    group: str | None,
    region: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "kind": formula["kind"],
        "expression": formula["expression"],
        "group_id": group,
        "anchor": {"row": anchor[0], "column": anchor[1]},
        "range": region,
        "cache_present": formula["cache_present"],
        "cache_kind": formula["cache_kind"],
    }

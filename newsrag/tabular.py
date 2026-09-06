"""Generation-neutral tabular values, exact rectangles and bounded evidence rendering."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

MAX_CELLS = 1_000_000
MAX_VALUE_CHARS = 8192
MAX_TOTAL_VALUE_CHARS = 10_485_760
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_FOCUS_CHARS = 16_384
MAX_CONTEXT_CHARS = 16_384
MAX_ITEM_CHARS = 32_768
MAX_ITEM_CELLS = 256
MAX_PASSAGES = 100_000
MAX_INDEX_BYTES = 64 * 1024 * 1024
RENDERER_VERSION = "2"
ContextRole = Literal["header", "preceding", "following", "merge-anchor"]


class TableError(ValueError):
    """Tabular content or evidence violates the canonical contract."""


def positive_integer(value: object, name: str) -> int:
    """Reject coercions, including bool-as-int coordinates."""
    if type(value) is not int or value < 1:
        raise TableError(f"{name} must be a positive integer")
    return value


def column_label(column: int) -> str:
    """Return an orientation label, never literal source text."""
    positive_integer(column, "column")
    result = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        result = chr(65 + remainder) + result
    return result


def serialized(value: object) -> str:
    """Use deterministic Unicode JSON with escaped embedded controls."""
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    value: str | bool | None = None
    kind: str = "string"
    presence: str = "stored"
    raw: str | None = None
    visible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def searchable(self) -> bool:
        return (
            self.visible
            and self.kind not in {"blank", "error", "unavailable", "merged-covered"}
            and self.value not in {None, ""}
        )

    def validate(self) -> None:
        positive_integer(self.row, "cell row")
        positive_integer(self.column, "cell column")
        if type(self.visible) is not bool:
            raise TableError("Cell visibility must be boolean")
        if self.kind not in {
            "string",
            "number",
            "boolean",
            "date",
            "blank",
            "error",
            "unavailable",
            "merged-covered",
        }:
            raise TableError("Unsupported cell value kind")
        if self.presence not in {
            "stored",
            "absent",
            "explicit-empty",
            "blank-record",
            "merged-covered",
        }:
            raise TableError("Unsupported cell presence")
        if self.kind == "boolean":
            if type(self.value) is not bool:
                raise TableError("Boolean cell requires a boolean value")
        elif self.kind in {"blank", "unavailable", "merged-covered"}:
            if self.value is not None:
                raise TableError("Blank/unavailable/covered cell cannot invent a value")
        elif not isinstance(self.value, str):
            raise TableError("Stored cell requires a literal string representation")
        if self.kind == "number":
            try:
                if (
                    not isinstance(self.value, str)
                    or re.fullmatch(
                        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", self.value
                    )
                    is None
                    or not Decimal(self.value).is_finite()
                ):
                    raise TableError("Number cell requires an exact finite numeric lexeme")
            except InvalidOperation as exc:
                raise TableError("Invalid numeric cell representation") from exc
        if self.kind == "date":
            try:
                if not isinstance(self.value, str):
                    raise ValueError("not a string")
                if "T" in self.value:
                    datetime.fromisoformat(self.value)
                else:
                    date.fromisoformat(self.value)
            except ValueError as exc:
                raise TableError("Invalid stored ISO date cell") from exc
        for value in (self.value, self.raw):
            if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
                raise TableError("Cell exceeds the 8192-character value budget")
        if self.raw is not None and not isinstance(self.raw, str):
            raise TableError("Raw stored cell representation must be a string")


@dataclass(frozen=True)
class TableRegion:
    table_id: str
    sheet_index: int
    region_kind: str
    row_start: int
    row_end: int
    column_start: int
    column_end: int

    def __post_init__(self) -> None:
        if not isinstance(self.table_id, str) or not self.table_id:
            raise TableError("Table selector requires a table ID")
        for name in ("sheet_index", "row_start", "row_end", "column_start", "column_end"):
            positive_integer(getattr(self, name), name)
        if self.row_end < self.row_start or self.column_end < self.column_start:
            raise TableError("Table selector is reversed")
        if self.region_kind not in {"table", "rows", "columns", "cells"}:
            raise TableError("Unsupported table region kind")

    @property
    def cell_count(self) -> int:
        return (self.row_end - self.row_start + 1) * (self.column_end - self.column_start + 1)

    @property
    def label(self) -> str:
        left, right = column_label(self.column_start), column_label(self.column_end)
        rows = (
            f"{self.row_start}–{self.row_end}"
            if self.row_start != self.row_end
            else str(self.row_start)
        )
        if self.region_kind == "table":
            return f"table {self.sheet_index} — rows {rows}, columns {left}–{right}"
        if self.region_kind == "rows":
            return f"rows {rows}"
        if self.region_kind == "columns":
            return f"columns {left}–{right} (rows {rows})"
        start, end = f"{left}{self.row_start}", f"{right}{self.row_end}"
        return f"cell {start}" if start == end else f"cells {start}:{end}"

    def to_dict(self) -> dict[str, Any]:
        return {"location_type": "table_region", **asdict(self)}

    @classmethod
    def from_dict(cls, value: object) -> TableRegion:
        if not isinstance(value, dict):
            raise TableError("Table selector must be an object")
        data = dict(value)
        if data.pop("location_type", "table_region") != "table_region":
            raise TableError("Expected table_region location type")
        try:
            return cls(**data)
        except TypeError as exc:
            raise TableError("Invalid table selector fields") from exc


@dataclass(frozen=True)
class Table:
    table_id: str
    sheet_index: int
    source_type: str
    row_start: int | None
    row_end: int | None
    column_start: int | None
    column_end: int | None
    cells: tuple[Cell, ...] = ()
    header_row: int | None = None
    sheet_name: str | None = None
    visible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def descriptor(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "cells"}

    def validate(self, *, extra_metadata_bytes: int = 0) -> None:
        positive_integer(self.sheet_index, "sheet_index")
        if self.table_id != f"sheet-{self.sheet_index}" or self.source_type not in {"csv", "xlsx"}:
            raise TableError("Invalid canonical table identity")
        if type(self.visible) is not bool:
            raise TableError("Table visibility must be boolean")
        descriptor_bytes = len(serialized(self.descriptor()).encode("utf-8")) + extra_metadata_bytes
        if descriptor_bytes > MAX_METADATA_BYTES:
            raise TableError("Table exceeds metadata budget")
        bounds = (self.row_start, self.row_end, self.column_start, self.column_end)
        if all(value is None for value in bounds):
            if self.cells or self.header_row is not None:
                raise TableError("Empty table cannot own cells or headers")
            return
        region = TableRegion(
            self.table_id,
            self.sheet_index,
            "table",
            *(positive_integer(value, "table extent") for value in bounds),
        )
        if region.cell_count > MAX_CELLS or len(self.cells) != region.cell_count:
            raise TableError("Table rectangle exceeds cell budget or is incomplete")
        if (
            region.column_end - region.column_start + 1 > 256
            or region.row_end - region.row_start + 1 > 100_000
        ):
            raise TableError("Table rectangle exceeds row/column budget")
        if (
            self.header_row is not None
            and not region.row_start
            <= positive_integer(self.header_row, "header row")
            <= region.row_end
        ):
            raise TableError("Header is outside the table extent")
        validate_table_metadata(self, region)
        raw_merges = self.metadata.get("merges", [])
        if not isinstance(raw_merges, list) or len(raw_merges) > 10_000:
            raise TableError("Invalid bounded merge descriptors")
        covered: dict[tuple[int, int], tuple[int, int]] = {}
        for value in raw_merges:
            merge = TableRegion.from_dict(value)
            if (
                merge.table_id != self.table_id
                or merge.sheet_index != self.sheet_index
                or not (
                    region.row_start <= merge.row_start <= merge.row_end <= region.row_end
                    and region.column_start
                    <= merge.column_start
                    <= merge.column_end
                    <= region.column_end
                )
            ):
                raise TableError("Merge descriptor is outside its owned table")
            anchor = merge.row_start, merge.column_start
            for row in range(merge.row_start, merge.row_end + 1):
                for column in range(merge.column_start, merge.column_end + 1):
                    if (row, column) in covered:
                        raise TableError("Overlapping merge descriptors")
                    covered[row, column] = anchor
        seen: set[tuple[int, int]] = set()
        value_chars = 0
        output_bytes = descriptor_bytes
        for cell in self.cells:
            cell.validate()
            coordinate = cell.row, cell.column
            if coordinate in seen:
                raise TableError("Table contains duplicate cells")
            seen.add(coordinate)
            cell_anchor = covered.get(coordinate)
            is_covered = cell_anchor is not None and coordinate != cell_anchor
            if is_covered != (cell.kind == "merged-covered"):
                raise TableError("Merged coverage does not match canonical cell presence")
            if (
                not region.row_start <= cell.row <= region.row_end
                or not region.column_start <= cell.column <= region.column_end
            ):
                raise TableError("Cell is outside the table rectangle")
            value_chars += len(cell.value) if isinstance(cell.value, str) else 0
            output_bytes += len(serialized(asdict(cell)).encode("utf-8"))
            if value_chars > MAX_TOTAL_VALUE_CHARS or output_bytes > MAX_METADATA_BYTES:
                raise TableError("Table exceeds value/metadata budget")


def interval_overlaps(intervals: list[list[int]], start: int, end: int) -> bool:
    """Query validated ordered intervals without scanning every hidden coordinate gap."""
    index = bisect_right(intervals, end, key=lambda interval: interval[0]) - 1
    return index >= 0 and intervals[index][1] >= start


def cell_is_visible(table: Table, cell: Cell) -> bool:
    """Apply descriptor exclusions as well as canonical per-cell visibility."""
    return (
        table.visible
        and table.metadata.get("sheet_state", "visible") == "visible"
        and cell.visible
        and not interval_overlaps(table.metadata.get("hidden_rows", []), cell.row, cell.row)
        and not interval_overlaps(
            table.metadata.get("hidden_columns", []), cell.column, cell.column
        )
    )


def native_region_at(table: Table, row: int, column: int) -> TableRegion | None:
    """Return the declared native boundary, never an inferred region."""
    for native in table.metadata.get("native_regions", []):
        region = native["region"]
        if (
            region["row_start"] <= row <= region["row_end"]
            and region["column_start"] <= column <= region["column_end"]
        ):
            return TableRegion.from_dict(region)
    return None


def header_at(table: Table, row: int, column: int) -> int | None:
    """An explicit sheet override takes precedence over native header declarations."""
    if table.header_row is not None:
        return table.header_row
    for native in table.metadata.get("native_regions", []):
        region = native["region"]
        if (
            native["header_row_count"] == 1
            and region["row_start"] <= row <= region["row_end"]
            and region["column_start"] <= column <= region["column_end"]
        ):
            return int(region["row_start"])
    return None


def context_row_allowed(table: Table, focus: TableRegion, row: int, *, header: bool) -> bool:
    """Check every column: context cannot bridge native boundaries or declared headers."""
    for column in range(focus.column_start, focus.column_end + 1):
        headers = {
            header_at(table, focus_row, column)
            for focus_row in range(focus.row_start, focus.row_end + 1)
        }
        if header:
            if headers != {row}:
                return False
        elif row == header_at(table, row, column) or any(
            native_region_at(table, focus_row, column) != native_region_at(table, row, column)
            for focus_row in range(focus.row_start, focus.row_end + 1)
        ):
            return False
    return True


def validate_table_metadata(table: Table, extent: TableRegion) -> None:
    """Validate shared native geometry and visibility without interpreting workbook values."""
    if table.metadata.get("sheet_state", "visible") not in {"visible", "hidden", "veryHidden"}:
        raise TableError("Invalid sheet visibility state")
    if table.metadata.get("date_system", "1900") not in {"1900", "1904"}:
        raise TableError("Invalid workbook date system")
    for key in ("hidden_rows", "hidden_columns"):
        intervals = table.metadata.get(key, [])
        if not isinstance(intervals, list):
            raise TableError("Invalid hidden coordinate intervals")
        previous = 0
        for interval in intervals:
            if not isinstance(interval, list) or len(interval) != 2:
                raise TableError("Invalid hidden coordinate interval")
            start, end = (positive_integer(value, key) for value in interval)
            if start <= previous or end < start:
                raise TableError("Hidden coordinate intervals overlap or are reversed")
            previous = end
    if table.header_row is not None and (
        not table.visible
        or table.metadata.get("sheet_state", "visible") != "visible"
        or any(
            start <= table.header_row <= end for start, end in table.metadata.get("hidden_rows", [])
        )
    ):
        raise TableError("Explicit header row must be visible")
    natives = table.metadata.get("native_regions", [])
    if not isinstance(natives, list) or len(natives) > 128:
        raise TableError("Invalid bounded native table descriptors")
    regions: list[TableRegion] = []
    for native in natives:
        if not isinstance(native, dict):
            raise TableError("Invalid native table descriptor")
        region = TableRegion.from_dict(native.get("region"))
        if (
            region.table_id != table.table_id
            or region.sheet_index != table.sheet_index
            or not (
                extent.row_start <= region.row_start <= region.row_end <= extent.row_end
                and extent.column_start
                <= region.column_start
                <= region.column_end
                <= extent.column_end
            )
        ):
            raise TableError("Native table descriptor is outside its owned table")
        for key in ("header_row_count", "totals_row_count"):
            if type(native.get(key)) is not int or native[key] not in {0, 1}:
                raise TableError("Invalid native header/totals declaration")
        names = native.get("column_names")
        if (
            not isinstance(names, list)
            or len(names) != region.column_end - region.column_start + 1
            or any(not isinstance(name, str) for name in names)
            or native["header_row_count"] + native["totals_row_count"]
            > region.row_end - region.row_start + 1
        ):
            raise TableError("Invalid native column/header/totals extent")
        if any(
            other.row_start <= region.row_end
            and region.row_start <= other.row_end
            and other.column_start <= region.column_end
            and region.column_start <= other.column_end
            for other in regions
        ):
            raise TableError("Overlapping native table regions")
        regions.append(region)


def evidence_cells(table: Table, cells: tuple[Cell, ...]) -> tuple[Cell, ...]:
    """Project visible values and safe provenance; expressions remain canonical-only."""
    merges = {
        (region.row_start, region.column_start): region.to_dict()
        for value in table.metadata.get("merges", [])
        for region in (TableRegion.from_dict(value),)
    }
    result = []
    for cell in cells:
        if not cell_is_visible(table, cell):
            continue
        metadata: dict[str, Any] = {}
        formula = cell.metadata.get("formula")
        if isinstance(formula, dict):
            metadata["formula"] = {
                key: formula[key]
                for key in ("kind", "group_id", "anchor", "range", "cache_present", "cache_kind")
                if key in formula
            }
        if cell.kind == "merged-covered" and "merge_anchor" in cell.metadata:
            anchor = cell.metadata["merge_anchor"]
            if not isinstance(anchor, dict):
                raise TableError("Invalid merge anchor reference")
            anchor_row = positive_integer(anchor.get("row"), "merge anchor row")
            anchor_column = positive_integer(anchor.get("column"), "merge anchor column")
            owned_merge = merges.get((anchor_row, anchor_column))
            if owned_merge is None or not (
                owned_merge["row_start"] <= cell.row <= owned_merge["row_end"]
                and owned_merge["column_start"] <= cell.column <= owned_merge["column_end"]
            ):
                raise TableError("Covered cell has an inconsistent merge anchor reference")
            metadata["merge_anchor"] = {"row": anchor_row, "column": anchor_column}
        elif cell.kind == "merged-covered":
            owned_merge = next(
                (
                    merge
                    for merge in merges.values()
                    if merge["row_start"] <= cell.row <= merge["row_end"]
                    and merge["column_start"] <= cell.column <= merge["column_end"]
                ),
                None,
            )
            if owned_merge is None:
                raise TableError("Covered cell has no owned merge anchor")
            metadata["merge_anchor"] = {
                "row": owned_merge["row_start"],
                "column": owned_merge["column_start"],
            }
        if (cell.row, cell.column) in merges:
            metadata["evidence_merge"] = merges[cell.row, cell.column]
        result.append(replace(cell, metadata=metadata))
    return tuple(result)


def hidden_exclusion_annotations(
    table: Table, region: TableRegion, *, counts: tuple[int, int] | None = None
) -> tuple[str, ...]:
    """Disclose exclusions without their values or closing coordinate gaps."""
    if table.source_type != "xlsx":
        return ()
    rows = table.metadata.get("hidden_rows", [])
    columns = table.metadata.get("hidden_columns", [])
    affected = interval_overlaps(rows, region.row_start, region.row_end) or interval_overlaps(
        columns, region.column_start, region.column_end
    )
    if counts is None:
        counts = (
            sum(end - start + 1 for start, end in rows),
            sum(end - start + 1 for start, end in columns),
        )
    return (
        "hidden exclusions: "
        f"sheet={int(not table.visible or table.metadata.get('sheet_state', 'visible') != 'visible')}; "
        f"rows={counts[0]}; "
        f"columns={counts[1]}; "
        f"selected region affected={'yes' if affected else 'no'}",
    )


def table_location_label(table: Table, region: TableRegion) -> str:
    """Keep source sheet identity and exact original coordinates in XLSX citations."""
    if table.source_type != "xlsx":
        return region.label
    label = region.label
    if region.region_kind == "table":
        label = (
            f"table extent {column_label(region.column_start)}{region.row_start}:"
            f"{column_label(region.column_end)}{region.row_end}"
        )
    return f"sheet {table.sheet_index} {serialized(table.sheet_name)} — {label}"


def validate_region(
    table: Table, region: TableRegion, *, max_cells: int = MAX_ITEM_CELLS
) -> tuple[Cell, ...]:
    """Resolve exactly a bounded visible rectangle, never a flattened row substring."""
    if region.table_id != table.table_id or region.sheet_index != table.sheet_index:
        raise TableError("Table selector has mismatched table/sheet ownership")
    if (
        table.row_start is None
        or table.row_end is None
        or table.column_start is None
        or table.column_end is None
    ):
        raise TableError("Empty table cannot supply evidence")
    for name in ("sheet_index", "row_start", "row_end", "column_start", "column_end"):
        positive_integer(getattr(table, name), f"persisted {name}")
    validate_table_metadata(
        table,
        TableRegion(
            table.table_id,
            table.sheet_index,
            "table",
            table.row_start,
            table.row_end,
            table.column_start,
            table.column_end,
        ),
    )
    if not (
        table.row_start <= region.row_start <= region.row_end <= table.row_end
        and table.column_start <= region.column_start <= region.column_end <= table.column_end
    ):
        raise TableError("Table selector is outside the persisted extent")
    if region.region_kind in {"table", "rows"} and (region.column_start, region.column_end) != (
        table.column_start,
        table.column_end,
    ):
        raise TableError("Table/rows selector requires full-width rows")
    if region.region_kind in {"table", "columns"} and (region.row_start, region.row_end) != (
        table.row_start,
        table.row_end,
    ):
        raise TableError("Table/columns selector requires full-height columns")
    if region.cell_count > max_cells:
        raise TableError("Evidence region exceeds the cell budget; select a narrower region")
    cells = tuple(
        sorted(
            (
                cell
                for cell in table.cells
                if region.row_start <= cell.row <= region.row_end
                and region.column_start <= cell.column <= region.column_end
            ),
            key=lambda cell: (cell.row, cell.column),
        )
    )
    if (
        len(cells) != region.cell_count
        or len({(cell.row, cell.column) for cell in cells}) != region.cell_count
    ):
        raise TableError("Evidence rectangle is incomplete or duplicated")
    if any(not cell_is_visible(table, cell) for cell in cells):
        raise TableError("Evidence region contains hidden cells")
    cells = evidence_cells(table, cells)
    if len(render_cells(cells)) > MAX_ITEM_CHARS:
        raise TableError("Evidence region exceeds the rendered character budget")
    return cells


def render_cells(cells: tuple[Cell, ...], *, table: Table | None = None) -> str:
    """Render visible extractive values with inert cache/merge provenance, never expressions.

    Supply the descriptor to disclose merges in normalized row text. Canonical storage
    retains expressions; evidence metadata and value indexes do not.
    """
    if table is not None:
        cells = evidence_cells(table, cells)
    lines = []
    for cell in cells:
        if not cell.visible:
            continue
        text = f"{column_label(cell.column)}{cell.row}={cell.kind}:{serialized(cell.value)}"
        formula = cell.metadata.get("formula")
        if isinstance(formula, dict):
            qualifier = (
                "formula-cache; freshness not verified"
                if formula.get("cache_present")
                else "formula-cache unavailable"
            )
            provenance = {
                key: formula[key]
                for key in ("kind", "group_id", "anchor", "range", "cache_kind")
                if key in formula
            }
            text += f" [{qualifier}; provenance={serialized(provenance)}]"
        merge = cell.metadata.get("evidence_merge")
        if merge is not None:
            text += f" [merged anchor: {TableRegion.from_dict(merge).label}]"
        anchor = cell.metadata.get("merge_anchor")
        if cell.kind == "merged-covered" and isinstance(anchor, dict):
            text += (
                f" [covered by merge anchor: {column_label(anchor['column'])}"
                f"{positive_integer(anchor['row'], 'merge anchor row')}; no independent value]"
            )
        lines.append(text)
    return "\n".join(lines)


@dataclass(frozen=True)
class TableContext:
    role: ContextRole
    region: TableRegion
    text: str
    location_label: str = ""
    annotations: tuple[str, ...] = ()


@dataclass(frozen=True)
class TablePassage:
    focus: TableRegion
    focus_text: str
    context: tuple[TableContext, ...] = ()
    omitted_context: tuple[str, ...] = ()
    annotations: tuple[str, ...] = ()

    @property
    def header_text(self) -> str:
        return "\n".join(item.text for item in self.context if item.role == "header")

    @property
    def keyword_text(self) -> str:
        return "\n".join(
            str(value)
            for text in (self.focus_text, self.header_text)
            for line in text.splitlines()
            if line.partition("=")[2].partition(":")[0]
            not in {"blank", "error", "unavailable", "merged-covered"}
            and (value := json.JSONDecoder().raw_decode(line.partition(":")[2])[0])
            not in {None, ""}
        )

    @property
    def text(self) -> str:
        return "\n".join(
            (
                "focus (extractive table representation):",
                self.focus_text,
                *self.annotations,
                *(
                    f"{item.role} ({item.location_label or item.region.label}):\n{item.text}"
                    + ("\n" + "\n".join(item.annotations) if item.annotations else "")
                    for item in self.context
                ),
            )
        )


def build_table_passages(
    tables: tuple[Table, ...], *, context_chars: int = MAX_CONTEXT_CHARS
) -> tuple[TablePassage, ...]:
    """Pack complete visible cells in source order with only immediate row context."""
    passages: list[TablePassage] = []
    index_bytes = 0
    total_positions = 0
    for table in tables:
        table.validate()
        total_positions += len(table.cells)
        if total_positions > MAX_CELLS:
            raise TableError("Artifact exceeds rectangular cell budget")
        if not table.visible or table.metadata.get("sheet_state", "visible") != "visible":
            continue
        rows: dict[int, list[Cell]] = {}
        for cell in sorted(table.cells, key=lambda cell: (cell.row, cell.column)):
            rows.setdefault(cell.row, []).append(cell)
        exclusion_counts = (
            sum(end - start + 1 for start, end in table.metadata.get("hidden_rows", [])),
            sum(end - start + 1 for start, end in table.metadata.get("hidden_columns", [])),
        )
        # Build the evidence projection once: never replicate formula expressions in passages.
        projected = {(cell.row, cell.column): cell for cell in evidence_cells(table, table.cells)}
        for row, cells in rows.items():
            if row == table.header_row or not any(cell.searchable for cell in cells):
                continue
            stripes: list[tuple[Cell, ...]] = []
            stripe: list[Cell] = []
            chars = 0
            for cell in cells:
                if (cell.row, cell.column) not in projected or row == header_at(
                    table, row, cell.column
                ):
                    if stripe:
                        stripes.append(tuple(stripe))
                    stripe, chars = [], 0
                    continue
                cell = projected[cell.row, cell.column]
                if stripe and (
                    native_region_at(table, row, stripe[-1].column)
                    != native_region_at(table, row, cell.column)
                    or header_at(table, row, stripe[-1].column)
                    != header_at(table, row, cell.column)
                ):
                    stripes.append(tuple(stripe))
                    stripe, chars = [], 0
                length = len(render_cells((cell,)))
                if length > MAX_FOCUS_CHARS:
                    raise TableError("Serialized cell exceeds focus budget; cannot split a cell")
                if stripe and (len(stripe) == 64 or chars + 1 + length > MAX_FOCUS_CHARS):
                    stripes.append(tuple(stripe))
                    stripe, chars = [], 0
                chars += length + bool(stripe)
                stripe.append(cell)
            if stripe:
                stripes.append(tuple(stripe))
            for stripe_cells in stripes:
                if table.source_type == "xlsx" and not any(
                    cell.searchable for cell in stripe_cells
                ):
                    continue
                focus = TableRegion(
                    table.table_id,
                    table.sheet_index,
                    "cells",
                    row,
                    row,
                    stripe_cells[0].column,
                    stripe_cells[-1].column,
                )
                contexts: list[TableContext] = []
                omitted: list[str] = []
                used_chars, used_cells = 0, len(stripe_cells)
                context_rows: tuple[tuple[ContextRole, int | None], ...] = (
                    ("header", header_at(table, row, focus.column_start)),
                    ("preceding", row - 1),
                    ("following", row + 1),
                )
                for role, context_row in context_rows:
                    if (
                        context_row is None
                        or context_row not in rows
                        or not context_row_allowed(
                            table, focus, context_row, header=role == "header"
                        )
                    ):
                        continue
                    if (
                        role != "header"
                        and table.source_type == "csv"
                        and (
                            not any(cell.searchable for cell in rows[context_row])
                            or any(not cell.visible for cell in rows[context_row])
                        )
                    ):
                        continue
                    selected = tuple(
                        cell
                        for cell in rows[context_row]
                        if focus.column_start <= cell.column <= focus.column_end
                    )
                    if any(not cell_is_visible(table, cell) for cell in selected):
                        omitted.append(f"{role}: hidden cells")
                        continue
                    if (
                        role != "header"
                        and table.source_type == "xlsx"
                        and not any(cell.searchable for cell in selected)
                    ):
                        continue
                    selected = tuple(projected[cell.row, cell.column] for cell in selected)
                    text = render_cells(selected)
                    if used_chars + len(text) > context_chars:
                        omitted.append(f"{role}: context character budget")
                        continue
                    if used_cells + len(selected) > MAX_ITEM_CELLS:
                        omitted.append(f"{role}: materialization cell budget")
                        continue
                    context = TableContext(
                        role,
                        TableRegion(
                            table.table_id,
                            table.sheet_index,
                            "cells",
                            context_row,
                            context_row,
                            focus.column_start,
                            focus.column_end,
                        ),
                        text,
                    )
                    context = replace(
                        context,
                        location_label=table_location_label(table, context.region),
                        annotations=hidden_exclusion_annotations(
                            table, context.region, counts=exclusion_counts
                        ),
                    )
                    candidate = TablePassage(
                        focus,
                        render_cells(stripe_cells),
                        (*contexts, context),
                        annotations=hidden_exclusion_annotations(
                            table, focus, counts=exclusion_counts
                        ),
                    )
                    if len(candidate.text) > MAX_ITEM_CHARS:
                        omitted.append(f"{role}: full passage character budget")
                        continue
                    contexts.append(context)
                    used_chars += len(text)
                    used_cells += len(selected)
                passage = TablePassage(
                    focus,
                    render_cells(stripe_cells),
                    tuple(contexts),
                    tuple(omitted),
                    annotations=hidden_exclusion_annotations(table, focus, counts=exclusion_counts),
                )
                if len(passage.text) > MAX_ITEM_CHARS:
                    raise TableError("Evidence representation exceeds materialization budget")
                index_bytes += len(passage.text.encode("utf-8"))
                if len(passages) >= MAX_PASSAGES or index_bytes > MAX_INDEX_BYTES:
                    raise TableError("Artifact exceeds passage/index budget")
                passages.append(passage)
    if not passages:
        raise TableError("Artifact contains no searchable data evidence")
    return tuple(passages)

"""Generation-neutral tabular values, exact rectangles and bounded evidence rendering."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
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
RENDERER_VERSION = "1"
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
    if not table.visible or any(not cell.visible for cell in cells):
        raise TableError("Evidence region contains hidden cells")
    if len(render_cells(cells)) > MAX_ITEM_CHARS:
        raise TableError("Evidence region exceeds the rendered character budget")
    return cells


def render_cells(cells: tuple[Cell, ...]) -> str:
    """Render extractive table representations, not verbatim prose quotations."""
    return "\n".join(
        f"{column_label(cell.column)}{cell.row}={cell.kind}:{serialized(cell.value)}"
        for cell in cells
    )


@dataclass(frozen=True)
class TableContext:
    role: ContextRole
    region: TableRegion
    text: str


@dataclass(frozen=True)
class TablePassage:
    focus: TableRegion
    focus_text: str
    context: tuple[TableContext, ...] = ()
    omitted_context: tuple[str, ...] = ()

    @property
    def header_text(self) -> str:
        return "\n".join(item.text for item in self.context if item.role == "header")

    @property
    def keyword_text(self) -> str:
        return "\n".join(
            str(value)
            for text in (self.focus_text, self.header_text)
            for line in text.splitlines()
            if (value := json.loads(line.partition(":")[2])) not in {None, ""}
        )

    @property
    def text(self) -> str:
        return "\n".join(
            (
                "focus (extractive table representation):",
                self.focus_text,
                *(f"{item.role} ({item.region.label}):\n{item.text}" for item in self.context),
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
        if not table.visible:
            continue
        rows: dict[int, list[Cell]] = {}
        for cell in sorted(table.cells, key=lambda cell: (cell.row, cell.column)):
            rows.setdefault(cell.row, []).append(cell)
        for row, cells in rows.items():
            if row == table.header_row or not any(cell.searchable for cell in cells):
                continue
            stripes: list[tuple[Cell, ...]] = []
            stripe: list[Cell] = []
            chars = 0
            for cell in cells:
                if not cell.visible:
                    if stripe:
                        stripes.append(tuple(stripe))
                    stripe, chars = [], 0
                    continue
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
                    ("header", table.header_row),
                    ("preceding", row - 1),
                    ("following", row + 1),
                )
                for role, context_row in context_rows:
                    if (
                        context_row is None
                        or context_row not in rows
                        or (role != "header" and context_row == table.header_row)
                    ):
                        continue
                    neighbor = rows[context_row]
                    if role != "header" and (
                        not any(cell.searchable for cell in neighbor)
                        or any(not cell.visible for cell in neighbor)
                    ):
                        continue
                    selected = tuple(
                        cell
                        for cell in neighbor
                        if focus.column_start <= cell.column <= focus.column_end
                    )
                    if any(not cell.visible for cell in selected):
                        omitted.append(f"{role}: hidden cells")
                        continue
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
                    candidate = TablePassage(
                        focus, render_cells(stripe_cells), (*contexts, context)
                    )
                    if len(candidate.text) > MAX_ITEM_CHARS:
                        omitted.append(f"{role}: full passage character budget")
                        continue
                    contexts.append(context)
                    used_chars += len(text)
                    used_cells += len(selected)
                passage = TablePassage(
                    focus, render_cells(stripe_cells), tuple(contexts), tuple(omitted)
                )
                index_bytes += len(passage.text.encode("utf-8"))
                if len(passages) >= MAX_PASSAGES or index_bytes > MAX_INDEX_BYTES:
                    raise TableError("Artifact exceeds passage/index budget")
                passages.append(passage)
    if not passages:
        raise TableError("Artifact contains no searchable data evidence")
    return tuple(passages)

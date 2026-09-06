"""Transactional, generation-owned tabular storage; legacy evidence stays untouched."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from newsrag.tabular import (
    MAX_ITEM_CELLS,
    MAX_ITEM_CHARS,
    Cell,
    Table,
    TableError,
    TablePassage,
    TableRegion,
    positive_integer,
    render_cells,
    serialized,
    validate_region,
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS source_tables (
        document_id TEXT NOT NULL REFERENCES documents(id),
        processing_generation_id TEXT NOT NULL REFERENCES processing_generations(id),
        table_id TEXT NOT NULL,
        descriptor_json TEXT NOT NULL,
        PRIMARY KEY(document_id, processing_generation_id, table_id)
    )""",
    """CREATE TABLE IF NOT EXISTS table_cells (
        document_id TEXT NOT NULL,
        processing_generation_id TEXT NOT NULL,
        table_id TEXT NOT NULL,
        row_number INTEGER NOT NULL CHECK(row_number > 0),
        column_number INTEGER NOT NULL CHECK(column_number > 0),
        source_unit_id TEXT NOT NULL REFERENCES source_units(id),
        cell_json TEXT NOT NULL,
        PRIMARY KEY(document_id, processing_generation_id, table_id, row_number, column_number),
        FOREIGN KEY(document_id, processing_generation_id, table_id)
            REFERENCES source_tables(document_id, processing_generation_id, table_id)
    )""",
    """CREATE TABLE IF NOT EXISTS table_passages (
        passage_id TEXT PRIMARY KEY REFERENCES passages(id),
        chunk_id TEXT NOT NULL UNIQUE REFERENCES chunks(id),
        document_id TEXT NOT NULL,
        processing_generation_id TEXT NOT NULL,
        table_id TEXT NOT NULL,
        focus_json TEXT NOT NULL,
        context_json TEXT NOT NULL,
        omitted_context_json TEXT NOT NULL,
        focus_text TEXT NOT NULL,
        header_text TEXT NOT NULL,
        FOREIGN KEY(document_id, processing_generation_id, table_id)
            REFERENCES source_tables(document_id, processing_generation_id, table_id)
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS table_values_fts USING fts5(
        passage_id UNINDEXED, focus, header, tokenize='porter unicode61'
    )""",
)


def initialize_tabular_schema(connection: sqlite3.Connection) -> None:
    """Add empty relations without assigning fictional locations to old evidence."""
    for statement in SCHEMA:
        connection.execute(statement)
    for table in ("discovery_evidence",):
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        for name in ("table_region_json", "table_context_json"):
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
    validate_tabular_ownership(connection)


def validate_tabular_ownership(connection: sqlite3.Connection) -> None:
    """Fail closed on inconsistent generation, document, row, or passage membership."""
    invalid = connection.execute("""
        SELECT t.table_id FROM source_tables t
        LEFT JOIN processing_generations g ON g.id = t.processing_generation_id
        LEFT JOIN documents d ON d.id = t.document_id
        WHERE g.id IS NULL OR d.id IS NULL OR g.document_id != t.document_id
            OR json_extract(t.descriptor_json, '$.table_id') IS NOT t.table_id
            OR json_type(t.descriptor_json, '$.sheet_index') IS NOT 'integer'
            OR t.table_id != 'sheet-' || json_extract(t.descriptor_json, '$.sheet_index')
        LIMIT 1
    """).fetchone()
    if invalid:
        raise TableError("Table descriptor has inconsistent document/generation ownership")
    invalid = connection.execute("""
        SELECT c.table_id FROM table_cells c
        LEFT JOIN source_tables t ON t.document_id = c.document_id
            AND t.processing_generation_id = c.processing_generation_id AND t.table_id = c.table_id
        LEFT JOIN source_units u ON u.id = c.source_unit_id
        WHERE t.table_id IS NULL OR u.id IS NULL OR u.document_id != c.document_id
            OR u.processing_generation_id IS NOT c.processing_generation_id
            OR u.location_type != 'table_row'
            OR json_extract(u.location_json, '$.table_id') IS NOT c.table_id
            OR json_extract(u.location_json, '$.row_start') IS NOT c.row_number
            OR json_extract(u.location_json, '$.row_end') IS NOT c.row_number
            OR json_extract(c.cell_json, '$.row') IS NOT c.row_number
            OR json_extract(c.cell_json, '$.column') IS NOT c.column_number
        LIMIT 1
    """).fetchone()
    if invalid:
        raise TableError("Table cell has inconsistent row/document/generation ownership")
    invalid = connection.execute("""
        SELECT t.passage_id FROM table_passages t
        LEFT JOIN passages p ON p.id = t.passage_id
        LEFT JOIN chunks c ON c.id = t.chunk_id
        WHERE p.id IS NULL OR c.id IS NULL OR p.chunk_id != t.chunk_id
            OR p.document_id != t.document_id OR c.document_id != t.document_id
            OR p.processing_generation_id IS NOT t.processing_generation_id
            OR c.processing_generation_id IS NOT t.processing_generation_id
            OR json_extract(t.focus_json, '$.table_id') IS NOT t.table_id
            OR json_extract(t.focus_json, '$.source_unit_start_id') IS NOT p.source_unit_start_id
            OR json_extract(t.focus_json, '$.source_unit_end_id') IS NOT p.source_unit_end_id
        LIMIT 1
    """).fetchone()
    if invalid:
        raise TableError("Table passage has inconsistent document/generation ownership")
    invalid = connection.execute("""
        SELECT t.passage_id FROM table_passages t, json_each(t.context_json) context
        LEFT JOIN source_units first ON first.id = json_extract(context.value, '$.source_unit_start_id')
        LEFT JOIN source_units last ON last.id = json_extract(context.value, '$.source_unit_end_id')
        WHERE first.id IS NULL OR last.id IS NULL
            OR first.document_id != t.document_id OR last.document_id != t.document_id
            OR first.processing_generation_id IS NOT t.processing_generation_id
            OR last.processing_generation_id IS NOT t.processing_generation_id
            OR json_extract(context.value, '$.table_id') IS NOT t.table_id
            OR json_extract(context.value, '$.role') NOT IN ('header', 'preceding', 'following', 'merge-anchor')
        LIMIT 1
    """).fetchone()
    if invalid:
        raise TableError("Table context has inconsistent document/generation ownership")
    invalid = connection.execute("""
        SELECT u.id FROM source_units u
        LEFT JOIN source_tables t ON t.document_id = u.document_id
            AND t.processing_generation_id = u.processing_generation_id
            AND t.table_id = json_extract(u.location_json, '$.table_id')
        WHERE u.location_type = 'table_row' AND t.table_id IS NULL LIMIT 1
    """).fetchone()
    if invalid:
        raise TableError("Table row has no owned descriptor; cannot fabricate a legacy location")


def validate_bundle_metadata(
    *,
    document_id: str,
    generation_id: str,
    tables: tuple[Table, ...],
    source_units: Sequence[tuple[object, ...]],
    passages: tuple[TablePassage, ...],
) -> None:
    """Bound the entire serialized owned bundle, including selectors and row labels."""
    from newsrag.tabular import MAX_METADATA_BYTES

    size = 0

    def consume(value: object) -> None:
        nonlocal size
        size += len(serialized(value).encode("utf-8"))
        if size > MAX_METADATA_BYTES:
            raise TableError("Tabular bundle exceeds the 64 MiB serialized metadata budget")

    anchors = {}
    for unit in source_units:
        consume((*unit, generation_id))
        location = json.loads(str(unit[5]))
        anchors[location["table_id"], location["row_start"]] = str(unit[0])
    for table in tables:
        consume((document_id, generation_id, table.table_id, table.descriptor()))
        for cell in table.cells:
            consume(
                (
                    document_id,
                    generation_id,
                    table.table_id,
                    anchors[table.table_id, cell.row],
                    asdict(cell),
                )
            )
    for passage in passages:
        references = []
        for role, region in [
            ("focus", passage.focus),
            *((item.role, item.region) for item in passage.context),
        ]:
            references.append(
                {
                    "role": role,
                    **region.to_dict(),
                    "source_unit_start_id": anchors[region.table_id, region.row_start],
                    "source_unit_end_id": anchors[region.table_id, region.row_end],
                }
            )
        consume((document_id, generation_id, references, passage.omitted_context))


def persist_tables(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    generation_id: str,
    tables: tuple[Table, ...],
    source_unit_ids: dict[int, str],
) -> None:
    """Insert the complete canonical bundle in the caller's publication transaction."""
    if not connection.in_transaction:
        raise TableError("Table publication requires a transaction")
    ordinal = 0
    for table in tables:
        table.validate()
        connection.execute(
            "INSERT INTO source_tables VALUES(?, ?, ?, ?)",
            (document_id, generation_id, table.table_id, serialized(table.descriptor())),
        )
        row_ids: dict[int, str] = {}
        if table.row_start is not None and table.row_end is not None:
            for row in range(table.row_start, table.row_end + 1):
                ordinal += 1
                if ordinal not in source_unit_ids:
                    raise TableError("Table row is missing its source-unit anchor")
                row_ids[row] = source_unit_ids[ordinal]
        connection.executemany(
            "INSERT INTO table_cells VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    document_id,
                    generation_id,
                    table.table_id,
                    cell.row,
                    cell.column,
                    row_ids[cell.row],
                    serialized(asdict(cell)),
                )
                for cell in table.cells
            ),
        )
    validate_tabular_ownership(connection)


@dataclass(frozen=True)
class ResolvedTableRegion:
    document_id: str
    processing_generation_id: str
    region: TableRegion
    source_unit_start_id: str
    source_unit_end_id: str
    cells: tuple[Cell, ...]
    annotations: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return render_cells(self.cells)

    def reference(self) -> dict[str, Any]:
        return {
            **self.region.to_dict(),
            "source_unit_start_id": self.source_unit_start_id,
            "source_unit_end_id": self.source_unit_end_id,
        }


def resolve_table_region(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    generation_id: str,
    region: TableRegion,
    source_unit_start_id: str | None = None,
    source_unit_end_id: str | None = None,
) -> ResolvedTableRegion:
    """Resolve bounded cells and all intervening row anchors against persisted ownership."""
    if region.cell_count > MAX_ITEM_CELLS:
        raise TableError("Evidence region exceeds cell budget; select a narrower region")
    row = connection.execute(
        "SELECT t.descriptor_json FROM source_tables t JOIN processing_generations g "
        "ON g.id = t.processing_generation_id AND g.document_id = t.document_id "
        "WHERE t.document_id = ? AND t.processing_generation_id = ? AND t.table_id = ?",
        (document_id, generation_id, region.table_id),
    ).fetchone()
    if row is None:
        raise TableError("Unknown table or mismatched document/generation ownership")
    records = connection.execute(
        """
        SELECT c.cell_json, c.row_number, c.column_number, c.source_unit_id,
            u.document_id, u.processing_generation_id, u.location_type, u.location_json, u.ordinal
        FROM table_cells c LEFT JOIN source_units u ON u.id = c.source_unit_id
        WHERE c.document_id = ? AND c.processing_generation_id = ? AND c.table_id = ?
            AND c.row_number BETWEEN ? AND ? AND c.column_number BETWEEN ? AND ?
        ORDER BY c.row_number, c.column_number
    """,
        (
            document_id,
            generation_id,
            region.table_id,
            region.row_start,
            region.row_end,
            region.column_start,
            region.column_end,
        ),
    ).fetchall()
    cells = []
    anchors: dict[int, tuple[str, int]] = {}
    for record in records:
        if record[4] != document_id or record[5] != generation_id or record[6] != "table_row":
            raise TableError("Cell row anchor has inconsistent ownership")
        location = json.loads(record[7])
        for name in ("sheet_index", "row_start", "row_end", "column_start", "column_end"):
            positive_integer(location.get(name), f"row anchor {name}")
        if any(
            location.get(key) != value
            for key, value in (
                ("table_id", region.table_id),
                ("sheet_index", region.sheet_index),
                ("row_start", record[1]),
                ("row_end", record[1]),
            )
        ):
            raise TableError("Cell coordinates do not match the row anchor")
        cell = Cell(**json.loads(record[0]))
        cell.validate()
        if (cell.row, cell.column) != (record[1], record[2]):
            raise TableError("Persisted cell coordinates are inconsistent")
        anchor = str(record[3]), int(record[8])
        if cell.row in anchors and anchors[cell.row] != anchor:
            raise TableError("Row has inconsistent source-unit anchors")
        anchors[cell.row] = anchor
        cells.append(cell)
    table = Table(**json.loads(row[0]), cells=tuple(cells))
    resolved = validate_region(table, region)
    ordered = list(anchors.values())
    if any(
        current[1] != previous[1] + 1
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        raise TableError("Evidence rows are not consecutive source units")
    start_id, end_id = ordered[0][0], ordered[-1][0]
    if (
        source_unit_start_id is not None
        and source_unit_start_id != start_id
        or source_unit_end_id is not None
        and source_unit_end_id != end_id
    ):
        raise TableError("Evidence rectangle does not match its source-unit endpoints")
    annotations = []
    for value in table.metadata.get("merges", []):
        merge = TableRegion.from_dict(value)
        if (
            region.row_start <= merge.row_start <= region.row_end
            and region.column_start <= merge.column_start <= region.column_end
        ):
            annotations.append(f"merged anchor: {merge.label}")
    return ResolvedTableRegion(
        document_id, generation_id, region, start_id, end_id, resolved, tuple(annotations)
    )


def persist_table_passage(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    generation_id: str,
    passage_id: str,
    chunk_id: str,
    passage: TablePassage,
) -> None:
    """Save focus and separately owned context; keyword indexes exclude neighbor values."""
    focus = resolve_table_region(
        connection, document_id=document_id, generation_id=generation_id, region=passage.focus
    )
    contexts = []
    for context in passage.context:
        resolved = resolve_table_region(
            connection, document_id=document_id, generation_id=generation_id, region=context.region
        )
        if resolved.text != context.text:
            raise TableError("Context representation does not match its selected cells")
        contexts.append({"role": context.role, **resolved.reference()})
    if focus.text != passage.focus_text or len(passage.text) > MAX_ITEM_CHARS:
        raise TableError("Focus representation does not match its selected cells/budget")
    from newsrag.tabular_evidence import resolve_contexts

    resolve_contexts(connection, focus, contexts)
    connection.execute(
        "INSERT INTO table_passages VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            passage_id,
            chunk_id,
            document_id,
            generation_id,
            passage.focus.table_id,
            serialized(focus.reference()),
            serialized(contexts),
            serialized(passage.omitted_context),
            passage.focus_text,
            passage.header_text,
        ),
    )
    connection.execute(
        "INSERT INTO table_values_fts(passage_id, focus, header) VALUES(?, ?, ?)",
        (
            passage_id,
            "\n".join(str(cell.value) for cell in focus.cells if cell.searchable),
            "\n".join(
                str(cell.value)
                for context in passage.context
                if context.role == "header"
                for cell in resolve_table_region(
                    connection,
                    document_id=document_id,
                    generation_id=generation_id,
                    region=context.region,
                ).cells
                if cell.searchable
            ),
        ),
    )

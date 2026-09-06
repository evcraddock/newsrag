"""Typed tabular evidence resolution shared by retrieval, discovery and packets."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from newsrag.tabular import (
    MAX_ITEM_CELLS,
    MAX_ITEM_CHARS,
    Table,
    TableError,
    TableRegion,
    context_row_allowed,
)
from newsrag.tabular_storage import ResolvedTableRegion, resolve_table_region


@dataclass(frozen=True)
class ContextEvidence:
    role: str
    selection: ResolvedTableRegion

    def reference(self) -> dict[str, Any]:
        return {"role": self.role, **self.selection.reference()}


@dataclass(frozen=True)
class TableEvidence:
    focus: ResolvedTableRegion
    context: tuple[ContextEvidence, ...] = ()
    omitted_context: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(
            (
                "focus (extractive table representation):",
                self.focus.text,
                *self.focus.annotations,
                *(
                    f"{item.role} ({item.selection.location_label or item.selection.region.label}):\n{item.selection.text}"
                    + (
                        "\n" + "\n".join(item.selection.annotations)
                        if item.selection.annotations
                        else ""
                    )
                    for item in self.context
                ),
            )
        )


def resolve_reference(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    generation_id: str,
    reference: dict[str, Any],
) -> ResolvedTableRegion:
    """Resolve a rectangle including its explicitly persisted row endpoints."""
    value = dict(reference)
    start = value.pop("source_unit_start_id", None)
    end = value.pop("source_unit_end_id", None)
    if not isinstance(start, str) or not isinstance(end, str):
        raise TableError("Table evidence requires exact row-unit endpoints")
    return resolve_table_region(
        connection,
        document_id=document_id,
        generation_id=generation_id,
        region=TableRegion.from_dict(value),
        source_unit_start_id=start,
        source_unit_end_id=end,
    )


def resolve_contexts(
    connection: sqlite3.Connection, focus: ResolvedTableRegion, references: object
) -> tuple[ContextEvidence, ...]:
    """Require attributable header/immediate-neighbor roles in deterministic order."""
    if not isinstance(references, list) or len(references) > 8:
        raise TableError("Evidence context must be a bounded ordered list")
    descriptor_row = connection.execute(
        "SELECT descriptor_json FROM source_tables WHERE document_id = ? AND processing_generation_id = ? AND table_id = ?",
        (focus.document_id, focus.processing_generation_id, focus.region.table_id),
    ).fetchone()
    if descriptor_row is None:
        raise TableError("Missing focus descriptor")
    descriptor = json.loads(descriptor_row[0])
    table = Table(**descriptor)
    roles = {"header": 0, "preceding": 1, "following": 2, "merge-anchor": 3}
    previous = -1
    contexts: list[ContextEvidence] = []
    count, chars = len(focus.cells), len(focus.text)
    context_chars = 0
    seen: set[str] = set()
    for reference in references:
        if not isinstance(reference, dict):
            raise TableError("Context reference must be an object")
        fields = dict(reference)
        role = fields.pop("role", None)
        if role not in roles or roles[role] < previous or (role != "merge-anchor" and role in seen):
            raise TableError("Context roles are unknown, duplicated or out of order")
        previous = roles[role]
        seen.add(role)
        selection = resolve_reference(
            connection,
            document_id=focus.document_id,
            generation_id=focus.processing_generation_id,
            reference=fields,
        )
        region = selection.region
        if (region.table_id, region.sheet_index) != (
            focus.region.table_id,
            focus.region.sheet_index,
        ):
            raise TableError("Context mixes table/sheet ownership")
        if role == "merge-anchor":
            raw_merges = descriptor.get("metadata", {}).get("merges", [])
            if not isinstance(raw_merges, list) or len(raw_merges) > 10_000:
                raise TableError("Invalid bounded merge descriptors")
            merges = [TableRegion.from_dict(value) for value in raw_merges]
            matching = [
                merge
                for merge in merges
                if merge.table_id == focus.region.table_id
                and merge.sheet_index == focus.region.sheet_index
                and (merge.row_start, merge.column_start) == (region.row_start, region.column_start)
                and not (
                    merge.row_end < focus.region.row_start
                    or merge.row_start > focus.region.row_end
                    or merge.column_end < focus.region.column_start
                    or merge.column_start > focus.region.column_end
                )
            ]
            if (
                region.cell_count != 1
                or len(matching) != 1
                or not selection.cells[0].searchable
                or (
                    focus.region.row_start <= region.row_start <= focus.region.row_end
                    and focus.region.column_start <= region.column_start <= focus.region.column_end
                )
            ):
                raise TableError(
                    "Merge-anchor context must select the stored anchor of an intersecting persisted merge"
                )
            if any(item.selection.region == region for item in contexts):
                raise TableError("Duplicate merge-anchor context")
            count += 1
            context_chars += len(selection.text)
            chars += len(selection.text)
            if count > MAX_ITEM_CELLS or chars > MAX_ITEM_CHARS or context_chars > 16_384:
                raise TableError("Evidence context exceeds materialization budget")
            contexts.append(ContextEvidence(role, selection))
            continue
        expected_row = {
            "header": region.row_start,
            "preceding": focus.region.row_start - 1,
            "following": focus.region.row_end + 1,
        }[role]
        if (
            region.row_start != expected_row
            or region.row_end != expected_row
            or (
                role == "header"
                and focus.region.row_start <= region.row_start <= focus.region.row_end
            )
            or not context_row_allowed(
                table, focus.region, region.row_start, header=role == "header"
            )
        ):
            raise TableError(
                "Context role crosses a native boundary or declared header/immediate neighbor"
            )
        if (
            role != "header"
            and table.source_type == "xlsx"
            and not any(cell.searchable for cell in selection.cells)
        ):
            raise TableError("Blank/error/unavailable rows cannot supply neighbor context")
        if (region.column_start, region.column_end) != (
            focus.region.column_start,
            focus.region.column_end,
        ):
            raise TableError("Context columns must match the focus stripe")
        count += len(selection.cells)
        context_chars += len(selection.text)
        chars += len(selection.text)
        if count > MAX_ITEM_CELLS or chars > MAX_ITEM_CHARS or context_chars > 16_384:
            raise TableError("Evidence context exceeds materialization budget")
        contexts.append(ContextEvidence(role, selection))
    evidence = TableEvidence(focus, tuple(contexts))
    if len(evidence.text) > MAX_ITEM_CHARS:
        raise TableError("Evidence representation exceeds materialization budget")
    return evidence.context


def load_passage_table_evidence(
    connection: sqlite3.Connection,
    *,
    passage_id: str,
    document_id: str,
    generation_id: str | None = None,
) -> TableEvidence | None:
    """Load retained generation-owned selectors without consulting the active pointer."""
    row = connection.execute(
        "SELECT t.document_id, t.processing_generation_id, t.focus_json, t.context_json, "
        "t.omitted_context_json, p.document_id, p.processing_generation_id, "
        "p.source_unit_start_id, p.source_unit_end_id, t.focus_text, t.header_text "
        "FROM table_passages t JOIN passages p ON p.id = t.passage_id WHERE t.passage_id = ?",
        (passage_id,),
    ).fetchone()
    if row is None:
        return None
    if (
        row[0] != document_id
        or row[5] != document_id
        or row[1] != row[6]
        or generation_id is not None
        and row[1] != generation_id
    ):
        raise TableError("Tabular passage has inconsistent document/generation ownership")
    focus = resolve_reference(
        connection, document_id=document_id, generation_id=str(row[1]), reference=json.loads(row[2])
    )
    if (focus.source_unit_start_id, focus.source_unit_end_id) != (
        row[7],
        row[8],
    ) or focus.text != row[9]:
        raise TableError("Tabular focus does not match the passage endpoints/values")
    contexts = resolve_contexts(connection, focus, json.loads(row[3]))
    if "\n".join(item.selection.text for item in contexts if item.role == "header") != row[10]:
        raise TableError("Tabular header index does not match its canonical cells")
    omitted = json.loads(row[4])
    if not isinstance(omitted, list) or any(not isinstance(reason, str) for reason in omitted):
        raise TableError("Invalid context omission reasons")
    return TableEvidence(focus, contexts, tuple(omitted))


def validate_table_quote(evidence: TableEvidence, quote: str) -> None:
    """Accept only exact selected values with their coordinates, not context substrings."""
    focus = evidence.focus
    if len(focus.cells) == 1 and quote == focus.cells[0].value and quote != "":
        return
    allowed = set(focus.text.splitlines())
    lines = quote.splitlines()
    if not lines or len(lines) != len(set(lines)) or any(line not in allowed for line in lines):
        raise TableError(
            "Table quote must map exact serialized values to selected cell coordinates"
        )

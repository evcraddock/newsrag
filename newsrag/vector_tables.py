from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import lancedb  # type: ignore[import-untyped]
import pyarrow as pa

_PARTITION_MARKER = "__dim_"
_REQUIRED_METADATA_FIELDS = ("provider", "model", "version")


def add_vector_records(
    lancedb_path: Path,
    table_name: str,
    records: Sequence[dict[str, object]],
) -> None:
    """Add generic vector records without mixing incompatible dimensions."""

    if not records:
        return
    _validate_table_name(table_name)
    grouped: dict[tuple[int, tuple[str, ...]], list[dict[str, object]]] = {}
    for record in records:
        dimensions = _validate_record(record)
        field_names = tuple(sorted(record))
        grouped.setdefault((dimensions, field_names), []).append(record)

    lancedb_path.mkdir(parents=True, exist_ok=True)
    database = lancedb.connect(lancedb_path)
    for (dimensions, field_names), grouped_records in grouped.items():
        target_name = _select_target_table(
            database,
            table_name,
            dimensions=dimensions,
            field_names=field_names,
        )
        if target_name is None:
            target_name = _next_partition_name(database, table_name, dimensions)
            database.create_table(target_name, data=grouped_records)
            continue

        try:
            database.open_table(target_name).add(grouped_records)
        except ValueError:
            # Older tables can have null-only optional columns whose Arrow type
            # cannot accept later values. Preserve them and start a compatible
            # partition rather than rewriting or dropping historical vectors.
            partition_name = _next_partition_name(database, table_name, dimensions)
            database.create_table(partition_name, data=grouped_records)


def delete_vector_records(
    lancedb_path: Path,
    table_name: str,
    key: str,
    ids: Sequence[str],
) -> None:
    """Delete selected vector IDs from a base table and all its partitions."""

    if not ids or not lancedb_path.exists():
        return
    _validate_table_name(table_name)
    if not key.strip():
        raise ValueError("Vector record key must be non-empty")

    database = lancedb.connect(lancedb_path)
    expression = f"{key} IN ({', '.join(_sql_string(value) for value in ids)})"
    for candidate_name in _vector_table_names(database, table_name):
        table = database.open_table(candidate_name)
        if key not in table.schema.names:
            continue
        table.delete(expression)


def search_vector_records(
    lancedb_path: Path,
    table_name: str,
    *,
    key: str,
    vector: Sequence[float],
    provider: str,
    model: str,
    version: str,
    limit: int,
) -> list[dict[str, object]]:
    """Search compatible vector partitions and return globally ranked unique rows."""

    if limit < 1 or not vector or not lancedb_path.exists():
        return []
    database = lancedb.connect(lancedb_path)
    dimensions = len(vector)
    metadata_filter = " AND ".join(
        (
            f"provider = {_sql_string(provider)}",
            f"model = {_sql_string(model)}",
            f"version = {_sql_string(version)}",
        )
    )
    rows_by_id: dict[str, dict[str, object]] = {}
    for candidate_name in _vector_table_names(database, table_name):
        table = database.open_table(candidate_name)
        if _table_vector_dimensions(table.schema) != dimensions:
            continue
        if key not in table.schema.names or any(
            field not in table.schema.names for field in _REQUIRED_METADATA_FIELDS
        ):
            continue
        rows = (
            table.search(list(vector)).where(metadata_filter, prefilter=True).limit(limit).to_list()
        )
        for row in rows:
            if not isinstance(row, dict) or row.get(key) is None or row.get("_distance") is None:
                continue
            row_id = str(row[key])
            distance = _row_distance(row)
            existing = rows_by_id.get(row_id)
            if existing is None or distance < _row_distance(existing):
                rows_by_id[row_id] = row

    return sorted(
        rows_by_id.values(),
        key=lambda row: (_row_distance(row), str(row[key])),
    )[:limit]


def _row_distance(row: dict[str, object]) -> float:
    value = row.get("_distance")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Vector search returned an invalid distance")
    return float(value)


def _validate_record(record: dict[str, object]) -> int:
    for field in _REQUIRED_METADATA_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Vector record {field} must be a non-empty string")
    raw_vector = record.get("vector")
    if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, str | bytes):
        raise ValueError("Vector record vector must be a non-empty numeric sequence")
    vector = list(raw_vector)
    if not vector:
        raise ValueError("Vector record vector must be a non-empty numeric sequence")
    if any(
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        for value in vector
    ):
        raise ValueError("Vector record vector must contain only finite numbers")
    return len(vector)


def _select_target_table(
    database: object,
    table_name: str,
    *,
    dimensions: int,
    field_names: tuple[str, ...],
) -> str | None:
    for candidate_name in _vector_table_names(database, table_name):
        table = database.open_table(candidate_name)  # type: ignore[attr-defined]
        if _table_vector_dimensions(table.schema) != dimensions:
            continue
        if tuple(sorted(table.schema.names)) == field_names:
            return candidate_name
    return None


def _next_partition_name(database: object, table_name: str, dimensions: int) -> str:
    existing = set(_database_table_names(database))
    if table_name not in existing:
        return table_name
    base_partition = f"{table_name}{_PARTITION_MARKER}{dimensions}"
    if base_partition not in existing:
        return base_partition
    suffix = 2
    while f"{base_partition}_{suffix}" in existing:
        suffix += 1
    return f"{base_partition}_{suffix}"


def _vector_table_names(database: object, table_name: str) -> list[str]:
    prefix = f"{table_name}{_PARTITION_MARKER}"
    names = _database_table_names(database)
    return sorted(name for name in names if name == table_name or name.startswith(prefix))


def _database_table_names(database: object) -> list[str]:
    names: list[str] = []
    page_token: str | None = None
    while True:
        response = database.list_tables(page_token=page_token)  # type: ignore[attr-defined]
        names.extend(str(name) for name in response.tables)
        page_token = response.page_token
        if page_token is None:
            return names


def _table_vector_dimensions(schema: pa.Schema) -> int | None:
    try:
        vector_type = schema.field("vector").type
    except KeyError:
        return None
    if pa.types.is_fixed_size_list(vector_type):
        return int(vector_type.list_size)
    return None


def _validate_table_name(table_name: str) -> None:
    if not table_name.strip():
        raise ValueError("Vector table name must be non-empty")


def _sql_string(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"

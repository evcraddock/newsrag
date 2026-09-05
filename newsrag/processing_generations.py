from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProcessingGenerationError(Exception):
    """Raised when processing-generation publication is stale or inconsistent."""


@dataclass(frozen=True)
class ProcessingGeneration:
    """One immutable processing result for a document."""

    id: str
    document_id: str
    fingerprint: str
    configuration: dict[str, Any]
    normalized_path: str | None
    job_id: str | None
    created_at: str


def publish_processing_generation(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    generation_id: str,
    fingerprint: str,
    configuration: dict[str, Any],
    normalized_path: str | None,
    job_id: str | None,
    expected_generation_id: str | None,
) -> None:
    """Insert a generation and compare-and-swap the document's current pointer.

    The caller owns the surrounding transaction and is responsible for committing or
    rolling it back. Initial publication requires ``expected_generation_id=None``;
    reprocessing must provide the current generation ID.
    """

    if not document_id.strip():
        raise ProcessingGenerationError("Cannot publish a generation without a document ID")
    if not generation_id.strip():
        raise ProcessingGenerationError("Cannot publish a generation without a generation ID")
    if not fingerprint.strip():
        raise ProcessingGenerationError(
            f"Cannot publish processing generation {generation_id}: fingerprint is empty"
        )
    if not isinstance(configuration, dict):
        raise ProcessingGenerationError(
            f"Cannot publish processing generation {generation_id}: configuration must be a map"
        )
    try:
        configuration_json = json.dumps(configuration, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProcessingGenerationError(
            f"Cannot publish processing generation {generation_id}: "
            f"configuration is not JSON serializable"
        ) from exc

    document_row = connection.execute(
        "SELECT current_processing_generation_id FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if document_row is None:
        raise ProcessingGenerationError(
            f"Cannot publish processing generation {generation_id}: "
            f"document {document_id} is missing"
        )
    current_generation_id = str(document_row[0]) if document_row[0] is not None else None

    conflicting_generation = connection.execute(
        "SELECT document_id FROM processing_generations WHERE id = ?",
        (generation_id,),
    ).fetchone()
    if conflicting_generation is not None:
        owner_id = str(conflicting_generation[0])
        raise ProcessingGenerationError(
            f"Cannot publish processing generation {generation_id}: "
            f"generation ID already belongs to document {owner_id}"
        )

    if expected_generation_id is None:
        if current_generation_id is not None:
            raise ProcessingGenerationError(
                f"Initial processing publication rejected for document {document_id}: "
                f"current generation is {current_generation_id}"
            )
        prior_generation = connection.execute(
            "SELECT id FROM processing_generations WHERE document_id = ? ORDER BY created_at, id LIMIT 1",
            (document_id,),
        ).fetchone()
        if prior_generation is not None:
            raise ProcessingGenerationError(
                f"Initial processing publication rejected for document {document_id}: "
                f"generation history already contains {prior_generation[0]}"
            )
    else:
        if not expected_generation_id.strip():
            raise ProcessingGenerationError(
                f"Invalid expected processing generation for document {document_id}"
            )
        expected_row = connection.execute(
            "SELECT document_id FROM processing_generations WHERE id = ?",
            (expected_generation_id,),
        ).fetchone()
        if expected_row is None or str(expected_row[0]) != document_id:
            raise ProcessingGenerationError(
                f"Expected processing generation {expected_generation_id} does not belong "
                f"to document {document_id}"
            )
        if current_generation_id != expected_generation_id:
            raise ProcessingGenerationError(
                f"Processing generation conflict for document {document_id}: expected "
                f"{expected_generation_id}, found {current_generation_id}"
            )

    if not connection.in_transaction:
        connection.execute("BEGIN")
    connection.execute("SAVEPOINT publish_processing_generation")
    try:
        connection.execute(
            """
            INSERT INTO processing_generations(
                id, document_id, fingerprint, configuration_json, normalized_path, job_id
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                document_id,
                fingerprint,
                configuration_json,
                normalized_path,
                job_id,
            ),
        )
        update_cursor = connection.execute(
            """
            UPDATE documents
            SET current_processing_generation_id = ?
            WHERE id = ? AND current_processing_generation_id IS ?
            """,
            (generation_id, document_id, expected_generation_id),
        )
        if update_cursor.rowcount != 1:
            raise ProcessingGenerationError(
                f"Processing generation conflict for document {document_id} while "
                f"publishing {generation_id}"
            )
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT publish_processing_generation")
        connection.execute("RELEASE SAVEPOINT publish_processing_generation")
        if isinstance(exc, ProcessingGenerationError):
            raise
        if isinstance(exc, sqlite3.IntegrityError):
            raise ProcessingGenerationError(
                f"Cannot publish processing generation {generation_id} for "
                f"document {document_id}: {exc}"
            ) from exc
        raise
    connection.execute("RELEASE SAVEPOINT publish_processing_generation")


def get_processing_generations(database_path: Path, document_id: str) -> list[ProcessingGeneration]:
    """Return a document's processing history in publication order."""

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id, document_id, fingerprint, configuration_json,
                normalized_path, job_id, created_at
            FROM processing_generations
            WHERE document_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (document_id,),
        ).fetchall()
    return [_row_to_processing_generation(row) for row in rows]


def _row_to_processing_generation(
    row: sqlite3.Row | tuple[object, ...],
) -> ProcessingGeneration:
    configuration = json.loads(str(row[3]))
    if not isinstance(configuration, dict):
        raise ProcessingGenerationError(
            f"Processing generation {row[0]} has a non-object configuration"
        )
    return ProcessingGeneration(
        id=str(row[0]),
        document_id=str(row[1]),
        fingerprint=str(row[2]),
        configuration=configuration,
        normalized_path=str(row[4]) if row[4] is not None else None,
        job_id=str(row[5]) if row[5] is not None else None,
        created_at=str(row[6]),
    )

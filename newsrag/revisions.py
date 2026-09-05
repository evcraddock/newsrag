from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path


class RevisionError(Exception):
    """Raised when revision publication or revision storage is inconsistent."""


@dataclass(frozen=True)
class Revision:
    """One published document's membership in a source revision history."""

    id: str
    source_id: str
    document_id: str
    revision_number: int
    published_at: str
    job_id: str | None


def publish_revision(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    job_id: str | None,
    expected_generation: int | None = None,
) -> Revision:
    """Publish a document revision and atomically advance its source pointer.

    ``expected_generation=None`` is reserved for an initial publication. An integer
    performs a compare-and-swap against an existing source's publication generation.
    The caller owns the surrounding transaction and is responsible for committing or
    rolling it back.
    """

    if not document_id.strip():
        raise RevisionError("Cannot publish a revision without a document ID")
    if isinstance(expected_generation, bool) or (
        expected_generation is not None and expected_generation < 0
    ):
        raise RevisionError(
            f"Invalid expected publication generation {expected_generation!r} "
            f"for document {document_id}"
        )

    document_row = connection.execute(
        """
        SELECT
            documents.id AS document_id,
            source_artifacts.source_id,
            source_artifacts.state
        FROM documents
        JOIN source_artifacts ON source_artifacts.id = documents.artifact_id
        WHERE documents.id = ?
        """,
        (document_id,),
    ).fetchone()
    if document_row is None:
        raise RevisionError(
            f"Cannot publish document {document_id}: it has no source-owned artifact"
        )

    source_id = str(document_row[1])
    artifact_state = str(document_row[2])
    if artifact_state != "published":
        raise RevisionError(
            f"Cannot publish document {document_id} for source {source_id}: "
            f"artifact state is {artifact_state!r}, expected 'published'"
        )

    existing_row = connection.execute(
        "SELECT id FROM source_revisions WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if existing_row is not None:
        raise RevisionError(
            f"Cannot publish document {document_id}: it already belongs to revision "
            f"{existing_row[0]}"
        )

    source_row = connection.execute(
        """
        SELECT current_revision_id, publication_generation
        FROM sources
        WHERE id = ?
        """,
        (source_id,),
    ).fetchone()
    if source_row is None:
        raise RevisionError(f"Cannot publish document {document_id}: source {source_id} is missing")

    current_revision_id = str(source_row[0]) if source_row[0] is not None else None
    current_generation = int(source_row[1])
    if expected_generation is None:
        if current_revision_id is not None:
            raise RevisionError(
                f"Initial publication rejected for source {source_id}: current revision "
                f"is {current_revision_id} at generation {current_generation}"
            )
        if current_generation != 0:
            raise RevisionError(
                f"Initial publication rejected for source {source_id}: no current revision "
                f"at generation {current_generation}"
            )
        prior_revision = connection.execute(
            "SELECT id FROM source_revisions WHERE source_id = ? LIMIT 1",
            (source_id,),
        ).fetchone()
        if prior_revision is not None:
            raise RevisionError(
                f"Initial publication rejected for source {source_id}: revision history "
                f"already contains {prior_revision[0]}"
            )
        revision_number = 1
        generation_condition = current_generation
    else:
        if current_generation != expected_generation:
            raise RevisionError(
                f"Publication generation conflict for source {source_id}: expected "
                f"{expected_generation}, found {current_generation}"
            )
        if current_revision_id is None:
            raise RevisionError(
                f"Revision publication rejected for source {source_id}: generation "
                f"{current_generation} has no current revision"
            )
        revision_number = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) + 1
                FROM source_revisions
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()[0]
        )
        generation_condition = expected_generation

    revision_id = f"revision-{uuid.uuid4().hex}"
    if not connection.in_transaction:
        connection.execute("BEGIN")
    connection.execute("SAVEPOINT publish_revision")
    try:
        connection.execute(
            """
            INSERT INTO source_revisions(
                id, source_id, document_id, revision_number, published_at, job_id
            )
            VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """,
            (revision_id, source_id, document_id, revision_number, job_id),
        )
        update_cursor = connection.execute(
            """
            UPDATE sources
            SET current_revision_id = ?,
                publication_generation = publication_generation + 1
            WHERE id = ?
                AND current_revision_id IS ?
                AND publication_generation = ?
            """,
            (revision_id, source_id, current_revision_id, generation_condition),
        )
        if update_cursor.rowcount != 1:
            raise RevisionError(
                f"Publication generation conflict for source {source_id} while publishing "
                f"document {document_id}"
            )

        row = connection.execute(
            """
            SELECT id, source_id, document_id, revision_number, published_at, job_id
            FROM source_revisions
            WHERE id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the successful insert above
            raise RevisionError(f"Published revision {revision_id} could not be reloaded")
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT publish_revision")
        connection.execute("RELEASE SAVEPOINT publish_revision")
        if isinstance(exc, RevisionError):
            raise
        if isinstance(exc, sqlite3.IntegrityError):
            raise RevisionError(
                f"Cannot publish document {document_id} for source {source_id}: {exc}"
            ) from exc
        raise
    connection.execute("RELEASE SAVEPOINT publish_revision")
    return _row_to_revision(row)


def get_current_revision(database_path: Path, source_id: str) -> Revision | None:
    """Return a source's current published revision, if it has one."""

    with sqlite3.connect(database_path) as connection:
        source_row = connection.execute(
            "SELECT current_revision_id FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if source_row is None or source_row[0] is None:
            return None
        revision_row = connection.execute(
            """
            SELECT id, source_id, document_id, revision_number, published_at, job_id
            FROM source_revisions
            WHERE id = ?
            """,
            (str(source_row[0]),),
        ).fetchone()

    if revision_row is None or str(revision_row[1]) != source_id:
        raise RevisionError(
            f"Source {source_id} has invalid current revision pointer {source_row[0]}"
        )
    return _row_to_revision(revision_row)


def get_document_revision(database_path: Path, document_id: str) -> Revision | None:
    """Return a document's published revision membership, if present."""

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, source_id, document_id, revision_number, published_at, job_id
            FROM source_revisions
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
    return _row_to_revision(row) if row is not None else None


def _row_to_revision(row: sqlite3.Row | tuple[object, ...]) -> Revision:
    return Revision(
        id=str(row[0]),
        source_id=str(row[1]),
        document_id=str(row[2]),
        revision_number=int(str(row[3])),
        published_at=str(row[4]),
        job_id=str(row[5]) if row[5] is not None else None,
    )

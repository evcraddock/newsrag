from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from newsrag.sources import SOURCE_KIND_URL, SourceIdentity, artifact_id_for_hash

ARTIFACT_STATE_PROCESSING = "processing"
ARTIFACT_STATE_CHANGE_DETECTED = "change_detected"
ARTIFACT_STATE_PUBLISHED = "published"

OUTCOME_CREATED = "created"
OUTCOME_DUPLICATE_IGNORED = "duplicate_ignored"
OUTCOME_CHANGE_DETECTED = "change_detected_artifact_saved"
OUTCOME_CHANGE_ALREADY_DETECTED = "change_already_detected"

DecisionAction = Literal["process", "complete"]


@dataclass(frozen=True)
class IngestionIdentityDecision:
    """Identity decision made after exact artifact acquisition."""

    action: DecisionAction
    source_id: str
    artifact_id: str
    artifact_path: Path
    acquired_at: str
    document_source_path: Path
    document_source_url: str | None
    outcome: str | None = None
    document_id: str | None = None

    def result(
        self, *, outcome: str | None = None, document_id: str | None = None
    ) -> dict[str, object]:
        """Build the structured job result for this identity decision."""

        resolved_outcome = outcome or self.outcome
        if resolved_outcome is None:
            raise ValueError("An ingestion outcome is required")
        result: dict[str, object] = {
            "outcome": resolved_outcome,
            "source_id": self.source_id,
            "artifact_id": self.artifact_id,
        }
        resolved_document_id = document_id or self.document_id
        if resolved_document_id is not None:
            result["document_id"] = resolved_document_id
        return result


def find_published_duplicate(
    database_path: Path,
    content_hash: str,
) -> IngestionIdentityDecision | None:
    """Return an exact published-artifact match without modifying the corpus."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = _artifact_row(connection, content_hash)
    if row is None or row["document_id"] is None:
        return None
    return _decision_from_row(
        row,
        action="complete",
        outcome=OUTCOME_DUPLICATE_IGNORED,
    )


def register_acquired_artifact(
    database_path: Path,
    *,
    source_identity: SourceIdentity,
    content_hash: str,
    media_type: str,
    byte_size: int,
    stored_path: Path,
    acquired_at: str,
    reported_media_type: str | None,
    provenance: dict[str, object],
) -> IngestionIdentityDecision:
    """Register exact bytes and decide whether ordinary processing may continue."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")

        artifact_row = _artifact_row(connection, content_hash)
        if artifact_row is not None:
            if (
                artifact_row["document_id"] is None
                and str(artifact_row["state"]) == ARTIFACT_STATE_PROCESSING
                and str(artifact_row["media_type"]) != media_type
            ):
                connection.execute(
                    "UPDATE source_artifacts SET media_type = ? WHERE id = ?",
                    (media_type, str(artifact_row["artifact_id"])),
                )
                artifact_row = _artifact_row(connection, content_hash)
                if artifact_row is None:
                    raise RuntimeError("Updated source artifact could not be reloaded")
            decision = _decision_for_existing_artifact(connection, artifact_row)
            connection.commit()
            return decision

        current_document_id = _published_document_for_source(connection, source_identity.id)
        _insert_source(connection, source_identity)
        artifact_id = artifact_id_for_hash(content_hash)
        state = (
            ARTIFACT_STATE_CHANGE_DETECTED
            if current_document_id is not None
            else ARTIFACT_STATE_PROCESSING
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id,
                source_id,
                media_type,
                byte_size,
                content_hash,
                stored_path,
                acquired_at,
                state,
                reported_media_type,
                provenance_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                source_identity.id,
                media_type,
                byte_size,
                content_hash,
                str(stored_path),
                acquired_at,
                state,
                reported_media_type,
                json.dumps(provenance, sort_keys=True),
            ),
        )
        connection.commit()

    action: DecisionAction = "complete" if current_document_id is not None else "process"
    outcome = OUTCOME_CHANGE_DETECTED if current_document_id is not None else None
    return IngestionIdentityDecision(
        action=action,
        source_id=source_identity.id,
        artifact_id=artifact_id,
        artifact_path=stored_path,
        acquired_at=acquired_at,
        document_source_path=_document_source_path(
            source_kind=source_identity.kind,
            submitted_reference=source_identity.submitted_reference,
            stored_path=stored_path,
        ),
        document_source_url=(
            source_identity.submitted_reference if source_identity.kind == SOURCE_KIND_URL else None
        ),
        outcome=outcome,
        document_id=current_document_id,
    )


def find_stored_artifact_path(database_path: Path, content_hash: str) -> Path | None:
    """Return the durable path for previously registered exact bytes."""

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT stored_path FROM source_artifacts WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
    return Path(str(row[0])) if row is not None else None


def find_document_for_artifact(database_path: Path, artifact_id: str) -> str | None:
    """Return the published document ID for an artifact, if one exists."""

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id
            FROM documents
            WHERE artifact_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (artifact_id,),
        ).fetchone()
    return str(row[0]) if row is not None else None


def _decision_for_existing_artifact(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> IngestionIdentityDecision:
    if row["document_id"] is not None:
        return _decision_from_row(
            row,
            action="complete",
            outcome=OUTCOME_DUPLICATE_IGNORED,
        )

    if str(row["state"]) == ARTIFACT_STATE_CHANGE_DETECTED:
        current_document_id = _published_document_for_source(
            connection,
            str(row["source_id"]),
        )
        return _decision_from_row(
            row,
            action="complete",
            outcome=OUTCOME_CHANGE_ALREADY_DETECTED,
            document_id=current_document_id,
        )

    return _decision_from_row(row, action="process")


def _decision_from_row(
    row: sqlite3.Row,
    *,
    action: DecisionAction,
    outcome: str | None = None,
    document_id: str | None = None,
) -> IngestionIdentityDecision:
    stored_path = Path(str(row["stored_path"]))
    source_kind = str(row["source_kind"])
    submitted_reference = str(row["submitted_reference"])
    return IngestionIdentityDecision(
        action=action,
        source_id=str(row["source_id"]),
        artifact_id=str(row["artifact_id"]),
        artifact_path=stored_path,
        acquired_at=str(row["acquired_at"]),
        document_source_path=_document_source_path(
            source_kind=source_kind,
            submitted_reference=submitted_reference,
            stored_path=stored_path,
        ),
        document_source_url=(submitted_reference if source_kind == SOURCE_KIND_URL else None),
        outcome=outcome,
        document_id=(
            document_id
            if document_id is not None
            else str(row["document_id"])
            if row["document_id"] is not None
            else None
        ),
    )


def _artifact_row(connection: sqlite3.Connection, content_hash: str) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT
            source_artifacts.id AS artifact_id,
            source_artifacts.source_id,
            source_artifacts.stored_path,
            source_artifacts.acquired_at,
            source_artifacts.state,
            source_artifacts.media_type,
            sources.kind AS source_kind,
            sources.submitted_reference,
            documents.id AS document_id
        FROM source_artifacts
        JOIN sources ON sources.id = source_artifacts.source_id
        LEFT JOIN documents ON documents.artifact_id = source_artifacts.id
        WHERE source_artifacts.content_hash = ?
        ORDER BY documents.created_at ASC, documents.id ASC
        LIMIT 1
        """,
        (content_hash,),
    ).fetchone()
    return row if isinstance(row, sqlite3.Row) else None


def _published_document_for_source(
    connection: sqlite3.Connection,
    source_id: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT source_revisions.document_id
        FROM sources
        JOIN source_revisions
            ON source_revisions.id = sources.current_revision_id
        WHERE sources.id = ?
        """,
        (source_id,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _insert_source(connection: sqlite3.Connection, source_identity: SourceIdentity) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO sources(
            id,
            kind,
            submitted_reference,
            normalized_reference,
            resolved_reference
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            source_identity.id,
            source_identity.kind,
            source_identity.submitted_reference,
            source_identity.normalized_reference,
            source_identity.resolved_reference,
        ),
    )


def _document_source_path(
    *,
    source_kind: str,
    submitted_reference: str,
    stored_path: Path,
) -> Path:
    if source_kind == SOURCE_KIND_URL:
        return stored_path
    return Path(submitted_reference)

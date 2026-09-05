from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from newsrag.ingestion_identity import _published_document_for_source
from newsrag.revisions import (
    RevisionError,
    get_current_revision,
    get_document_revision,
    publish_revision,
)
from newsrag.storage import initialize_storage


def _insert_published_document(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    artifact_id: str,
    document_id: str,
    content_hash: str,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO sources(
            id, kind, submitted_reference, normalized_reference
        )
        VALUES(?, 'local_path', ?, ?)
        """,
        (source_id, f"/tmp/{source_id}.pdf", f"/tmp/{source_id}.pdf"),
    )
    connection.execute(
        """
        INSERT INTO source_artifacts(
            id, source_id, media_type, content_hash, stored_path, acquired_at, state
        )
        VALUES(?, ?, 'application/pdf', ?, ?, CURRENT_TIMESTAMP, 'published')
        """,
        (artifact_id, source_id, content_hash, f"/tmp/{artifact_id}.pdf"),
    )
    connection.execute(
        """
        INSERT INTO documents(id, source_hash, metadata_json, artifact_id)
        VALUES(?, ?, '{}', ?)
        """,
        (document_id, content_hash, artifact_id),
    )


def test_publish_initial_revision_and_load_it(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-1",
            document_id="document-1",
            content_hash="hash-1",
        )
        revision = publish_revision(
            connection,
            document_id="document-1",
            job_id="job-1",
        )

    assert revision.source_id == "source-1"
    assert revision.document_id == "document-1"
    assert revision.revision_number == 1
    assert revision.job_id == "job-1"
    assert revision.published_at
    assert get_current_revision(paths.database, "source-1") == revision
    assert get_document_revision(paths.database, "document-1") == revision
    assert get_document_revision(paths.database, "missing") is None

    with sqlite3.connect(paths.database) as connection:
        source_state = connection.execute(
            """
            SELECT current_revision_id, publication_generation
            FROM sources WHERE id = 'source-1'
            """
        ).fetchone()
    assert source_state == (revision.id, 1)


def test_publish_revision_appends_with_generation_check(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-1",
            document_id="document-1",
            content_hash="hash-1",
        )
        first = publish_revision(connection, document_id="document-1", job_id=None)
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-2",
            document_id="document-2",
            content_hash="hash-2",
        )
        second = publish_revision(
            connection,
            document_id="document-2",
            job_id="job-2",
            expected_generation=1,
        )

    assert first.revision_number == 1
    assert second.revision_number == 2
    assert get_current_revision(paths.database, "source-1") == second
    with sqlite3.connect(paths.database) as connection:
        generation = connection.execute(
            "SELECT publication_generation FROM sources WHERE id = 'source-1'"
        ).fetchone()[0]
        memberships = connection.execute(
            """
            SELECT document_id, revision_number
            FROM source_revisions
            WHERE source_id = 'source-1'
            ORDER BY revision_number
            """
        ).fetchall()
        published_document_id = _published_document_for_source(connection, "source-1")
    assert generation == 2
    assert published_document_id == "document-2"
    assert memberships == [("document-1", 1), ("document-2", 2)]


def test_initial_publication_cannot_replace_current_revision(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-1",
            document_id="document-1",
            content_hash="hash-1",
        )
        publish_revision(connection, document_id="document-1", job_id=None)
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-2",
            document_id="document-2",
            content_hash="hash-2",
        )
        with pytest.raises(RevisionError, match="Initial publication rejected.*source-1"):
            publish_revision(connection, document_id="document-2", job_id=None)


def test_stale_generation_does_not_add_revision(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-1",
            document_id="document-1",
            content_hash="hash-1",
        )
        first = publish_revision(connection, document_id="document-1", job_id=None)
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-2",
            document_id="document-2",
            content_hash="hash-2",
        )
        with pytest.raises(RevisionError, match="expected 0, found 1"):
            publish_revision(
                connection,
                document_id="document-2",
                job_id="stale-job",
                expected_generation=0,
            )

    assert get_current_revision(paths.database, "source-1") == first
    assert get_document_revision(paths.database, "document-2") is None


def test_publish_revision_requires_published_source_owned_artifact(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-1', 'source-1', 'application/pdf', 'hash-1',
                '/tmp/report.pdf', CURRENT_TIMESTAMP, 'processing'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, metadata_json, artifact_id)
            VALUES('document-1', '{}', 'artifact-1')
            """
        )
        with pytest.raises(RevisionError, match="artifact state is 'processing'"):
            publish_revision(connection, document_id="document-1", job_id=None)
        with pytest.raises(RevisionError, match="no source-owned artifact"):
            publish_revision(connection, document_id="missing", job_id=None)


def test_caller_rollback_reverts_revision_and_pointer(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    connection = sqlite3.connect(paths.database)
    try:
        connection.execute("BEGIN")
        _insert_published_document(
            connection,
            source_id="source-1",
            artifact_id="artifact-1",
            document_id="document-1",
            content_hash="hash-1",
        )
        publish_revision(connection, document_id="document-1", job_id=None)
        connection.rollback()
    finally:
        connection.close()

    assert get_current_revision(paths.database, "source-1") is None
    assert get_document_revision(paths.database, "document-1") is None

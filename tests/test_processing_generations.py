from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from newsrag.processing_generations import (
    ProcessingGenerationError,
    get_processing_generations,
    publish_processing_generation,
)
from newsrag.storage import initialize_storage


def _insert_document(database_path: Path, document_id: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO documents(id, metadata_json) VALUES(?, '{}')",
            (document_id,),
        )


def test_publish_processing_generation_advances_pointer_without_committing(
    tmp_path: Path,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    _insert_document(paths.database, "document-1")

    connection = sqlite3.connect(paths.database)
    publish_processing_generation(
        connection,
        document_id="document-1",
        generation_id="generation-1",
        fingerprint="pipeline:v1",
        configuration={"ocr": False, "language": "en"},
        normalized_path="/tmp/normalized.pdf",
        job_id="job-1",
        expected_generation_id=None,
    )

    pointer = connection.execute(
        "SELECT current_processing_generation_id FROM documents WHERE id = 'document-1'"
    ).fetchone()
    generation = connection.execute(
        """
        SELECT fingerprint, configuration_json, normalized_path, job_id
        FROM processing_generations WHERE id = 'generation-1'
        """
    ).fetchone()
    assert pointer == ("generation-1",)
    assert generation == (
        "pipeline:v1",
        '{"language": "en", "ocr": false}',
        "/tmp/normalized.pdf",
        "job-1",
    )

    connection.rollback()
    connection.close()
    assert get_processing_generations(paths.database, "document-1") == []


def test_publish_processing_generation_supports_reprocessing_cas(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    _insert_document(paths.database, "document-1")

    with sqlite3.connect(paths.database) as connection:
        publish_processing_generation(
            connection,
            document_id="document-1",
            generation_id="generation-1",
            fingerprint="pipeline:v1",
            configuration={},
            normalized_path=None,
            job_id=None,
            expected_generation_id=None,
        )
        publish_processing_generation(
            connection,
            document_id="document-1",
            generation_id="generation-2",
            fingerprint="pipeline:v2",
            configuration={"ocr": True},
            normalized_path="/tmp/ocr.pdf",
            job_id="job-2",
            expected_generation_id="generation-1",
        )

    generations = get_processing_generations(paths.database, "document-1")
    assert [generation.id for generation in generations] == ["generation-1", "generation-2"]
    assert generations[1].configuration == {"ocr": True}
    with sqlite3.connect(paths.database) as connection:
        pointer = connection.execute(
            "SELECT current_processing_generation_id FROM documents WHERE id = 'document-1'"
        ).fetchone()
    assert pointer == ("generation-2",)


def test_publish_processing_generation_rejects_stale_and_foreign_expectations(
    tmp_path: Path,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    _insert_document(paths.database, "document-1")
    _insert_document(paths.database, "document-2")

    with sqlite3.connect(paths.database) as connection:
        publish_processing_generation(
            connection,
            document_id="document-1",
            generation_id="generation-1",
            fingerprint="pipeline:v1",
            configuration={},
            normalized_path=None,
            job_id=None,
            expected_generation_id=None,
        )
        publish_processing_generation(
            connection,
            document_id="document-1",
            generation_id="generation-2",
            fingerprint="pipeline:v2",
            configuration={},
            normalized_path=None,
            job_id=None,
            expected_generation_id="generation-1",
        )
        publish_processing_generation(
            connection,
            document_id="document-2",
            generation_id="generation-foreign",
            fingerprint="pipeline:v1",
            configuration={},
            normalized_path=None,
            job_id=None,
            expected_generation_id=None,
        )

        with pytest.raises(ProcessingGenerationError, match="does not belong"):
            publish_processing_generation(
                connection,
                document_id="document-1",
                generation_id="generation-bad-owner",
                fingerprint="pipeline:v2",
                configuration={},
                normalized_path=None,
                job_id=None,
                expected_generation_id="generation-foreign",
            )
        with pytest.raises(ProcessingGenerationError, match="conflict"):
            publish_processing_generation(
                connection,
                document_id="document-1",
                generation_id="generation-stale",
                fingerprint="pipeline:v2",
                configuration={},
                normalized_path=None,
                job_id=None,
                expected_generation_id="generation-1",
            )

        generation_ids = connection.execute(
            "SELECT id FROM processing_generations ORDER BY id"
        ).fetchall()
    assert generation_ids == [
        ("generation-1",),
        ("generation-2",),
        ("generation-foreign",),
    ]

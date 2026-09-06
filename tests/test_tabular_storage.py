from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import test_text_ingestion as support

from newsrag.storage import StorageError, initialize_storage


def test_schema7_migration_is_lossless_and_idempotent(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "legacy.txt"
    source.write_text("Council approved drainage.\nLiteral retained evidence.\n")
    job = corpus.ingest(str(source))
    assert job.status == "done", job.error
    names = (
        "documents",
        "source_units",
        "chunks",
        "passages",
        "passages_fts",
        "processing_generations",
    )
    before = {name: corpus.rows(f"SELECT * FROM {name}") for name in names}
    with sqlite3.connect(corpus.paths.database) as connection:
        for name in ("table_values_fts", "table_passages", "table_cells", "source_tables"):
            connection.execute(f"DROP TABLE {name}")
        connection.execute("ALTER TABLE discovery_evidence DROP COLUMN table_region_json")
        connection.execute("ALTER TABLE discovery_evidence DROP COLUMN table_context_json")
        connection.execute("UPDATE metadata SET value = '7' WHERE key = 'schema_version'")
    for _ in range(2):
        initialize_storage(corpus.paths.data_dir)
        assert {name: corpus.rows(f"SELECT * FROM {name}") for name in names} == before
        assert corpus.rows("SELECT * FROM source_tables") == []
        assert corpus.rows("SELECT * FROM table_passages") == []
    assert support._keyword_results(corpus.paths.database, "drainage")[0].citation.endswith(
        "line 1"
    )


def test_migration_rejects_unowned_table_rows_without_fabrication(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "legacy.txt"
    source.write_text("Council approved drainage.")
    assert corpus.ingest(str(source)).status == "done"
    with sqlite3.connect(corpus.paths.database) as connection:
        connection.execute("UPDATE source_units SET location_type = 'table_row'")
    with pytest.raises(StorageError, match="cannot fabricate"):
        initialize_storage(corpus.paths.data_dir)
    assert corpus.rows("SELECT * FROM source_tables") == []

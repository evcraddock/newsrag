from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.storage import StorageError, StoragePaths, get_storage_status, initialize_storage


def legacy_corpus(tmp_path: Path, *, relative: bool = False) -> tuple[StoragePaths, Path]:
    paths = initialize_storage(tmp_path / "corpus")
    original = paths.source_artifacts / "original"
    original.write_bytes(b"original evidence")
    legacy = paths.data_dir / "ocr-pdfs" / "old" / "normalized.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"normalized evidence")
    reference = str(legacy.relative_to(paths.data_dir) if relative else legacy)
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "INSERT INTO documents(id, normalized_path, current_processing_generation_id) "
            "VALUES('document', ?, 'generation-new')",
            (reference,),
        )
        for generation in ("generation-old", "generation-new"):
            connection.execute(
                "INSERT INTO processing_generations(id, document_id, fingerprint, "
                "configuration_json, normalized_path) VALUES(?, 'document', 'legacy:unknown', '{}', ?)",
                (generation, reference),
            )
    return paths, legacy


def references(paths: StoragePaths) -> list[tuple[str, str]]:
    with sqlite3.connect(paths.database) as connection:
        return connection.execute(
            "SELECT id, normalized_path FROM processing_generations UNION ALL "
            "SELECT id, normalized_path FROM documents ORDER BY id"
        ).fetchall()


@pytest.mark.parametrize("relative", [False, True])
def test_transition_preserves_history_originals_and_legacy_packet_paths(
    tmp_path: Path, relative: bool
) -> None:
    paths, legacy = legacy_corpus(tmp_path, relative=relative)
    before = references(paths)
    report = get_storage_status(paths.data_dir)
    assert any(
        check.name == "derived_transition" and check.status == "warn" for check in report.checks
    )
    assert references(paths) == before  # status is read-only
    assert list(paths.derived_artifacts.iterdir()) == []

    initialize_storage(paths.data_dir)
    after = references(paths)
    destinations = dict(after)
    for generation in ("generation-old", "generation-new"):
        path = Path(destinations[generation])
        assert path == paths.derived_artifacts / generation / "normalized.pdf"
        assert path.read_bytes() == b"normalized evidence"
    assert destinations["document"] == destinations["generation-new"]
    assert legacy.read_bytes() == b"normalized evidence"  # saved packets still work
    assert (paths.source_artifacts / "original").read_bytes() == b"original evidence"
    initialize_storage(paths.data_dir)
    assert references(paths) == after
    assert get_storage_status(paths.data_dir).summary == "ok"


def test_transition_ignores_legacy_name_in_corpus_ancestor(tmp_path: Path) -> None:
    paths, legacy = legacy_corpus(tmp_path / "ocr-pdfs")
    initialize_storage(paths.data_dir)
    after = references(paths)
    initialize_storage(paths.data_dir)
    assert references(paths) == after
    assert legacy.read_bytes() == b"normalized evidence"
    assert get_storage_status(paths.data_dir).summary == "ok"


def test_transition_uses_external_absolute_legacy_path_without_deleting_it(tmp_path: Path) -> None:
    paths, legacy = legacy_corpus(tmp_path)
    external = tmp_path / "external" / "ocr-pdfs" / "flat.pdf"
    external.parent.mkdir(parents=True)
    legacy.rename(external)
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE documents SET normalized_path = ?", (str(external),))
        connection.execute(
            "UPDATE processing_generations SET normalized_path = ?", (str(external),)
        )
    initialize_storage(paths.data_dir)
    assert external.read_bytes() == b"normalized evidence"
    assert all(Path(value).read_bytes() == external.read_bytes() for _, value in references(paths))
    assert all(Path(value).name == "flat.pdf" for _, value in references(paths))


def test_transition_keeps_document_reference_to_its_original_generation(tmp_path: Path) -> None:
    paths, legacy = legacy_corpus(tmp_path)
    newer = legacy.with_name("newer.pdf")
    newer.write_bytes(b"newer normalized evidence")
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "UPDATE processing_generations SET normalized_path = ? WHERE id = 'generation-new'",
            (str(newer),),
        )
    initialize_storage(paths.data_dir)
    values = dict(references(paths))
    assert values["document"] == values["generation-old"]
    assert Path(values["generation-new"]).read_bytes() == b"newer normalized evidence"


@pytest.mark.parametrize("failure", ["missing", "conflict", "unsafe-generation"])
def test_transition_fails_closed_without_changing_references(tmp_path: Path, failure: str) -> None:
    paths, legacy = legacy_corpus(tmp_path)
    if failure == "missing":
        legacy.unlink()
    elif failure == "conflict":
        destination = paths.derived_artifacts / "generation-old" / "normalized.pdf"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"conflicting evidence")
    else:
        with sqlite3.connect(paths.database) as connection:
            connection.execute(
                "UPDATE processing_generations SET id = '../escape' WHERE id = 'generation-old'"
            )
    before = references(paths)
    with pytest.raises(StorageError, match="derived artifact transition"):
        initialize_storage(paths.data_dir)
    assert references(paths) == before
    report = get_storage_status(paths.data_dir)
    assert any(
        check.name == "derived_transition" and check.status == "error" for check in report.checks
    )
    if failure != "missing":
        assert legacy.read_bytes() == b"normalized evidence"
    if failure == "conflict":
        assert destination.read_bytes() == b"conflicting evidence"


@pytest.mark.parametrize("failure", ["copy", "database"])
def test_transition_recovers_after_copy_before_database_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from newsrag import derived_artifacts

    paths, legacy = legacy_corpus(tmp_path)
    before = references(paths)
    copy = derived_artifacts._copy_verified
    calls = 0

    def interrupted(source: Path, destination: Path) -> None:
        nonlocal calls
        copy(source, destination)
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption before commit")

    if failure == "database":
        with sqlite3.connect(paths.database) as connection:
            connection.execute(
                "CREATE TRIGGER interrupt_transition BEFORE UPDATE OF normalized_path ON documents "
                "BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END"
            )
    with monkeypatch.context() as patch:
        if failure == "copy":
            patch.setattr(derived_artifacts, "_copy_verified", interrupted)
        with pytest.raises(StorageError, match="simulated interruption"):
            initialize_storage(paths.data_dir)
    if failure == "database":
        with sqlite3.connect(paths.database) as connection:
            connection.execute("DROP TRIGGER interrupt_transition")
    assert references(paths) == before
    assert legacy.read_bytes() == b"normalized evidence"
    assert len(list(paths.derived_artifacts.glob("*/*.pdf"))) == 2
    initialize_storage(paths.data_dir)
    assert all(Path(value).read_bytes() == b"normalized evidence" for _, value in references(paths))
    assert get_storage_status(paths.data_dir).summary == "ok"


def test_transition_retries_partial_copy_without_publishing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import BinaryIO

    paths, legacy = legacy_corpus(tmp_path)
    before = references(paths)

    def partial_copy(source: BinaryIO, destination: BinaryIO) -> None:
        destination.write(source.read(3))
        raise OSError("simulated disk full")

    with monkeypatch.context() as patch:
        patch.setattr("newsrag.derived_artifacts.shutil.copyfileobj", partial_copy)
        with pytest.raises(StorageError, match="disk full"):
            initialize_storage(paths.data_dir)
    assert references(paths) == before
    assert not list(paths.derived_artifacts.glob("*/*.pdf"))
    assert legacy.read_bytes() == b"normalized evidence"
    # A hard-killed process can leave a temporary file; retries never adopt it.
    (paths.derived_artifacts / "generation-new" / ".transition-abandoned").write_bytes(b"bad")
    initialize_storage(paths.data_dir)
    assert all(Path(value).read_bytes() == b"normalized evidence" for _, value in references(paths))


def test_transition_recovers_moved_corpus_absolute_reference(tmp_path: Path) -> None:
    paths, _ = legacy_corpus(tmp_path)
    moved = tmp_path / "moved"
    paths.data_dir.rename(moved)
    migrated = initialize_storage(moved)
    assert all(
        Path(value).is_relative_to(moved / "artifacts" / "derived")
        for _, value in references(migrated)
    )
    assert all(
        Path(value).read_bytes() == b"normalized evidence" for _, value in references(migrated)
    )
    assert (moved / "ocr-pdfs" / "old" / "normalized.pdf").read_bytes() == b"normalized evidence"


def test_transition_rejects_ambiguous_relocated_copy(tmp_path: Path) -> None:
    paths, legacy = legacy_corpus(tmp_path)
    external = tmp_path / "other" / "ocr-pdfs" / "old" / "normalized.pdf"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"different corpus")
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "UPDATE processing_generations SET normalized_path = ?", (str(external),)
        )
        connection.execute("UPDATE documents SET normalized_path = ?", (str(external),))
    before = references(paths)
    with pytest.raises(StorageError, match="conflicting legacy copies"):
        initialize_storage(paths.data_dir)
    assert references(paths) == before
    assert legacy.read_bytes() == b"normalized evidence"
    assert external.read_bytes() == b"different corpus"


def test_transition_leaves_unreferenced_legacy_files_and_custom_paths_untouched(
    tmp_path: Path,
) -> None:
    paths, legacy = legacy_corpus(tmp_path)
    custom = tmp_path / "custom.pdf"
    custom.write_bytes(b"custom normalized evidence")
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE documents SET normalized_path = ?", (str(custom),))
        connection.execute("UPDATE processing_generations SET normalized_path = ?", (str(custom),))
    before = references(paths)
    initialize_storage(paths.data_dir)
    assert references(paths) == before
    assert legacy.read_bytes() == b"normalized evidence"
    assert custom.read_bytes() == b"custom normalized evidence"
    assert get_storage_status(paths.data_dir).summary == "ok"


def test_status_initialize_reports_missing_legacy_artifact_and_can_retry(tmp_path: Path) -> None:
    paths, legacy = legacy_corpus(tmp_path)
    legacy.unlink()
    runner = CliRunner()
    failed = runner.invoke(app, ["--data-dir", str(paths.data_dir), "status", "--initialize"])
    assert failed.exit_code == 1
    assert "derived_transition: error" in failed.stdout
    assert "restore" in failed.stdout.lower()
    legacy.write_bytes(b"normalized evidence")
    recovered = runner.invoke(app, ["--data-dir", str(paths.data_dir), "status", "--initialize"])
    assert recovered.exit_code == 0
    assert "summary: ok" in recovered.stdout

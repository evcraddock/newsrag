"""Copy legacy normalized artifacts before reconciling their database references."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path


class DerivedArtifactError(Exception):
    """Raised when a legacy artifact cannot be relocated without guessing or data loss."""


@dataclass(frozen=True)
class _Relocation:
    table: str
    row_id: str
    source: Path
    destination: Path


def transition_derived_artifacts(database: Path, data_dir: Path, derived: Path) -> None:
    """Reconcile all recognized legacy references atomically, retaining every old copy.

    Copies become durable before SQLite commits. An interrupted attempt can leave
    extra verified copies, but never references a partial file or deletes its source.
    Stop old-version workers before upgrading; they could publish new legacy paths.
    """
    try:
        with sqlite3.connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = _migration_plan(connection, data_dir, derived)
            for item in plan:
                _copy_verified(item.source, item.destination)
                # Table names come only from the fixed internal plan builder.
                connection.execute(
                    f"UPDATE {item.table} SET normalized_path = ? WHERE id = ?",
                    (str(item.destination), item.row_id),
                )
            if plan:
                for directory in (derived, derived.parent, data_dir):
                    _fsync_directory(directory)
    except (OSError, sqlite3.Error) as exc:
        raise DerivedArtifactError(f"Artifact copy/reference update failed: {exc}") from exc


def inspect_derived_transition(database: Path, data_dir: Path, derived: Path) -> tuple[str, str]:
    """Report transition readiness without creating directories, files, or database rows."""
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
            if "current_processing_generation_id" not in columns:
                return "warn", "initialize storage to upgrade the schema before artifact transition"
            plan = _migration_plan(connection, data_dir, derived)
            for item in plan:
                _validate_copy(item.source, item.destination)
        if plan:
            return (
                "warn",
                f"{len(plan)} legacy artifact reference(s) pending; stop old daemons, back up the "
                "corpus, then run status --initialize; legacy copies will be retained",
            )
        return "ok", "no pending legacy references; any remaining ocr-pdfs files are retained"
    except (DerivedArtifactError, OSError, sqlite3.Error) as exc:
        return "error", f"Cannot complete derived artifact transition: {exc}"


def _legacy_suffix(reference: str, data_dir: Path) -> Path | None:
    path = Path(reference)
    # A corpus (or one of its ancestors) can itself be named ocr-pdfs. Only
    # the legacy top-level child is relevant for paths inside the current corpus.
    if path.absolute().is_relative_to(data_dir.absolute()):
        path = path.absolute().relative_to(data_dir.absolute())
        if not path.parts or path.parts[0] != "ocr-pdfs":
            return None
    parts = path.parts
    if "ocr-pdfs" not in parts:
        return None
    if parts.count("ocr-pdfs") != 1 or ".." in parts:
        raise DerivedArtifactError(
            f"Unsafe legacy artifact path: {reference}; restore a valid reference"
        )
    suffix = Path(*parts[parts.index("ocr-pdfs") + 1 :])
    if suffix == Path("."):
        raise DerivedArtifactError(f"Legacy artifact path names a directory: {reference}")
    return suffix


def _legacy_source(reference: str, data_dir: Path, suffix: Path) -> Path:
    recorded = Path(reference)
    local = data_dir / "ocr-pdfs" / suffix
    candidates = [local]
    if recorded.is_absolute() and recorded != local:
        candidates.append(recorded)
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        raise DerivedArtifactError(
            f"Missing legacy artifact {reference}; restore it from backup at {local} "
            "or its recorded absolute path, then rerun status --initialize"
        )
    source = existing[0]
    if any(_digest(path) != _digest(source) for path in existing[1:]):
        raise DerivedArtifactError(
            f"Found conflicting legacy copies for {reference} and {local}; "
            "restore the correct corpus copy before retrying"
        )
    return source


def _destination(derived: Path, generation: str, filename: str) -> Path:
    if not generation or generation in {".", ".."} or Path(generation).name != generation:
        raise DerivedArtifactError(f"Unsafe processing generation ID: {generation}")
    destination = derived / generation / filename
    if not destination.resolve().is_relative_to(derived.resolve()):
        raise DerivedArtifactError(f"Derived artifact destination escapes storage: {destination}")
    return destination.absolute()


def _migration_plan(
    connection: sqlite3.Connection, data_dir: Path, derived: Path
) -> list[_Relocation]:
    plan: list[_Relocation] = []
    generations: dict[str, list[_Relocation]] = {}
    for generation, document, reference in connection.execute(
        "SELECT id, document_id, normalized_path FROM processing_generations "
        "WHERE normalized_path IS NOT NULL ORDER BY id"
    ):
        suffix = _legacy_suffix(reference, data_dir)
        if suffix is None:
            continue
        item = _Relocation(
            "processing_generations",
            generation,
            _legacy_source(reference, data_dir, suffix),
            _destination(derived, generation, suffix.name),
        )
        plan.append(item)
        generations.setdefault(document, []).append(item)
    for document, reference, current in connection.execute(
        "SELECT id, normalized_path, current_processing_generation_id FROM documents "
        "WHERE normalized_path IS NOT NULL ORDER BY id"
    ):
        suffix = _legacy_suffix(reference, data_dir)
        if suffix is None:
            continue
        source = _legacy_source(reference, data_dir, suffix)
        matches = [
            item
            for item in generations.get(document, [])
            if item.source.resolve() == source.resolve()
        ]
        if not matches:
            raise DerivedArtifactError(
                f"Legacy document {document} has no matching generation for {reference}; "
                "restore consistent document/generation references before retrying"
            )
        # documents.normalized_path may describe the initial, not active generation.
        selected = next((item for item in matches if item.row_id == current), matches[0])
        plan.append(_Relocation("documents", document, source, selected.destination))
    return plan


def _digest(path: Path) -> bytes:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").digest()


def _validate_copy(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise DerivedArtifactError(f"Refusing symlink artifact destination: {destination}")
    if destination.exists() and (
        not destination.is_file() or _digest(source) != _digest(destination)
    ):
        raise DerivedArtifactError(
            f"Conflicting derived artifact at {destination}; preserve both copies and "
            "restore the correct artifact before rerunning status --initialize"
        )


def _copy_verified(source: Path, destination: Path) -> None:
    _validate_copy(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        # A private temporary file cannot be mistaken for a completed artifact on retry.
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".transition-") as output:
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
            temporary = Path(output.name)
            if _digest(source) != _digest(temporary):
                raise DerivedArtifactError(
                    f"Artifact changed while copying {source}; retry with workers stopped"
                )
            try:
                os.link(temporary, destination)  # atomic publication without overwriting a copy
            except FileExistsError:
                _validate_copy(source, destination)
    # Also sync a verified destination left by an interrupted prior attempt.
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(destination.parent)
    _fsync_directory(destination.parent.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

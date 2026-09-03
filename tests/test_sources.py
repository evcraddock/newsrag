from __future__ import annotations

from pathlib import Path

from newsrag.sources import (
    SOURCE_KIND_LOCAL_PATH,
    SOURCE_KIND_URL,
    artifact_id_for_hash,
    build_source_identity,
    normalize_url_reference,
    source_unit_id_for_page,
)


def test_normalize_url_reference_uses_conservative_identity_rules() -> None:
    assert (
        normalize_url_reference("HTTPS://Example.COM:443/Reports/Packet/?view=full#page-2")
        == "https://example.com/Reports/Packet/?view=full"
    )
    assert normalize_url_reference("http://Example.COM:8080/a") == "http://example.com:8080/a"


def test_build_source_identity_distinguishes_urls_and_local_paths(tmp_path: Path) -> None:
    local_path = tmp_path / "Packet.PDF"
    local_identity = build_source_identity(source_path=local_path, source_url=None)
    url_identity = build_source_identity(
        source_path=local_path,
        source_url="https://example.test/Packet.PDF",
    )

    assert local_identity.kind == SOURCE_KIND_LOCAL_PATH
    assert local_identity.submitted_reference == str(local_path)
    assert local_identity.normalized_reference == str(local_path.resolve())
    assert url_identity.kind == SOURCE_KIND_URL
    assert url_identity.submitted_reference == "https://example.test/Packet.PDF"
    assert url_identity.normalized_reference == "https://example.test/Packet.PDF"
    assert local_identity.id != url_identity.id


def test_derived_artifact_and_source_unit_ids_are_stable() -> None:
    assert artifact_id_for_hash("hash-a") == artifact_id_for_hash("hash-a")
    assert artifact_id_for_hash("hash-a") != artifact_id_for_hash("hash-b")
    assert source_unit_id_for_page("page-1") == source_unit_id_for_page("page-1")
    assert source_unit_id_for_page("page-1") != source_unit_id_for_page("page-2")

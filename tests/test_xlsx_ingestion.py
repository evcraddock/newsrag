from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest
import test_text_ingestion as support
from typer.testing import CliRunner
from xlsx_fixtures import REL_NS, XLSX_NS, make_xlsx

from newsrag.acquisition import SafeSourceArtifactAcquirer
from newsrag.cli import app
from newsrag.documents import (
    DocumentFilters,
    format_document_detail,
    get_document_detail,
    list_document_summaries,
)
from newsrag.ingest import IngestError, enqueue_ingest_source, prepare_ingest_source
from newsrag.manifests import ManifestError, load_manifest
from newsrag.search import SearchFilters
from newsrag.sources import XLSX_MAX_SOURCE_BYTES, XLSX_MEDIA_TYPE


def _workbook(path: Path, marker: str = "Roads", *, hidden: bool = False) -> Path:
    rows = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Amount</t></is></c></row>'
        f'<row r="2"><c r="A2" t="inlineStr"><is><t>{escape(marker)}</t></is></c>'
        '<c r="B2"><v>1200</v></c></row>'
        f'<row r="3" hidden="{int(hidden)}"><c r="A3" t="inlineStr"><is>'
        '<t>Secretvalue</t></is></c><c r="B3"><v>500</v></c></row>'
    )
    sheets = {
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{XLSX_NS}"><sheetData>{rows}</sheetData></worksheet>'
        )
    }
    workbook = None
    if hidden:
        sheets["xl/worksheets/sheet2.xml"] = (
            f'<worksheet xmlns="{XLSX_NS}"><sheetData><row r="4">'
            '<c r="B4" t="inlineStr"><is><t>Secretworksheet</t></is></c>'
            "</row></sheetData></worksheet>"
        )
        workbook = (
            f'<workbook xmlns="{XLSX_NS}" xmlns:r="{REL_NS}"><sheets>'
            '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
            '<sheet name="Hidden" sheetId="2" r:id="rId2" state="veryHidden"/>'
            "</sheets></workbook>"
        )
    return make_xlsx(path, workbook=workbook, sheets=sheets)


def test_local_workbook_keeps_hidden_row_anchors_and_inventory(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = _workbook(tmp_path / "budget.XLSX", hidden=True)
    job = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database,
            source=str(source),
            xlsx_options={"header_rows": {"Sheet1": 1}},
        ).jobs[0]
    )
    assert job.status == "done", job.error
    assert job.result is not None
    document_id = str(job.result["document_id"])
    rows = corpus.rows("SELECT ordinal, normalized_text FROM source_units ORDER BY ordinal")
    assert [row[0] for row in rows] == [1, 2, 3, 4]
    assert rows[2][1] == rows[3][1] == ""
    assert "Secret" not in "".join(row[1] for row in rows)
    assert "Secretvalue" in str(corpus.rows("SELECT cell_json FROM table_cells"))
    assert support._keyword_results(corpus.paths.database, "Secretvalue") == []
    assert support._keyword_results(corpus.paths.database, "Secretworksheet") == []
    assert support._keyword_results(corpus.paths.database, "Roads")[0].source_type == "xlsx"
    detail = get_document_detail(corpus.paths.database, document_id)
    rendered = format_document_detail(detail)
    assert detail.extent_label == "rows" and detail.extent_count == 4
    assert "sheets: 2" in rendered and "tables: 2" in rendered
    assert "excluded_hidden_sheets: 1" in rendered
    assert "excluded_hidden_rows: 1" in rendered
    assert "A1:B3" in rendered and "B4:B4" in rendered
    assert "Secret" not in rendered


@pytest.mark.parametrize(
    "media,hint,filename",
    [
        (XLSX_MEDIA_TYPE, None, "export"),
        ("application/zip", "xlsx", "export"),
        ("application/zip", None, "budget.XLSX"),
        ("application/octet-stream", "xlsx", "export"),
        ("application/octet-stream", None, "budget.XlSx"),
    ],
)
def test_public_workbook_selection(
    tmp_path: Path, media: str, hint: str | None, filename: str
) -> None:
    content = _workbook(tmp_path / "fixture.xlsx").read_bytes()
    response = support.HttpResponse(200, {"content-type": media}, (content,))
    transport = support.HttpTransport([response])
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(resolver=support._public_resolver, transport=transport),
    )
    job = corpus.ingest(f"https://example.gov/{filename}", source_type=hint)
    assert job.status == "done", job.error
    assert corpus.rows("SELECT media_type, reported_media_type FROM source_artifacts") == [
        (XLSX_MEDIA_TYPE, media)
    ]
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "media,hint,filename",
    [
        ("application/zip", None, "export"),
        ("application/octet-stream", None, "export"),
        ("application/vnd.ms-excel.sheet.macroenabled.12", None, "budget.xlsx"),
        ("application/vnd.ms-excel", "xlsx", "budget.xlsx"),
        ("text/plain", "xlsx", "budget.xlsx"),
        *[
            (XLSX_MEDIA_TYPE, "xlsx", f"budget.{ext}")
            for ext in ("xlsm", "xlsb", "xls", "xlt", "xltx", "xltm", "docx", "pptx")
        ],
    ],
)
def test_zip_magic_and_contradictory_office_evidence_rejected(
    tmp_path: Path, media: str, hint: str | None, filename: str
) -> None:
    content = _workbook(tmp_path / "fixture.xlsx").read_bytes()
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=support._public_resolver,
            transport=support.HttpTransport(
                [support.HttpResponse(200, {"content-type": media}, (content,))]
            ),
        ),
    )
    job = corpus.ingest(f"https://example.gov/{filename}", source_type=hint)
    assert job.status == "failed", job.result
    assert corpus.rows("SELECT * FROM documents") == []


@pytest.mark.parametrize(
    "options",
    [
        {"header_rows": {"Sheet1": True}},
        {"header_rows": {"Sheet1": 0}},
        {"header_rows": {"Sheet1": -1}},
        {"header_rows": {"Sheet1": 1.5}},
        {"header_rows": {"Sheet1": "1"}},
        {"header_rows": []},
        {"unknown": {}},
    ],
)
def test_invalid_header_maps_fail_before_enqueue(
    tmp_path: Path, options: dict[str, object]
) -> None:
    with pytest.raises(IngestError):
        prepare_ingest_source(source=str(tmp_path / "budget.xlsx"), xlsx_options=options)


def test_recipe_conflicts_and_directory_rejection(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="directory"):
        prepare_ingest_source(source=str(tmp_path), xlsx_options={"header_rows": {}})
    conflicts: tuple[dict[str, Any], ...] = (
        {"source_type": "csv"},
        {"csv_options": {}},
        {"pdf_extractor": "auto"},
    )
    for extra in conflicts:
        with pytest.raises(IngestError, match="conflict"):
            prepare_ingest_source(source="budget.xlsx", xlsx_options={}, **extra)
    batch = prepare_ingest_source(source="export", xlsx_options={"header_rows": {}})
    assert batch.payloads[0]["source_type"] == "xlsx"
    assert batch.payloads[0]["xlsx"] == {"header_rows": {}}


def test_cli_and_manifest_options_are_atomic_and_mix_sources(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    _workbook(source_dir / "budget.XLSX")
    (source_dir / "notes.txt").write_text("Roads meeting notes")
    runner = CliRunner()
    base = [
        "--config-path",
        str(tmp_path / "missing.yaml"),
        "--data-dir",
        str(corpus.paths.data_dir),
    ]
    result = runner.invoke(app, [*base, "ingest", str(source_dir)])
    assert result.exit_code == 0, result.stdout
    assert all(job.status == "done" for job in corpus.drain())
    assert {
        item.source_type for item in support._keyword_results(corpus.paths.database, "Roads")
    } == {"xlsx", "text"}
    assert (
        len(
            support._keyword_results(
                corpus.paths.database, "Roads", filters=SearchFilters(source_type="xlsx")
            )
        )
        == 1
    )
    assert (
        list_document_summaries(
            corpus.paths.database, filters=DocumentFilters(source_type="xlsx")
        ).total
        == 1
    )
    count = len(corpus.rows("SELECT id FROM jobs"))
    for value in ("[]", '{"Sheet1":true}', '{"Sheet1":0}', '{"Sheet1":1,"Sheet1":2}', "invalid"):
        result = runner.invoke(app, [*base, "ingest", "budget.xlsx", "--xlsx-header-rows", value])
        assert result.exit_code == 1, result.stdout
        assert len(corpus.rows("SELECT id FROM jobs")) == count
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n  - source: ./sources/budget.XLSX\n    xlsx:\n      header_rows:\n        Sheet1: 1\n  - source: ./other.xlsx\n    xlsx:\n      unknown: true\n"
    )
    with pytest.raises(ManifestError):
        load_manifest(manifest)
    result = runner.invoke(app, [*base, "ingest-manifest", str(manifest)])
    assert result.exit_code == 1 and len(corpus.rows("SELECT id FROM jobs")) == count
    manifest.write_text(
        "documents:\n  - source: ./sources/budget.XLSX\n    xlsx:\n      header_rows:\n        Sheet1: 1\n"
    )
    parsed = load_manifest(manifest).documents[0]
    assert parsed.source_type == "xlsx" and parsed.xlsx_options == {"header_rows": {"Sheet1": 1}}
    result = runner.invoke(app, [*base, "ingest-manifest", str(manifest)])
    assert result.exit_code == 0, result.stdout
    jobs = corpus.drain()
    assert all(job.status == "done" for job in jobs)


@pytest.mark.parametrize(
    "hint,filename,media",
    [
        ("xlsx", "export", "application/zip"),
        (None, "budget.XLSX", "application/octet-stream"),
        (None, "export", XLSX_MEDIA_TYPE),
    ],
)
def test_acquisition_enforces_xlsx_raw_limit(
    tmp_path: Path, hint: str | None, filename: str, media: str
) -> None:
    response = support.HttpResponse(
        200,
        {
            "content-type": media,
            "content-length": str(XLSX_MAX_SOURCE_BYTES + 1),
        },
        (b"not read",),
    )
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=support._public_resolver,
            transport=support.HttpTransport([response]),
        ),
    )
    job = corpus.ingest(f"https://example.gov/{filename}", source_type=hint)
    assert job.status == "failed" and str(XLSX_MAX_SOURCE_BYTES) in str(job.error)
    assert corpus.rows("SELECT * FROM source_artifacts") == []
    assert response.closed


def test_ingest_url_alias_captures_header_recipe(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    result = CliRunner().invoke(
        app,
        [
            "--config-path",
            str(tmp_path / "missing.yaml"),
            "--data-dir",
            str(corpus.paths.data_dir),
            "ingest-url",
            "https://example.gov/export",
            "--xlsx-header-rows",
            '{"Sheet1":1}',
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(corpus.rows("SELECT payload_json FROM jobs")[0][0])
    assert payload["xlsx"] == {"header_rows": {"Sheet1": 1}}
    assert payload["source_type"] == "xlsx"


@pytest.mark.parametrize(
    "recipe",
    [
        "    type: csv\n    xlsx: {}\n",
        "    csv: {}\n    xlsx: {}\n",
        "    xlsx:\n      header_rows:\n        Sheet1: true\n",
        "    xlsx:\n      header_rows:\n        Sheet1: 1\n        Sheet1: 2\n",
        "    xlsx: {}\n    xlsx: {header_rows: {Sheet1: 1}}\n",
    ],
)
def test_manifest_recipe_conflicts_reject_before_enqueue(tmp_path: Path, recipe: str) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("documents:\n  - source: budget.xlsx\n" + recipe)
    with pytest.raises(ManifestError):
        load_manifest(manifest)


def test_local_xlsx_raw_limit_is_enforced_before_registration(tmp_path: Path) -> None:
    source = tmp_path / "large.XLSX"
    with source.open("wb") as stream:
        stream.truncate(XLSX_MAX_SOURCE_BYTES + 1)
    corpus = support._corpus(tmp_path / "corpus")
    failed = corpus.ingest(str(source))
    assert failed.status == "failed" and str(XLSX_MAX_SOURCE_BYTES) in str(failed.error)
    assert corpus.rows("SELECT * FROM source_artifacts") == []


def test_sheet_inventory_keeps_numeric_workbook_order_and_empty_extents(tmp_path: Path) -> None:
    sheets = {
        f"xl/worksheets/sheet{index}.xml": (
            f'<worksheet xmlns="{XLSX_NS}"><sheetData>'
            + (
                f'<row r="1"><c r="A1" t="inlineStr"><is><t>Roads {index}</t></is></c></row>'
                if index != 2
                else ""
            )
            + "</sheetData></worksheet>"
        )
        for index in range(1, 12)
    }
    source = make_xlsx(tmp_path / "sheets.xlsx", sheets=sheets)
    corpus = support._corpus(tmp_path / "corpus")
    job = corpus.ingest(str(source))
    assert job.status == "done" and job.result is not None, job.error
    detail = get_document_detail(corpus.paths.database, str(job.result["document_id"]))
    assert [table["sheet_index"] for table in detail.table_descriptors] == list(range(1, 12))
    assert detail.table_descriptors[1]["row_start"] is None
    assert 'table 2 sheet "Sheet2": empty' in format_document_detail(detail)

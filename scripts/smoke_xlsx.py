"""Run XLSX CLI/daemon checks in disposable storage using make dev and local embeddings."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"
SECRET = "HiddenValueMustNeverBecomeEvidence"


def workbook(path: Path, *, amount: str = "1200", label: str = "Roads") -> None:
    """Write a small real workbook with hidden values, original offsets, caches, and a merge."""
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{C}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in (1, 2)
        )
        + "</Types>",
        "_rels/.rels": f'<Relationships xmlns="{P}"><Relationship Id="main" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Hidden" sheetId="1" state="hidden" r:id="one"/><sheet name="FY 2026" sheetId="2" r:id="two"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{P}"><Relationship Id="one" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="two" Type="{R}/worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{SECRET}</t></is></c></row></sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{S}"><cols><col min="5" max="5" hidden="1"/></cols><sheetData><row r="4"><c r="B4" t="inlineStr"><is><t>Department</t></is></c><c r="C4" t="inlineStr"><is><t>Amount</t></is></c></row><row r="5"><c r="B5" t="inlineStr"><is><t>{label}</t></is></c><c r="C5"><f>SUM(C1:C3)</f><v>{amount}</v></c><c r="D5"><f>1+2</f></c><c r="E5" t="inlineStr"><is><t>{SECRET}</t></is></c></row><row r="6" hidden="1"><c r="B6" t="inlineStr"><is><t>{SECRET}</t></is></c></row><row r="7"><c r="B7" t="inlineStr"><is><t>Merged heading</t></is></c></row></sheetData><mergeCells count="1"><mergeCell ref="B7:C7"/></mergeCells></worksheet>',
    }
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


class EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = request["input"]
        if isinstance(inputs, str):
            inputs = [inputs]
        if any(SECRET in text or "SUM(C1:C3)" in text for text in inputs):
            self.send_error(400, "Excluded content reached the embedding API")
            return
        body = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": [1.0, float("Roads" in text), float("Library" in text)],
                    }
                    for index, text in enumerate(inputs)
                ],
                "model": "xlsx-smoke",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # Do not log embedding request contents.


def smoke(scratch: Path, port: int) -> None:
    repo = Path(__file__).resolve().parents[1]
    data = scratch / "corpus"
    config = scratch / "config.yaml"
    config.write_text(
        f"embedding:\n  provider: openai_compatible\n  base_url: http://127.0.0.1:{port}/v1\n  model: xlsx-smoke\n"
    )
    base = [sys.executable, "-m", "newsrag", "--config-path", str(config), "--data-dir", str(data)]
    env = {**os.environ, "PYTHONPATH": str(repo), "NEWSRAG_LOG_LEVEL": "WARNING"}
    (scratch / "Procfile.dev").write_text("newsrag: " + shlex.join([*base, "daemon", "run"]) + "\n")
    database = data / "newsrag.sqlite3"

    def run(*arguments: str, success: bool = True) -> str:
        result = subprocess.run(
            [*base, *arguments], cwd=scratch, env=env, capture_output=True, text=True, timeout=30
        )
        if (result.returncode == 0) != success:
            raise RuntimeError(
                f"CLI {arguments} failed expectation: {result.stdout}\n{result.stderr}"
            )
        return result.stdout

    def rows(sql: str, args: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(database) as connection:
            return connection.execute(sql, args).fetchall()

    def wait_jobs(*, allow_failure: bool = False) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            jobs = rows("SELECT status, error FROM jobs")
            if jobs and all(job[0] in {"done", "failed"} for job in jobs):
                if not allow_failure and any(job[0] == "failed" for job in jobs):
                    raise RuntimeError(f"Disposable daemon job failed: {jobs}")
                return
            time.sleep(0.1)
        raise RuntimeError("Disposable daemon did not finish its jobs within 30 seconds")

    run("status", "--initialize")
    started = False
    try:
        subprocess.run(
            ["make", "-f", str(repo / "Makefile"), "dev"],
            cwd=scratch,
            env=env,
            check=True,
            timeout=30,
        )
        started = True
        sources = scratch / "sources"
        sources.mkdir()
        source = sources / "budget.XLSX"
        workbook(source)
        run("ingest", str(source), "--xlsx-header-rows", '{"FY 2026":4}')
        wait_jobs()
        document_id, source_id = rows(
            "SELECT d.id, a.source_id FROM documents d JOIN source_artifacts a ON a.id=d.artifact_id"
        )[0]
        assert rows("SELECT COUNT(*) FROM source_tables")[0][0] == 2
        assert rows("SELECT COUNT(*) FROM table_cells")[0][0] == 17
        assert (
            rows("SELECT COUNT(*) FROM table_cells WHERE cell_json LIKE ?", (f"%{SECRET}%",))[0][0]
            == 3
        )
        assert (
            rows(
                "SELECT COUNT(*) FROM source_units WHERE normalized_text LIKE ?", (f"%{SECRET}%",)
            )[0][0]
            == 0
        )
        result = run("search", "Roads", "--source-type", "xlsx")
        assert "cells B5:D5" in result and "sheet 2" in result
        assert "excluded_hidden_sheets: 1" in run("documents", "show", str(document_id))
        packet = scratch / "packet.md"
        run("packet", "Roads", "--source-type", "xlsx", "--out", str(packet))
        old_packet = packet.read_bytes()
        assert b"1200" in old_packet and b"freshness not verified" in old_packet
        assert SECRET.encode() not in old_packet and b"SUM(C1:C3)" not in old_packet
        (sources / "notes.txt").write_text("Roads public meeting notes.\n")
        run("ingest", str(sources))
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 2
        source.unlink()
        run("reprocess", str(document_id), "--xlsx-header-rows", "{}")
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM source_tables")[0][0] == 4
        assert old_packet == packet.read_bytes()
        workbook(source, amount="1300")
        run("refresh", str(source_id))
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 3
        descriptor = json.loads(
            rows("SELECT descriptor_json FROM source_tables ORDER BY rowid DESC LIMIT 1")[0][0]
        )
        assert descriptor["header_row"] is None
        assert descriptor["metadata"]["requested_recipe"] == {"header_rows": {}}
        extensionless = scratch / "export"
        workbook(extensionless, amount="500", label="Library")
        manifest = scratch / "manifest.yaml"
        manifest.write_text(
            "documents:\n  - source: ./export\n    xlsx:\n      header_rows:\n        FY 2026: 4\n"
        )
        run("ingest-manifest", str(manifest))
        wait_jobs()
        assert "cells B5:D5" in run("search", "Library", "--source-type", "xlsx")
        count = rows("SELECT COUNT(*) FROM jobs")[0][0]
        run("ingest", str(sources), "--xlsx-header-rows", "{}", success=False)
        assert rows("SELECT COUNT(*) FROM jobs")[0][0] == count
        bad = scratch / "bad.xlsx"
        bad.write_bytes(b"not a workbook")
        run("ingest", str(bad))
        wait_jobs(allow_failure=True)
        assert rows("SELECT COUNT(*) FROM jobs WHERE status='failed'")[0][0] == 1
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 4
        assert old_packet == packet.read_bytes()
        print(
            "XLSX smoke passed: hidden-value exclusion, caches, typed citations, directory/manifest ingestion, duplicates, no-refetch reprocessing, refresh recipes, and atomic failures."
        )
    finally:
        socket = scratch / ".overmind.sock"
        if started or socket.exists():
            subprocess.run(
                ["overmind", "quit", "-s", str(socket)],
                cwd=scratch,
                env=env,
                check=True,
                timeout=30,
            )


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="newsrag-xlsx-smoke-") as directory:
            smoke(Path(directory), server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

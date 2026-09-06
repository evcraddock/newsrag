"""Exercise the real CLI/daemon in disposable storage with a local mock embedding API.

Run with: uv run python scripts/smoke_csv.py
Requires the existing development prerequisites (make, overmind, tmux).
"""

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


class EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = request["input"]
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": [1.0, float("Roads" in text), float("Parks" in text)],
            }
            for index, text in enumerate(inputs)
        ]
        body = json.dumps({"object": "list", "data": data, "model": "csv-smoke"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # Keep deterministic smoke output free of request contents.


def smoke(scratch: Path, port: int) -> None:
    repo = Path(__file__).resolve().parents[1]
    data = scratch / "corpus"
    config = scratch / "config.yaml"
    config.write_text(
        f"embedding:\n  provider: openai_compatible\n  base_url: http://127.0.0.1:{port}/v1\n  model: csv-smoke\n"
    )
    base = [sys.executable, "-m", "newsrag", "--config-path", str(config), "--data-dir", str(data)]
    env = {**os.environ, "PYTHONPATH": str(repo), "NEWSRAG_LOG_LEVEL": "WARNING"}
    (scratch / "Procfile.dev").write_text("newsrag: " + shlex.join([*base, "daemon", "run"]) + "\n")
    database = data / "newsrag.sqlite3"

    def run(*arguments: str, success: bool = True) -> str:
        completed = subprocess.run(
            [*base, *arguments], cwd=scratch, env=env, capture_output=True, text=True, timeout=30
        )
        if (completed.returncode == 0) != success:
            raise RuntimeError(
                f"CLI {arguments} failed expectation: {completed.stdout}\n{completed.stderr}"
            )
        return completed.stdout

    def rows(sql: str, args: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(database) as connection:
            return connection.execute(sql, args).fetchall()

    def wait_jobs(*, allow_failure: bool = False) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            jobs = rows("SELECT status, error FROM jobs")
            if jobs and all(row[0] in {"done", "failed"} for row in jobs):
                if not allow_failure and any(row[0] == "failed" for row in jobs):
                    raise RuntimeError(f"Daemon job failed: {jobs}")
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
        nested = sources / "nested"
        nested.mkdir(parents=True)
        csv = nested / "expenses.CSV"
        csv.write_text('Department,Amount,Note\nRoads,001200,"Paving\nphase two"\nParks,,"=1+1"\n')
        (sources / "notes.txt").write_text("Roads public meeting notes.\n")
        run("ingest", str(sources))
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 2
        document_id, source_id = rows(
            "SELECT d.id, a.source_id FROM documents d JOIN source_artifacts a ON a.id=d.artifact_id WHERE a.media_type='text/csv'"
        )[0]
        assert rows("SELECT COUNT(*) FROM table_cells")[0][0] == 9
        assert "cells A2:C2" in run("search", "Roads", "--source-type", "csv")
        assert "tables: 1" in run("documents", "show", str(document_id))
        packet = scratch / "packet.md"
        run("packet", "Roads", "--source-type", "csv", "--out", str(packet))
        old_packet = packet.read_bytes()
        assert b"001200" in old_packet and b"phase two" in old_packet
        run("ingest", str(csv), "--csv-header", "absent")
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 2
        saved = csv.read_bytes()
        csv.unlink()
        run("reprocess", str(document_id), "--csv-header", "absent")
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM source_tables")[0][0] == 2
        assert packet.read_bytes() == old_packet
        csv.write_bytes(saved.replace(b"001200", b"001300"))
        run("refresh", str(source_id))
        wait_jobs()
        descriptor = json.loads(
            rows("SELECT descriptor_json FROM source_tables ORDER BY rowid DESC LIMIT 1")[0][0]
        )
        assert descriptor["header_row"] is None
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 3
        export = scratch / "export"
        export.write_text("Library;0005\n")
        manifest = scratch / "manifest.yaml"
        manifest.write_text(
            "documents:\n  - source: ./export\n    csv:\n      delimiter: semicolon\n      header: absent\n"
        )
        run("ingest-manifest", str(manifest))
        wait_jobs()
        assert "cells A1:B1" in run("search", "Library", "--source-type", "csv")
        count = rows("SELECT COUNT(*) FROM jobs")[0][0]
        run("ingest", str(sources), "--csv-header", "absent", success=False)
        assert rows("SELECT COUNT(*) FROM jobs")[0][0] == count
        bad = scratch / "malformed.csv"
        bad.write_text('Name\n"unfinished')
        run("ingest", str(bad))
        wait_jobs(allow_failure=True)
        assert rows("SELECT COUNT(*) FROM jobs WHERE status='failed'")[0][0] == 1
        assert rows("SELECT COUNT(*) FROM documents")[0][0] == 4
        run("status", "--initialize")
        print(
            "CSV smoke passed: directory/manifest ingestion, typed search/inventory/packets, duplicate, no-refetch reprocessing, refresh recipe inheritance, malformed input, and atomic flags."
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
        with tempfile.TemporaryDirectory(prefix="newsrag-csv-smoke-") as directory:
            smoke(Path(directory), server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

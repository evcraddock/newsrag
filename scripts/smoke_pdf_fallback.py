"""Exercise OCR exit-4 fallback through the real CLI/daemon in disposable storage.

Run: uv run python scripts/smoke_pdf_fallback.py [--source-pdf /path/to/reproduction.pdf]
Only the private daemon's OCR executable is replaced; both PDF extractors are real.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import fitz  # type: ignore[import-untyped]
from smoke_csv import EmbeddingHandler


def make_pdf(path: Path, text: str) -> None:
    with fitz.open() as document:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
        document.save(path)


def smoke(scratch: Path, port: int, source_pdf: Path | None) -> None:
    repo = Path(__file__).resolve().parents[1]
    data = scratch / "corpus"
    config = scratch / "config.yaml"
    config.write_text(
        f"embedding:\n  provider: openai_compatible\n  base_url: http://127.0.0.1:{port}/v1\n  model: csv-smoke\n"
    )
    code_file = scratch / "ocr-exit-code"
    code_file.write_text("4")
    binary_dir = scratch / "bin"
    binary_dir.mkdir()
    shim = binary_dir / "ocrmypdf"
    shim.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "if '--version' in sys.argv:\n    print('disposable exit-code smoke runner')\n    sys.exit(0)\n"
        "Path(sys.argv[-1]).write_bytes(b'PARTIAL OCR OUTPUT MUST NOT BE INDEXED')\n"
        "sys.stderr.write('simulated OCR validation failure\\n')\n"
        f"sys.exit(int(Path({str(code_file)!r}).read_text()))\n"
    )
    shim.chmod(0o700)
    env = {
        **os.environ,
        "PYTHONPATH": str(repo),
        "NEWSRAG_LOG_LEVEL": "WARNING",
        "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
    }
    base = [sys.executable, "-m", "newsrag", "--config-path", str(config), "--data-dir", str(data)]
    # Set PATH in the process command too: tmux's login shell may reset inherited PATH.
    (scratch / "Procfile.dev").write_text(
        "newsrag: " + shlex.join(["env", f"PATH={env['PATH']}", *base, "daemon", "run"]) + "\n"
    )
    database = data / "newsrag.sqlite3"

    def run(*args: str) -> str:
        result = subprocess.run(
            [*base, *args], cwd=scratch, env=env, capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError(f"CLI {args} failed: {result.stdout}\n{result.stderr}")
        return result.stdout

    def rows(sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(database) as connection:
            return connection.execute(sql, parameters).fetchall()

    def wait_jobs(*, allow_failure: bool = False) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            jobs = rows("SELECT status, error FROM jobs")
            if jobs and all(status in {"done", "failed"} for status, _ in jobs):
                if not allow_failure and any(status == "failed" for status, _ in jobs):
                    raise RuntimeError(f"Smoke job failed: {jobs}")
                return
            time.sleep(0.1)
        raise RuntimeError("Disposable PDF daemon did not finish within 90 seconds")

    for _ in range(2):
        status = run("status", "--initialize")
        assert "source_pdfs:" not in status and "downloaded_pdfs:" not in status
        assert not (data / "source-pdfs").exists()
        assert not (data / "downloaded-pdfs").exists()
        assert (data / "ocr-pdfs").is_dir()
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
        source = scratch / "council.pdf"
        alternate = scratch / "alternate.pdf"
        make_pdf(source, "Roads council approved drainage improvements.")
        make_pdf(alternate, "Parks council discussed alternate extraction.")
        run("ingest", str(source))
        run("ingest", str(alternate), "--pdf-extractor", "pdfplumber")
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM documents") == [(2,)]
        assert set(rows("SELECT extractor FROM pages")) == {("pymupdf",), ("pdfplumber",)}
        assert rows(
            "SELECT COUNT(*) FROM processing_generations WHERE normalized_path IS NOT NULL"
        ) == [(0,)]
        assert "p. 1" in run("search", "drainage", "--source-type", "pdf")
        packet = scratch / "packet.md"
        run("packet", "drainage", "--source-type", "pdf", "--out", str(packet))
        old_packet = packet.read_bytes()
        assert b"drainage" in old_packet and b"normalized artifact:" not in old_packet
        run("ingest", str(source))
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM documents") == [(2,)]
        document_id = rows("SELECT id FROM documents ORDER BY rowid LIMIT 1")[0][0]
        source.unlink()
        run("reprocess", document_id, "--pdf-extractor", "pdfplumber")
        wait_jobs()
        assert rows("SELECT COUNT(*) FROM processing_generations") == [(3,)]
        assert packet.read_bytes() == old_packet
        if source_pdf is not None:
            reported = scratch / "reported.pdf"
            shutil.copyfile(source_pdf, reported)
            run("ingest", str(reported), "--pdf-extractor", "pdfplumber")
            wait_jobs()
            with fitz.open(reported) as document:
                assert rows(
                    "SELECT COUNT(*) FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.source_path LIKE '%reported.pdf'"
                ) == [(len(document),)]
                print(
                    f"Reported PDF: exit-4 fallback indexed {len(document)} original pages with pdfplumber."
                )
        before = rows("SELECT id FROM documents")
        blank = scratch / "blank.pdf"
        make_pdf(blank, "")
        run("ingest", str(blank))
        wait_jobs(allow_failure=True)
        assert rows("SELECT id FROM documents") == before
        error = rows("SELECT error FROM jobs WHERE status='failed'")[0][0]
        assert "exit status 4" in error and "no usable" in error and "retry" in error
        code_file.write_text("5")
        retry_source = scratch / "retry.pdf"
        make_pdf(retry_source, "Recovered council retry evidence.")
        run("ingest", str(retry_source))
        wait_jobs(allow_failure=True)
        assert rows("SELECT id FROM documents") == before
        retry_id, error = rows("SELECT id, error FROM jobs ORDER BY rowid DESC LIMIT 1")[0]
        assert "exit status 5" in error
        code_file.write_text("4")
        run("jobs", "retry", retry_id)
        wait_jobs(allow_failure=True)
        assert rows("SELECT status FROM jobs WHERE id=?", (retry_id,)) == [("done",)]
        assert rows("SELECT COUNT(*) FROM documents") == [(len(before) + 1,)]
        assert rows(
            "SELECT COUNT(*) FROM source_units WHERE normalized_text LIKE '%PARTIAL OCR OUTPUT%'"
        ) == [(0,)]
        print(
            "PDF fallback smoke passed: auto/pdfplumber, original provenance, citations/packets, duplicates, saved-byte reprocessing, atomic empty-text failure, non-4 rejection and retry."
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pdf", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="newsrag-pdf-fallback-smoke-") as directory:
            smoke(Path(directory), server.server_port, args.source_pdf)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.daemon import DaemonConfig, run_daemon
from newsrag.jobs import Job, list_jobs
from newsrag.storage import initialize_storage
from newsrag.watches import WATCH_JOB_KIND, WatchDebouncer, add_watch

runner = CliRunner()


def test_watch_add_and_list(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()

    add_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "watch",
            "add",
            str(watch_dir),
            "--body",
            "City Council",
            "--document-type",
            "agenda_packet",
        ],
    )
    list_result = runner.invoke(app, ["--data-dir", str(data_dir), "watch", "list"])

    assert add_result.exit_code == 0
    assert "Added watch" in add_result.stdout
    assert list_result.exit_code == 0
    assert "NewsRAG Watches" in list_result.stdout
    assert str(watch_dir.resolve()) in list_result.stdout
    assert "City Council" in list_result.stdout
    assert "agenda_packet" in list_result.stdout


def test_daemon_enqueues_job_for_pdf_watch_event(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    pdf_path = watch_dir / "packet.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=watch_dir, metadata={"body": "City Council"})

    async def fake_watch_stream(
        paths_to_watch: tuple[str, ...],
    ) -> AsyncIterator[set[tuple[object, str]]]:
        assert paths_to_watch == (str(watch_dir.resolve()),)
        yield {("added", str(pdf_path.resolve()))}

    asyncio.run(
        run_daemon(
            DaemonConfig(
                data_dir=data_dir, poll_interval=0, max_loops=1, watch_stability_seconds=0
            ),
            handlers={WATCH_JOB_KIND: _noop_handler},
            watch_stream_factory=fake_watch_stream,
        )
    )

    jobs = list_jobs(paths.database)
    assert len(jobs) == 1
    assert jobs[0].kind == WATCH_JOB_KIND
    assert jobs[0].payload["path"] == str(pdf_path.resolve())
    assert jobs[0].payload["metadata"]["body"] == "City Council"


def test_non_pdf_files_are_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    text_path = watch_dir / "notes.txt"
    text_path.write_text("ignore me", encoding="utf-8")

    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=watch_dir)

    async def fake_watch_stream(_: tuple[str, ...]) -> AsyncIterator[set[tuple[object, str]]]:
        yield {("added", str(text_path.resolve()))}

    asyncio.run(
        run_daemon(
            DaemonConfig(
                data_dir=data_dir, poll_interval=0, max_loops=1, watch_stability_seconds=0
            ),
            handlers={WATCH_JOB_KIND: _noop_handler},
            watch_stream_factory=fake_watch_stream,
        )
    )

    assert list_jobs(paths.database) == []


def test_duplicate_unchanged_pdf_events_do_not_create_duplicate_jobs(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    pdf_path = watch_dir / "packet.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=watch_dir)

    async def fake_watch_stream(_: tuple[str, ...]) -> AsyncIterator[set[tuple[object, str]]]:
        change = ("added", str(pdf_path.resolve()))
        yield {change}
        yield {change}

    asyncio.run(
        run_daemon(
            DaemonConfig(
                data_dir=data_dir, poll_interval=0, max_loops=2, watch_stability_seconds=0
            ),
            handlers={WATCH_JOB_KIND: _noop_handler},
            watch_stream_factory=fake_watch_stream,
        )
    )

    jobs = list_jobs(paths.database)
    assert len(jobs) == 1
    assert jobs[0].payload["path"] == str(pdf_path.resolve())


def test_burst_of_pdf_events_enqueues_once_after_stabilization(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    pdf_path = watch_dir / "packet.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    clock = FakeClock()

    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=watch_dir)
    debouncer = WatchDebouncer(
        database_path=paths.database,
        stability_seconds=5,
        monotonic=clock.now,
    )

    change = ("modified", str(pdf_path.resolve()))
    debouncer.consider_changes({change})
    debouncer.consider_changes({change})
    assert debouncer.flush_ready() == []

    clock.advance(5)
    jobs = debouncer.flush_ready()

    assert len(jobs) == 1
    assert jobs[0].payload["path"] == str(pdf_path.resolve())
    assert debouncer.flush_ready() == []
    assert len(list_jobs(paths.database)) == 1


def test_changing_pdf_waits_until_signature_is_stable(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    pdf_path = watch_dir / "packet.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\npartial")
    clock = FakeClock()

    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=watch_dir)
    debouncer = WatchDebouncer(
        database_path=paths.database,
        stability_seconds=5,
        monotonic=clock.now,
    )

    debouncer.consider_changes({("modified", str(pdf_path.resolve()))})
    clock.advance(5)
    pdf_path.write_bytes(b"%PDF-1.4\ncomplete")

    assert debouncer.flush_ready() == []

    clock.advance(4)
    assert debouncer.flush_ready() == []

    clock.advance(1)
    jobs = debouncer.flush_ready()

    assert len(jobs) == 1
    assert jobs[0].payload["path"] == str(pdf_path.resolve())


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def _noop_handler(_: Job) -> None:
    return None

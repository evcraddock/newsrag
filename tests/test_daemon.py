from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.daemon import DaemonRunner
from newsrag.jobs import DONE, FAILED, RUNNING, Job, create_job, get_job, set_job_status
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_daemon_run_command_starts_against_initialized_storage(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "daemon",
            "run",
            "--poll-interval",
            "0",
            "--max-loops",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "NewsRAG daemon running" in result.stdout


def test_mocked_job_moves_from_pending_to_running_to_done(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    seen_running_statuses: list[str] = []

    job = create_job(paths.database, kind="mock")

    async def handler(_: Job) -> None:
        current = get_job(paths.database, job.id)
        seen_running_statuses.append(current.status)

    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={"mock": handler},
        poll_interval=0,
    )

    processed = asyncio.run(runner_instance.run_cycle())

    assert processed is True
    assert seen_running_statuses == [RUNNING]
    assert get_job(paths.database, job.id).status == DONE


def test_failing_mocked_job_records_failed_state_and_error(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    job = create_job(paths.database, kind="mock")

    async def handler(_: Job) -> None:
        raise RuntimeError("boom")

    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={"mock": handler},
        poll_interval=0,
    )

    processed = asyncio.run(runner_instance.run_cycle())
    updated_job = get_job(paths.database, job.id)

    assert processed is True
    assert updated_job.status == FAILED
    assert updated_job.error == "boom"


def test_jobs_list_shows_all_job_statuses(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)

    pending_job = create_job(paths.database, kind="pending-job", job_id="job-pending")
    running_job = create_job(paths.database, kind="running-job", job_id="job-running")
    done_job = create_job(paths.database, kind="done-job", job_id="job-done")
    failed_job = create_job(paths.database, kind="failed-job", job_id="job-failed")

    set_job_status(paths.database, running_job.id, status=RUNNING)
    set_job_status(paths.database, done_job.id, status=DONE)
    set_job_status(paths.database, failed_job.id, status=FAILED, error="boom")

    result = runner.invoke(app, ["--data-dir", str(data_dir), "jobs", "list"])

    assert result.exit_code == 0
    assert "NewsRAG Jobs" in result.stdout
    assert pending_job.id in result.stdout
    assert "pending" in result.stdout
    assert running_job.id in result.stdout
    assert "running" in result.stdout
    assert done_job.id in result.stdout
    assert "done" in result.stdout
    assert failed_job.id in result.stdout
    assert "failed" in result.stdout
    assert "boom" in result.stdout

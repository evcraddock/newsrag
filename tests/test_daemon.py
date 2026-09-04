from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.daemon import DaemonRunner
from newsrag.jobs import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    Job,
    create_job,
    get_job,
    mark_job_done,
    set_job_status,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_daemon_run_command_starts_against_initialized_storage(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: nomic-embed-text-v1.5
""".strip(),
        encoding="utf-8",
    )
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_path),
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
    assert "daemon_started" in result.stderr
    assert str(data_dir) in result.stderr


def test_daemon_run_command_honors_error_log_level(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: nomic-embed-text-v1.5
""".strip(),
        encoding="utf-8",
    )
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "daemon",
            "run",
            "--poll-interval",
            "0",
            "--max-loops",
            "1",
        ],
        env={"NEWSRAG_LOG_LEVEL": "ERROR"},
    )

    assert result.exit_code == 0
    assert "daemon_started" not in result.stderr


def test_daemon_run_command_requires_embedding_configuration(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    config_path = tmp_path / "missing-config.yaml"
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_path),
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

    assert result.exit_code == 1
    assert "No embedding provider configured" in result.stdout
    assert "embedding.provider=openai_compatible" in result.stdout


def test_mocked_job_moves_from_pending_to_running_to_done(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    seen_running_statuses: list[str] = []
    source_path = tmp_path / "packet.pdf"
    job = create_job(paths.database, kind="mock", payload={"path": str(source_path)})

    async def handler(_: Job) -> None:
        current = get_job(paths.database, job.id)
        seen_running_statuses.append(current.status)

    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={"mock": handler},
        poll_interval=0,
    )

    with caplog.at_level(logging.INFO, logger="newsrag.daemon"):
        processed = asyncio.run(runner_instance.run_cycle())

    assert processed is True
    assert seen_running_statuses == [RUNNING]
    assert get_job(paths.database, job.id).status == DONE
    assert f"job_started job_id={job.id} kind=mock" in caplog.text
    assert f"job_completed job_id={job.id} kind=mock" in caplog.text
    assert f"source_path='{source_path}'" in caplog.text
    assert "elapsed_ms=" in caplog.text


def test_successful_job_persists_structured_result(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    job = create_job(paths.database, kind="mock")

    async def handler(_: Job) -> dict[str, object]:
        return {
            "outcome": "created",
            "artifact_id": "artifact-1",
            "document_id": "document-1",
        }

    asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={"mock": handler},
            poll_interval=0,
        ).run_cycle()
    )

    updated_job = get_job(paths.database, job.id)
    assert updated_job.status == DONE
    assert updated_job.result == {
        "artifact_id": "artifact-1",
        "document_id": "document-1",
        "outcome": "created",
    }


def test_failing_mocked_job_records_failed_state_and_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    source_path = tmp_path / "packet.pdf"
    job = create_job(paths.database, kind="mock", payload={"path": str(source_path)})

    async def handler(_: Job) -> None:
        raise RuntimeError("boom")

    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={"mock": handler},
        poll_interval=0,
    )

    with caplog.at_level(logging.INFO, logger="newsrag.daemon"):
        processed = asyncio.run(runner_instance.run_cycle())
    updated_job = get_job(paths.database, job.id)

    assert processed is True
    assert updated_job.status == FAILED
    assert updated_job.error == "boom"
    assert f"job_failed job_id={job.id} kind=mock" in caplog.text
    assert f"source_path='{source_path}'" in caplog.text
    assert "error='boom'" in caplog.text


def test_idle_daemon_cycle_does_not_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    runner_instance = DaemonRunner(database_path=paths.database, poll_interval=0)

    with caplog.at_level(logging.INFO, logger="newsrag.daemon"):
        processed = asyncio.run(runner_instance.run_cycle())

    assert processed is False
    assert caplog.records == []


def test_jobs_list_shows_all_job_statuses(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)

    pending_job = create_job(paths.database, kind="pending-job", job_id="job-pending")
    running_job = create_job(paths.database, kind="running-job", job_id="job-running")
    done_job = create_job(paths.database, kind="done-job", job_id="job-done")
    failed_job = create_job(paths.database, kind="failed-job", job_id="job-failed")

    set_job_status(paths.database, running_job.id, status=RUNNING)
    mark_job_done(
        paths.database,
        done_job.id,
        result={"outcome": "created", "document_id": "document-1"},
    )
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
    assert "outcome=created" in result.stdout
    assert "document_id=document-1" in result.stdout
    assert failed_job.id in result.stdout
    assert "failed" in result.stdout
    assert "failed_at=" in result.stdout
    assert "error=boom" in result.stdout


def test_failed_job_appears_in_status_with_error_count(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    failed_job = create_job(
        paths.database,
        kind="mock",
        payload={"path": str(tmp_path / "packet.pdf")},
        job_id="job-failed",
    )
    set_job_status(paths.database, failed_job.id, status=FAILED, error="ocr boom")

    status_result = runner.invoke(app, ["--data-dir", str(data_dir), "status"])
    jobs_result = runner.invoke(app, ["--data-dir", str(data_dir), "jobs", "list"])

    assert status_result.exit_code == 0
    assert "jobs: warn" in status_result.stdout
    assert "failed=1" in status_result.stdout
    assert "summary: warn" in status_result.stdout
    assert jobs_result.exit_code == 0
    assert "job-failed" in jobs_result.stdout
    assert "path=" in jobs_result.stdout
    assert "error=ocr boom" in jobs_result.stdout


def test_jobs_retry_moves_failed_job_back_to_pending_and_daemon_reprocesses(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    job = create_job(paths.database, kind="mock", job_id="job-retry")
    set_job_status(paths.database, job.id, status=FAILED, error="boom")
    seen_statuses: list[str] = []

    result = runner.invoke(app, ["--data-dir", str(data_dir), "jobs", "retry", job.id])

    async def handler(_: Job) -> None:
        seen_statuses.append(get_job(paths.database, job.id).status)

    processed = asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={"mock": handler},
            poll_interval=0,
        ).run_cycle()
    )

    assert result.exit_code == 0
    assert "Retried job-retry; status=pending" in result.stdout
    assert seen_statuses == [RUNNING]
    assert processed is True
    assert get_job(paths.database, job.id).status == DONE
    assert get_job(paths.database, job.id).error is None


def test_jobs_retry_reports_clear_errors_for_unknown_or_non_failed_jobs(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    pending_job = create_job(paths.database, kind="mock", job_id="job-pending")

    unknown_result = runner.invoke(app, ["--data-dir", str(data_dir), "jobs", "retry", "missing"])
    non_failed_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "jobs", "retry", pending_job.id],
    )

    assert unknown_result.exit_code == 1
    assert "Unknown job: missing" in unknown_result.stdout
    assert non_failed_result.exit_code == 1
    assert "Job job-pending is pending; only failed jobs can be retried" in non_failed_result.stdout
    assert get_job(paths.database, pending_job.id).status == PENDING

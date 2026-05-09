from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from newsrag import __version__
from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.doctor import DoctorCheck, DoctorReport

runner = CliRunner()


def test_help_shows_cli_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "status" in result.stdout
    assert "daemon" in result.stdout
    assert "ingest" in result.stdout
    assert "ingest-url" in result.stdout
    assert "ingest-manifest" in result.stdout
    assert "search" in result.stdout
    assert "jobs" in result.stdout
    assert "watch" in result.stdout


def test_version_option_shows_project_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert f"newsrag {__version__}" in result.stdout


def test_doctor_runs_without_crashing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["--config-path", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "NewsRAG Doctor" in result.stdout
    assert "summary:" in result.stdout


def test_doctor_command_applies_embedding_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_embedding: list[EmbeddingConfig] = []
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    def fake_run_doctor(
        settings: Any,
        *,
        config_error: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> DoctorReport:
        del config_error, timeout_seconds
        embedding = settings.config.embedding
        seen_embedding.append(embedding)
        return DoctorReport(checks=(DoctorCheck("embedding", "ok", "mock"),))

    monkeypatch.setattr("newsrag.cli.run_doctor", fake_run_doctor)

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_path),
            "doctor",
            "--embedding-provider",
            "ollama",
            "--embedding-base-url",
            "http://localhost:11434",
            "--embedding-model",
            "nomic-embed-text",
        ],
    )

    assert result.exit_code == 0
    assert seen_embedding == [
        EmbeddingConfig(
            provider="ollama",
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            api_key_env=None,
        )
    ]

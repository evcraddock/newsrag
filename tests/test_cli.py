from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app

runner = CliRunner()


def test_help_shows_cli_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_doctor_runs_without_crashing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["--config-path", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "NewsRAG Doctor" in result.stdout
    assert "summary:" in result.stdout

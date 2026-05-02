from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from newsrag.cli import app

runner = CliRunner()


def test_help_shows_cli_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_doctor_runs_without_crashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
embedding:
  provider: ollama
  ollama:
    host: http://127.0.0.1:11434
    model: nomic-embed-text
""".strip(),
        encoding="utf-8",
    )

    def fake_get(url: str, timeout: float) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={"models": [{"name": "nomic-embed-text:latest"}]},
        )

    monkeypatch.setattr("newsrag.doctor.httpx.get", fake_get)

    result = runner.invoke(app, ["--config-path", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "NewsRAG Doctor" in result.stdout
    assert "summary: ok" in result.stdout

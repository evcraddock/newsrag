from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from newsrag.config import AppConfig, RuntimeSettings
from newsrag.doctor import DoctorCheck, DoctorReport, format_report, run_doctor


def test_format_report_uses_warn_summary_for_warnings(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(source_path=tmp_path / "config.yaml"),
    )
    report = DoctorReport(checks=(DoctorCheck("embedding", "warn", "not configured"),))

    formatted = format_report(report, settings=settings)

    assert "summary: warn" in formatted


def test_format_report_uses_error_summary_for_errors(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(source_path=tmp_path / "config.yaml"),
    )
    report = DoctorReport(checks=(DoctorCheck("config", "error", "invalid"),))

    formatted = format_report(report, settings=settings)

    assert "summary: error" in formatted


def test_run_doctor_warns_when_embedding_provider_is_unconfigured(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(source_path=tmp_path / "config.yaml"),
    )

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "warn"
    assert "no embedding provider configured" in embedding_check.detail
    assert report.summary == "warn"


def test_run_doctor_handles_malformed_ollama_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding_provider="ollama",
        ),
    )

    def fake_get(url: str, timeout: float) -> httpx.Response:
        request = httpx.Request("GET", url)
        response = httpx.Response(200, request=request, content=b"not-json")
        return response

    monkeypatch.setattr("newsrag.doctor.httpx.get", fake_get)
    monkeypatch.setattr("newsrag.doctor.shutil.which", lambda command: f"/usr/bin/{command}")

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "warn"
    assert "malformed JSON" in embedding_check.detail


def test_run_doctor_checks_generic_openai_compatible_provider(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding_provider="openai_compatible",
            embedding_base_url="http://localhost:1234/v1",
            embedding_model="text-embedding-3-small",
        ),
    )

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "ok"
    assert "provider=openai_compatible" in embedding_check.detail

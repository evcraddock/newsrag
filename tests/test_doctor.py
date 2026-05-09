from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from newsrag.config import AppConfig, DaemonConfig, EmbeddingConfig, RuntimeSettings
from newsrag.doctor import DoctorCheck, DoctorReport, format_report, run_doctor
from newsrag.storage import initialize_storage
from newsrag.watches import add_watch


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


def test_run_doctor_reports_daemon_connectivity_when_configured(tmp_path: Path) -> None:
    socket_path = tmp_path / "missing.sock"
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            daemon=DaemonConfig(socket_path=socket_path),
        ),
    )

    report = run_doctor(settings)

    daemon_check = next(check for check in report.checks if check.name == "daemon")
    assert daemon_check.status == "warn"
    assert str(socket_path) in daemon_check.detail
    assert "not found" in daemon_check.detail


def test_run_doctor_reports_actionable_watcher_health_failures(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    missing_watch_dir = tmp_path / "missing"
    paths = initialize_storage(data_dir)
    add_watch(paths.database, path=missing_watch_dir)
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=data_dir,
        config=AppConfig(source_path=tmp_path / "config.yaml"),
    )

    report = run_doctor(settings)

    watcher_check = next(check for check in report.checks if check.name == "watcher")
    assert watcher_check.status == "warn"
    assert "missing watched folder" in watcher_check.detail
    assert "remove/re-add the watch" in watcher_check.detail


def test_run_doctor_handles_malformed_ollama_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding=EmbeddingConfig(provider="ollama", model="nomic-embed-text"),
        ),
    )

    def fake_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, content=b"not-json")

    monkeypatch.setattr("newsrag.doctor.httpx.get", fake_get)

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "warn"
    assert "malformed JSON" in embedding_check.detail


def test_run_doctor_checks_generic_openai_compatible_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding=EmbeddingConfig(
                provider="openai_compatible",
                base_url="http://localhost:1234/v1",
                model="text-embedding-3-small",
            ),
        ),
    )

    def fake_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> httpx.Response:
        request = httpx.Request("GET", url, headers=headers)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"id": "text-embedding-3-small"}]},
        )

    monkeypatch.setattr("newsrag.doctor.httpx.get", fake_get)

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "ok"
    assert "provider=openai_compatible" in embedding_check.detail


def test_run_doctor_warns_when_ollama_model_is_missing(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding=EmbeddingConfig(provider="ollama"),
        ),
    )

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "warn"
    assert "embedding.model is missing" in embedding_check.detail


def test_run_doctor_warns_when_ollama_is_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding=EmbeddingConfig(
                provider="ollama",
                base_url="http://127.0.0.1:11434",
                model="nomic-embed-text",
            ),
        ),
    )

    def fake_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> httpx.Response:
        request = httpx.Request("GET", url, headers=headers)
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr("newsrag.doctor.httpx.get", fake_get)

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "warn"
    assert "is unreachable" in embedding_check.detail


def test_run_doctor_checks_ollama_provider_when_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RuntimeSettings(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / ".newsrag",
        config=AppConfig(
            source_path=tmp_path / "config.yaml",
            embedding=EmbeddingConfig(
                provider="ollama",
                base_url="http://127.0.0.1:11434",
                model="nomic-embed-text",
            ),
        ),
    )

    def fake_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={"models": [{"name": "nomic-embed-text:latest"}]},
        )

    monkeypatch.setattr("newsrag.doctor.httpx.get", fake_get)

    report = run_doctor(settings)

    embedding_check = next(check for check in report.checks if check.name == "embedding")
    assert embedding_check.status == "ok"
    assert "provider=ollama" in embedding_check.detail

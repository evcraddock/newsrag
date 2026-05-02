from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from newsrag.config import AppConfig, RuntimeSettings


@dataclass(frozen=True)
class DoctorCheck:
    """One doctor check result."""

    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    """Structured doctor output."""

    checks: tuple[DoctorCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)


def run_doctor(
    settings: RuntimeSettings,
    *,
    config_error: str | None = None,
    timeout_seconds: float = 2.0,
) -> DoctorReport:
    """Run environment checks for NewsRAG."""

    checks: list[DoctorCheck] = []
    checks.append(_config_check(settings.config_path, config_error))
    checks.append(_data_dir_check(settings.data_dir))
    checks.extend(_ocr_checks())
    checks.append(_embedding_check(settings.config, timeout_seconds=timeout_seconds))
    checks.append(_daemon_check(settings.config))
    return DoctorReport(checks=tuple(checks))


def format_report(report: DoctorReport, *, settings: RuntimeSettings) -> str:
    """Format a doctor report for terminal output."""

    lines = [
        "NewsRAG Doctor",
        f"config_path: {settings.config_path}",
        f"data_dir: {settings.data_dir}",
    ]

    for check in report.checks:
        lines.append(f"{check.name}: {check.status} - {check.detail}")

    summary = "error" if report.has_errors else "ok"
    lines.append(f"summary: {summary}")
    return "\n".join(lines)


def _config_check(config_path: Path, config_error: str | None) -> DoctorCheck:
    if config_error is not None:
        return DoctorCheck("config", "error", config_error)
    if config_path.exists():
        return DoctorCheck("config", "ok", f"loaded {config_path}")
    return DoctorCheck("config", "ok", f"missing {config_path}; using defaults")


def _data_dir_check(data_dir: Path) -> DoctorCheck:
    if data_dir.exists() and not data_dir.is_dir():
        return DoctorCheck("data_dir", "error", f"{data_dir} exists but is not a directory")

    target = data_dir if data_dir.exists() else _nearest_existing_parent(data_dir)
    if target is None:
        return DoctorCheck("data_dir", "error", f"cannot resolve writable parent for {data_dir}")

    if not os.access(target, os.W_OK | os.X_OK):
        return DoctorCheck("data_dir", "error", f"{target} is not writable")

    if data_dir.exists():
        return DoctorCheck("data_dir", "ok", f"{data_dir} exists and is writable")
    return DoctorCheck(
        "data_dir", "ok", f"{data_dir} does not exist yet; parent {target} is writable"
    )


def _ocr_checks() -> tuple[DoctorCheck, ...]:
    return (
        _command_check("ocrmypdf", "ocrmypdf"),
        _command_check("tesseract", "tesseract"),
        _command_check("ghostscript", "gs"),
        _command_check("qpdf", "qpdf"),
    )


def _command_check(name: str, command: str) -> DoctorCheck:
    location = shutil.which(command)
    if location is None:
        return DoctorCheck(name, "warn", f"missing command '{command}'")
    return DoctorCheck(name, "ok", f"found {location}")


def _embedding_check(config: AppConfig, *, timeout_seconds: float) -> DoctorCheck:
    provider = config.embedding_provider.lower()
    if provider == "ollama":
        return _ollama_check(config, timeout_seconds=timeout_seconds)
    if provider == "openai":
        return _openai_check()
    return DoctorCheck(
        "embedding",
        "error",
        f"unsupported provider '{config.embedding_provider}'",
    )


def _ollama_check(config: AppConfig, *, timeout_seconds: float) -> DoctorCheck:
    ollama_binary = shutil.which("ollama")
    if ollama_binary is None:
        return DoctorCheck(
            "embedding",
            "warn",
            "provider=ollama but the 'ollama' command is missing",
        )

    endpoint = config.ollama.host.rstrip("/") + "/api/tags"
    try:
        response = httpx.get(endpoint, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError:
        return DoctorCheck(
            "embedding",
            "warn",
            (
                "provider=ollama but the local server is unreachable at "
                f"{config.ollama.host}; start Ollama and run 'ollama pull {config.ollama.model}'"
            ),
        )

    model_names = _extract_ollama_model_names(payload)
    if _ollama_model_available(config.ollama.model, model_names):
        return DoctorCheck(
            "embedding",
            "ok",
            (f"provider=ollama host={config.ollama.host} model={config.ollama.model} is available"),
        )

    return DoctorCheck(
        "embedding",
        "warn",
        (
            f"provider=ollama host={config.ollama.host} is reachable but "
            f"model={config.ollama.model} is not installed; run 'ollama pull {config.ollama.model}'"
        ),
    )


def _openai_check() -> DoctorCheck:
    if os.getenv("OPENAI_API_KEY"):
        return DoctorCheck("embedding", "ok", "provider=openai and OPENAI_API_KEY is set")
    return DoctorCheck(
        "embedding",
        "warn",
        "provider=openai but OPENAI_API_KEY is not set",
    )


def _daemon_check(config: AppConfig) -> DoctorCheck:
    socket_path = config.daemon.socket_path
    if socket_path is None:
        return DoctorCheck("daemon", "info", "not configured")

    try:
        mode = socket_path.stat().st_mode
    except FileNotFoundError:
        return DoctorCheck("daemon", "warn", f"configured socket {socket_path} not found")

    if stat.S_ISSOCK(mode):
        return DoctorCheck("daemon", "ok", f"socket available at {socket_path}")
    return DoctorCheck(
        "daemon", "warn", f"configured path {socket_path} exists but is not a socket"
    )


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def _extract_ollama_model_names(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()

    models = payload.get("models")
    if not isinstance(models, list):
        return set()

    names: set[str] = set()
    for model in models:
        if isinstance(model, dict):
            name = model.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _ollama_model_available(expected_model: str, installed_models: set[str]) -> bool:
    return any(
        model_name == expected_model or model_name.startswith(f"{expected_model}:")
        for model_name in installed_models
    )

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from newsrag.config import EmbeddingConfig, RuntimeSettings
from newsrag.storage import build_storage_paths

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"


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

    @property
    def has_warnings(self) -> bool:
        return any(check.status == "warn" for check in self.checks)

    @property
    def summary(self) -> str:
        if self.has_errors:
            return "error"
        if self.has_warnings:
            return "warn"
        return "ok"


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
    checks.append(_binary_check("ocrmypdf", "ocrmypdf"))
    checks.append(_binary_check("tesseract", "tesseract"))
    checks.append(_binary_check("ghostscript", "gs"))
    checks.append(_binary_check("qpdf", "qpdf"))
    checks.append(_embedding_check(settings.config.embedding, timeout_seconds=timeout_seconds))
    checks.append(_daemon_check(settings.config.daemon.socket_path))
    checks.append(_watcher_check(settings.data_dir))
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

    lines.append(f"summary: {report.summary}")
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


def _binary_check(name: str, command: str) -> DoctorCheck:
    location = _which(command)
    if location is None:
        return DoctorCheck(name, "warn", f"missing command '{command}'")
    return DoctorCheck(name, "ok", f"found {location}")


def _embedding_check(embedding: EmbeddingConfig, *, timeout_seconds: float) -> DoctorCheck:
    provider = embedding.provider
    if provider is None:
        return DoctorCheck("embedding", "warn", "no embedding provider configured")

    normalized_provider = provider.lower()
    if normalized_provider == "ollama":
        return _ollama_check(embedding, timeout_seconds=timeout_seconds)
    if normalized_provider in {"openai", "openai_compatible"}:
        return _openai_compatible_check(embedding, timeout_seconds=timeout_seconds)
    return DoctorCheck("embedding", "error", f"unsupported provider '{provider}'")


def _ollama_check(embedding: EmbeddingConfig, *, timeout_seconds: float) -> DoctorCheck:
    if embedding.model is None:
        return DoctorCheck(
            "embedding",
            "warn",
            "provider=ollama is configured but embedding.model is missing",
        )

    base_url = embedding.base_url or DEFAULT_OLLAMA_BASE_URL
    endpoint = f"{base_url.rstrip('/')}/api/tags"
    payload = _json_get(
        endpoint,
        provider_label="ollama",
        timeout_seconds=timeout_seconds,
    )
    if isinstance(payload, DoctorCheck):
        return payload

    model_names = _extract_ollama_model_names(payload)
    if _model_available(embedding.model, model_names):
        return DoctorCheck(
            "embedding",
            "ok",
            f"provider=ollama base_url={base_url} model={embedding.model} is available",
        )

    return DoctorCheck(
        "embedding",
        "warn",
        (
            f"provider=ollama base_url={base_url} is reachable but model={embedding.model} "
            f"is not installed; run 'ollama pull {embedding.model}'"
        ),
    )


def _openai_compatible_check(
    embedding: EmbeddingConfig,
    *,
    timeout_seconds: float,
) -> DoctorCheck:
    provider = embedding.provider or "openai_compatible"
    if embedding.model is None:
        return DoctorCheck(
            "embedding",
            "warn",
            f"provider={provider} is configured but embedding.model is missing",
        )

    api_key_env = embedding.api_key_env
    if provider.lower() == "openai" and api_key_env is None:
        api_key_env = DEFAULT_OPENAI_API_KEY_ENV

    headers: dict[str, str] = {}
    if api_key_env is not None:
        api_key = os.getenv(api_key_env)
        if not api_key:
            return DoctorCheck(
                "embedding",
                "warn",
                f"provider={provider} expects environment variable {api_key_env}",
            )
        headers["Authorization"] = f"Bearer {api_key}"

    base_url = embedding.base_url
    if provider.lower() == "openai" and base_url is None:
        base_url = DEFAULT_OPENAI_BASE_URL

    if base_url is None:
        return DoctorCheck(
            "embedding",
            "warn",
            f"provider={provider} is configured but embedding.base_url is missing",
        )

    endpoint = f"{base_url.rstrip('/')}/models"
    payload = _json_get(
        endpoint,
        provider_label=provider,
        timeout_seconds=timeout_seconds,
        headers=headers,
    )
    if isinstance(payload, DoctorCheck):
        return payload

    model_names = _extract_openai_compatible_model_names(payload)
    if model_names and not _model_available(embedding.model, model_names):
        return DoctorCheck(
            "embedding",
            "warn",
            (
                f"provider={provider} base_url={base_url} is reachable but model={embedding.model} "
                "is not listed"
            ),
        )

    return DoctorCheck(
        "embedding",
        "ok",
        f"provider={provider} base_url={base_url} model={embedding.model} is available",
    )


def _daemon_check(socket_path: Path | None) -> DoctorCheck:
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


def _watcher_check(data_dir: Path) -> DoctorCheck:
    database_path = build_storage_paths(data_dir).database
    if not database_path.exists():
        return DoctorCheck("watcher", "info", "storage is not initialized; no watches to inspect")

    try:
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute("SELECT path FROM watches ORDER BY path ASC").fetchall()
    except sqlite3.Error as exc:
        return DoctorCheck("watcher", "warn", f"could not inspect watches: {exc}")

    if not rows:
        return DoctorCheck("watcher", "ok", "no watched folders configured")

    problems: list[str] = []
    for (raw_path,) in rows:
        path = Path(str(raw_path))
        if not path.exists():
            problems.append(
                f"missing watched folder {path}; recreate it or remove/re-add the watch"
            )
        elif not path.is_dir():
            problems.append(f"watched path {path} is not a directory; remove/re-add the watch")
        elif not os.access(path, os.R_OK | os.X_OK):
            problems.append(f"watched folder {path} is not readable; fix permissions")

    if problems:
        return DoctorCheck("watcher", "warn", "; ".join(problems))
    return DoctorCheck("watcher", "ok", f"{len(rows)} watched folder(s) ready")


def _json_get(
    endpoint: str,
    *,
    provider_label: str,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | DoctorCheck:
    try:
        response = httpx.get(endpoint, timeout=timeout_seconds, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError:
        return DoctorCheck(
            "embedding",
            "warn",
            f"provider={provider_label} endpoint {endpoint} is unreachable",
        )
    except ValueError:
        return DoctorCheck(
            "embedding",
            "warn",
            f"provider={provider_label} endpoint {endpoint} returned malformed JSON",
        )

    if not isinstance(payload, dict):
        return DoctorCheck(
            "embedding",
            "warn",
            f"provider={provider_label} endpoint {endpoint} returned an unexpected payload shape",
        )
    return payload


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def _extract_ollama_model_names(payload: dict[str, Any]) -> set[str]:
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


def _extract_openai_compatible_model_names(payload: dict[str, Any]) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return set()

    names: set[str] = set()
    for model in data:
        if isinstance(model, dict):
            identifier = model.get("id")
            if isinstance(identifier, str):
                names.add(identifier)
    return names


def _model_available(expected_model: str, installed_models: set[str]) -> bool:
    return any(
        model_name == expected_model or model_name.startswith(f"{expected_model}:")
        for model_name in installed_models
    )


def _which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None

from __future__ import annotations

import logging

import pytest

from newsrag.logging_config import LoggingConfigError, resolve_log_level


def test_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWSRAG_LOG_LEVEL", raising=False)

    assert resolve_log_level() == logging.INFO


def test_log_level_honors_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSRAG_LOG_LEVEL", "debug")

    assert resolve_log_level() == logging.DEBUG


def test_log_level_rejects_invalid_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSRAG_LOG_LEVEL", "verbose-ish")

    with pytest.raises(LoggingConfigError, match="NEWSRAG_LOG_LEVEL"):
        resolve_log_level()

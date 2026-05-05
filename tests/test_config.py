from __future__ import annotations

from pathlib import Path

import pytest

from newsrag.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    EmbeddingConfig,
    load_config,
    resolve_data_dir,
    resolve_runtime_settings,
)


def test_load_config_uses_defaults_for_empty_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    config = load_config(config_path)

    assert config == AppConfig(source_path=config_path)


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.yaml"

    config = load_config(config_path)

    assert config.source_path == config_path
    assert config.data_dir is None
    assert config.embedding == EmbeddingConfig()


def test_load_config_reads_generic_embedding_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
embedding:
  provider: openai_compatible
  base_url: http://localhost:1234/v1
  model: text-embedding-3-small
  api_key_env: OPENAI_API_KEY
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.embedding.provider == "openai_compatible"
    assert config.embedding.base_url == "http://localhost:1234/v1"
    assert config.embedding.model == "text-embedding-3-small"
    assert config.embedding.api_key_env == "OPENAI_API_KEY"


def test_load_config_invalid_yaml_raises(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("data_dir: [", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_resolve_runtime_settings_prefers_cli_data_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("data_dir: from-config\n", encoding="utf-8")
    cli_data_dir = tmp_path / "from-cli"

    settings = resolve_runtime_settings(
        config_path=config_path,
        data_dir=cli_data_dir,
        cwd=tmp_path,
    )

    assert settings.config_path == config_path
    assert settings.data_dir == cli_data_dir
    assert settings.config.data_dir == Path("from-config")


def test_resolve_data_dir_uses_config_value(tmp_path: Path) -> None:
    config = AppConfig(source_path=DEFAULT_CONFIG_PATH, data_dir=Path("configured-dir"))

    data_dir = resolve_data_dir(None, config, cwd=tmp_path)

    assert data_dir == (tmp_path / "configured-dir").resolve()


def test_resolve_data_dir_defaults_to_local_newsrag(tmp_path: Path) -> None:
    config = AppConfig(source_path=DEFAULT_CONFIG_PATH)

    data_dir = resolve_data_dir(None, config, cwd=tmp_path)

    assert data_dir == (tmp_path / ".newsrag").resolve()

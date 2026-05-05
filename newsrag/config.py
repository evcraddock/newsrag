from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/newsrag/config.yaml").expanduser()
DEFAULT_DATA_DIR_NAME = ".newsrag"


class ConfigError(Exception):
    """Raised when the NewsRAG config file is invalid."""


@dataclass(frozen=True)
class EmbeddingConfig:
    """Embedding provider configuration."""

    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class DaemonConfig:
    """Settings for optional daemon connectivity checks."""

    socket_path: Path | None = None


@dataclass(frozen=True)
class AppConfig:
    """User-global NewsRAG configuration."""

    source_path: Path
    data_dir: Path | None = None
    embedding: EmbeddingConfig = EmbeddingConfig()
    daemon: DaemonConfig = DaemonConfig()


@dataclass(frozen=True)
class RuntimeSettings:
    """Resolved runtime settings after CLI overrides are applied."""

    config_path: Path
    data_dir: Path
    config: AppConfig


EMPTY_CONFIG = AppConfig(source_path=DEFAULT_CONFIG_PATH)


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load the NewsRAG config file from disk.

    Args:
        config_path: Optional override path for the config file.

    Returns:
        The loaded config, or defaults when the file does not exist.

    Raises:
        ConfigError: If the config file exists but is invalid.
    """

    resolved_path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    if not resolved_path.exists():
        return AppConfig(source_path=resolved_path)

    raw_content = resolved_path.read_text(encoding="utf-8")

    try:
        loaded = yaml.safe_load(raw_content)
    except yaml.YAMLError as exc:  # pragma: no cover - exercised by tests via ConfigError
        raise ConfigError(f"Invalid YAML in {resolved_path}: {exc}") from exc

    if loaded is None:
        data: Mapping[str, object] = {}
    elif isinstance(loaded, dict):
        data = loaded
    else:
        raise ConfigError(f"Config file {resolved_path} must contain a top-level mapping.")

    return AppConfig(
        source_path=resolved_path,
        data_dir=_optional_path(data.get("data_dir"), field_name="data_dir"),
        embedding=_load_embedding_config(_mapping_value(data, "embedding", default={})),
        daemon=_load_daemon_config(_mapping_value(data, "daemon", default={})),
    )


def resolve_data_dir(
    cli_data_dir: Path | None,
    config: AppConfig,
    *,
    cwd: Path | None = None,
) -> Path:
    """Resolve the active corpus data directory.

    Args:
        cli_data_dir: CLI override for the data directory.
        config: Loaded application config.
        cwd: Optional working directory for resolving relative paths.

    Returns:
        The absolute active data directory path.
    """

    base_dir = cwd or Path.cwd()
    selected = cli_data_dir or config.data_dir or Path(DEFAULT_DATA_DIR_NAME)
    return _normalize_path(selected, cwd=base_dir)


def resolve_runtime_settings(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    cwd: Path | None = None,
) -> RuntimeSettings:
    """Load config and apply CLI overrides to build runtime settings."""

    config = load_config(config_path)
    resolved_config_path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    resolved_data_dir = resolve_data_dir(data_dir, config, cwd=cwd)
    return RuntimeSettings(
        config_path=resolved_config_path,
        data_dir=resolved_data_dir,
        config=config,
    )


def _load_embedding_config(embedding_data: Mapping[str, object]) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider=_optional_string(embedding_data.get("provider"), field_name="embedding.provider"),
        base_url=_optional_string(embedding_data.get("base_url"), field_name="embedding.base_url"),
        model=_optional_string(embedding_data.get("model"), field_name="embedding.model"),
        api_key_env=_optional_string(
            embedding_data.get("api_key_env"),
            field_name="embedding.api_key_env",
        ),
    )


def _load_daemon_config(daemon_data: Mapping[str, object]) -> DaemonConfig:
    return DaemonConfig(
        socket_path=_optional_path(daemon_data.get("socket_path"), field_name="daemon.socket_path")
    )


def _mapping_value(
    data: Mapping[str, object],
    key: str,
    *,
    default: Mapping[str, object],
) -> Mapping[str, object]:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, dict):
        return value
    raise ConfigError(f"Config field {key} must be a mapping.")


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    raise ConfigError(f"Config field {field_name} must be a string.")


def _optional_path(value: object, *, field_name: str) -> Path | None:
    string_value = _optional_string(value, field_name=field_name)
    if string_value is None:
        return None
    return Path(string_value).expanduser()


def _normalize_path(path: Path, *, cwd: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return (cwd / expanded).resolve()

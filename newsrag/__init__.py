"""NewsRAG package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("newsrag")
except PackageNotFoundError:  # pragma: no cover - source tree without installed metadata
    __version__ = "0.0.0"

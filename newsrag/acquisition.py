from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import mimetypes
import os
import socket
import ssl
import stat
import tempfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from newsrag.sources import normalize_url_reference

SOURCE_KIND_LOCAL_PATH: Literal["local_path"] = "local_path"
SOURCE_KIND_URL: Literal["url"] = "url"
DEFAULT_MAX_SOURCE_BYTES = 250 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_READ_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip"})
LOGGER = logging.getLogger(__name__)

SourceKind = Literal["local_path", "url"]
Resolver = Callable[[str, int], tuple[str, ...]]


class AcquisitionError(Exception):
    """Raised when source acquisition fails safely."""

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(f"Acquisition {stage} failed: {message}")


@dataclass(frozen=True)
class AcquisitionLimits:
    """Resource limits applied while acquiring one source."""

    max_local_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_compressed_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_decompressed_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True)
class AcquisitionRequest:
    """One explicit local path or public HTTP(S) URL to acquire."""

    kind: SourceKind
    reference: str


@dataclass(frozen=True)
class StagedSourceArtifact:
    """Exact acquired bytes staged for an ingestion identity decision."""

    source_kind: SourceKind
    submitted_reference: str
    normalized_reference: str
    resolved_reference: str
    staged_path: Path
    content_hash: str
    byte_size: int
    acquired_at: str
    reported_media_type: str | None
    provenance: dict[str, object]

    @property
    def safe_reference(self) -> str:
        """Return a log-safe source reference."""

        if self.source_kind == SOURCE_KIND_URL:
            return safe_url_reference(self.submitted_reference)
        return self.submitted_reference


class _ByteWriter(Protocol):
    def write(self, value: bytes) -> int:
        """Write bytes and return the number written."""


class _Digest(Protocol):
    def update(self, value: bytes) -> None:
        """Add bytes to the digest."""


class _Decompressor(Protocol):
    @property
    def unconsumed_tail(self) -> bytes:
        """Return encoded input not consumed because of the output bound."""

    @property
    def eof(self) -> bool:
        """Return whether the compressed stream reached its end marker."""

    @property
    def unused_data(self) -> bytes:
        """Return encoded bytes found after the compressed stream."""

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        """Decompress one encoded chunk."""

    def flush(self, length: int = 0) -> bytes:
        """Flush buffered decompressed bytes."""


class HttpResponseStream(Protocol):
    """Minimal streaming HTTP response used by safe acquisition."""

    status_code: int
    headers: Mapping[str, str]

    def iter_raw(self) -> Iterator[bytes]:
        """Yield encoded response-body bytes."""

    def close(self) -> None:
        """Close response and connection resources."""


class HttpTransport(Protocol):
    """Transport that connects to one prevalidated address."""

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        timeout_seconds: float,
    ) -> HttpResponseStream:
        """Open one streaming GET request without redirects or ambient credentials."""


@dataclass
class _StdlibHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    response: http.client.HTTPResponse
    connection: http.client.HTTPConnection

    def iter_raw(self) -> Iterator[bytes]:
        while True:
            chunk = self.response.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.connection.close()


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        connect_ip: str,
        *,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._connect_ip = connect_ip
        self._newsrag_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
        )
        try:
            self.sock = self._newsrag_context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


@dataclass(frozen=True)
class PinnedHttpTransport:
    """HTTP/1.1 transport pinned to a previously validated public address."""

    ssl_context: ssl.SSLContext = field(default_factory=ssl.create_default_context)

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        timeout_seconds: float,
    ) -> HttpResponseStream:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None:
            raise AcquisitionError("validation", "URL must include a host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHttpsConnection(
                host,
                connect_ip,
                port=port,
                timeout=timeout_seconds,
                context=self.ssl_context,
            )
        else:
            connection = http.client.HTTPConnection(
                connect_ip,
                port=port,
                timeout=timeout_seconds,
            )

        request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = {
            "Accept-Encoding": "gzip",
            "Host": _host_header(parsed),
            "User-Agent": "NewsRAG",
        }
        try:
            connection.request("GET", request_target, headers=headers)
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        return _StdlibHttpResponse(
            status_code=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
            response=response,
            connection=connection,
        )


class SourceArtifactAcquirer(Protocol):
    """Acquire one explicit source into bounded temporary storage."""

    def acquire(self, request: AcquisitionRequest, staging_dir: Path) -> StagedSourceArtifact:
        """Acquire exact bytes without publishing an artifact record."""


@dataclass(frozen=True)
class SafeSourceArtifactAcquirer:
    """Safely acquire local files and public HTTP(S) resources."""

    limits: AcquisitionLimits = AcquisitionLimits()
    resolver: Resolver = field(default_factory=lambda: _default_resolver)
    transport: HttpTransport = field(default_factory=PinnedHttpTransport)

    def acquire(self, request: AcquisitionRequest, staging_dir: Path) -> StagedSourceArtifact:
        """Acquire one source into a staged exact-byte artifact."""

        if request.kind == SOURCE_KIND_LOCAL_PATH:
            return self._acquire_local(request.reference, staging_dir)
        if request.kind == SOURCE_KIND_URL:
            return self._acquire_url(request.reference, staging_dir)
        raise AcquisitionError("validation", f"Unsupported source kind: {request.kind}")

    def _acquire_local(self, reference: str, staging_dir: Path) -> StagedSourceArtifact:
        submitted_path = _absolute_path(reference)
        try:
            supplied_state = os.lstat(submitted_path)
            resolved_path = submitted_path.resolve(strict=True)
            target_state = _file_state(resolved_path)
        except (OSError, RuntimeError) as exc:
            raise AcquisitionError(
                "local_validation",
                f"Cannot resolve local source {submitted_path} ({type(exc).__name__})",
            ) from exc

        if not stat.S_ISREG(target_state.mode):
            raise AcquisitionError(
                "local_validation",
                f"Local source is not a regular file: {submitted_path}",
            )
        if target_state.size > self.limits.max_local_bytes:
            raise AcquisitionError(
                "local_size_limit",
                f"Local source exceeds {self.limits.max_local_bytes} bytes: {submitted_path}",
            )

        staged_path = _new_staging_path(staging_dir)
        read_started_at = _current_timestamp()
        digest = hashlib.sha256()
        byte_size = 0
        try:
            open_flags = (
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(
                    os,
                    "O_NOFOLLOW",
                    0,
                )
            )
            source_fd = os.open(resolved_path, open_flags)
            try:
                opened_state = _file_state_from_fd(source_fd)
                if opened_state != target_state:
                    raise AcquisitionError(
                        "local_integrity",
                        f"Local source changed before reading: {submitted_path}",
                    )
                with staged_path.open("wb") as output:
                    while True:
                        chunk = os.read(source_fd, _READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        byte_size += len(chunk)
                        if byte_size > self.limits.max_local_bytes:
                            raise AcquisitionError(
                                "local_size_limit",
                                f"Local source exceeds {self.limits.max_local_bytes} bytes: "
                                f"{submitted_path}",
                            )
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                completed_state = _file_state_from_fd(source_fd)
            finally:
                os.close(source_fd)

            current_state = _file_state(resolved_path)
            if (
                completed_state != opened_state
                or current_state != opened_state
                or byte_size != opened_state.size
                or (
                    stat.S_ISLNK(supplied_state.st_mode)
                    and submitted_path.resolve(strict=True) != resolved_path
                )
            ):
                raise AcquisitionError(
                    "local_integrity",
                    f"Local source changed while reading: {submitted_path}",
                )
        except AcquisitionError:
            staged_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            staged_path.unlink(missing_ok=True)
            raise AcquisitionError(
                "local_read",
                f"Cannot read local source {submitted_path} ({type(exc).__name__})",
            ) from exc

        acquired_at = _current_timestamp()
        reported_media_type, _ = mimetypes.guess_type(submitted_path.name)
        return StagedSourceArtifact(
            source_kind=SOURCE_KIND_LOCAL_PATH,
            submitted_reference=str(submitted_path),
            normalized_reference=str(submitted_path),
            resolved_reference=str(resolved_path),
            staged_path=staged_path,
            content_hash=digest.hexdigest(),
            byte_size=byte_size,
            acquired_at=acquired_at,
            reported_media_type=reported_media_type,
            provenance={
                "file_mtime_ns": opened_state.modified_at_ns,
                "file_read_completed_at": acquired_at,
                "file_read_started_at": read_started_at,
                "resolved_path": str(resolved_path),
                "submitted_path": str(submitted_path),
            },
        )

    def _acquire_url(self, reference: str, staging_dir: Path) -> StagedSourceArtifact:
        submitted_url = reference.strip()
        current_url = _without_fragment(submitted_url)
        redirects: list[str] = []

        while True:
            parsed, addresses = _validate_public_url(current_url, self.resolver)
            safe_reference = safe_url_reference(current_url)
            try:
                response = self.transport.get(
                    url=current_url,
                    connect_ip=addresses[0],
                    timeout_seconds=self.limits.timeout_seconds,
                )
            except TimeoutError as exc:
                raise AcquisitionError(
                    "remote_timeout",
                    f"Request timed out for {safe_reference}",
                ) from exc
            except AcquisitionError:
                raise
            except Exception as exc:
                raise AcquisitionError(
                    "remote_request",
                    f"Request failed for {safe_reference} ({type(exc).__name__})",
                ) from exc

            try:
                if response.status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if location is None or not location.strip():
                        raise AcquisitionError(
                            "remote_redirect",
                            f"Redirect is missing a destination for {safe_reference}",
                        )
                    if len(redirects) >= self.limits.max_redirects:
                        raise AcquisitionError(
                            "remote_redirect_limit",
                            f"Source exceeded {self.limits.max_redirects} redirects",
                        )
                    next_url = _without_fragment(urljoin(current_url, location.strip()))
                    if (
                        parsed.scheme.lower() == "https"
                        and urlsplit(next_url).scheme.lower() == "http"
                    ):
                        raise AcquisitionError(
                            "remote_redirect",
                            f"HTTPS downgrade redirect is not allowed for {safe_reference}",
                        )
                    redirects.append(next_url)
                    current_url = next_url
                    continue

                if response.status_code != 200:
                    raise AcquisitionError(
                        "remote_status",
                        f"HTTP status {response.status_code} for {safe_reference}",
                    )

                staged_path, content_hash, byte_size, compressed_byte_size = (
                    self._stage_response_body(response, staging_dir, safe_reference)
                )
                acquired_at = _current_timestamp()
                reported_media_type = _reported_media_type(response.headers)
                return StagedSourceArtifact(
                    source_kind=SOURCE_KIND_URL,
                    submitted_reference=submitted_url,
                    normalized_reference=normalize_url_reference(submitted_url),
                    resolved_reference=current_url,
                    staged_path=staged_path,
                    content_hash=content_hash,
                    byte_size=byte_size,
                    acquired_at=acquired_at,
                    reported_media_type=reported_media_type,
                    provenance={
                        "compressed_byte_size": compressed_byte_size,
                        "content_encoding": _content_encoding(response.headers),
                        "redirects": redirects,
                        "reported_content_length": _content_length(response.headers),
                        "resolved_url": current_url,
                        "retrieved_at": acquired_at,
                        "submitted_url": submitted_url,
                    },
                )
            finally:
                _close_response(response, safe_reference)

    def _stage_response_body(
        self,
        response: HttpResponseStream,
        staging_dir: Path,
        safe_reference: str,
    ) -> tuple[Path, str, int, int]:
        content_length = _content_length(response.headers)
        if content_length is not None and content_length > self.limits.max_compressed_bytes:
            raise AcquisitionError(
                "remote_compressed_size_limit",
                f"Response exceeds {self.limits.max_compressed_bytes} compressed bytes for "
                f"{safe_reference}",
            )

        encoding = _content_encoding(response.headers)
        if encoding not in _SUPPORTED_CONTENT_ENCODINGS:
            raise AcquisitionError(
                "remote_content_encoding",
                f"Unsupported Content-Encoding for {safe_reference}",
            )
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        staged_path = _new_staging_path(staging_dir)
        digest = hashlib.sha256()
        compressed_byte_size = 0
        byte_size = 0
        try:
            with staged_path.open("wb") as output:
                for chunk in response.iter_raw():
                    compressed_byte_size += len(chunk)
                    if compressed_byte_size > self.limits.max_compressed_bytes:
                        raise AcquisitionError(
                            "remote_compressed_size_limit",
                            f"Response exceeds {self.limits.max_compressed_bytes} compressed "
                            f"bytes for {safe_reference}",
                        )
                    decoded = _decode_chunk(
                        decompressor,
                        chunk,
                        remaining=self.limits.max_decompressed_bytes - byte_size,
                        safe_reference=safe_reference,
                        limit=self.limits.max_decompressed_bytes,
                    )
                    byte_size = _write_decoded_chunk(
                        output,
                        digest,
                        decoded,
                        byte_size=byte_size,
                        limit=self.limits.max_decompressed_bytes,
                        safe_reference=safe_reference,
                    )
                if decompressor is not None:
                    decoded = _flush_decoder(
                        decompressor,
                        remaining=self.limits.max_decompressed_bytes - byte_size,
                        safe_reference=safe_reference,
                        limit=self.limits.max_decompressed_bytes,
                    )
                    byte_size = _write_decoded_chunk(
                        output,
                        digest,
                        decoded,
                        byte_size=byte_size,
                        limit=self.limits.max_decompressed_bytes,
                        safe_reference=safe_reference,
                    )
                if content_length is not None and compressed_byte_size != content_length:
                    raise AcquisitionError(
                        "remote_transfer",
                        f"Response length did not match Content-Length for {safe_reference}",
                    )
                output.flush()
                os.fsync(output.fileno())
        except AcquisitionError:
            staged_path.unlink(missing_ok=True)
            raise
        except TimeoutError as exc:
            staged_path.unlink(missing_ok=True)
            raise AcquisitionError(
                "remote_timeout",
                f"Request timed out for {safe_reference}",
            ) from exc
        except (OSError, zlib.error) as exc:
            staged_path.unlink(missing_ok=True)
            raise AcquisitionError(
                "remote_transfer",
                f"Could not read response for {safe_reference} ({type(exc).__name__})",
            ) from exc
        return staged_path, digest.hexdigest(), byte_size, compressed_byte_size


def _close_response(response: HttpResponseStream, safe_reference: str) -> None:
    try:
        response.close()
    except Exception as exc:
        LOGGER.warning(
            "acquisition_response_close_failed source_reference=%r error_type=%s",
            safe_reference,
            type(exc).__name__,
        )


def preserve_staged_artifact(
    artifact: StagedSourceArtifact,
    artifact_dir: Path,
) -> Path:
    """Atomically promote staged bytes into durable content-addressed storage."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    destination = artifact_dir / artifact.content_hash
    if destination.exists():
        if (
            destination.stat().st_size != artifact.byte_size
            or _hash_file(destination) != artifact.content_hash
        ):
            raise AcquisitionError(
                "artifact_integrity",
                f"Stored artifact failed integrity check: {destination}",
            )
        artifact.staged_path.unlink(missing_ok=True)
        return destination

    try:
        artifact.staged_path.replace(destination)
        _fsync_directory(artifact_dir)
    except OSError as exc:
        raise AcquisitionError(
            "artifact_storage",
            f"Could not preserve acquired artifact ({type(exc).__name__})",
        ) from exc
    return destination


def validate_url_submission(url: str) -> str:
    """Validate fields that must never be persisted in a URL job payload."""

    submitted_url = url.strip()
    if not submitted_url or any(
        ord(character) < 32 or ord(character) == 127 for character in submitted_url
    ):
        raise AcquisitionError(
            "remote_validation",
            "URL is empty or contains control characters",
        )
    try:
        parsed = urlsplit(submitted_url)
    except ValueError as exc:
        raise AcquisitionError("remote_validation", "URL is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise AcquisitionError("remote_validation", "URL credentials are not allowed")
    return submitted_url


def safe_url_reference(url: str) -> str:
    """Return a URL suitable for logs and errors without credentials or query data."""

    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "invalid-host"
        port = parsed.port
    except ValueError:
        return "invalid-url"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    authority = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", "", ""))


def _validate_public_url(url: str, resolver: Resolver) -> tuple[SplitResult, tuple[str, ...]]:
    submitted_url = validate_url_submission(url)
    try:
        parsed = urlsplit(submitted_url)
        port = parsed.port
    except ValueError as exc:
        raise AcquisitionError("remote_validation", "URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise AcquisitionError("remote_validation", "Only public HTTP(S) URLs are supported")
    host = parsed.hostname
    if host is None:
        raise AcquisitionError("remote_validation", "URL must include a host")
    normalized_host = host.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise AcquisitionError("remote_destination", "Localhost destinations are not allowed")

    resolved_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        literal_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            addresses = resolver(normalized_host, resolved_port)
        except AcquisitionError:
            raise
        except Exception as exc:
            raise AcquisitionError(
                "remote_resolution",
                f"Could not resolve {safe_url_reference(url)} ({type(exc).__name__})",
            ) from exc
    else:
        addresses = (str(literal_address),)

    if not addresses:
        raise AcquisitionError(
            "remote_resolution",
            f"No addresses resolved for {safe_url_reference(url)}",
        )
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise AcquisitionError(
                "remote_resolution", "Resolver returned an invalid address"
            ) from exc
        if not _is_public_unicast(parsed_address):
            raise AcquisitionError(
                "remote_destination",
                f"Non-public destination is not allowed for {safe_url_reference(url)}",
            )
    return parsed, tuple(dict.fromkeys(addresses))


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


def _absolute_path(reference: str) -> Path:
    if not reference.strip():
        raise AcquisitionError("local_validation", "Local source path is empty")
    return Path(os.path.abspath(os.path.expanduser(reference)))


@dataclass(frozen=True)
class _FileState:
    device: int
    inode: int
    mode: int
    size: int
    modified_at_ns: int


def _file_state(path: Path) -> _FileState:
    return _state_from_stat(os.stat(path))


def _file_state_from_fd(file_descriptor: int) -> _FileState:
    return _state_from_stat(os.fstat(file_descriptor))


def _state_from_stat(value: os.stat_result) -> _FileState:
    return _FileState(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        size=value.st_size,
        modified_at_ns=value.st_mtime_ns,
    )


def _new_staging_path(staging_dir: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix="acquisition-", dir=staging_dir)
    os.close(descriptor)
    return Path(raw_path)


def _decode_chunk(
    decompressor: _Decompressor | None,
    chunk: bytes,
    *,
    remaining: int,
    safe_reference: str,
    limit: int,
) -> bytes:
    if decompressor is None:
        return chunk
    if remaining < 0:
        _raise_decompressed_limit(safe_reference, limit)
    decoded = decompressor.decompress(chunk, remaining + 1)
    if len(decoded) > remaining or decompressor.unconsumed_tail:
        _raise_decompressed_limit(safe_reference, limit)
    if decompressor.unused_data:
        raise AcquisitionError(
            "remote_transfer",
            f"Compressed response contains trailing data for {safe_reference}",
        )
    return decoded


def _flush_decoder(
    decompressor: _Decompressor,
    *,
    remaining: int,
    safe_reference: str,
    limit: int,
) -> bytes:
    if remaining < 0:
        _raise_decompressed_limit(safe_reference, limit)
    decoded = decompressor.flush(remaining + 1)
    if len(decoded) > remaining:
        _raise_decompressed_limit(safe_reference, limit)
    if not decompressor.eof:
        raise AcquisitionError(
            "remote_transfer",
            f"Compressed response ended early for {safe_reference}",
        )
    if decompressor.unused_data:
        raise AcquisitionError(
            "remote_transfer",
            f"Compressed response contains trailing data for {safe_reference}",
        )
    return decoded


def _write_decoded_chunk(
    output: _ByteWriter,
    digest: _Digest,
    decoded: bytes,
    *,
    byte_size: int,
    limit: int,
    safe_reference: str,
) -> int:
    next_size = byte_size + len(decoded)
    if next_size > limit:
        _raise_decompressed_limit(safe_reference, limit)
    if decoded:
        output.write(decoded)
        digest.update(decoded)
    return next_size


def _raise_decompressed_limit(safe_reference: str, limit: int) -> None:
    raise AcquisitionError(
        "remote_decompressed_size_limit",
        f"Response exceeds {limit} decompressed bytes for {safe_reference}",
    )


def _content_encoding(headers: Mapping[str, str]) -> str:
    return headers.get("content-encoding", "").strip().lower()


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw_value = headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise AcquisitionError("remote_headers", "Content-Length is invalid") from exc
    if value < 0:
        raise AcquisitionError("remote_headers", "Content-Length is invalid")
    return value


def _reported_media_type(headers: Mapping[str, str]) -> str | None:
    raw_value = headers.get("content-type")
    if raw_value is None:
        return None
    media_type = raw_value.split(";", maxsplit=1)[0].strip().lower()
    return media_type or None


def _without_fragment(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise AcquisitionError("remote_validation", "URL is malformed") from exc
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _host_header(parsed: SplitResult) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if (
        port is None
        or (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        return host
    return f"{host}:{port}"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _current_timestamp() -> str:
    return datetime.now(UTC).isoformat()

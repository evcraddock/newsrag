from __future__ import annotations

import gzip
import hashlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import newsrag.acquisition as acquisition_module
from newsrag.acquisition import (
    SOURCE_KIND_LOCAL_PATH,
    SOURCE_KIND_URL,
    AcquisitionError,
    AcquisitionLimits,
    AcquisitionRequest,
    SafeSourceArtifactAcquirer,
    preserve_staged_artifact,
)

PUBLIC_IP = "93.184.216.34"


def test_default_acquisition_limits_match_approved_policy() -> None:
    limits = AcquisitionLimits()

    assert limits.max_local_bytes == 250 * 1024 * 1024
    assert limits.max_compressed_bytes == 250 * 1024 * 1024
    assert limits.max_decompressed_bytes == 250 * 1024 * 1024
    assert limits.max_redirects == 5
    assert limits.timeout_seconds == 30.0


def test_local_acquisition_stages_exact_bytes_and_provenance(tmp_path: Path) -> None:
    source_path = tmp_path / "packet.pdf"
    content = b"%PDF-1.4\nlocal"
    source_path.write_bytes(content)
    acquirer = SafeSourceArtifactAcquirer()

    artifact = acquirer.acquire(
        AcquisitionRequest(kind=SOURCE_KIND_LOCAL_PATH, reference=str(source_path)),
        tmp_path / "staging",
    )
    stored_path = preserve_staged_artifact(artifact, tmp_path / "artifacts")

    assert stored_path.read_bytes() == content
    assert stored_path.name == hashlib.sha256(content).hexdigest()
    assert not artifact.staged_path.exists()
    assert artifact.submitted_reference == str(source_path)
    assert artifact.normalized_reference == str(source_path)
    assert artifact.resolved_reference == str(source_path.resolve())
    assert artifact.byte_size == len(content)
    assert artifact.reported_media_type == "application/pdf"
    assert artifact.provenance["submitted_path"] == str(source_path)
    assert artifact.provenance["resolved_path"] == str(source_path.resolve())
    assert isinstance(artifact.provenance["file_mtime_ns"], int)
    assert "file_read_started_at" in artifact.provenance
    assert "file_read_completed_at" in artifact.provenance


def test_explicit_local_symlink_records_submitted_and_resolved_paths(tmp_path: Path) -> None:
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"%PDF-1.4\nsymlink")
    symlink_path = tmp_path / "submitted.pdf"
    symlink_path.symlink_to(target_path)

    artifact = SafeSourceArtifactAcquirer().acquire(
        AcquisitionRequest(kind=SOURCE_KIND_LOCAL_PATH, reference=str(symlink_path)),
        tmp_path / "staging",
    )

    assert artifact.submitted_reference == str(symlink_path)
    assert artifact.normalized_reference == str(symlink_path)
    assert artifact.resolved_reference == str(target_path.resolve())
    assert artifact.staged_path.read_bytes() == target_path.read_bytes()


def test_local_acquisition_rejects_missing_special_oversized_and_changed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = tmp_path / "missing.pdf"
    with pytest.raises(AcquisitionError, match="Cannot resolve local source"):
        SafeSourceArtifactAcquirer().acquire(
            AcquisitionRequest(kind=SOURCE_KIND_LOCAL_PATH, reference=str(missing_path)),
            tmp_path / "staging-missing",
        )

    fifo_path = tmp_path / "special.pdf"
    os.mkfifo(fifo_path)
    with pytest.raises(AcquisitionError, match="not a regular file"):
        SafeSourceArtifactAcquirer().acquire(
            AcquisitionRequest(kind=SOURCE_KIND_LOCAL_PATH, reference=str(fifo_path)),
            tmp_path / "staging-special",
        )

    oversized_path = tmp_path / "oversized.pdf"
    oversized_path.write_bytes(b"12345")
    with pytest.raises(AcquisitionError, match="exceeds 4 bytes"):
        SafeSourceArtifactAcquirer(limits=AcquisitionLimits(max_local_bytes=4)).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_LOCAL_PATH, reference=str(oversized_path)),
            tmp_path / "staging-oversized",
        )

    changed_path = tmp_path / "changed.pdf"
    changed_path.write_bytes(b"stable")
    real_file_state_from_fd = acquisition_module._file_state_from_fd
    calls = 0

    def changed_file_state(file_descriptor: int) -> acquisition_module._FileState:
        nonlocal calls
        calls += 1
        state = real_file_state_from_fd(file_descriptor)
        return replace(state, modified_at_ns=state.modified_at_ns + 1) if calls == 2 else state

    monkeypatch.setattr(acquisition_module, "_file_state_from_fd", changed_file_state)
    with pytest.raises(AcquisitionError, match="changed while reading"):
        SafeSourceArtifactAcquirer().acquire(
            AcquisitionRequest(kind=SOURCE_KIND_LOCAL_PATH, reference=str(changed_path)),
            tmp_path / "staging-changed",
        )
    assert list((tmp_path / "staging-changed").iterdir()) == []


def test_remote_acquisition_revalidates_redirect_and_records_provenance(
    tmp_path: Path,
) -> None:
    first_url = "https://example.gov/start?token=private"
    final_url = "https://cdn.example.gov/packet.pdf"
    content = b"%PDF-1.4\nremote"
    compressed = gzip.compress(content)
    transport = FakeHttpTransport(
        responses={
            first_url: [
                FakeHttpResponse(
                    status_code=302,
                    headers={"location": final_url},
                )
            ],
            final_url: [
                FakeHttpResponse(
                    status_code=200,
                    headers={
                        "content-encoding": "gzip",
                        "content-length": str(len(compressed)),
                        "content-type": "application/pdf; charset=binary",
                    },
                    chunks=(compressed,),
                )
            ],
        }
    )
    resolver = RecordingResolver(
        addresses={"example.gov": (PUBLIC_IP,), "cdn.example.gov": (PUBLIC_IP,)}
    )

    artifact = SafeSourceArtifactAcquirer(
        resolver=resolver,
        transport=transport,
    ).acquire(
        AcquisitionRequest(kind=SOURCE_KIND_URL, reference=first_url),
        tmp_path / "staging",
    )

    assert artifact.staged_path.read_bytes() == content
    assert artifact.content_hash == hashlib.sha256(content).hexdigest()
    assert artifact.reported_media_type == "application/pdf"
    assert artifact.resolved_reference == final_url
    assert artifact.provenance["redirects"] == [final_url]
    assert artifact.provenance["compressed_byte_size"] == len(compressed)
    assert artifact.provenance["reported_content_length"] == len(compressed)
    assert resolver.hosts == ["example.gov", "cdn.example.gov"]
    assert transport.requests == [(first_url, PUBLIC_IP), (final_url, PUBLIC_IP)]
    assert all(
        response.closed for responses in transport.responses.values() for response in responses
    )


@pytest.mark.parametrize(
    ("url", "addresses", "message"),
    [
        ("file:///tmp/packet.pdf", {}, "Only public HTTP"),
        ("https://user:secret@example.gov/packet.pdf", {}, "credentials"),
        ("https://localhost/packet.pdf", {}, "Localhost"),
        ("https://127.0.0.1/packet.pdf", {}, "Non-public"),
        ("https://169.254.1.2/packet.pdf", {}, "Non-public"),
        ("https://224.0.0.1/packet.pdf", {}, "Non-public"),
        ("https://0.0.0.0/packet.pdf", {}, "Non-public"),
        ("https://[::1]/packet.pdf", {}, "Non-public"),
        ("https://[fe80::1]/packet.pdf", {}, "Non-public"),
        (
            "https://internal.example.gov/packet.pdf",
            {"internal.example.gov": ("10.0.0.2",)},
            "Non-public",
        ),
    ],
)
def test_remote_acquisition_rejects_unsafe_destinations_before_request(
    tmp_path: Path,
    url: str,
    addresses: dict[str, tuple[str, ...]],
    message: str,
) -> None:
    transport = FakeHttpTransport()

    with pytest.raises(AcquisitionError, match=message):
        SafeSourceArtifactAcquirer(
            resolver=RecordingResolver(addresses=addresses),
            transport=transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "staging",
        )

    assert transport.requests == []


def test_remote_acquisition_rejects_private_redirect_before_following_it(
    tmp_path: Path,
) -> None:
    first_url = "https://example.gov/packet.pdf"
    private_url = "https://10.0.0.4/internal.pdf"
    transport = FakeHttpTransport(
        responses={
            first_url: [FakeHttpResponse(status_code=302, headers={"location": private_url})]
        }
    )

    with pytest.raises(AcquisitionError, match="Non-public"):
        SafeSourceArtifactAcquirer(
            resolver=RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)}),
            transport=transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=first_url),
            tmp_path / "staging",
        )

    assert transport.requests == [(first_url, PUBLIC_IP)]


def test_remote_acquisition_rejects_https_downgrade_redirect(tmp_path: Path) -> None:
    url = "https://example.gov/packet.pdf"
    transport = FakeHttpTransport(
        responses={
            url: [
                FakeHttpResponse(
                    status_code=302,
                    headers={"location": "http://example.gov/packet.pdf"},
                )
            ]
        }
    )

    with pytest.raises(AcquisitionError, match="HTTPS downgrade"):
        SafeSourceArtifactAcquirer(
            resolver=RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)}),
            transport=transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "staging",
        )

    assert transport.requests == [(url, PUBLIC_IP)]


def test_remote_acquisition_enforces_redirect_limit(tmp_path: Path) -> None:
    first_url = "https://example.gov/first"
    second_url = "https://example.gov/second"
    transport = FakeHttpTransport(
        responses={
            first_url: [FakeHttpResponse(302, {"location": second_url})],
            second_url: [FakeHttpResponse(302, {"location": "/third"})],
        }
    )

    with pytest.raises(AcquisitionError, match="exceeded 1 redirects"):
        SafeSourceArtifactAcquirer(
            limits=AcquisitionLimits(max_redirects=1),
            resolver=RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)}),
            transport=transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=first_url),
            tmp_path / "staging",
        )

    assert transport.requests == [(first_url, PUBLIC_IP), (second_url, PUBLIC_IP)]


def test_remote_acquisition_rejects_partial_response(tmp_path: Path) -> None:
    url = "https://example.gov/packet.pdf"

    with pytest.raises(AcquisitionError, match="HTTP status 206"):
        SafeSourceArtifactAcquirer(
            resolver=RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)}),
            transport=FakeHttpTransport(
                responses={url: [FakeHttpResponse(206, {}, (b"partial",))]}
            ),
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "staging",
        )


def test_remote_acquisition_rejects_unsupported_encoding_and_length_mismatch(
    tmp_path: Path,
) -> None:
    url = "https://example.gov/packet.pdf"
    resolver = RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)})
    with pytest.raises(AcquisitionError, match="Unsupported Content-Encoding"):
        SafeSourceArtifactAcquirer(
            resolver=resolver,
            transport=FakeHttpTransport(
                responses={
                    url: [
                        FakeHttpResponse(
                            200,
                            {"content-encoding": "br"},
                            (b"encoded",),
                        )
                    ]
                }
            ),
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "encoding-staging",
        )

    with pytest.raises(AcquisitionError, match="did not match Content-Length"):
        SafeSourceArtifactAcquirer(
            resolver=resolver,
            transport=FakeHttpTransport(
                responses={url: [FakeHttpResponse(200, {"content-length": "10"}, (b"short",))]}
            ),
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "length-staging",
        )


def test_remote_acquisition_enforces_compressed_and_decompressed_limits(
    tmp_path: Path,
) -> None:
    url = "https://example.gov/packet.pdf"
    resolver = RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)})
    compressed_transport = FakeHttpTransport(
        responses={url: [FakeHttpResponse(status_code=200, headers={}, chunks=(b"12345",))]}
    )
    with pytest.raises(AcquisitionError, match="compressed bytes"):
        SafeSourceArtifactAcquirer(
            limits=AcquisitionLimits(max_compressed_bytes=4),
            resolver=resolver,
            transport=compressed_transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "compressed-staging",
        )

    expanded = b"x" * 20
    encoded = gzip.compress(expanded)
    decompressed_transport = FakeHttpTransport(
        responses={
            url: [
                FakeHttpResponse(
                    status_code=200,
                    headers={"content-encoding": "gzip"},
                    chunks=(encoded,),
                )
            ]
        }
    )
    with pytest.raises(AcquisitionError, match="decompressed bytes"):
        SafeSourceArtifactAcquirer(
            limits=AcquisitionLimits(max_decompressed_bytes=10),
            resolver=resolver,
            transport=decompressed_transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "decompressed-staging",
        )


def test_remote_acquisition_does_not_fetch_secondary_resources(tmp_path: Path) -> None:
    url = "https://example.gov/page"
    content = b'<html><img src="https://other.example/image.png"></html>'
    transport = FakeHttpTransport(
        responses={
            url: [
                FakeHttpResponse(
                    status_code=200,
                    headers={"content-type": "text/html"},
                    chunks=(content,),
                )
            ]
        }
    )

    artifact = SafeSourceArtifactAcquirer(
        resolver=RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)}),
        transport=transport,
    ).acquire(
        AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
        tmp_path / "staging",
    )

    assert artifact.staged_path.read_bytes() == content
    assert transport.requests == [(url, PUBLIC_IP)]


def test_remote_errors_do_not_expose_query_or_response_body(tmp_path: Path) -> None:
    url = "https://example.gov/packet.pdf?token=secret"
    transport = FakeHttpTransport(error=TimeoutError("response body secret"))

    with pytest.raises(AcquisitionError) as raised:
        SafeSourceArtifactAcquirer(
            resolver=RecordingResolver(addresses={"example.gov": (PUBLIC_IP,)}),
            transport=transport,
        ).acquire(
            AcquisitionRequest(kind=SOURCE_KIND_URL, reference=url),
            tmp_path / "staging",
        )

    message = str(raised.value)
    assert "token=secret" not in message
    assert "response body secret" not in message
    assert "https://example.gov/packet.pdf" in message


@dataclass
class FakeHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    chunks: tuple[bytes, ...] = ()
    closed: bool = False

    def iter_raw(self) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeHttpTransport:
    responses: dict[str, list[FakeHttpResponse]] = field(default_factory=dict)
    error: Exception | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        timeout_seconds: float,
    ) -> FakeHttpResponse:
        del timeout_seconds
        self.requests.append((url, connect_ip))
        if self.error is not None:
            raise self.error
        return self.responses[url].pop(0)


@dataclass
class RecordingResolver:
    addresses: dict[str, tuple[str, ...]] = field(default_factory=dict)
    hosts: list[str] = field(default_factory=list)

    def __call__(self, host: str, port: int) -> tuple[str, ...]:
        del port
        self.hosts.append(host)
        return self.addresses.get(host, (PUBLIC_IP,))

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path


def test_macos_artifact_script_builds_versioned_archive(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    uname = bin_dir / "uname"
    uname.write_text(
        """#!/usr/bin/env bash
if [[ "$1" == "-s" ]]; then
    echo Darwin
else
    echo arm64
fi
""",
        encoding="utf-8",
    )
    uname.chmod(0o755)

    uv = bin_dir / "uv"
    uv.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "$1" == "run" && "$2" == "python" ]]; then
    echo 9.8.7
    exit 0
fi
if [[ "$1" == "run" && "$2" == "pyinstaller" ]]; then
    mkdir -p dist/macos-arm64/newsrag
    cat > dist/macos-arm64/newsrag/newsrag <<'SCRIPT'
#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "--version" ]]; then
    echo "newsrag 9.8.7"
    exit 0
fi
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--data-dir" ]]; then
        mkdir -p "$2"
        touch "$2/newsrag.sqlite3"
        exit 0
    fi
    shift
done
exit 1
SCRIPT
    chmod +x dist/macos-arm64/newsrag/newsrag
    exit 0
fi
echo "unexpected uv invocation: $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    script = Path("scripts/build-macos-artifact.sh").resolve()
    result = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    artifact = tmp_path / "dist/newsrag-9.8.7-macos-arm64.tar.gz"
    assert artifact.is_file()
    with tarfile.open(artifact) as archive:
        assert "./newsrag" in archive.getnames()


def test_ci_builds_macos_artifact() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "macos-package:" in workflow
    assert "runs-on: macos-15" in workflow
    assert "./scripts/build-macos-artifact.sh" in workflow
    assert "dist/newsrag-*-macos-arm64.tar.gz" in workflow


def test_release_workflow_publishes_macos_artifact() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "build-macos:" in workflow
    assert "runs-on: macos-15" in workflow
    assert "./scripts/build-macos-artifact.sh" in workflow
    assert "dist/newsrag-*-macos-arm64.tar.gz" in workflow
    assert "needs: [validate, build, build-macos]" in workflow


def test_macos_artifact_script_rejects_unsupported_platform(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uname = bin_dir / "uname"
    uname.write_text("#!/usr/bin/env bash\necho Linux\n", encoding="utf-8")
    uname.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    result = subprocess.run(
        ["/bin/bash", "scripts/build-macos-artifact.sh"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "must be built on Apple Silicon" in result.stderr

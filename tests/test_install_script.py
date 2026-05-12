from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_install_script_runs_when_piped_to_bash(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    git = bin_dir / "git"
    git.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "clone" ]]; then
    mkdir -p "$3/.git"
    exit 0
fi
if [[ "${1:-}" == "-C" ]]; then
    case "${3:-}" in
        fetch|checkout|pull) exit 0 ;;
    esac
fi
echo "unexpected git invocation: $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    git.chmod(0o755)

    uv = bin_dir / "uv"
    uv.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "tool" && "${2:-}" == "install" ]]; then
    exit 0
fi
echo "unexpected uv invocation: $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    env = os.environ.copy()
    cache_home = tmp_path / "cache"
    checkout = cache_home / "newsrag" / "source"
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CACHE_HOME": str(cache_home),
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        }
    )

    script = Path("scripts/install.sh").read_text(encoding="utf-8")
    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"Cloning NewsRAG into {checkout}" in result.stdout
    assert "Installing NewsRAG with uv" in result.stdout
    assert (checkout / ".git").is_dir()

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_install_script_uses_homebrew_formula_when_piped_to_bash(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_path = tmp_path / "brew-invocation"

    brew = bin_dir / "brew"
    brew.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" > "$BREW_INVOCATION_PATH"
""",
        encoding="utf-8",
    )
    brew.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "BREW_INVOCATION_PATH": str(invocation_path),
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
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
    assert "Installing NewsRAG with Homebrew" in result.stdout
    assert invocation_path.read_text(encoding="utf-8").strip() == ("install evcraddock/tap/newsrag")


def test_install_script_reports_missing_homebrew(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)

    script = Path("scripts/install.sh").read_text(encoding="utf-8")
    result = subprocess.run(
        ["/bin/bash"],
        input=script,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "Homebrew is required" in result.stderr

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_dev_process_uses_repo_local_data_dir_by_default(tmp_path: Path) -> None:
    invocation = _run_dev_process(tmp_path)

    assert invocation == "run newsrag --data-dir .newsrag daemon run"


def test_dev_process_honors_data_dir_override(tmp_path: Path) -> None:
    override = tmp_path / "custom-corpus"

    invocation = _run_dev_process(tmp_path, data_dir=override)

    assert invocation == f"run newsrag --data-dir {override} daemon run"


def test_repo_local_dev_data_is_gitignored() -> None:
    ignored_paths = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".newsrag/" in ignored_paths


def _run_dev_process(tmp_path: Path, *, data_dir: Path | None = None) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_path = tmp_path / "uv-invocation"
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "$UV_INVOCATION_PATH"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)

    process_line = Path("Procfile.dev").read_text(encoding="utf-8").strip()
    _, command = process_line.split(": ", maxsplit=1)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "UV_INVOCATION_PATH": str(invocation_path),
        }
    )
    if data_dir is None:
        env.pop("NEWSRAG_DATA_DIR", None)
    else:
        env["NEWSRAG_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        ["/bin/bash", "-c", command],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return invocation_path.read_text(encoding="utf-8").strip()

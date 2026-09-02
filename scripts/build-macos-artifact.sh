#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "The NewsRAG macOS artifact must be built on Apple Silicon." >&2
    exit 1
fi

command -v uv >/dev/null 2>&1 || {
    echo "Missing required command: uv" >&2
    exit 1
}

version="$(uv run python -c 'from newsrag import __version__; print(__version__)')"
build_root="build/pyinstaller-macos-arm64"
bundle_root="dist/macos-arm64"
bundle_dir="$bundle_root/newsrag"
artifact="dist/newsrag-${version}-macos-arm64.tar.gz"

rm -rf "$build_root" "$bundle_root" "$artifact"

uv run pyinstaller \
    --noconfirm \
    --clean \
    --onedir \
    --name newsrag \
    --distpath "$bundle_root" \
    --workpath "$build_root" \
    --specpath "$build_root" \
    --collect-binaries lancedb \
    --collect-data lancedb \
    scripts/newsrag_pyinstaller_entrypoint.py

"$bundle_dir/newsrag" --version
smoke_root="$(mktemp -d)"
trap 'rm -rf "$smoke_root"' EXIT
smoke_data_dir="$smoke_root/data"
"$bundle_dir/newsrag" --data-dir "$smoke_data_dir" status --initialize >/dev/null
test -f "$smoke_data_dir/newsrag.sqlite3"

/usr/bin/tar -czf "$artifact" -C "$bundle_dir" .
shasum -a 256 "$artifact"
echo "Built $artifact"

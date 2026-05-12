#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NEWSRAG_REPO_URL:-https://github.com/evcraddock/newsrag.git}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
INSTALL_DIR="${NEWSRAG_INSTALL_DIR:-$CACHE_HOME/newsrag/source}"
REF="${NEWSRAG_REF:-main}"

log() { printf '%s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

require_command git
require_command uv

source_dir=""
script_path="${BASH_SOURCE[0]-}"
if [[ -n "$script_path" && -f "$script_path" ]]; then
    candidate_dir="$(cd "$(dirname "$script_path")/.." && pwd)"
    if [[ -f "$candidate_dir/pyproject.toml" && -d "$candidate_dir/newsrag" ]]; then
        source_dir="$candidate_dir"
    fi
fi

if [[ -n "$source_dir" ]]; then
    log "Installing NewsRAG from source at $source_dir"
else
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        log "Updating NewsRAG checkout at $INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch --tags origin
    else
        if [[ -e "$INSTALL_DIR" ]]; then
            die "$INSTALL_DIR exists but is not a git checkout. Move it aside or set NEWSRAG_INSTALL_DIR."
        fi
        log "Cloning NewsRAG into $INSTALL_DIR"
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi

    git -C "$INSTALL_DIR" checkout "$REF"
    git -C "$INSTALL_DIR" pull --ff-only origin "$REF" 2>/dev/null || true
    source_dir="$INSTALL_DIR"
fi

log "Installing NewsRAG with uv"
uv tool install --force "$source_dir"

if command -v newsrag >/dev/null 2>&1; then
    log "Installed $(newsrag --version 2>/dev/null || printf 'newsrag')"
else
    log "Installed newsrag. Ensure uv's tool bin directory is on PATH."
fi

log "Done. Try: newsrag --help"

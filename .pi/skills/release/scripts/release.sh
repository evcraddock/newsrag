#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

die() { echo -e "${RED}Error: $1${NC}" >&2; exit 1; }
info() { echo -e "${GREEN}$1${NC}"; }
warn() { echo -e "${YELLOW}$1${NC}"; }

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <version>"
  echo "Example: $0 0.2.0"
  exit 1
fi

NEW_VERSION="$1"
NEW_TAG="v${NEW_VERSION}"

if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$'; then
  die "Invalid version format: $NEW_VERSION (expected X.Y.Z or X.Y.Z-suffix)"
fi

command -v python3 >/dev/null 2>&1 || die "Missing required command: python3"
command -v uv >/dev/null 2>&1 || die "Missing required command: uv"

CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
  die "Must be on main branch (currently on '$CURRENT_BRANCH')"
fi

git fetch origin main --quiet
UNPUSHED=$(git log origin/main..HEAD --oneline)
if [[ -n "$UNPUSHED" ]]; then
  die "Unpushed commits on main:\n$UNPUSHED\n\nPush these first."
fi

if git tag --list | grep -q "^${NEW_TAG}$"; then
  die "Tag $NEW_TAG already exists locally"
fi
if git ls-remote --tags origin | grep -q "refs/tags/${NEW_TAG}$"; then
  die "Tag $NEW_TAG already exists on remote"
fi

if [[ ! -f CHANGELOG.md ]]; then
  die "CHANGELOG.md is required and must be updated before release"
fi

info "Releasing: $NEW_TAG"

python3 - "$NEW_VERSION" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

version = sys.argv[1]
path = Path("pyproject.toml")
text = path.read_text(encoding="utf-8")
pattern = r'(?m)^(version\s*=\s*)"[^"]+"'
if re.search(pattern, text) is None:
    raise SystemExit("project.version not found in pyproject.toml")
updated = re.sub(pattern, rf'\1"{version}"', text, count=1)
path.write_text(updated, encoding="utf-8")
PY

uv lock

git add pyproject.toml uv.lock CHANGELOG.md

if git diff --cached --quiet; then
  warn "No version or changelog changes to commit"
else
  git commit -m "chore: release v${NEW_VERSION}"
  info "Committed release v${NEW_VERSION}"
fi

git tag -a "$NEW_TAG" -m "Release $NEW_TAG"
info "Created tag: $NEW_TAG"

info "Pushing to origin..."
git push origin main --follow-tags

info "Verifying tag on remote..."
sleep 2
if ! git ls-remote --tags origin | grep -q "refs/tags/${NEW_TAG}$"; then
  die "Tag $NEW_TAG was NOT pushed to remote. Manually push with: git push origin $NEW_TAG"
fi

info ""
info "✅ Released $NEW_TAG"
info "✅ Tag verified on remote"
info ""
info "GitHub Actions will create or update the GitHub Release from the tag."
info "Monitor with: gh run list --workflow Release --limit 1"

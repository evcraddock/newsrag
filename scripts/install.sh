#!/usr/bin/env bash
set -euo pipefail

FORMULA="${NEWSRAG_HOMEBREW_FORMULA:-evcraddock/tap/newsrag}"

if ! command -v brew >/dev/null 2>&1; then
    printf '%s\n' "Error: Homebrew is required to install NewsRAG on macOS." >&2
    printf '%s\n' "Install Homebrew from https://brew.sh and rerun this command." >&2
    exit 1
fi

printf '%s\n' "Installing NewsRAG with Homebrew"
brew install "$FORMULA"
printf '%s\n' "Installed NewsRAG. Run: newsrag doctor"

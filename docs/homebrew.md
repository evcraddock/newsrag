# Homebrew packaging

NewsRAG is distributed on Apple Silicon macOS through the `evcraddock/homebrew-tap` repository. The formula installs a self-contained NewsRAG application artifact because LanceDB does not publish a source distribution compatible with Homebrew's standard Python virtual environment helper.

## Packaging model

The upstream release workflow builds `newsrag-<version>-macos-arm64.tar.gz` on GitHub's Apple Silicon macOS runner. The archive contains the NewsRAG executable, Python runtime, and Python dependencies. The Homebrew formula installs that archive under the formula's `libexec` directory and links the `newsrag` executable into Homebrew's `bin` directory.

Native commands invoked by NewsRAG remain formula dependencies:

- SQLite
- OCRmyPDF
- Tesseract
- Ghostscript
- qpdf

Ollama is optional. The formula must not install Ollama or download an embedding model during installation.

## Build and validate an artifact

Run the packaging script on Apple Silicon macOS:

```bash
uv sync --locked --dev
./scripts/build-macos-artifact.sh
```

The script creates and smoke-tests `dist/newsrag-<version>-macos-arm64.tar.gz`. It verifies both `newsrag --version` and storage initialization before creating the archive.

## Update the formula for a release

After the NewsRAG GitHub release workflow publishes the macOS archive:

1. Download the release artifact and calculate its checksum:

   ```bash
   curl -LO https://github.com/evcraddock/newsrag/releases/download/v<VERSION>/newsrag-<VERSION>-macos-arm64.tar.gz
   shasum -a 256 newsrag-<VERSION>-macos-arm64.tar.gz
   ```

2. In `evcraddock/homebrew-tap`, create a branch and update `Formula/newsrag.rb` with the new immutable release URL and SHA-256 checksum.
3. Run the local formula checks:

   ```bash
   brew style --formula evcraddock/tap/newsrag
   brew audit --strict --online evcraddock/tap/newsrag
   brew install --build-from-source evcraddock/tap/newsrag
   brew test evcraddock/tap/newsrag
   ```

4. Open a pull request in the tap. The tap's formula checks workflow runs style, strict audit, installation, and functional tests on supported macOS runners.
5. Merge the formula only after both macOS checks pass.

Never point the formula at `main`, a branch archive, or another mutable URL. Formula updates must follow a completed stable NewsRAG release.

## User commands

```bash
brew install evcraddock/tap/newsrag
brew upgrade newsrag
brew uninstall newsrag
```

Uninstalling the formula does not remove the user's corpus under `~/.local/share/newsrag`.

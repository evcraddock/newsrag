---
name: release
description: Release newsrag. Use when user says "release newsrag", "create a release", "new version", "bump version", "ship it", "cut a release", or similar.
---

# Release newsrag

Guide the release process through conversation. Draft a changelog collaboratively, bump the project version, tag, and push. GitHub Actions creates or updates the GitHub Release from the pushed tag.

## Quick Reference

```bash
.pi/skills/release/scripts/release.sh <version>  # e.g. 0.2.0
```

## Workflow

### 1. Pre-flight

Verify readiness — stop and report if any check fails.

```bash
git branch --show-current          # must be main
git fetch origin main
git log origin/main..HEAD --oneline  # must be empty
gh pr list --state open --json number,title  # warn if any open
make check
uv build
```

### 2. Determine current version

The source of truth is `project.version` in `pyproject.toml`.

```bash
python - <<'PY'
import tomllib
from pathlib import Path
print(tomllib.loads(Path('pyproject.toml').read_text())['project']['version'])
PY
```

Also check the latest release tag:

```bash
git tag --list 'v*' --sort=-v:refname | head -1
```

### 3. Gather changes

Collect changes since the last tag:

```bash
LATEST_TAG=$(git tag --list 'v*' --sort=-v:refname | head -1)
git log ${LATEST_TAG:-HEAD}..HEAD --oneline --no-merges
```

Cross-reference task IDs from commit messages where present.

### 4. Recommend version bump

Analyze commit prefixes:
- `feat!:` or `BREAKING CHANGE:` → major
- `feat:` → minor
- `fix:` → patch

Present recommendation with reasoning. User confirms or overrides.

### 5. Draft changelog conversationally

If `CHANGELOG.md` exists, update it. If it does not exist, create it with a Keep a Changelog style header. Do not run the release script until the user approves the changelog.

### 6. Run release script

Once the changelog is approved:

```bash
.pi/skills/release/scripts/release.sh <version>
```

The script handles:
1. Validate on `main` with no unpushed commits.
2. Update `project.version` in `pyproject.toml`.
3. Refresh `uv.lock` metadata.
4. Commit version/changelog artifacts.
5. Create annotated tag `v<version>`.
6. Push commit and tag.
7. Verify tag exists on remote.

The tag push triggers `.github/workflows/release.yml`, which validates the tag, runs checks, builds the Python distribution artifacts, extracts the matching `CHANGELOG.md` section, and creates or updates the GitHub Release with the built artifacts.

### 7. Post-release

After the script succeeds, verify the release workflow:

```bash
gh run list --workflow Release --limit 1 --json databaseId,status,conclusion,event,headBranch
```

If needed for an existing tag, manually dispatch the release workflow with the tag name.

## Important

- Never force push or use force flags.
- Never run the release script without user approval of the changelog.
- If anything fails, stop and report; do not retry blindly.

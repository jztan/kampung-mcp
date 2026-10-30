#!/usr/bin/env bash
# The one pip-audit invocation, used by:
#   - .github/workflows/ci.yml
#   - .github/workflows/dependency-audit.yml (weekly, and on lockfile changes)
#   - .github/workflows/publish-pypi.yml (before anything is published)
#   - scripts/release.py (preflight)
#
# Audits the LOCKED third-party dependencies of both packages (what the image
# and `uv tool install kampung-mcp` actually ship), not the ambient Python:
# a uv venv has no pip, so a bare `pip-audit` would audit whatever global
# interpreter is on PATH. Every exported line is pinned, so --no-deps
# --disable-pip checks exactly those versions without resolving anything.
#
# Queries osv.dev rather than pip-audit's default PyPI advisory service,
# which has carried incomplete affected ranges for some advisories.
#
# Blind spot: an exported line with an environment marker that does not match
# the interpreter running this (for example `sys_platform == 'win32'`) is
# skipped, not audited.
#
# Keep any ignore list here (pass --ignore-vuln ID), so local preflight and CI
# cannot drift. There are currently no ignored vulnerabilities.
set -euo pipefail

cd "$(dirname "$0")/.."
req="$(mktemp)"
trap 'rm -f "$req"' EXIT
uv export --frozen --no-dev --no-emit-workspace --all-packages \
  --no-hashes --format requirements-txt --quiet -o "$req"

exec uvx --quiet pip-audit==2.10.1 \
  --vulnerability-service osv \
  --requirement "$req" --no-deps --disable-pip --strict \
  --progress-spinner off "$@"

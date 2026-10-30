<!-- Thanks for contributing! See docs/contributing.md for setup and conventions.
     For features or non-trivial changes, please open an issue first so the
     approach can be discussed before you write code. Small bug fixes and doc
     typos can skip this. -->

## Summary

<!-- What this changes and why. Link the issue it addresses: Fixes #N -->

## Changes

-

## Testing

<!-- Commands you ran and their results, and any tests you added. -->

## Checklist

- [ ] Linked to an issue above, or this is a small fix that doesn't need one
- [ ] `uv run pytest` passes (and `KAMPUNG_DOCKER_TESTS=1 uv run pytest -m docker` if the image or compose changed)
- [ ] `uv run black --check .`, `uv run flake8 packages scripts` and `uv run mypy packages/server/src packages/client/src` are clean
- [ ] `CHANGELOG.md` [Unreleased] has a bullet for changes a server operator or agent user would notice
- [ ] No manual version bumps: `pyproject.toml`, `server.json` and changelog version headers are managed by the release script
- [ ] If `limits.py` changed, the Limits table in `SECURITY.md` matches
- [ ] If an admin template or `admin.css` changed, it was checked in a browser at the widths in docs/contributing.md, light and dark

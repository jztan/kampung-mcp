import json
from pathlib import Path

import pytest

import release

ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path, version: str = "0.1.0") -> Path:
    for rel in release.PYPROJECTS:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    for rel in release.VERSION_MODULES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'"""Doc."""\n\n__version__ = "{version}"\n')
    (tmp_path / release.SERVER_JSON).write_text(
        json.dumps({"version": version, "packages": [{"version": version}]})
    )
    return tmp_path


def test_the_real_repo_has_one_consistent_version() -> None:
    assert release.get_current_version(ROOT)


def test_bump_writes_every_version(tmp_path: Path) -> None:
    root = _project(tmp_path)
    release.bump_versions(root, "0.2.0", dry_run=False)
    assert set(release.read_versions(root).values()) == {"0.2.0"}
    assert len(release.read_versions(root)) == 6


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    release.bump_versions(root, "0.2.0", dry_run=True)
    assert set(release.read_versions(root).values()) == {"0.1.0"}


def test_disagreeing_versions_stop_the_release(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / release.VERSION_MODULES[1]).write_text('__version__ = "0.0.9"\n')
    with pytest.raises(ValueError, match="disagree"):
        release.get_current_version(root)


@pytest.mark.parametrize(
    "bump,want", [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")]
)
def test_calculate_new_version(bump: str, want: str) -> None:
    assert release.calculate_new_version("1.2.3", bump) == want


CHANGELOG = """# Changelog

## [Unreleased]

### Added

- A thing.
"""


def test_stamp_changelog_moves_unreleased_under_the_version() -> None:
    out = release.stamp_changelog(CHANGELOG, "0.2.0", "2026-10-30")
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-10-30\n\n### Added" in out
    assert release.version_body(out, "0.2.0") == "### Added\n\n- A thing."
    assert out.rstrip().endswith(
        "[0.2.0]: https://github.com/jztan/kampung-mcp/releases/tag/v0.2.0"
    )


def test_an_empty_unreleased_section_stops_the_release() -> None:
    with pytest.raises(ValueError, match="empty"):
        release.stamp_changelog("## [Unreleased]\n\n### Added\n", "0.2.0", "d")


def test_the_real_changelog_can_be_stamped() -> None:
    text = (ROOT / "CHANGELOG.md").read_text()
    body = release.unreleased_body(text) or ""
    if all(not line.strip() or line.startswith("#") for line in body.split("\n")):
        pytest.skip("nothing unreleased: a release commit has just stamped it")
    release.stamp_changelog(text, "9.9.9", "2026-10-30")


def test_client_readme_links_and_images_become_absolute() -> None:
    out = release.render_client_readme(
        '[a](./docs/x.md) [b](SECURITY.md) [c](docs/y.md#z) <img src="docs/i.png">'
    )
    assert f"[a]({release.BLOB_URL}docs/x.md)" in out
    assert f"[b]({release.BLOB_URL}SECURITY.md)" in out
    assert f"[c]({release.BLOB_URL}docs/y.md#z)" in out
    assert f'<img src="{release.RAW_URL}docs/i.png">' in out


def test_client_readme_leaves_anchors_and_full_urls_alone() -> None:
    text = "[a](#license) [b](https://example.com/docs/x.md)"
    assert release.render_client_readme(text) == text


def test_sync_writes_the_client_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[a](docs/x.md)\n", encoding="utf-8")
    target = tmp_path / release.CLIENT_README
    target.parent.mkdir(parents=True)
    release.sync_client_readme(tmp_path, dry_run=False)
    assert target.read_text(encoding="utf-8") == (f"[a]({release.BLOB_URL}docs/x.md)\n")


def test_contributors_are_kept_verbatim_with_wrapped_lines() -> None:
    section = (
        "### Fixed\n\n- A bug.\n\n### Contributors\n\n"
        "- @alice reported the crash\n  ([#3](https://x/3))\n- @bob: tested it\n"
    )
    body, ack = release.split_contributors(section)
    assert "Contributors" not in body and "A bug." in body
    assert "Thanks to **@alice**" in ack and "([#3](https://x/3))" in ack
    assert "Thanks to **@bob**" in ack


def test_unparseable_contributors_stop_the_release() -> None:
    with pytest.raises(ValueError):
        release.split_contributors("### Contributors\n\nthanks everyone\n")


def test_notes_file_checks(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    release.write_notes_file(path, "v0.2.0: Big", "Thanks to **@alice** ...")
    ack = "Thanks to **@alice** for contributing:\n- x"
    assert release.load_approved_notes(path, "0.2.0", ack)[0] == "v0.2.0: Big"
    with pytest.raises(release.NotesFileError, match="titled"):
        release.load_approved_notes(path, "0.2.1", ack)
    release.write_notes_file(path, "v0.2.0: Big", "no credits here")
    with pytest.raises(release.NotesFileError, match="credits"):
        release.load_approved_notes(path, "0.2.0", ack)


def test_release_body_has_the_install_lines() -> None:
    body = release.build_release_body("## Highlights\n\nNew.", "", "0.2.0")
    assert "uv tool install kampung-mcp==0.2.0" in body
    assert "KAMPUNG_IMAGE_TAG=0.2.0" in body


def test_parse_title() -> None:
    assert release.parse_title("TITLE: v1.0.0: Hi\nbody", "v1.0.0") == (
        "v1.0.0: Hi",
        "body",
    )
    assert release.parse_title("no title", "v1.0.0") == ("v1.0.0", "no title")


@pytest.mark.parametrize(
    "current,wanted,ok",
    [
        ("0.1.0", "0.1.0", True),  # the first release ships the version as is
        ("0.1.0", "0.2.0", True),
        ("0.1.0", "1.0.0", True),
        ("0.2.0", "0.1.0", False),  # never backwards
        ("0.1.0", "0.1", False),
        ("0.1.0", "v0.1.0", False),
    ],
)
def test_explicit_version(current: str, wanted: str, ok: bool) -> None:
    if ok:
        assert release.explicit_version(current, wanted, set()) == wanted
    else:
        with pytest.raises(ValueError):
            release.explicit_version(current, wanted, set())


def test_explicit_version_refuses_an_existing_tag() -> None:
    with pytest.raises(ValueError, match="already"):
        release.explicit_version("0.1.0", "0.1.0", {"v0.1.0"})


@pytest.mark.parametrize(
    "listing,want",
    [
        ("origin\tgit@github.com:jztan/kampung-mcp.git (fetch)\n", "origin"),
        (
            "fork\tgit@github.com:someone/kampung-mcp.git (fetch)\n"
            "upstream\thttps://github.com/jztan/kampung-mcp (fetch)\n",
            "upstream",
        ),
        ("kampung-mcp\tgit@github.com:jztan/kampung-mcp.git (push)\n", "kampung-mcp"),
    ],
)
def test_find_remote_by_url(listing: str, want: str) -> None:
    assert release.find_remote(listing) == want


def test_find_remote_refuses_when_none_points_at_the_repo() -> None:
    with pytest.raises(ValueError, match="jztan/kampung-mcp"):
        release.find_remote("origin\tgit@github.com:jztan/other.git (fetch)\n")

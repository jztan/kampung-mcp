import pytest

import upgrade_rehearsal as ur


def test_previous_tag_picks_the_highest_release_by_version() -> None:
    tags = ["v0.2.2", "v0.10.0", "v0.9.1", "launch-test", "v1.0.0-rc1", "v0.2.10"]
    assert ur.previous_tag(tags) == "v0.10.0"


def test_previous_tag_needs_a_release() -> None:
    with pytest.raises(ValueError):
        ur.previous_tag(["launch-test", "v1.0.0-rc1"])


def test_parse_key_takes_the_key_line() -> None:
    out = "some warning\nkmp_AbC-123_xyz\n"
    assert ur.parse_key(out) == "kmp_AbC-123_xyz"


def test_parse_key_refuses_output_without_a_key() -> None:
    with pytest.raises(ValueError):
        ur.parse_key("error: name taken\n")


def test_version_of_tag_drops_the_v() -> None:
    assert ur.version_of("v0.2.2") == "0.2.2"

import pytest

from kampung_server.addressing import Address, parse_address, validate_name
from kampung_server.errors import KampungError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("laptop/api", Address("laptop", "api", None)),
        ("desktop/blog#a1", Address("desktop", "blog", "a1")),
    ],
)
def test_parse_address_valid(raw: str, expected: Address) -> None:
    assert parse_address(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "laptop",
        "laptop/",
        "/api",
        "laptop/api#",
        "laptop/api#zz",
        "laptop/../api",
        "a/b/c",
        "laptop/api#a1#b2",
        "",
    ],
)
def test_parse_address_invalid(raw: str) -> None:
    with pytest.raises(KampungError) as e:
        parse_address(raw)
    assert e.value.code == "invalid_address"


def test_str_round_trip() -> None:
    assert str(parse_address("desktop/blog#a1")) == "desktop/blog#a1"
    assert str(parse_address("laptop/api")) == "laptop/api"


@pytest.mark.parametrize("bad", [".hidden", "-x", "a" * 65, "", "a b", "x/y"])
def test_validate_name_rejects(bad: str) -> None:
    with pytest.raises(KampungError) as e:
        validate_name("repo", bad)
    assert e.value.code == "invalid_name"
    assert "repo" in e.value.hint


def test_validate_name_accepts() -> None:
    assert validate_name("machine", "laptop-2.home_x") == "laptop-2.home_x"


def test_error_to_dict() -> None:
    assert KampungError("x", "y").to_dict() == {"error": "x", "hint": "y"}

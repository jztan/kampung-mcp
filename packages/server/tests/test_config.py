import pytest

from kampung_server.config import Settings, check_public_url
from kampung_server.errors import KampungError


def test_public_url_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAMPUNG_PUBLIC_URL", raising=False)
    assert Settings.from_env().public_url is None


def test_public_url_empty_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMPUNG_PUBLIC_URL", "")
    assert Settings.from_env().public_url is None


def test_public_url_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMPUNG_PUBLIC_URL", "https://kampung.example.com")
    assert Settings.from_env().public_url == "https://kampung.example.com"


@pytest.mark.parametrize(
    "bad",
    [
        "http://kampung.example.com",
        "https://Kampung.example.com",
        "https://kampung.example.com/",
        "https://kampung.example.com/mcp",
        "https://kampung.example.com:8443",
        "https://kampung.example.com?x=1",
    ],
)
def test_check_public_url_refuses(bad: str) -> None:
    with pytest.raises(KampungError) as e:
        check_public_url(bad)
    assert e.value.code == "invalid_public_url"


def test_check_public_url_accepts() -> None:
    url = "https://kampung.example.com"
    assert check_public_url(url) == url


def test_from_env_refuses_a_bad_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMPUNG_PUBLIC_URL", "http://kampung.example.com")
    with pytest.raises(KampungError):
        Settings.from_env()

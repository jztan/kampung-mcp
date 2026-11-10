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


# Loop guard settings


def test_guard_settings_defaults() -> None:
    from datetime import timedelta

    from kampung_server.guard import GuardSettings

    g = GuardSettings.from_env({})
    assert (g.pair_size, g.pair_refill) == (9, timedelta(minutes=10))
    assert (g.mailbox_budget, g.connector_cooldown) == (30, timedelta(minutes=10))
    assert g == GuardSettings()


def test_guard_settings_from_env() -> None:
    from datetime import timedelta

    from kampung_server.guard import GuardSettings

    g = GuardSettings.from_env(
        {
            "KAMPUNG_PAIR_WAKE_SIZE": "5",
            "KAMPUNG_PAIR_WAKE_REFILL_MINUTES": "3",
            "KAMPUNG_MAILBOX_WAKE_BUDGET": "12",
            "KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES": "20",
        }
    )
    assert g.pair_size == 5 and g.pair_refill == timedelta(minutes=3)
    assert g.mailbox_budget == 12 and g.connector_cooldown == timedelta(minutes=20)


@pytest.mark.parametrize(
    "name, value",
    [
        ("KAMPUNG_PAIR_WAKE_SIZE", "0"),
        ("KAMPUNG_PAIR_WAKE_SIZE", "101"),
        ("KAMPUNG_PAIR_WAKE_SIZE", "abc"),
        ("KAMPUNG_PAIR_WAKE_SIZE", "-1"),
        ("KAMPUNG_PAIR_WAKE_REFILL_MINUTES", "0"),
        ("KAMPUNG_PAIR_WAKE_REFILL_MINUTES", "1441"),
        ("KAMPUNG_MAILBOX_WAKE_BUDGET", "1001"),
        ("KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES", "0"),
    ],
)
def test_guard_settings_rejects(name: str, value: str) -> None:
    from kampung_server.guard import GuardSettings

    with pytest.raises(ValueError) as e:
        GuardSettings.from_env({name: value})
    assert name in str(e.value)


def test_settings_carry_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMPUNG_PAIR_WAKE_SIZE", "4")
    assert Settings.from_env().guard.pair_size == 4

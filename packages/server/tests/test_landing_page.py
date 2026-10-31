"""The landing page quotes Kampung's wake and envelope strings; keep them in sync."""

from pathlib import Path

import pytest

from kampung_client import hook
from kampung_server import mail

PAGE = Path(__file__).resolve().parents[3] / "pages" / "index.html"


@pytest.fixture(scope="module")
def page() -> str:
    assert PAGE.is_file(), f"landing page missing at {PAGE}"
    return PAGE.read_text(encoding="utf-8")


def test_page_quotes_the_stop_reason_for_one_message(page: str) -> None:
    assert hook.STOP_REASON.format(n=1) in page


def test_page_quotes_the_stop_block_reason(page: str) -> None:
    assert hook.STOP_BLOCK_REASON.format(n=1) in page


def test_page_quotes_the_codex_wake_prefix(page: str) -> None:
    assert f'"{hook.WAKE_PREFIX}"' in page


def test_page_quotes_the_envelope_kind(page: str) -> None:
    assert mail.KIND in page


def test_page_quotes_the_peers_kind(page: str) -> None:
    assert mail.PEERS_KIND in page


def test_page_has_no_em_dash(page: str) -> None:
    assert chr(0x2014) not in page

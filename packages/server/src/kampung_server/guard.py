"""The loop guard: wake budgets per pair of mailboxes and per mailbox.

A pair of mailboxes may wake each other `pair_size` times in a row; one wake
comes back every `pair_refill`. A mailbox may send `mailbox_budget` waking
messages until a human refills it (a typed prompt, the dashboard, or a
waking message from the owner's Claude app). Mail past either budget is
stored and delivered; only the wake is held, so a runaway loop stops at the
first held message.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta


def _int_setting(env: Mapping[str, str], name: str, default: int, top: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= top:
        raise ValueError(f"{name} must be a whole number from 1 to {top}; got {raw!r}.")
    return value


@dataclass(frozen=True)
class GuardSettings:
    pair_size: int = 9
    pair_refill: timedelta = timedelta(minutes=10)
    mailbox_budget: int = 30
    connector_cooldown: timedelta = timedelta(minutes=10)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GuardSettings":
        return cls(
            pair_size=_int_setting(env, "KAMPUNG_PAIR_WAKE_SIZE", 9, 100),
            pair_refill=timedelta(
                minutes=_int_setting(env, "KAMPUNG_PAIR_WAKE_REFILL_MINUTES", 10, 1440)
            ),
            mailbox_budget=_int_setting(env, "KAMPUNG_MAILBOX_WAKE_BUDGET", 30, 1000),
            connector_cooldown=timedelta(
                minutes=_int_setting(
                    env, "KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES", 10, 1440
                )
            ),
        )

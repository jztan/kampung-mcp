"""What the HTTP layer needs: where the store is, the clock, the send limiter
and the in-memory index of key hashes."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db
from .keyindex import KeyIndex
from .ratelimit import RateLimiter


@dataclass
class ServerDeps:
    db_path: Path
    clock: Callable[[], datetime]
    limiter: RateLimiter
    admin_token: str | None = None
    keys: KeyIndex = field(init=False)

    def __post_init__(self) -> None:
        self.keys = KeyIndex(self.connect)

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

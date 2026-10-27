"""What the HTTP layer needs: where the store is, the clock, the send limiter."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db
from .ratelimit import RateLimiter


@dataclass
class ServerDeps:
    db_path: Path
    clock: Callable[[], datetime]
    limiter: RateLimiter

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

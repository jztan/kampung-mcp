"""Hashes of active machine keys, held in memory so a wrong key costs nothing.

A flood of bad keys would otherwise open SQLite once per request and tie up
the thread pool. A key in the index is still checked against the database,
so revocation takes effect at once; a new key may wait up to MAX_AGE seconds.
"""

import sqlite3
import threading
import time
from collections.abc import Callable

from .identity import authenticate
from .keys import hash_key
from .models import Machine

MAX_AGE = 1.0


class KeyIndex:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connect
        self._monotonic = monotonic
        self._hashes: frozenset[bytes] = frozenset()
        self._loaded_at: float | None = None
        self._lock = threading.Lock()

    def is_unknown(self, key: str) -> bool:
        """True when the key is surely wrong; never touches the database."""
        return self._fresh() and hash_key(key) not in self._hashes

    def is_known(self, key: str) -> bool:
        """True when the key was active at the last load; never blocks.

        Used to let a valid key past the per-address throttle. A key revoked
        since the last load still passes here, then fails the real check."""
        return hash_key(key) in self._hashes

    def lookup(self, key: str) -> Machine | None:
        """The key's machine; blocking, so run it in the thread pool."""
        conn = self._connect()
        try:
            if hash_key(key) not in self._hashes:
                self._reload(conn)
                if hash_key(key) not in self._hashes:
                    return None
            return authenticate(conn, key)
        finally:
            conn.close()

    def _fresh(self) -> bool:
        loaded = self._loaded_at
        return loaded is not None and self._monotonic() - loaded < MAX_AGE

    def _reload(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if self._fresh():
                return
            rows = conn.execute(
                "SELECT key_hash FROM machines WHERE revoked_at IS NULL"
            ).fetchall()
            self._hashes = frozenset(bytes(r[0]) for r in rows)
            self._loaded_at = self._monotonic()

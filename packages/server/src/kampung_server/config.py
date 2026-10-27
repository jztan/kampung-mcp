"""Server settings, read from the environment."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str
    port: int
    admin_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("KAMPUNG_DB_PATH", "/data/kampung.db")),
            host=os.environ.get("KAMPUNG_HTTP_HOST", "127.0.0.1"),
            port=int(os.environ.get("KAMPUNG_HTTP_PORT", "8000")),
            admin_token=os.environ.get("KAMPUNG_ADMIN_TOKEN") or None,
        )

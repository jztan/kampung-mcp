"""Server settings, read from the environment."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import KampungError

# https, lowercase host, no port, path, query or trailing slash: the exact
# string the owner types into Claude, plus /mcp.
_PUBLIC_URL = re.compile(r"https://[a-z0-9.-]+")


def check_public_url(url: str) -> str:
    if _PUBLIC_URL.fullmatch(url) is None:
        raise KampungError(
            "invalid_public_url",
            "KAMPUNG_PUBLIC_URL must be https://<lowercase host> with no port, "
            f"path or trailing slash, not {url!r}.",
        )
    return url


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str
    port: int
    admin_token: str | None = None
    # Peers whose X-Forwarded-For uvicorn trusts for the client IP. Compose
    # sets "*": the port is published on loopback only, and Caddy replaces
    # any X-Forwarded-For a client sends.
    forwarded_allow_ips: str = "127.0.0.1"
    # Public https address; set, it turns on the Claude app connector.
    public_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        public = os.environ.get("KAMPUNG_PUBLIC_URL") or None
        return cls(
            db_path=Path(os.environ.get("KAMPUNG_DB_PATH", "/data/kampung.db")),
            host=os.environ.get("KAMPUNG_HTTP_HOST", "127.0.0.1"),
            port=int(os.environ.get("KAMPUNG_HTTP_PORT", "8000")),
            admin_token=os.environ.get("KAMPUNG_ADMIN_TOKEN") or None,
            forwarded_allow_ips=os.environ.get(
                "KAMPUNG_FORWARDED_ALLOW_IPS", "127.0.0.1"
            ),
            public_url=check_public_url(public) if public else None,
        )

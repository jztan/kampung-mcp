"""Machine API keys. Only the SHA-256 hash is ever stored."""

import hashlib
import secrets


def new_key() -> str:
    return "kmp_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()

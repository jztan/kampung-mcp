"""A scan for secrets in message bodies and text attachments.

It protects against accidents and careless sends. A tricked session can
still get a secret past it by encoding it twice or splitting it across
messages; SECURITY.md says so. There is no way to override a hit.

Every pattern is bounded, never a lazy span, so 256 KB of hostile input
scans in linear time. A finding holds the kind and the line, never the match.
"""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    line: int


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private key",
        re.compile(r"-----BEGIN (?:[A-Z]{1,20} ){0,3}PRIVATE KEY(?: BLOCK)?-----"),
    ),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_\w{40,255})"),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "AWS secret key",
        re.compile(
            r"(?i)aws_?secret_?access_?key[\"']?\s{0,5}[:=]\s{0,5}[\"']?"
            r"[A-Za-z0-9/+]{40}\b"
        ),
    ),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,255}")),
    (
        "OpenAI key",
        re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,255}"),
    ),
    ("Stripe key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,255}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,255}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,255}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,255}")),
    (
        "JWT",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,4096}\.eyJ[A-Za-z0-9_-]{8,4096}"
            r"\.[A-Za-z0-9_-]{16,4096}"
        ),
    ),
    (
        "password in URL",
        re.compile(
            r"\b[a-z][a-z0-9+.-]{1,30}://[^\s:/@]{1,128}:[^\s:/@]{3,128}@[\w.-]{1,255}"
        ),
    ),
    (
        "env secret",
        re.compile(
            # Upper-case names only, as in .env files, and a literal value:
            # code that reads a secret (os.getenv("API_KEY"), $VAR, ${VAR})
            # is not a secret and must be sendable.
            r"(?m)^\s{0,20}(?:export\s{1,5})?[A-Z0-9_]{0,60}"
            r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)[A-Z0-9_]{0,60}"
            r"\s{0,5}=\s{0,5}[\"']?[^\s\"'<>()\[\]{}$]{8,512}(?=[\s\"']|$)"
        ),
    ),
    ("Kampung key", re.compile(r"\bkmp_[A-Za-z0-9_-]{40,64}")),
    ("Kampung token", re.compile(r"\bkp[arc]_[A-Za-z0-9_-]{40,64}")),
]

_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _normalize(text: str) -> str:
    # NFKC folds lookalikes such as fullwidth letters; format characters
    # (zero-width spaces and joiners, bidi marks) are dropped.
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _first(text: str) -> tuple[str, int] | None:
    best: tuple[int, str] | None = None
    for kind, pattern in _PATTERNS:
        match = pattern.search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), kind)
    return None if best is None else (best[1], best[0])


def scan(text: str) -> SecretFinding | None:
    """The first secret in `text`, or None."""
    clean = _normalize(text)
    found = _first(clean)
    if found is not None:
        return SecretFinding(found[0], _line_of(clean, found[1]))
    for run in _BASE64_RUN.finditer(clean):
        try:
            decoded = base64.b64decode(run.group(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        inner = _first(_normalize(decoded))
        if inner is not None:
            return SecretFinding(inner[0], _line_of(clean, run.start()))
    return None

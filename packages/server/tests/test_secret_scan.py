import base64
import time

import pytest

from kampung_server.secret_scan import SecretFinding, scan


@pytest.mark.parametrize(
    "kind,sample",
    [
        ("private key", "-----BEGIN OPENSSH PRIVATE KEY-----"),
        ("private key", "-----BEGIN RSA PRIVATE KEY-----"),
        ("private key", "-----BEGIN PGP PRIVATE KEY BLOCK-----"),
        ("private key", '"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIE'),
        ("GitHub token", "ghp_" + "a" * 36),
        ("GitHub token", "github_pat_" + "a" * 60),
        ("AWS access key", "AKIA" + "A" * 16),
        ("AWS secret key", "aws_secret_access_key = " + "a" * 40),
        ("Anthropic key", "sk-ant-api03-" + "a" * 40),
        ("OpenAI key", "sk-proj-" + "a" * 40),
        ("OpenAI key", "sk-" + "a" * 48),
        ("Stripe key", "sk_live_" + "a" * 24),
        ("Google API key", "AIza" + "a" * 35),
        ("GitLab token", "glpat-" + "a" * 20),
        ("npm token", "npm_" + "a" * 36),
        ("Hugging Face token", "hf_" + "a" * 34),
        ("Slack token", "xoxb-" + "1" * 12 + "-" + "a" * 24),
        ("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0." + "a" * 30),
        ("password in URL", "postgres://user:hunter2@db.example.com/x"),
        ("env secret", "DB_PASSWORD=hunter2hunter2"),
        ("env secret", "export API_TOKEN='abcdefgh12345678'"),
        ("Kampung key", "kmp_" + "a" * 43),
        ("Kampung token", "kpa_" + "a" * 43),
    ],
)
def test_finds(kind: str, sample: str) -> None:
    assert scan(f"intro\n{sample}\nend") == SecretFinding(kind, 2)


@pytest.mark.parametrize(
    "text",
    [
        "sk-xxxx",
        "ghp_xxxx",
        "PASSWORD=",
        "PASSWORD=<your password>",
        "AKIA",
        "kmp_...",
        "see https://example.com/a:b and http://localhost:8000/x",
        "-----BEGIN PUBLIC KEY-----",
        "-----BEGIN CERTIFICATE-----",
        "the token prefix is kpa_ followed by 43 characters",
        "risk-assessment-and-task-planning-overview-for-the-quarter",
        "An ordinary README with a ## heading and `code`.",
    ],
)
def test_placeholders_and_prose_pass(text: str) -> None:
    assert scan(text) is None


def test_zero_width_and_compatibility_characters_are_removed_first() -> None:
    assert scan("gh​p_" + "a" * 36) == SecretFinding("GitHub token", 1)
    assert scan("ｇｈｐ_" + "a" * 36) == SecretFinding("GitHub token", 1)


def test_one_base64_layer_is_decoded() -> None:
    hidden = base64.b64encode(("ghp_" + "a" * 36).encode()).decode()
    assert scan(f"one\ntwo\n{hidden}") == SecretFinding("GitHub token", 3)


def test_finding_never_holds_the_secret() -> None:
    found = scan("ghp_" + "a" * 36)
    assert found is not None and "aaaa" not in repr(found)


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN " * 25000,
        "a" * 262144,
        "sk-" * 87000,
        "A" * 262144,
        "x=" * 131000,
        "://" + "a" * 262000 + ":",
        "eyJ" + "a." * 131000,
        "AAAA" * 65536,
    ],
    # Short ids: pytest puts the id in PYTEST_CURRENT_TEST, and Windows caps
    # an environment variable at 32,767 characters.
    ids=["begin", "letters", "sk", "capitals", "assign", "url", "jwt", "base64"],
)
def test_256_kb_of_adversarial_input_scans_fast(text: str) -> None:
    start = time.perf_counter()
    scan(text)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    "line",
    [
        'API_KEY = os.getenv("API_KEY")',
        'SECRET_KEY = os.environ["SECRET_KEY"]',
        "    token = request.headers",
        "PASSWORD=$DB_PASSWORD_FROM_VAULT",
        "API_TOKEN=${API_TOKEN_FROM_CI}",
        "db_password = settings.database_password",
    ],
)
def test_ordinary_code_reading_secrets_passes(line: str) -> None:
    assert scan(line) is None

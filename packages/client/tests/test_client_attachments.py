import base64
import io
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from PIL import Image

from client_testlib import LiveServer, db_query, git_repo, hook_chain, peer_send
from kampung_client import adapter, hook
from kampung_client.adapter import build_adapter

KEY = "ghp_" + "a" * 36


def hooked(cwd: Path, sid: str) -> dict[str, object]:
    data = hook.register("claude-code", str(cwd), True, sid)
    data["ancestors"] = hook_chain()
    hook.write_session_file(sid, data)
    return data


def desktop_blog(server: LiveServer) -> None:
    # peer_send registers desktop/blog; the message itself is harmless.
    peer_send(server, "laptop/api", "hello")


def _png(w: int, h: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (20, 160, 90)).save(out, "PNG")
    return out.getvalue()


async def test_send_text_file_from_outbox_then_read_it_in_slices(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    desktop_blog(server)
    box = repo / "kampung-outbox"
    box.mkdir()
    (box / "README.md").write_text("# Title\n" + "x" * 20000)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        sent = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "doc", "attachment": "README.md"}
        )
        assert "error" not in sent.data, sent.data
        first = await c.call_tool(
            "read_attachment", {"message_id": sent.data["id"], "limit": 1000}
        )
        assert (
            first.data["text"].startswith("# Title")
            and first.data["next_offset"] == 1000
        )
        assert first.data["headings"] == [{"level": 1, "title": "Title", "offset": 0}]
    assert not (box / "README.md").exists()


async def test_send_png_is_shrunk_and_read_back_as_an_image(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    desktop_blog(server)
    box = repo / "kampung-outbox"
    box.mkdir()
    (box / "shot.png").write_bytes(_png(3000, 2000))
    async with Client(build_adapter("claude-code", str(repo))) as c:
        sent = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "screen", "attachment": "shot.png"}
        )
        assert "error" not in sent.data, sent.data
        got = await c.call_tool("read_attachment", {"message_id": sent.data["id"]})
    assert [b.type for b in got.content] == ["text", "image"]
    image = got.content[1]
    data = base64.b64decode(image.data)  # type: ignore[union-attr]
    assert max(Image.open(io.BytesIO(data)).size) == 1568


async def test_file_kept_after_a_rejected_send(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    desktop_blog(server)
    box = repo / "kampung-outbox"
    box.mkdir()
    (box / "env.txt").write_text(f"TOKEN line\n{KEY}\n")
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "env.txt"}
        )
    assert r.data["error"] == "secret_detected"
    assert (box / "env.txt").read_text() == f"TOKEN line\n{KEY}\n"


@pytest.mark.parametrize(
    "case, code",
    [
        ("unpaired", "not_paired"),
        ("body", "body_too_large"),
        ("mailbox", "unknown_mailbox"),
    ],
)
async def test_file_kept_after_each_kind_of_server_rejection(
    home: Path, server: LiveServer, tmp_path: Path, case: str, code: str
) -> None:
    # Review Focus 5 past the secret check: unpaired, oversized, misaddressed.
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    desktop_blog(server)
    if case == "unpaired":
        server.admin.unpair("laptop", "desktop", "admin:cli")
    box = repo / "kampung-outbox"
    box.mkdir()
    (box / "notes.md").write_text("# Notes\nplain text\n")
    args = {
        "to": "desktop/nowhere" if case == "mailbox" else "desktop/blog",
        "body": "x" * 9000 if case == "body" else "notes",
        "attachment": "notes.md",
    }
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool("send", args)
    assert r.data["error"] == code
    assert (box / "notes.md").read_text() == "# Notes\nplain text\n"
    assert db_query(server, "SELECT count(*) FROM attachments") == [(0,)]


async def test_missing_file_names_the_full_outbox_path_and_creates_it(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    async with Client(build_adapter("claude-code", str(repo / "sub"))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "nope.md"}
        )
    assert r.data["error"] == "attachment_not_found"
    assert str(repo / "kampung-outbox") in r.data["hint"]
    assert (repo / "kampung-outbox").is_dir()
    assert "/kampung-outbox/" in (repo / ".git" / "info" / "exclude").read_text()


def codex_meta(sid: str) -> dict[str, object]:
    return {"x-codex-turn-metadata": {"session_id": sid, "thread_id": sid}}


async def test_codex_windows_resolve_their_own_outbox(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    api, web = git_repo(tmp_path, "api"), git_repo(tmp_path, "web")
    for sid, repo in (("cx-1", api), ("cx-2", web)):
        hook.write_session_file(
            sid, hook.register("codex", str(repo), True, sid, ancestry=False)
        )
        (repo / "kampung-outbox").mkdir()
        (repo / "kampung-outbox" / "a.md").write_text(f"from {repo.name}")
    desktop_blog(server)
    async with Client(build_adapter("codex", str(api), wait=0)) as c:
        for sid, name in (("cx-1", "api"), ("cx-2", "web")):
            sent = await c.call_tool(
                "send",
                {"to": "desktop/blog", "body": "x", "attachment": "a.md"},
                meta=codex_meta(sid),
            )
            assert "error" not in sent.data, sent.data
            got = await c.call_tool(
                "read_attachment", {"message_id": sent.data["id"]}, meta=codex_meta(sid)
            )
            assert got.data["text"] == f"from {name}"


async def test_binary_files_are_refused(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    box = repo / "kampung-outbox"
    box.mkdir()
    (box / "song.mp3").write_bytes(b"ID3\x03\x00\xff\xfe\x00binary")
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "song.mp3"}
        )
    assert r.data["error"] == "unsupported_attachment_type"
    assert (box / "song.mp3").exists()


async def test_oversized_request_is_refused_before_sending(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "MAX_REQUEST_BYTES", 2000)
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    box = repo / "kampung-outbox"
    box.mkdir()
    (box / "c.txt").write_text("\x01" * 500)  # JSON escapes each to \u0001
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "c.txt"}
        )
    assert r.data["error"] == "attachment_too_large"


async def test_no_attachment_argument_when_unused(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    desktop_blog(server)
    seen: list[dict[str, Any]] = []
    real = Client.call_tool

    async def spy(
        self: Client, name: str, args: dict[str, Any], *a: Any, **kw: Any
    ) -> Any:
        seen.append(dict(args))
        return await real(self, name, args, *a, **kw)

    monkeypatch.setattr(Client, "call_tool", spy)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        await c.call_tool("send", {"to": "desktop/blog", "body": "plain"})
    assert all("attachment" not in a for a in seen)
    assert db_query(server, "SELECT count(*) FROM attachments") == [(0,)]


async def test_bomb_image_in_the_outbox_is_an_inline_error(
    home: Path, server: LiveServer, tmp_path: Path
) -> None:
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body))
        )

    bomb = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")
    (repo / "kampung-outbox").mkdir()
    (repo / "kampung-outbox" / "big.png").write_bytes(bomb)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "big.png"}
        )
    assert r.data["error"] == "attachment_too_large"


async def test_session_failure_during_an_attachment_send_is_inline(
    home: Path, server: LiveServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path, "api")
    hooked(repo, "cc-1")

    def boom(self: object) -> dict[str, object]:
        raise OSError("registration failed")

    monkeypatch.setattr(adapter.SessionResolver, "current", boom)
    async with Client(build_adapter("claude-code", str(repo))) as c:
        r = await c.call_tool(
            "send", {"to": "desktop/blog", "body": "x", "attachment": "a.md"}
        )
    assert "error" in r.data

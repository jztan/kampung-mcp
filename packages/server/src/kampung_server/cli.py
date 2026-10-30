"""The `kampung` command line: machine keys, pairing, revocation, and serve."""

import argparse
import sqlite3
import sys

from . import app, db
from .admin import AdminService
from .clock import utcnow
from .config import Settings
from .deps import ServerDeps
from .errors import KampungError
from .ratelimit import RateLimiter

ACTOR = "admin:cli"
NO_KEYS = (
    "No machine keys yet. Every request will get 401 until you run: "
    "kampung key add <name>"
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kampung", description="Kampung server admin.")
    sub = p.add_subparsers(dest="command", required=True)
    key = sub.add_parser("key", help="manage machine keys").add_subparsers(
        dest="key_command", required=True
    )
    key.add_parser("add", help="create a machine and print its key").add_argument(
        "name"
    )
    key.add_parser("list", help="list machines")
    key.add_parser(
        "rotate", help="give a machine a new key, keeping its mailboxes"
    ).add_argument("name")
    for name, text in (("pair", "let two machines talk"), ("unpair", "undo pair")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("a")
        sp.add_argument("b")
    sub.add_parser("revoke", help="disable a machine key").add_argument("name")
    sub.add_parser("serve", help="run the HTTP server")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.from_env()
    try:
        conn = db.connect(settings.db_path)
        db.migrate(conn)
        conn.close()
    except sqlite3.OperationalError as e:
        if e.sqlite_errorcode & 0xFF not in (
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_READONLY,
        ):
            raise
        print(
            f"error: cannot open the database at {settings.db_path} ({e}). Create "
            "its folder, or set KAMPUNG_DB_PATH to a writable path.",
            file=sys.stderr,
        )
        return 1
    admin = AdminService(lambda: db.connect(settings.db_path), utcnow)
    try:
        if args.command == "key" and args.key_command == "add":
            _, key = admin.add_machine(args.name, ACTOR)
            print(
                f"Machine {args.name} created. Its key is shown once, on the "
                "next line:",
                file=sys.stderr,
            )
            # Alone on stdout, so it can be piped into `setup --key -`.
            print(key)
        elif args.command == "key" and args.key_command == "rotate":
            key = admin.rotate_key(args.name, ACTOR)
            print(
                f"New key for {args.name}; the old one no longer works and its "
                "sessions are ended. Shown once, on the next line:",
                file=sys.stderr,
            )
            print(key)
        elif args.command == "key":
            for m in admin.list_machines():
                state = "revoked" if m.revoked_at else "active"
                print(f"{m.name}  {m.created_at}  {state}")
        elif args.command == "pair":
            admin.pair(args.a, args.b, ACTOR)
            print(f"Paired {args.a} and {args.b}.")
        elif args.command == "unpair":
            admin.unpair(args.a, args.b, ACTOR)
            print(f"Unpaired {args.a} and {args.b}.")
        elif args.command == "revoke":
            admin.revoke(args.name, ACTOR)
            print(f"Revoked {args.name}; its sessions are ended.")
        elif args.command == "serve":
            if not admin.list_machines():
                print(NO_KEYS, file=sys.stderr)
            deps = ServerDeps(
                settings.db_path, utcnow, RateLimiter(), settings.admin_token
            )
            app.serve(
                deps,
                settings.host,
                settings.port,
                forwarded_allow_ips=settings.forwarded_allow_ips,
            )
    except KampungError as e:
        print(f"error: {e.code}: {e.hint}", file=sys.stderr)
        return 1
    return 0

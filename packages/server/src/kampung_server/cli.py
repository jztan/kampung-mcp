"""The `kampung` command line: machine keys, pairing, revocation, and serve."""

import argparse
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
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    conn.close()
    admin = AdminService(lambda: db.connect(settings.db_path), utcnow)
    try:
        if args.command == "key" and args.key_command == "add":
            _, key = admin.add_machine(args.name, ACTOR)
            print(f"Machine {args.name} created. Key (shown once): {key}")
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

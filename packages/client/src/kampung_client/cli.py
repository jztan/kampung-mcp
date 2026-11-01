"""The `kampung-mcp` command line: run the adapter, or set up or check a machine."""

import argparse
import os
import sys


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kampung-mcp", description="Kampung client.")
    sub = p.add_subparsers(dest="command", required=True)
    adapter = sub.add_parser("adapter", help="run the stdio MCP adapter")
    adapter.add_argument(
        "--agent", choices=["claude-code", "codex"], default="claude-code"
    )
    adapter.add_argument(
        "--channels",
        action="store_true",
        help="push a nudge to Claude Code (Channels) when mail arrives",
    )
    adapter.add_argument(
        "--codex-bin",
        metavar="PATH",
        help="Codex: wake an idle session with `PATH queue` when mail arrives",
    )
    setup = sub.add_parser("setup", help="connect this machine to a Kampung server")
    setup.add_argument(
        "--server", required=True, help="e.g. https://kampung.example.com"
    )
    setup.add_argument(
        "--key",
        help="this machine's key from kampung key add; '-' reads it from stdin. "
        "Without it, KAMPUNG_KEY is used, or you are asked for it.",
    )
    setup.add_argument(
        "--agent", choices=["claude-code", "codex", "both"], default="claude-code"
    )
    setup.add_argument(
        "--channels",
        action="store_true",
        help="Claude Code: wake idle sessions with Channels push (experimental)",
    )
    setup.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="send the key over plain http to any host (trusted networks only)",
    )
    set_key = sub.add_parser(
        "set-key", help="save a new key for this machine (after kampung key rotate)"
    )
    set_key.add_argument(
        "--key",
        help="the new key; '-' reads it from stdin. Without it, KAMPUNG_KEY is "
        "used, or you are asked for it.",
    )
    sub.add_parser("doctor", help="check that this machine's Kampung setup works")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "adapter":
        from . import hook
        from .adapter import build_adapter

        try:
            hook.load_config()
        except hook.InsecureServerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except hook.HookError:
            pass  # no config yet: each tool call answers with an inline error
        warning = hook.insecure_http_warning()
        if warning is not None:
            print(warning, file=sys.stderr)

        build_adapter(
            args.agent, channels=args.channels, codex_bin=args.codex_bin
        ).run()
    elif args.command == "setup":
        from . import setup

        agents = ["claude-code", "codex"] if args.agent == "both" else [args.agent]
        key = _setup_key(args.key)
        if not key:
            print(
                "error: no key. Pass --key -, set KAMPUNG_KEY, or run setup "
                "in a terminal to be asked for it.",
                file=sys.stderr,
            )
            return 1
        try:
            lines = setup.run_setup(
                args.server,
                key,
                agents,
                setup.default_runner,
                args.channels,
                args.allow_insecure_http,
            )
        except setup.SetupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("\n".join(lines))
    elif args.command == "set-key":
        from . import setup

        key = _setup_key(args.key)
        if not key:
            print("error: no key.", file=sys.stderr)
            return 1
        try:
            print("\n".join(setup.set_key(key)))
        except setup.SetupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    elif args.command == "doctor":
        from . import doctor

        return doctor.main()
    return 0


def _setup_key(flag: str | None) -> str:
    """The key from --key, stdin, KAMPUNG_KEY or a prompt, in that order.

    Everything but --key VALUE keeps the key out of shell history."""
    if flag == "-":
        return sys.stdin.readline().strip()
    if flag:
        return flag
    env = os.environ.get("KAMPUNG_KEY", "").strip()
    if env:
        return env
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("Machine key: ").strip()
    return ""

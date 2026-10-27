"""The `kampung-mcp` command line: run the adapter, or set up a machine."""

import argparse
import sys


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kampung-mcp", description="Kampung client.")
    sub = p.add_subparsers(dest="command", required=True)
    adapter = sub.add_parser("adapter", help="run the stdio MCP adapter")
    adapter.add_argument(
        "--agent", choices=["claude-code", "codex"], default="claude-code"
    )
    setup = sub.add_parser("setup", help="connect this machine to a Kampung server")
    setup.add_argument(
        "--server", required=True, help="e.g. https://kampung.example.com"
    )
    setup.add_argument(
        "--key", required=True, help="this machine's key from kampung key add"
    )
    setup.add_argument(
        "--agent", choices=["claude-code", "codex", "both"], default="claude-code"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "adapter":
        from .adapter import build_adapter

        build_adapter(args.agent).run()
    elif args.command == "setup":
        from . import setup

        agents = ["claude-code", "codex"] if args.agent == "both" else [args.agent]
        try:
            lines = setup.run_setup(args.server, args.key, agents, setup.default_runner)
        except setup.SetupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("\n".join(lines))
    return 0

"""The `kampung-mcp` command line: run the adapter, or set up a machine."""

import argparse


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kampung-mcp", description="Kampung client.")
    sub = p.add_subparsers(dest="command", required=True)
    adapter = sub.add_parser("adapter", help="run the stdio MCP adapter")
    adapter.add_argument(
        "--agent", choices=["claude-code", "codex"], default="claude-code"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "adapter":
        from .adapter import build_adapter

        build_adapter(args.agent).run()
    return 0

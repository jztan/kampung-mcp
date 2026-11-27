"""`kampung-mcp --version` and `kampung-mcp adapter --agent other`."""

import pytest

from kampung_client import __version__, cli


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"kampung-mcp {__version__}"


def test_adapter_accepts_agent_other():
    args = cli._parser().parse_args(["adapter", "--agent", "other"])
    assert args.agent == "other"

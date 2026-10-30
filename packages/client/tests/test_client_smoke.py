import tomllib
from pathlib import Path

import kampung_client


def test_version() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert kampung_client.__version__ == project["version"]

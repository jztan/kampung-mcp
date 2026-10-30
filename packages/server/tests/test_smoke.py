import tomllib
from pathlib import Path

import kampung_server


def test_version() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert kampung_server.__version__ == project["version"]

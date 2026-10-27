import ast
import sys
from pathlib import Path

from kampung_client import hook


def test_hook_imports_stdlib_only() -> None:
    tree = ast.parse(Path(hook.__file__).read_text())
    mods = {
        n.module.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0
    } | {
        a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
    }
    relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
    assert not relative
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}

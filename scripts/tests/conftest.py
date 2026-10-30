import sys
from pathlib import Path

# scripts/ is not a package; make release.py importable as `release`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sys
from pathlib import Path

# Ensure tests find the package from a source checkout without requiring an
# editable install.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

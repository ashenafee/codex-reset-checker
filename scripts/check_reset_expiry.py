#!/usr/bin/env python3
"""Run the package CLI from a source checkout."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from codex_reset_checker.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

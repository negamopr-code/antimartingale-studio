#!/usr/bin/env python3
"""Launch the 3-tab GUI. Run on a host with python3-tk (not in the dev container)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from antimg.app import main  # noqa: E402

if __name__ == "__main__":
    main()

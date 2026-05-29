import os
import sys
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

#!/usr/bin/env python3
"""Local dev server for the FastAPI app (auto-reload).

    .venv/bin/python scripts/run_web.py
    # then open http://127.0.0.1:8090

Production uses gunicorn+uvicorn workers (see deploy/Dockerfile).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    # default 8090: host 8000 is taken by zoe-serve, 9000 by dash-serve on this box
    uvicorn.run("antimg.web.api:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8090")), reload=True)

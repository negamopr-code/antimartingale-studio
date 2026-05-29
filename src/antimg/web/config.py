"""Env-driven settings (12-factor). No secrets in code.

All knobs are read from the environment so the same image runs in any environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    # CORS — comma-separated origins, "*" for any (dev default)
    cors_origins: list[str] = field(default_factory=lambda: _csv("ANTIMG_CORS_ORIGINS", "*"))
    # storage
    cache_dir: str = os.environ.get("ANTIMG_CACHE", "/workspace/.cache")
    signal_db: str = os.environ.get("ANTIMG_SIGNAL_DB", "/workspace/.cache/signals.db")
    # TradingView webhook shared secret (passphrase). Empty => webhook rejects all.
    webhook_secret: str = os.environ.get("ANTIMG_WEBHOOK_SECRET", "")
    # safety caps for a public endpoint (anti-DoS)
    max_iterations: int = int(os.environ.get("ANTIMG_MAX_ITERATIONS", "2000000"))
    max_target_streak: int = int(os.environ.get("ANTIMG_MAX_TARGET_STREAK", "40"))
    max_points: int = int(os.environ.get("ANTIMG_MAX_POINTS", "3000"))  # series downsample cap
    # data fetch
    default_start: str = os.environ.get("ANTIMG_DEFAULT_START", "2005-01-01")

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_secret)


settings = Settings()

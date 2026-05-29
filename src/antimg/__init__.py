"""antimg — antimartingale (pyramid-on-wins) toolkit.

Three layers, mirrored by the GUI tabs:
  - simcore:      abstract coin-flip pyramid simulator (Tab 1)
  - atr_strategy: weekly-entry / daily-resolution ATR port on a real asset (Tab 2)
  - options:      same strategy expressed via options, BS delta auto-computed (Tab 3)

The math doctrine lives in ~/.claude/skills/antimartingal-strategy/SKILL.md.
Core EV identity:  E[cycle] = b * ((2p)^N - 1).
"""

__all__ = ["simcore", "data", "instruments", "atr_strategy", "options"]

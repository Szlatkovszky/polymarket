"""Polymarket Research Lab — PAPER and DEMO modes only.

This package never signs orders, stores wallet keys, converts/redeems tokens,
withdraws funds, or auto-promotes to live. Success is cost-adjusted
out-of-sample economics within risk limits — not tip count or hit rate.
No edge is claimed.
"""

from __future__ import annotations

__version__ = "0.1.0"

ALLOWED_MODES = ("PAPER", "DEMO")

"""
Canonical icon set for Cliara's console output.

Define all symbolic markers (success, failure, warnings, thinking, etc.)
in one place so the visual language stays consistent across the app.
"""

# Success / failure
OK = "✓"        # success (green)
FAIL = "✗"      # failure (red)

# Warnings / danger
WARN = "⚠"      # caution (yellow)
DANGER = "⛔"    # dangerous / critical (bold red)

# Informational / status
INFO = "◆"      # general info (cyan)
THINK = "⟳"     # processing / AI thinking (dim)
CANCEL = "∅"    # cancelled (dim)
GATE = "⚑"      # Copilot Gate intercept (yellow)

# Ghost Run — parallel-universe shadow execution.
# The emoji is astral-plane (U+1F47B) and crashes legacy cp1252 console
# rendering, so fall back to ASCII when stdout isn't UTF-8.
import sys as _sys

_enc = (getattr(_sys.stdout, "encoding", "") or "").lower()
GHOST = "👻" if ("utf" in _enc or "65001" in _enc) else "(ghost)"
del _sys, _enc


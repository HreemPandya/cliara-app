"""
Shared Rich console for Cliara's UI.

All Cliara-originated output (banner, messages, panels) uses this console.
Subprocess stdout/stderr are never passed through it.
"""

import sys
from typing import Optional

from rich.console import Console

_console: Optional[Console] = None


def get_console() -> Console:
    """Return the single shared Rich console (stdout)."""
    global _console
    if _console is None:
        _console = Console(file=sys.stdout, force_terminal=None)
    return _console

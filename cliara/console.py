"""
Shared Rich console for Cliara's UI.

All Cliara-originated output (banner, messages, panels) uses this console.
Subprocess stdout/stderr are never passed through it.
"""

import sys
from typing import Optional

from rich.console import Console

_console: Optional[Console] = None
_ui_theme: Optional[str] = None


def get_console() -> Console:
    """Return the single shared Rich console (stdout)."""
    global _console
    if _console is None:
        _console = Console(file=sys.stdout, force_terminal=None)
    return _console


def set_ui_theme(name: Optional[str]) -> None:
    """Remember the active Cliara color theme for neutral Rich output (print_info)."""
    global _ui_theme
    from cliara.highlighting import DEFAULT_THEME, list_themes

    n = (name or "").strip().lower()
    themes = frozenset(list_themes())
    _ui_theme = n if n in themes else DEFAULT_THEME


def get_ui_theme() -> str:
    """Active theme name for UI styling (defaults before shell init: dracula)."""
    from cliara.highlighting import DEFAULT_THEME

    return _ui_theme if _ui_theme else DEFAULT_THEME

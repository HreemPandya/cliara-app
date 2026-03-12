"""
Cliara installation welcome logo.

Displayed exactly once — on the first run after installation.
Uses pixel-block art (similar to Claude Code's style) rendered via Rich.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Pixel-block font — each letter is a 5-row × 5-col grid.
# Each grid cell is 2 terminal characters wide: "██" (filled) or "  " (empty).
# Total per-letter width = 10 chars.
# ---------------------------------------------------------------------------
_LETTERS: dict[str, list[str]] = {
    "C": [
        "  ██████  ",
        "██        ",
        "██        ",
        "██        ",
        "  ██████  ",
    ],
    "L": [
        "██        ",
        "██        ",
        "██        ",
        "██        ",
        "██████████",
    ],
    "I": [
        "██████████",
        "    ██    ",
        "    ██    ",
        "    ██    ",
        "██████████",
    ],
    "A": [
        "  ██████  ",
        "██      ██",
        "██████████",
        "██      ██",
        "██      ██",
    ],
    "R": [
        "████████  ",
        "██      ██",
        "████████  ",
        "██  ██    ",
        "██      ██",
    ],
}

# Gradient colors applied top-to-bottom across the logo rows
_ROW_COLORS = [
    "#00e5cc",  # bright teal
    "#00cfba",
    "#00b8a5",
    "#00cfba",
    "#00e5cc",
]


def _build_logo_lines(word: str) -> list[str]:
    """Return 5 assembled pixel-art rows for *word*."""
    gap = "   "
    rows = []
    for row_idx in range(5):
        parts = [_LETTERS[ch][row_idx] for ch in word.upper() if ch in _LETTERS]
        rows.append(gap.join(parts))
    return rows


def print_install_logo(version: str = "") -> None:
    """Print the Cliara pixel-art welcome banner to stdout.

    Falls back gracefully to plain text if Rich is unavailable or the
    terminal cannot render Unicode block characters.
    """
    try:
        import sys

        # Ensure stdout uses UTF-8 so block characters render on Windows.
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

        from rich.console import Console
        from rich.text import Text
        from rich.align import Align

        console = Console(highlight=False)
        logo_lines = _build_logo_lines("CLIARA")

        logo_text = Text()
        for i, line in enumerate(logo_lines):
            color = _ROW_COLORS[i % len(_ROW_COLORS)]
            logo_text.append(line, style=f"bold {color}")
            logo_text.append("\n")

        ver_str = f"v{version}  ·  " if version else ""

        console.print()
        console.print(Align.center(logo_text))
        console.print(
            Align.center(
                f"[bold #00e5cc]AI-powered shell[/]  [dim]·[/]  "
                "[dim]natural language[/]  [dim]·[/]  [dim]macros[/]"
            )
        )
        console.print(
            Align.center(f"[dim]{ver_str}Type [bold]help[/bold] to get started[/dim]")
        )
        console.print()

    except Exception:
        _plain_fallback(version)


def _plain_fallback(version: str = "") -> None:
    ver = f" v{version}" if version else ""
    print()
    print("=" * 60)
    print(f"   Welcome to Cliara{ver}!")
    print("   AI-powered shell · natural language · macros")
    print("=" * 60)
    print()

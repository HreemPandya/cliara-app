"""
Syntax highlighting for the Cliara shell prompt.

Provides real-time colour coding as you type commands:

  Flags   (-m, --verbose)     grey
  Strings ("...", '...')      green
  Variables ($VAR, ${VAR})    yellow
  Pipes & operators (|, &&)   cyan
  Numbers                     purple
  Comments (#...)             dark grey / italic

Uses Pygments for lexing and prompt_toolkit for rendering.
"""

from pygments.lexer import RegexLexer
from pygments.token import (
    Token,
    Comment,
    String,
    Name,
    Number,
    Operator,
    Punctuation,
)
from pygments.style import Style as PygmentsStyle


# ---------------------------------------------------------------------------
# Custom lexer — tuned for interactive shell input
# ---------------------------------------------------------------------------

class ShellLexer(RegexLexer):
    """
    Lightweight lexer for interactive shell command highlighting.

    Designed for the single-line commands users type at a prompt, not for
    full shell scripts.  Recognises flags, strings, variables, operators,
    and numbers while leaving everything else as plain text.
    """

    name = "ShellInput"
    aliases = ["shellinput"]

    tokens = {
        "root": [
            # ── comments ──
            (r"#.*$", Comment.Single),

            # ── strings ──
            (r'"(?:\\.|[^"\\])*"', String.Double),
            (r"'[^']*'", String.Single),
            (r"`[^`]*`", String.Backtick),

            # ── shell variables ──
            (r"\$\{[^}]+\}", Name.Variable),
            (r"\$[A-Za-z_]\w*", Name.Variable),

            # ── flags ──
            # Long flags:  --flag-name  --verbose
            (r"--[A-Za-z0-9][\w-]*", Name.Tag),
            # Short flags: -m  -rf  (must follow whitespace or be at start)
            (r"(?<=\s)-[A-Za-z0-9]+", Name.Tag),
            (r"^-[A-Za-z0-9]+", Name.Tag),

            # ── operators & redirects ──
            (r"\|{1,2}", Operator),         # |  ||
            (r"&&", Operator),              # &&
            (r"[12]?>{1,2}", Operator),     # >  >>  2>  2>>
            (r"<", Operator),               # <
            (r";", Punctuation),            # ;

            # ── numbers ──
            (r"\b\d+\b", Number.Integer),

            # ── catch-all ──
            (r"\S+", Token.Text),
            (r"\s+", Token.Text),
        ],
    }


# ---------------------------------------------------------------------------
# Colour palette (Monokai-inspired, great on dark backgrounds)
# ---------------------------------------------------------------------------

class CliaraStyle(PygmentsStyle):
    """Pygments colour theme for Cliara's command highlighting."""

    default_style = ""
    styles = {
        Token.Text:        "",                  # terminal default
        Comment.Single:    "#6a6a6a italic",    # dark grey
        String.Double:     "#a6e22e",           # green
        String.Single:     "#a6e22e",
        String.Backtick:   "#a6e22e",
        Name.Variable:     "#e6db74",           # yellow
        Name.Tag:          "#888888",           # grey  — flags
        Operator:          "#66d9ef",           # cyan  — pipes, redirects
        Punctuation:       "#66d9ef",
        Number.Integer:    "#ae81ff",           # purple
    }


# ---------------------------------------------------------------------------
# Prompt segment styles (the "cliara:dir >" part)
# ---------------------------------------------------------------------------

PROMPT_STYLE = {
    "prompt-name":  "#00d7d7 bold",     # cyan bold   — "cliara"
    "prompt-sep":   "#888888",          # grey        — ":"
    "prompt-path":  "#ffffff",          # white       — directory name
    "prompt-arrow": "#6a6a6a",          # dim grey    — ">"
}

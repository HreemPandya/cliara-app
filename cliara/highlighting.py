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
# Colour palettes — theme system (Monokai, Dracula, Nord, Solarized, Catppuccin, Light)
# ---------------------------------------------------------------------------

# Token keys match Pygments token types used in ShellLexer
THEMES = {
    "monokai": {
        "styles": {
            Token.Text:        "",
            Comment.Single:    "#6a6a6a italic",
            String.Double:     "#a6e22e",
            String.Single:     "#a6e22e",
            String.Backtick:   "#a6e22e",
            Name.Variable:     "#e6db74",
            Name.Tag:          "#888888",
            Operator:          "#66d9ef",
            Punctuation:       "#66d9ef",
            Number.Integer:    "#ae81ff",
        },
        "prompt_style": {
            "prompt-name":  "ansicyan bold",
            "prompt-sep":   "ansibrightblack",
            "prompt-path":  "ansiwhite",
            "prompt-arrow": "ansibrightblack",
            "prompt-exit-success": "ansigreen bold",
            "prompt-exit-fail": "ansired bold",
        },
        # Rich markup used for the instant preview line
        "preview": {
            "name":     "[bold cyan]",
            "string":   "[bright_green]",
            "flag":     "[bright_black]",
            "var":      "[yellow]",
            "op":       "[cyan]",
            "num":      "[bright_magenta]",
        },
    },
    "dracula": {
        "styles": {
            Token.Text:        "",
            Comment.Single:    "#6272a4 italic",
            String.Double:     "#50fa7b",
            String.Single:     "#50fa7b",
            String.Backtick:   "#50fa7b",
            Name.Variable:     "#f1fa8c",
            Name.Tag:          "#6272a4",
            Operator:          "#8be9fd",
            Punctuation:       "#8be9fd",
            Number.Integer:    "#bd93f9",
        },
        "prompt_style": {
            "prompt-name":  "ansimagenta bold",
            "prompt-sep":   "ansibrightblack",
            "prompt-path":  "ansiwhite",
            "prompt-arrow": "ansibrightblack",
            "prompt-exit-success": "ansigreen bold",
            "prompt-exit-fail": "ansired bold",
        },
        "preview": {
            "name":     "[bold magenta]",
            "string":   "[bright_green]",
            "flag":     "[color(99)]",
            "var":      "[bright_yellow]",
            "op":       "[bright_cyan]",
            "num":      "[magenta]",
        },
    },
    "nord": {
        "styles": {
            Token.Text:        "",
            Comment.Single:    "#616e88 italic",
            String.Double:     "#a3be8c",
            String.Single:     "#a3be8c",
            String.Backtick:   "#a3be8c",
            Name.Variable:     "#ebcb8b",
            Name.Tag:          "#81a1c1",
            Operator:          "#81a1c1",
            Punctuation:       "#81a1c1",
            Number.Integer:    "#b48ead",
        },
        "prompt_style": {
            "prompt-name":  "ansiblue bold",
            "prompt-sep":   "ansibrightblack",
            "prompt-path":  "ansiwhite",
            "prompt-arrow": "ansibrightblack",
            "prompt-exit-success": "ansigreen bold",
            "prompt-exit-fail": "ansired bold",
        },
        "preview": {
            "name":     "[bold blue]",
            "string":   "[green]",
            "flag":     "[bright_blue]",
            "var":      "[yellow]",
            "op":       "[blue]",
            "num":      "[bright_magenta]",
        },
    },
    "solarized": {
        # Dark background: bright ANSI colors (widely supported)
        "styles": {
            Token.Text:        "",
            Comment.Single:    "ansicyan italic",
            String.Double:     "ansigreen",
            String.Single:     "ansigreen",
            String.Backtick:   "ansigreen",
            Name.Variable:     "ansiyellow",
            Name.Tag:          "ansibrightblack",
            Operator:          "ansicyan",
            Punctuation:       "ansicyan",
            Number.Integer:    "ansimagenta",
        },
        "prompt_style": {
            "prompt-name":  "ansibrightred bold",   # Solarized orange
            "prompt-sep":   "ansibrightblack",
            "prompt-path":  "ansibrightwhite",
            "prompt-arrow": "ansibrightblack",
            "prompt-exit-success": "ansigreen bold",
            "prompt-exit-fail": "ansired bold",
        },
        "preview": {
            "name":     "[bold bright_red]",
            "string":   "[green]",
            "flag":     "[bright_black]",
            "var":      "[yellow]",
            "op":       "[cyan]",
            "num":      "[magenta]",
        },
    },
    "catppuccin": {
        "styles": {
            Token.Text:        "",
            Comment.Single:    "#6c7086 italic",
            String.Double:     "#a6e3a1",
            String.Single:     "#a6e3a1",
            String.Backtick:   "#a6e3a1",
            Name.Variable:     "#f9e2af",
            Name.Tag:          "#a6adc8",
            Operator:          "#89dceb",
            Punctuation:       "#89dceb",
            Number.Integer:    "#cba6f7",
        },
        "prompt_style": {
            "prompt-name":  "ansigreen bold",
            "prompt-sep":   "ansibrightblack",
            "prompt-path":  "ansiwhite",
            "prompt-arrow": "ansibrightblack",
            "prompt-exit-success": "ansicyan bold",
            "prompt-exit-fail": "ansired bold",
        },
        "preview": {
            "name":     "[bold green]",
            "string":   "[bright_green]",
            "flag":     "[bright_black]",
            "var":      "[bright_yellow]",
            "op":       "[bright_cyan]",
            "num":      "[color(183)]",
        },
    },
    "light": {
        # Light background: dark ANSI colors so text is readable (clearly different from solarized).
        "styles": {
            Token.Text:        "",
            Comment.Single:    "ansiblue italic",
            String.Double:     "ansigreen",
            String.Single:     "ansigreen",
            String.Backtick:   "ansigreen",
            Name.Variable:     "ansiyellow",
            Name.Tag:          "ansiblue",
            Operator:          "ansiblue",
            Punctuation:       "ansiblue",
            Number.Integer:    "ansimagenta",
        },
        "prompt_style": {
            "prompt-name":  "ansiblue bold",       # Dark blue on light (not red/orange)
            "prompt-sep":   "ansiblack",
            "prompt-path":  "ansiblack",
            "prompt-arrow": "ansiblack",
            "prompt-exit-success": "ansigreen bold",
            "prompt-exit-fail": "ansired bold",
        },
        "preview": {
            "name":     "[bold blue]",
            "string":   "[green]",
            "flag":     "[blue]",
            "var":      "[yellow]",
            "op":       "[blue]",
            "num":      "[magenta]",
        },
    },
}

# Default prompt style (used when theme unknown)
PROMPT_STYLE = THEMES["dracula"]["prompt_style"]

# Default theme when none set or invalid (always apply a theme)
DEFAULT_THEME = "dracula"


def get_style_for_theme(theme_name: str):
    """
    Return (PygmentsStyleClass, prompt_style_dict) for the given theme.

    theme_name: one of dracula, monokai, nord, solarized, catppuccin, light.
    Unknown or missing names fall back to DEFAULT_THEME (dracula).
    """
    name = (theme_name or "").strip().lower()
    if name not in THEMES:
        name = DEFAULT_THEME
    data = THEMES[name]
    style_cls = type(
        "CliaraThemeStyle",
        (PygmentsStyle,),
        {"default_style": "", "styles": data["styles"]},
    )
    return style_cls, data["prompt_style"]


def get_theme_preview_markup(theme_name: str) -> str:
    """
    Return a Rich markup string previewing the theme colors.
    Shown immediately after switching so the user sees the change at once.
    """
    name = (theme_name or "").strip().lower()
    if name not in THEMES:
        name = DEFAULT_THEME
    p = THEMES[name]["preview"]
    reset = "[/]"

    label   = f"{p['name']}[cliara]{reset}"
    path    = "~/projects/myapp"
    arrow   = f"{p['name']}>{reset}"
    cmd     = "echo"
    string  = f'{p["string"]}"hello world"{reset}'
    flag    = f'{p["flag"]}--verbose{reset}'
    var     = f'{p["var"]}$USER{reset}'
    op      = f'{p["op"]}|{reset}'
    grep    = "grep"
    num     = f'{p["num"]}42{reset}'

    return f"  {label} {path} {arrow} {cmd} {string} {flag} {var} {op} {grep} {num}"


def list_themes():
    """Return list of available theme names."""
    return list(THEMES.keys())


def get_prompt_name_ansi(theme_name: str) -> tuple:
    """
    Return (prefix, suffix) ANSI escape sequences for the prompt name (e.g. 'cliara').
    Used when prompt_toolkit is unavailable so the prompt name is still themed.
    """
    name = (theme_name or "").strip().lower()
    if name not in THEMES:
        name = DEFAULT_THEME
    # Map theme prompt-name style to raw ANSI: (bold, fg_code). 31=red, 32=green, 33=yellow, 34=blue, 35=magenta, 36=cyan.
    # ANSI fg: 31 red, 32 green, 33 yellow, 34 blue, 35 magenta, 36 cyan, 91 bright red (orange)
    _prompt_ansi = {
        "monokai": (True, 36),    # cyan bold
        "dracula": (True, 35),    # magenta bold
        "nord": (True, 34),       # blue bold
        "solarized": (True, 91),  # bright red / orange
        "catppuccin": (True, 32), # green bold
        "light": (True, 34),      # blue bold (dark on light bg; distinct from solarized)
    }
    bold, fg = _prompt_ansi.get(name, (True, 35))
    # Standard ANSI: 30-37 normal, 90-97 bright. 256-color: 0-255 via 38;5;n
    if (30 <= fg <= 37) or (90 <= fg <= 97):
        prefix = f"\033[1;{fg}m" if bold else f"\033[{fg}m"
    else:
        prefix = f"\033[1;38;5;{fg}m" if bold else f"\033[38;5;{fg}m"
    return (prefix, "\033[0m")


# Backward compatibility: default style class (Monokai)
class CliaraStyle(PygmentsStyle):
    """Pygments colour theme for Cliara's command highlighting (default: Monokai)."""

    default_style = ""
    styles = THEMES["monokai"]["styles"]


# ---------------------------------------------------------------------------
# Prompt segment styles (the "cliara:dir >" part) — PROMPT_STYLE is set above from THEMES["monokai"]
# ---------------------------------------------------------------------------

"""
Cliara LLM Setup Wizard.

Guides users through configuring a free LLM provider on first launch
(or whenever 'setup-llm' is run explicitly).

Supported providers and their free tiers:
  - Groq    — 14,400 req/day free, no credit card, signup at console.groq.com
  - Gemini  — 1,500 req/day free, requires Google account, aistudio.google.com
  - Ollama  — fully local, no internet required, ollama.com
  - OpenAI  — paid, but widely used
  - Anthropic — paid

Public entry-points:
  run_wizard(shell)         — interactive menu + key entry
  auto_detect_ollama(shell) — silently configure if Ollama is already running
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from rich import box
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from cliara import icons

if TYPE_CHECKING:
    from cliara.shell import CliaraShell


def _wiz_console():
    from cliara.console import get_console
    return get_console()

# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

_PROVIDERS = [
    {
        "id": "groq",
        "label": "Groq",
        "tagline": "Free, no credit card · 14,400 req/day · fastest inference",
        "signup_url": "https://console.groq.com",
        "env_var": "GROQ_API_KEY",
        "key_hint": "Paste your Groq API key (starts with gsk_...): ",
        "recommended": True,
    },
    {
        "id": "gemini",
        "label": "Gemini",
        "tagline": "Free Google account · 1,500 req/day",
        "signup_url": "https://aistudio.google.com/app/apikey",
        "env_var": "GEMINI_API_KEY",
        "key_hint": "Paste your Gemini API key (starts with AIza...): ",
        "recommended": False,
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "tagline": "Fully local, no internet required · privacy-first",
        "signup_url": "https://ollama.com",
        "env_var": "OLLAMA_BASE_URL",
        "key_hint": None,  # No key needed
        "recommended": False,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "tagline": "Requires paid API key · gpt-4o-mini",
        "signup_url": "https://platform.openai.com/api-keys",
        "env_var": "OPENAI_API_KEY",
        "key_hint": "Paste your OpenAI API key (starts with sk-...): ",
        "recommended": False,
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "tagline": "Requires paid API key · Claude models",
        "signup_url": "https://console.anthropic.com",
        "env_var": "ANTHROPIC_API_KEY",
        "key_hint": "Paste your Anthropic API key (starts with sk-ant-...): ",
        "recommended": False,
    },
]

_OLLAMA_DEFAULT_URL = "http://localhost:11434"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_env_path() -> Path:
    """Return the path to ~/.cliara/.env (created if the directory is missing)."""
    env_dir = Path.home() / ".cliara"
    env_dir.mkdir(parents=True, exist_ok=True)
    return env_dir / ".env"


def _write_env_var(key: str, value: str) -> Path:
    """Write KEY=VALUE to ~/.cliara/.env (upsert).

    If the key already exists (even commented-out), it is updated in-place.
    Otherwise the line is appended.  Returns the path written.
    """
    env_path = _user_env_path()
    new_line = f"{key}={value}\n"

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        written = False
        for i, line in enumerate(lines):
            bare = line.lstrip("#").lstrip().split("=")[0].strip()
            if bare == key:
                lines[i] = new_line
                written = True
                break
        if not written:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(new_line)
        env_path.write_text("".join(lines), encoding="utf-8")
    else:
        env_path.write_text(new_line, encoding="utf-8")

    return env_path


def _ollama_running(url: str = _OLLAMA_DEFAULT_URL, timeout: int = 2) -> bool:
    """Return True if an Ollama service is reachable at *url*."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _mask_key(key: str) -> str:
    """Show first 6 and last 4 chars of a key, mask the middle."""
    if len(key) <= 12:
        return key[:3] + "..." + key[-2:] if len(key) > 5 else "***"
    return key[:6] + "..." + key[-4:]


def _apply_env_and_reinit(shell: "CliaraShell", provider_id: str, env_var: str, key: str) -> bool:
    """Set the env var in the current process and re-initialise the LLM client."""
    os.environ[env_var] = key

    # Reload config settings from env
    shell.config._load_env_vars()

    provider = shell.config.get_llm_provider()
    api_key = shell.config.get_llm_api_key()
    base_url = shell.config.get_ollama_base_url() if provider == "ollama" else None

    if provider and api_key:
        return shell.nl_handler.initialize_llm(provider, api_key, base_url=base_url)
    return False


# ---------------------------------------------------------------------------
# Ollama auto-detect (silent, called on every startup)
# ---------------------------------------------------------------------------

def auto_detect_ollama(shell: "CliaraShell") -> bool:
    """Silently configure Ollama if it is already running locally.

    Returns True if Ollama was detected and the LLM client was initialised.
    Does nothing and returns False if Ollama is not running.
    """
    if not _ollama_running():
        return False

    # Already configured — nothing to do
    if shell.nl_handler.llm_enabled:
        return True

    # If a non-Ollama model is stored in config (e.g. gpt-4o-mini from a
    # previous OpenAI setup), clear it so Ollama uses its own default model.
    _clear_incompatible_model(shell)

    env_path = _write_env_var("OLLAMA_BASE_URL", _OLLAMA_DEFAULT_URL)
    ok = _apply_env_and_reinit(shell, "ollama", "OLLAMA_BASE_URL", _OLLAMA_DEFAULT_URL)
    if ok:
        try:
            from cliara.shell import print_success
            model = shell.nl_handler.resolved_model_for_display()
            print_success(
                f"  LLM: OLLAMA · {model} auto-detected and connected  "
                f"(saved to {env_path})"
            )
        except Exception:
            print(f"  [{icons.OK}] Ollama auto-detected at {_OLLAMA_DEFAULT_URL}")
    return ok


# Known OpenAI/Anthropic/Groq/Gemini model name prefixes that are invalid in Ollama
_CLOUD_MODEL_PREFIXES = ("gpt-", "claude-", "llama-3.", "gemini-", "mixtral-", "text-")


def _clear_incompatible_model(shell: "CliaraShell") -> None:
    """If the stored llm_model looks like a cloud model, clear it.

    This prevents a leftover 'gpt-4o-mini' (from a previous OpenAI config)
    from being sent to Ollama, which would cause a 404 error.
    """
    stored_model = shell.config.get("llm_model") or ""
    if any(stored_model.startswith(prefix) for prefix in _CLOUD_MODEL_PREFIXES):
        shell.config.settings["llm_model"] = None
        shell.config.save()


# ---------------------------------------------------------------------------
# Interactive setup wizard
# ---------------------------------------------------------------------------

def _print_header_and_menu() -> None:
    """Render the LLM setup screen with Rich panels and a provider table."""
    console = _wiz_console()

    subtitle = Text()
    subtitle.append("No AI provider configured yet.\n", style="dim")
    subtitle.append("Choose one to enable ", style="dim")
    subtitle.append("?", style="bold cyan")
    subtitle.append(", ", style="dim")
    subtitle.append("explain", style="bold cyan")
    subtitle.append(", smart push, macros, and other AI features.", style="dim")

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", justify="center", width=3)
    table.add_column("Provider", style="bold white", width=12, no_wrap=True)
    table.add_column("Details", min_width=36)

    for i, p in enumerate(_PROVIDERS, 1):
        detail = Text(p["tagline"], style="white")
        if p["recommended"]:
            detail.append("\n")
            detail.append("★ Recommended — free, no card", style="bold green")
        table.add_row(str(i), p["label"], detail)

    skip_detail = Text()
    skip_detail.append("Shell, git, and macros only", style="dim")
    skip_detail.append("\n")
    skip_detail.append("Run setup-llm later anytime", style="italic dim")
    table.add_row(
        "s",
        Text("Skip", style="bold yellow"),
        skip_detail,
    )

    body = Group(subtitle, Text(""), table)
    panel = Panel(
        body,
        title=Text.from_markup("[bold cyan]Cliara[/] [dim]·[/] [bold white]LLM Setup[/]"),
        subtitle=Text.from_markup("[dim]Powers natural language & AI-assisted workflows[/]"),
        border_style="cyan",
        box=box.DOUBLE,
        padding=(1, 2),
    )
    console.print()
    console.print(panel)


def _print_choice_prompt() -> None:
    console = _wiz_console()
    line = Text()
    line.append("   ➜ ", style="bold cyan")
    line.append("Your choice ", style="bold white")
    line.append("(number, name, or ", style="dim")
    line.append("s", style="bold yellow")
    line.append(" to skip) ", style="dim")
    line.append("[1]", style="bold cyan")
    line.append(": ", style="dim")
    console.print(line, end="")


def _read_masked(prompt: str) -> str:
    """Read a line with masking on terminals that support getpass; fall back to plain input."""
    try:
        import getpass
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt)


def run_wizard(shell: "CliaraShell") -> bool:
    """Run the interactive LLM setup wizard.

    Returns True if the LLM was successfully configured, False otherwise.
    """
    _print_header_and_menu()
    _print_choice_prompt()

    # --- Read user choice ---
    try:
        raw = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        _wiz_console().print()
        _wiz_console().print("   [dim]Setup cancelled.[/]")
        _mark_dismissed(shell)
        return False

    if not raw:
        raw = "1"

    if raw in ("s", "skip", "q", "quit", "n", "no"):
        _mark_dismissed(shell)
        _print_skip_info()
        return False

    # Map number to provider
    provider_info = None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(_PROVIDERS):
            provider_info = _PROVIDERS[idx]
    else:
        # Try to match by id/label
        for p in _PROVIDERS:
            if raw in (p["id"], p["label"].lower()):
                provider_info = p
                break

    if provider_info is None:
        c = _wiz_console()
        c.print()
        c.print(f"   [yellow]Unrecognised choice[/] [bold]'{raw}'[/]. Skipping setup.")
        _mark_dismissed(shell)
        return False

    # --- Ollama special path ---
    if provider_info["id"] == "ollama":
        return _handle_ollama(shell)

    # --- API-key providers ---
    return _handle_api_key_provider(shell, provider_info)


def _handle_ollama(shell: "CliaraShell") -> bool:
    """Guide the user through Ollama setup (delegates to existing wizard)."""
    c = _wiz_console()
    c.print()
    c.print(
        Panel(
            Text.from_markup(
                "[bold]Ollama[/] runs models on your machine — no API key.\n"
                "[dim]The next steps can install Ollama (if needed), pick a model, and connect Cliara.[/]"
            ),
            title=Text.from_markup("[bold cyan]Local LLM[/]"),
            border_style="cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    c.print()
    try:
        from cliara import setup_ollama
        setup_ollama.run(shell)
        return shell.nl_handler.llm_enabled
    except Exception as exc:
        c.print()
        c.print(f"   [red]Error during Ollama setup:[/] {exc}")
        return False


def _handle_api_key_provider(shell: "CliaraShell", provider_info: dict) -> bool:
    """Prompt for an API key, save it, and initialise the LLM client."""
    pid = provider_info["id"]
    label = provider_info["label"]
    signup_url = provider_info["signup_url"]
    env_var = provider_info["env_var"]
    key_hint = provider_info["key_hint"]
    c = _wiz_console()

    c.print()
    c.print(
        Panel(
            Group(
                Text.from_markup(f"Open the link below and create a key for [bold]{label}[/]."),
                Text(""),
                Text(signup_url, style=Style(color="cyan", bold=True, underline=True, link=signup_url)),
            ),
            title=Text.from_markup(f"[bold white]{label}[/] [dim]· API key[/]"),
            border_style="blue",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    c.print()

    try:
        api_key = _read_masked(f"   {key_hint}").strip()
    except (EOFError, KeyboardInterrupt):
        c.print()
        c.print("   [dim]Setup cancelled.[/]")
        _mark_dismissed(shell)
        return False

    if not api_key:
        c.print()
        c.print("   [yellow]No key entered.[/] Skipping setup.")
        _mark_dismissed(shell)
        return False

    # Save to ~/.cliara/.env
    env_path = _write_env_var(env_var, api_key)

    # Apply in-process and reinitialise
    ok = _apply_env_and_reinit(shell, pid, env_var, api_key)

    c.print()
    if ok:
        masked = _mask_key(api_key)
        c.print(
            Panel(
                Group(
                    Text.from_markup(
                        f"[green]{escape(icons.OK)}[/] [bold]{escape(label)}[/] connected  "
                        f"[dim](key: {escape(masked)})[/]"
                    ),
                    Text.from_markup(
                        f"[green]{escape(icons.OK)}[/] Saved to [cyan]{escape(str(env_path))}[/] "
                        "— persists across sessions"
                    ),
                    Text(""),
                    Text.from_markup(
                        "[dim]Try:[/] [bold cyan]?[/] [dim]list python files changed today[/]"
                    ),
                    Text.from_markup(
                        "[dim]     [/][bold cyan]explain[/] [dim]git rebase -i HEAD~3[/]"
                    ),
                ),
                title=Text.from_markup("[bold green]Connected[/]"),
                border_style="green",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        # Clear the dismissed flag in case it was set before
        shell.config.settings["llm_wizard_dismissed"] = False
        shell.config.save()
    else:
        c.print(
            Panel(
                Text.from_markup(
                    f"[red]{escape(icons.FAIL)}[/] Could not connect to [bold]{escape(label)}[/].\n\n"
                    "[dim]Double-check your API key and run[/] [bold]setup-llm[/] [dim]again.[/]"
                ),
                title=Text.from_markup("[bold red]Connection failed[/]"),
                border_style="red",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    return ok


def _mark_dismissed(shell: "CliaraShell") -> None:
    """Persist the user's choice to skip the wizard so it doesn't re-appear."""
    shell.config.settings["llm_wizard_dismissed"] = True
    shell.config.save()
    _print_skip_info()


def _print_skip_info() -> None:
    c = _wiz_console()
    c.print()
    c.print(
        Panel(
            Text.from_markup(
                "[dim]AI features ([bold]?[/], [bold]explain[/], smart push, …) are off.\n\n"
                "Run [bold cyan]setup-llm[/] anytime to add Groq, Gemini, Ollama, or OpenAI.\n\n"
                "Your normal shell, git, and macros keep working as usual.[/]"
            ),
            title=Text.from_markup("[yellow]Shell-only mode[/]"),
            border_style="yellow",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    c.print()

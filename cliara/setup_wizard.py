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

if TYPE_CHECKING:
    from cliara.shell import CliaraShell

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
            print_success(
                f"  LLM: OLLAMA auto-detected and connected  "
                f"(saved to {env_path})"
            )
        except Exception:
            print(f"  [OK] Ollama auto-detected at {_OLLAMA_DEFAULT_URL}")
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

def _print_header():
    print()
    print("  " + "─" * 58)
    print("  Cliara — LLM Setup  (powers ?, explain, smart push, ...)")
    print("  " + "─" * 58)


def _print_menu():
    print()
    print("  No AI provider configured.  Pick one to get started:")
    print()
    for i, p in enumerate(_PROVIDERS, 1):
        rec = "  ← recommended (free)" if p["recommended"] else ""
        print(f"    [{i}] {p['label']:<12}  {p['tagline']}{rec}")
    print(f"    [s] Skip          Use Cliara without AI features")
    print()


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
    _print_header()
    _print_menu()

    # --- Read user choice ---
    try:
        raw = input("  Your choice [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
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
        print(f"\n  Unrecognised choice '{raw}'. Skipping setup.")
        _mark_dismissed(shell)
        return False

    # --- Ollama special path ---
    if provider_info["id"] == "ollama":
        return _handle_ollama(shell)

    # --- API-key providers ---
    return _handle_api_key_provider(shell, provider_info)


def _handle_ollama(shell: "CliaraShell") -> bool:
    """Guide the user through Ollama setup (delegates to existing wizard)."""
    print()
    print("  Launching the Ollama setup wizard...")
    print()
    try:
        from cliara import setup_ollama
        setup_ollama.run(shell)
        return shell.nl_handler.llm_enabled
    except Exception as exc:
        print(f"\n  Error during Ollama setup: {exc}")
        return False


def _handle_api_key_provider(shell: "CliaraShell", provider_info: dict) -> bool:
    """Prompt for an API key, save it, and initialise the LLM client."""
    pid = provider_info["id"]
    label = provider_info["label"]
    signup_url = provider_info["signup_url"]
    env_var = provider_info["env_var"]
    key_hint = provider_info["key_hint"]

    print()
    print(f"  Get your free {label} API key at:")
    print(f"    {signup_url}")
    print()

    try:
        api_key = _read_masked(f"  {key_hint}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        _mark_dismissed(shell)
        return False

    if not api_key:
        print("\n  No key entered. Skipping setup.")
        _mark_dismissed(shell)
        return False

    # Save to ~/.cliara/.env
    env_path = _write_env_var(env_var, api_key)

    # Apply in-process and reinitialise
    ok = _apply_env_and_reinit(shell, pid, env_var, api_key)

    print()
    if ok:
        masked = _mask_key(api_key)
        print(f"  [OK] {label} connected  (key: {masked})")
        print(f"  [OK] Key saved to {env_path}  (persists across sessions)")
        print()
        print("  Try it now:  ? list python files changed today")
        print("               explain git rebase -i HEAD~3")
        print()
        # Clear the dismissed flag in case it was set before
        shell.config.settings["llm_wizard_dismissed"] = False
        shell.config.save()
    else:
        print(f"  [Error] Could not connect to {label}.")
        print("  Double-check your API key and try 'setup-llm' again.")

    return ok


def _mark_dismissed(shell: "CliaraShell") -> None:
    """Persist the user's choice to skip the wizard so it doesn't re-appear."""
    shell.config.settings["llm_wizard_dismissed"] = True
    shell.config.save()
    _print_skip_info()


def _print_skip_info() -> None:
    print()
    print("  AI features are disabled. Run 'setup-llm' any time to set up a provider.")
    print("  Normal shell commands continue to work without an AI provider.")
    print()

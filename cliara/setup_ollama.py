"""
Ollama setup wizard for Cliara.

Handles the full lifecycle of getting Ollama working:
  1. Locate the ollama binary
  2. Start the background service if it isn't running
  3. Present an interactive model picker
  4. Pull the chosen model with live progress
  5. Write OLLAMA_BASE_URL to the project .env file
  6. Persist llm_model to ~/.cliara/config.json
  7. Re-initialise the live LLM client (no restart required)

Public entry-point
------------------
  run(shell)   — called from CliaraShell._handle_setup_ollama()
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from cliara.shell import CliaraShell

# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------

RECOMMENDED_MODELS = [
    {
        "name": "llama3.2",
        "size": "~2 GB",
        "desc": "Best starting point — fast, accurate, low RAM",
        "recommended": True,
    },
    {
        "name": "mistral",
        "size": "~4 GB",
        "desc": "Excellent at following instructions",
        "recommended": False,
    },
    {
        "name": "phi3",
        "size": "~2 GB",
        "desc": "Microsoft's model — very fast, lightweight",
        "recommended": False,
    },
    {
        "name": "qwen2.5",
        "size": "~4 GB",
        "desc": "Strong at code and technical tasks",
        "recommended": False,
    },
    {
        "name": "gemma2",
        "size": "~5 GB",
        "desc": "Google's model — high quality, needs more RAM",
        "recommended": False,
    },
]

DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Common non-PATH install locations per OS
_CANDIDATE_PATHS = {
    "Windows": [
        Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe",
    ],
    "Darwin": [
        Path("/usr/local/bin/ollama"),
        Path("/opt/homebrew/bin/ollama"),
    ],
    "Linux": [
        Path("/usr/local/bin/ollama"),
        Path("/usr/bin/ollama"),
    ],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_ollama() -> Optional[str]:
    """Return path to the ollama binary, or None if not found."""
    found = shutil.which("ollama")
    if found:
        return found
    for candidate in _CANDIDATE_PATHS.get(platform.system(), []):
        if candidate.exists():
            return str(candidate)
    return None


def _service_running(url: str, timeout: int = 3) -> bool:
    """Return True if Ollama is reachable at *url*."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _start_service(ollama_bin: str) -> bool:
    """Attempt to start 'ollama serve' in the background.

    Returns True if the service becomes reachable within ~8 seconds.
    """
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        subprocess.Popen([ollama_bin, "serve"], **kwargs)
    except Exception:
        return False

    for _ in range(8):
        time.sleep(1)
        if _service_running(DEFAULT_OLLAMA_URL):
            return True
    return False


def _list_local_models(ollama_bin: str) -> List[str]:
    """Return model names that are already downloaded (strips :latest tag)."""
    try:
        result = subprocess.run(
            [ollama_bin, "list"],
            capture_output=True, text=True, timeout=10,
        )
        names: List[str] = []
        for line in result.stdout.splitlines()[1:]:   # first line is the header
            parts = line.split()
            if parts:
                names.append(parts[0].split(":")[0])
        return names
    except Exception:
        return []


def _pull_model(ollama_bin: str, model: str, print_fn) -> bool:
    """Stream 'ollama pull <model>' to the terminal.

    Returns True on success.
    """
    try:
        proc = subprocess.Popen(
            [ollama_bin, "pull", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip()
            if line:
                print_fn(f"    {line}")
        proc.wait()
        return proc.returncode == 0
    except Exception as exc:
        print_fn(f"  Error during pull: {exc}")
        return False


def _write_env_var(env_path: Path, key: str, value: str) -> None:
    """Set KEY=VALUE in *env_path*, updating in-place or appending.

    Handles commented-out lines (``# KEY=...``) by un-commenting and updating
    them.  Every other line is left untouched.
    """
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


def _resolve_env_path() -> Path:
    """Find the project .env (searches upward from cwd), or use ./env."""
    try:
        from dotenv import find_dotenv
        found = find_dotenv(usecwd=True)
        if found:
            return Path(found)
    except ImportError:
        pass
    return Path(".env")


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def run(shell: "CliaraShell") -> None:  # noqa: C901  (wizard flow is inherently long)
    """Run the interactive Ollama setup wizard."""
    # Import shell print helpers lazily to avoid circular imports
    from cliara.shell import (
        print_dim, print_error, print_header, print_info,
        print_success, print_warning,
    )

    print()
    print_header("─" * 60)
    print_info("  Cliara × Ollama Setup")
    print_header("─" * 60)
    print()

    # ── Step 1: locate ollama binary ─────────────────────────────────
    print_dim("  Step 1/4  Checking Ollama installation...")

    ollama_bin = _find_ollama()
    if not ollama_bin:
        print_error(
            "\n  Ollama is not installed or not on your PATH.\n"
            "\n  Download the installer from:  https://ollama.com\n"
            "  After installing, re-run:     setup-ollama\n"
        )
        return

    print_success(f"  ✓ Ollama found:  {ollama_bin}")
    print()

    # ── Step 2: ensure the service is running ────────────────────────
    print_dim("  Step 2/4  Checking Ollama service...")

    if not _service_running(DEFAULT_OLLAMA_URL):
        print_warning("  Service not running — attempting to start it...")
        if _start_service(ollama_bin):
            print_success(f"  ✓ Ollama started at {DEFAULT_OLLAMA_URL}")
        else:
            print_error(
                "\n  Could not start Ollama automatically.\n"
                "  Please open Ollama from the Start menu (Windows) or run\n"
                "  'ollama serve' in a separate terminal, then re-run setup-ollama.\n"
            )
            return
    else:
        print_success(f"  ✓ Ollama is running at {DEFAULT_OLLAMA_URL}")

    print()

    # ── Step 3: model picker ─────────────────────────────────────────
    already_have = _list_local_models(ollama_bin)

    print_dim("  Step 3/4  Choose a model\n")
    print(f"  {'#':<4} {'Model':<14} {'Size':<10} Description")
    print(f"  {'─'*4} {'─'*14} {'─'*10} {'─'*40}")
    for i, m in enumerate(RECOMMENDED_MODELS, 1):
        tag  = "  ← recommended" if m["recommended"]   else ""
        have = "  (downloaded)"  if m["name"] in already_have else ""
        print(f"  {i:<4} {m['name']:<14} {m['size']:<10} {m['desc']}{tag}{have}")
    print()

    if already_have:
        print_dim(f"  Already downloaded: {', '.join(already_have)}")
        print()

    try:
        raw = input("  Enter number or model name [llama3.2]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print_dim("  Setup cancelled.")
        return

    # Resolve to a model name
    if raw.isdigit():
        idx = int(raw) - 1
        if not (0 <= idx < len(RECOMMENDED_MODELS)):
            print_error("  Invalid number.")
            return
        chosen = RECOMMENDED_MODELS[idx]["name"]
    elif raw:
        chosen = raw
    else:
        chosen = "llama3.2"

    print()

    # ── Step 4: pull ─────────────────────────────────────────────────
    if chosen in already_have:
        print_success(f"  ✓ {chosen} is already downloaded — skipping pull.")
    else:
        print_dim(f"  Step 4/4  Pulling {chosen}  (may take several minutes)...\n")
        ok = _pull_model(ollama_bin, chosen, print)
        if not ok:
            print_error(f"\n  Pull failed.  Check the model name and try again.")
            return
        print()
        print_success(f"  ✓ {chosen} downloaded.")

    print()

    # ── Persist settings ─────────────────────────────────────────────
    env_path = _resolve_env_path()
    _write_env_var(env_path, "OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)
    print_success(f"  ✓ OLLAMA_BASE_URL written to {env_path}")

    shell.config.set("llm_model",      chosen)
    shell.config.set("llm_provider",   "ollama")
    shell.config.set("ollama_base_url", DEFAULT_OLLAMA_URL)
    print_success(f"  ✓ llm_model = {chosen}  (saved to config)")

    # Re-initialise live — no restart needed
    if shell.nl_handler.initialize_llm("ollama", "ollama", base_url=DEFAULT_OLLAMA_URL):
        print_success("  ✓ LLM re-initialised — ready now!")
    else:
        print_warning("  Re-init failed. Restart cliara to apply changes.")

    print()
    print_header("─" * 60)
    print_info(f"  All done!  Model: {chosen}")
    print_dim( "  Try it:  ? list python files changed today")
    print_dim( "           explain git rebase -i HEAD~3")
    print_header("─" * 60)
    print()

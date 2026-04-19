"""
Ollama setup wizard for Cliara.

Handles the full lifecycle of getting Ollama working:
  1. Locate the ollama binary (optional: run the official installer if missing)
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
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cliara.console import get_console

if TYPE_CHECKING:
    from cliara.shell_app.orchestrator import CliaraShell

# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------

DEFAULT_OLLAMA_MODEL = "gemma4"

RECOMMENDED_MODELS = [
    {
        "name": DEFAULT_OLLAMA_MODEL,
        "size": "~10 GB",
        "desc": "Google Gemma 4 — default, strong general quality (needs more disk/RAM)",
        "recommended": True,
    },
    {
        "name": "llama3.2",
        "size": "~2 GB",
        "desc": "Fast, accurate, lower RAM if Gemma 4 is too heavy",
        "recommended": False,
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
]

DEFAULT_OLLAMA_URL = "http://localhost:11434"
OLLAMA_INSTALL_SCRIPT = "https://ollama.com/install.sh"
OLLAMA_DOWNLOAD_PAGE = "https://ollama.com/download"

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


def _wait_for_ollama_binary(max_seconds: int = 45, interval: float = 1.0) -> Optional[str]:
    """Poll until ``ollama`` appears (installer may update PATH or drop binaries)."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        found = _find_ollama()
        if found:
            return found
        time.sleep(interval)
    return _find_ollama()


def _can_auto_install_ollama() -> Tuple[bool, str]:
    """Whether we can offer a one-shot official install, and a short reason if not."""
    system = platform.system()
    if system == "Windows":
        if shutil.which("winget"):
            return True, ""
        return False, "winget (Windows Package Manager) not found — install Ollama manually."
    if system == "Darwin":
        if shutil.which("brew") or shutil.which("curl"):
            return True, ""
        return False, "Need Homebrew or curl for automatic install."
    if system == "Linux":
        if shutil.which("brew") or shutil.which("curl"):
            return True, ""
        return False, "Need curl (or Homebrew) for automatic install."
    return False, "Automatic Ollama install is not set up for this OS."


def _run_official_ollama_install() -> bool:
    """Run Ollama's supported installer for this OS. Uses inherited stdio (sudo / UAC prompts).

    Returns True if the subprocess exited with code 0.
    """
    system = platform.system()
    inherit = {"stdin": None, "stdout": None, "stderr": None}

    if system == "Darwin":
        brew = shutil.which("brew")
        if brew:
            r = subprocess.run([brew, "install", "ollama"], **inherit)  # type: ignore[arg-type]
            return r.returncode == 0
        r = subprocess.run(
            ["sh", "-c", f"curl -fsSL {OLLAMA_INSTALL_SCRIPT} | sh"],
            **inherit,  # type: ignore[arg-type]
        )
        return r.returncode == 0

    if system == "Linux":
        brew = shutil.which("brew")
        if brew:
            r = subprocess.run([brew, "install", "ollama"], **inherit)  # type: ignore[arg-type]
            if r.returncode == 0:
                return True
        r = subprocess.run(
            ["sh", "-c", f"curl -fsSL {OLLAMA_INSTALL_SCRIPT} | sh"],
            **inherit,  # type: ignore[arg-type]
        )
        return r.returncode == 0

    if system == "Windows":
        winget = shutil.which("winget")
        if not winget:
            return False
        r = subprocess.run(
            [
                winget,
                "install",
                "-e",
                "--id",
                "Ollama.Ollama",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            **inherit,  # type: ignore[arg-type]
        )
        return r.returncode == 0

    return False


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


def _print_recommended_models_table(already_have: Set[str]) -> None:
    """Render the model catalogue as a bordered Rich table."""
    console = get_console()
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", justify="center", width=3)
    table.add_column("Model", style="bold white", min_width=10, no_wrap=True)
    table.add_column("Size", style="cyan", width=9, no_wrap=True)
    table.add_column("Description", min_width=28)

    for i, m in enumerate(RECOMMENDED_MODELS, 1):
        notes = Text(m["desc"], style="white")
        badges: List[Tuple[str, str]] = []
        if m["recommended"]:
            badges.append(("★ Recommended", "bold green"))
        if m["name"] in already_have:
            badges.append(("✓ Downloaded", "green"))
        if badges:
            notes.append("\n")
            for j, (label, style) in enumerate(badges):
                if j:
                    notes.append("  ·  ", style="dim")
                notes.append(label, style=style)
        table.add_row(str(i), m["name"], m["size"], notes)

    panel = Panel(
        table,
        title=Text.from_markup("[bold white]Choose a model[/] [dim]·[/] [cyan]ollama[/]"),
        border_style="cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print()
    console.print(panel)
    console.print()


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def run(shell: "CliaraShell") -> None:  # noqa: C901  (wizard flow is inherently long)
    """Run the interactive Ollama setup wizard."""
    # Import shell print helpers lazily to avoid circular imports
    from cliara.shell_app.orchestrator import (
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
        print_warning("\n  Ollama is not installed or not on your PATH.\n")
        auto_ok, auto_msg = _can_auto_install_ollama()
        if not auto_ok:
            print_error(f"  {auto_msg}")
            print_dim(f"\n  Download:  {OLLAMA_DOWNLOAD_PAGE}")
            print_dim("  After installing, re-run:  setup-ollama")
            print_dim("  Or run setup-llm for Groq, Gemini, OpenAI, etc.\n")
            return

        print_dim(
            "  Cliara can launch the official Ollama installer (internet required;"
        )
        print_dim(
            "  Linux/macOS may prompt for your password; Windows may show winget / UAC).\n"
        )
        try:
            raw_install = input("  Install Ollama now? (y/n) [n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print_dim("  Setup cancelled.")
            return

        if raw_install not in ("y", "yes"):
            print_dim(f"\n  Install manually when ready:  {OLLAMA_DOWNLOAD_PAGE}")
            print_dim("  Then re-run:  setup-ollama")
            print_dim("  Or run setup-llm for cloud providers.\n")
            return

        print_dim("\n  Running installer (follow any prompts below)...\n")
        if not _run_official_ollama_install():
            print_error("\n  Installer exited with an error or was cancelled.")
            print_dim(f"  Try again or install from:  {OLLAMA_DOWNLOAD_PAGE}\n")
            return

        print_dim("\n  Looking for the Ollama command...")
        ollama_bin = _wait_for_ollama_binary()
        if not ollama_bin:
            print_error(
                "\n  Ollama still not found. Open a new terminal if PATH was updated,"
            )
            print_error("  or install from the link below and re-run setup-ollama.")
            print_dim(f"\n  {OLLAMA_DOWNLOAD_PAGE}\n")
            return

        print_success(f"  ✓ Ollama found:  {ollama_bin}")
        print()
    else:
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
    already_have_list = _list_local_models(ollama_bin)
    already_have = set(already_have_list)

    print_dim("  Step 3/4  Choose a model")
    _print_recommended_models_table(already_have)

    if already_have_list:
        print_dim(f"  Already downloaded: {', '.join(already_have_list)}")
        print()

    try:
        raw = input(f"  Enter number or model name [{DEFAULT_OLLAMA_MODEL}]: ").strip()
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
        chosen = DEFAULT_OLLAMA_MODEL

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

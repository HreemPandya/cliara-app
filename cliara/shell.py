"""
Shell wrapper/proxy for Cliara.
Handles command pass-through, NL routing, and macro execution.
"""

import subprocess
import sys
import os
import platform
import queue
import random
import re
import shlex
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional, List, Tuple, Union
from pathlib import Path

from cliara.config import Config
from cliara.macros import MacroManager
from cliara.safety import SafetyChecker, DangerLevel
from cliara.nl_handler import NLHandler
from cliara.diff_preview import DiffPreview
from cliara.deploy_detector import detect_all as detect_deploy_targets, DeployPlan
from cliara.deploy_store import DeployStore
from cliara.semantic_history import SemanticHistoryStore
from cliara.session_store import (
    SessionStore,
    TaskSession,
    _get_project_root,
    _get_branch,
)
from cliara.execution_graph import (
    build_execution_tree,
    render_execution_tree,
    export_tree_json,
)
from cliara.cross_platform import (
    get_base_command,
    command_exists,
    is_powershell,
    translate_command,
    translate_pipeline,
)
from cliara import regression
from cliara.copilot_gate import (
    SourceDetector,
    InputSource,
    RiskEngine,
    CopilotGate,
)


# ---------------------------------------------------------------------------
# Colorized output helpers (Rich-backed for Cliara UI)
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Check if the terminal supports ANSI colors (used by progress bar and spinner)."""
    if os.getenv("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return True

_COLOR = _supports_color()

# Enable ANSI escape sequences on Windows 10+
if _COLOR and platform.system() == "Windows":
    os.system("")


def _c(code: str, text: str) -> str:
    """Wrap *text* with an ANSI escape if colors are enabled (progress bar, spinner)."""
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _cliara_console():
    """Lazy import to avoid circular deps; Rich used for all Cliara print_* output."""
    from cliara.console import get_console
    return get_console()


def print_success(msg: str):
    """Print a green success message."""
    _cliara_console().print(msg, style="green")


def print_error(msg: str, **kw):
    """
    Print an error message with the important parts highlighted in light red,
    without making the entire line bright red (which can be jarring).
    """
    from rich.text import Text

    # If the caller passes a Rich Text object or something already styled,
    # just print it as-is.
    if isinstance(msg, Text):
        _cliara_console().print(msg, **kw)
        return

    text = Text(str(msg))

    # Highlight common error prefixes lightly so only the important cue is red.
    prefixes = ["[Error]", "[Cliara]", "[X]"]
    for p in prefixes:
        idx = text.plain.find(p)
        if idx != -1:
            text.stylize("bold bright_red", idx, idx + len(p))
            break

    _cliara_console().print(text, **kw)


def print_warning(msg: str):
    """Print a yellow warning message."""
    _cliara_console().print(msg, style="yellow")


def print_info(msg: str):
    """Print a cyan informational message."""
    _cliara_console().print(msg, style="cyan")


def print_header(msg: str):
    """Print a bold header message."""
    _cliara_console().print(msg, style="bold")


def print_dim(msg: str):
    """Print a dimmed/muted message."""
    _cliara_console().print(msg, style="dim")


# ---------------------------------------------------------------------------
# Typo-tolerant "fix" detection
# ---------------------------------------------------------------------------

def _edit_distance(s: str, t: str) -> int:
    """Levenshtein edit distance between two short strings."""
    if len(s) < len(t):
        return _edit_distance(t, s)
    if not t:
        return len(s)
    prev = list(range(len(t) + 1))
    for sc in s:
        curr = [prev[0] + 1]
        for j, tc in enumerate(t):
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + (0 if sc == tc else 1)))
        prev = curr
    return prev[-1]


def _looks_like_fix(query: str) -> bool:
    """
    Return True if *query* is 'fix' or an obvious typo of it.

    Catches: fox, fxi, fiz, fux, fi, fixe, etc. — without ever prompting
    the user.  Only considers single short words so normal NL queries
    like 'fix the deploy script' are NOT caught.
    """
    word = query.strip().lower()
    if word == "fix":
        return True
    # Multi-word → real NL query, not a typo
    if " " in word or len(word) > 5 or len(word) < 2:
        return False
    # Single substitution / insertion / deletion
    if _edit_distance(word, "fix") <= 1:
        return True
    # Adjacent-key transposition like "fxi" or "ifx"
    if sorted(word) == sorted("fix"):
        return True
    return False


def _looks_like_why(query: str) -> bool:
    """
    Return True if *query* is 'why' or an obvious typo (for regression deep-dive).
    """
    word = query.strip().lower()
    if word == "why":
        return True
    if " " in word or len(word) > 4 or len(word) < 2:
        return False
    if _edit_distance(word, "why") <= 1:
        return True
    if sorted(word) == sorted("why"):
        return True
    return False


def _is_semantic_history_search_intent(query: str) -> bool:
    """Return True if the query looks like a search over past commands by intent."""
    q = query.strip().lower()
    if not q:
        return False
    if q.startswith("find "):
        return True
    if q.startswith("when did i "):
        return True
    if q.startswith("what did i run"):
        return True
    if "when did i " in q:
        return True
    if "what did i run " in q or q == "what did i run":
        return True
    if q.startswith("search history"):
        return True
    if q.startswith("history ") and len(q) > 8:
        return True
    return False


# ---------------------------------------------------------------------------
# Startup progress bar
# ---------------------------------------------------------------------------

class _StartupProgress:
    """
    Pip/npm-style progress bar for startup initialization.

    Renders a single updating line like:
        Initializing Cliara...  [########·············]  Loading macros
    """

    BAR_WIDTH = 30  # characters inside the brackets

    def __init__(self, total_steps: int):
        self.total = total_steps
        self.current = 0
        self._label = ""
        self._finished = False
        # Check mark / cross — keep it simple for all terminals
        self._check = _c("32", "OK") if _COLOR else "OK"

    # -- internal helpers ---------------------------------------------------
    def _render(self):
        """Redraw the progress line in-place, respecting terminal width."""
        frac = self.current / self.total if self.total else 1
        filled = int(frac * self.BAR_WIDTH)
        empty = self.BAR_WIDTH - filled

        bar_filled = _c("36", "#" * filled) if _COLOR else "#" * filled
        bar_empty = _c("2", "." * empty) if _COLOR else "." * empty
        pct = f"{int(frac * 100):>3}%"

        # Fixed-width prefix:  "  [" + 30-char bar + "] NNN%  " = 41 visible chars
        prefix = f"  [{bar_filled}{bar_empty}] {pct}  "
        prefix_visible_len = 2 + 1 + self.BAR_WIDTH + 2 + 4 + 2  # 41

        # Truncate the label so the full line never exceeds terminal width
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80
        max_label = max(cols - prefix_visible_len - 1, 0)  # -1 safety margin
        label = self._label[:max_label]

        line = f"{prefix}{label}"
        # \r returns to column 0; \033[K clears from cursor to end of line
        clear = "\033[K" if _COLOR else " " * max(cols - prefix_visible_len - len(label), 0)
        sys.stdout.write(f"\r{line}{clear}")
        sys.stdout.flush()

    # -- public API ---------------------------------------------------------
    def step(self, label: str):
        """Advance progress by one step and display *label*."""
        self.current = min(self.current + 1, self.total)
        self._label = label
        self._render()
        # Tiny pause so the user can actually see the bar move — without
        # this, fast steps would flash by invisibly.
        time.sleep(0.08)

    def finish(self):
        """Complete the bar and move to the next line."""
        if self._finished:
            return
        self._finished = True
        self.current = self.total
        self._label = _c("32", "Ready!") if _COLOR else "Ready!"
        self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Live spinner / elapsed-time timer for long-running commands
# ---------------------------------------------------------------------------

class _NullTimer:
    """No-op timer used when the spinner feature is disabled."""

    def start(self):
        pass

    def stop(self):
        pass

    @contextmanager
    def output_lock(self):
        yield


class _LiveTimer:
    """
    Background spinner + elapsed-time indicator for long-running commands.

    After *delay* seconds of silence, starts showing:
      - The terminal title bar with a spinner + elapsed time (always)
      - An inline dim spinner on stderr (only when *inline=True*)

    In **capture mode** (``inline=True``) nothing else prints to the
    terminal, so the inline spinner is safe.  In **streaming mode**
    (``inline=False``) the child's stdout is inherited and shares the
    terminal cursor, so only the title bar is updated to avoid garbled
    output.
    """

    FRAMES = ("|", "/", "-", "\\")

    def __init__(self, command: str, delay: float = 3.0, inline: bool = True):
        short = command if len(command) <= 30 else command[:27] + "..."
        self._short_cmd = short
        self._delay = delay
        self._inline = inline
        self._start_time = time.time()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._spinner_visible = False
        self._title_changed = False

    # ── public API ─────────────────────────────────────────────────

    def start(self):
        """Launch the background timer thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the timer and clean up terminal artefacts."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._clear_spinner()
            self._restore_title()

    @contextmanager
    def output_lock(self):
        """
        Context manager for external writers (e.g. the stderr drain thread).

        Clears the spinner line, yields so the caller can write freely,
        then releases.  The spinner redraws itself on its next tick.
        """
        with self._lock:
            self._clear_spinner()
            yield

    # ── internals ──────────────────────────────────────────────────

    def _clear_spinner(self):
        """Erase the spinner line if it is currently visible."""
        if self._spinner_visible:
            if _COLOR:
                sys.stderr.write("\r\033[K")
            else:
                sys.stderr.write("\r" + " " * 40 + "\r")
            sys.stderr.flush()
            self._spinner_visible = False

    def _restore_title(self):
        """Reset the terminal title to 'Cliara'."""
        if self._title_changed and _COLOR:
            sys.stderr.write("\033]0;Cliara\007")
            sys.stderr.flush()
            self._title_changed = False

    def _run(self):
        """Timer loop: wait for the delay, then tick every 0.5 s."""
        # If the command finishes before the delay, exit silently
        if self._stop_event.wait(timeout=self._delay):
            return

        idx = 0
        while not self._stop_event.is_set():
            elapsed = time.time() - self._start_time
            elapsed_str = self._fmt(elapsed)
            frame = self.FRAMES[idx % len(self.FRAMES)]

            with self._lock:
                # Terminal title (written to stderr to avoid interleaving
                # with child stdout which is inherited)
                if _COLOR:
                    sys.stderr.write(
                        f"\033]0;{frame} {self._short_cmd}  {elapsed_str}\007"
                    )
                    self._title_changed = True

                # Inline spinner — only in capture mode where nothing
                # else is printing to the terminal.
                if self._inline:
                    line = f"  {frame} running... {elapsed_str}"
                    if _COLOR:
                        sys.stderr.write(f"\r\033[K\033[2m{line}\033[0m")
                    else:
                        sys.stderr.write(f"\r{line}        ")
                    self._spinner_visible = True

                sys.stderr.flush()

            idx += 1
            self._stop_event.wait(timeout=0.5)

    @staticmethod
    def _fmt(seconds: float) -> str:
        """Format seconds as a compact elapsed-time string."""
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"


class CommandHistory:
    """Track command history with on-disk persistence and readline support."""
    
    def __init__(self, max_size: int = 1000, history_file: Optional[Path] = None):
        self.history: List[str] = []
        self.max_size = max_size
        self.last_commands: List[str] = []  # Commands from last execution
        self.history_file: Optional[Path] = history_file
        self._readline = None  # Will be set during setup_readline()
        
        # Load persisted history from disk
        if self.history_file:
            self._load_from_file()
    
    # ------------------------------------------------------------------
    # Readline integration (arrow-key recall across sessions)
    # ------------------------------------------------------------------
    def setup_readline(self):
        """
        Set up readline so arrow-up/down recalls previous commands.
        Must be called once before the main input loop.
        """
        try:
            # On Windows, the built-in readline stub doesn't work.
            # Try pyreadline3 first, then fall back to the stdlib module.
            try:
                import pyreadline3  # noqa: F401  (import activates it)
                import readline
            except ImportError:
                import readline
            
            self._readline = readline
            
            # Feed persisted history into readline's buffer
            for cmd in self.history:
                readline.add_history(cmd)
            
            # Try to bind tab-completion (nice-to-have, not essential)
            try:
                readline.parse_and_bind("tab: complete")
            except Exception:
                pass
            
        except ImportError:
            # readline completely unavailable – arrow keys won't work,
            # but file persistence still will.
            self._readline = None
    
    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _load_from_file(self):
        """Load history lines from the on-disk file."""
        if not self.history_file or not self.history_file.exists():
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                lines = [line.rstrip("\n") for line in f if line.strip()]
            # Keep only the last max_size entries
            self.history = lines[-self.max_size:]
        except Exception:
            # Corrupt / unreadable file – start fresh
            self.history = []
    
    def _append_to_file(self, command: str):
        """Append a single command to the on-disk history file."""
        if not self.history_file:
            return
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(command + "\n")
        except Exception:
            pass  # Non-critical – don't crash the shell
    
    def _trim_file(self):
        """Trim the on-disk file to max_size lines (called occasionally)."""
        if not self.history_file or not self.history_file.exists():
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > self.max_size * 2:
                # Only trim when the file is significantly over limit
                with open(self.history_file, "w", encoding="utf-8") as f:
                    f.writelines(lines[-self.max_size:])
        except Exception:
            pass
    
    # ------------------------------------------------------------------
    # Public API (unchanged signatures)
    # ------------------------------------------------------------------
    def add(self, command: str):
        """Add command to history (memory + disk + readline)."""
        self.history.append(command)
        if len(self.history) > self.max_size:
            self.history.pop(0)
        
        # Persist to disk
        self._append_to_file(command)
        self._trim_file()
        
        # Push into readline buffer so arrow-up sees it immediately
        if self._readline:
            try:
                self._readline.add_history(command)
            except Exception:
                pass
    
    def set_last_execution(self, commands: List[str]):
        """Store commands from last execution."""
        self.last_commands = commands.copy()
    
    def get_last(self) -> List[str]:
        """Get last executed commands."""
        return self.last_commands.copy()
    
    def get_recent(self, n: int = 10) -> List[str]:
        """Get n most recent commands."""
        return self.history[-n:] if n < len(self.history) else self.history.copy()


class CliaraShell:
    """Main Cliara shell - wraps user's real shell."""
    
    def __init__(self, config: Optional[Config] = None):
        """
        Initialize Cliara shell.
        
        Args:
            config: Configuration object (creates default if None)
        """
        # --- Startup progress bar ---
        progress = _StartupProgress(total_steps=6)

        print()  # blank line before the bar
        progress.step("Loading config...")
        self.config = config or Config()

        progress.step("Setting up macros...")
        # Pass config dict to MacroManager for storage backend selection
        config_dict = {
            "storage_backend": self.config.get("storage_backend", "json"),
            "storage_path": str(self.config.get_macros_path()),
            "macro_storage": str(self.config.get_macros_path()),
            "postgres": self.config.get("postgres", {}),
            "connection_string": self.config.get("connection_string"),
        }
        self.macros = MacroManager(config=config_dict)

        progress.step("Loading safety checker...")
        self.safety = SafetyChecker()
        self.diff_preview = DiffPreview()
        self.nl_handler = NLHandler(self.safety, config=self.config)

        # Copilot Gate — AI-command interception
        self._source_detector = SourceDetector()
        self._risk_engine = RiskEngine(self.safety, self.diff_preview)
        self._copilot_gate = CopilotGate(
            self._risk_engine,
            auto_approve_safe=self.config.get("copilot_gate_auto_approve_safe", True),
        )

        progress.step("Loading history...")
        history_file = self.config.config_dir / "history.txt"
        self.history = CommandHistory(
            max_size=self.config.get("history_size", 1000),
            history_file=history_file,
        )

        self.running = True
        self.shell_path = self.config.get("shell")
        if not self.shell_path:
            self.shell_path = self.config._detect_shell()

        # Deploy store — persisted per-project deploy configs
        self.deploy_store = DeployStore()

        # Task sessions — named, resumable workflow context
        sessions_path = self.config.config_dir / "sessions.json"
        self.session_store = SessionStore(store_path=sessions_path)
        self.current_session: Optional[TaskSession] = None
        # When set, the next recorded command is linked as child of this id (e.g. fix after failure)
        self._next_command_parent_id: Optional[str] = None

        # Error translator state — populated by execute_shell_command()
        self.last_stderr: str = ""
        self.last_exit_code: int = 0
        self.last_command: str = ""  # Last shell command that was executed

        # Prompt session reference — set in run().
        self._prompt_session = None

        # Pending fix command — set by _auto_suggest_fix(), consumed by
        # the Tab key binding in prompt_toolkit.  Pressing Tab on an empty
        # prompt fills in this command; any other input clears it.
        self._pending_fix: Optional[str] = None

        # Regression detection — last report (ranked_causes, last_snapshot, current_snapshot)
        # for ? why after an automatic regression check on failure.
        self._last_regression_report: Optional[Tuple[List[Tuple[str, str]], dict, dict]] = None

        # Semantic history — store + background worker for ? find / ? when did I ...
        self._semantic_history: Optional[SemanticHistoryStore] = None
        self._semantic_history_queue: Optional[queue.Queue] = None
        self._semantic_history_thread: Optional[threading.Thread] = None
        self._last_explained_command: Optional[str] = None
        self._last_explained_summary: Optional[str] = None
        if self.config.get("semantic_history_enabled", True):
            max_entries = self.config.get("semantic_history_max_entries", 500)
            store_path = self.config.config_dir / "semantic_history.json"
            self._semantic_history = SemanticHistoryStore(
                store_path=store_path,
                max_entries=max_entries,
            )
            if self.config.get("semantic_history_summary_on_add", True):
                self._semantic_history_queue = queue.Queue()
                self._semantic_history_thread = threading.Thread(
                    target=self._semantic_history_worker,
                    daemon=True,
                )
                self._semantic_history_thread.start()

        progress.step("Connecting LLM...")
        # Initialize LLM if API key is available
        self._initialize_llm(quiet=True)

        progress.step("Detecting environment...")
        # Finish the progress bar
        progress.finish()
        
        # Show LLM status after the progress bar (single clean line)
        if self.nl_handler.llm_enabled:
            print_success(f"  LLM: {self.nl_handler.provider.upper()} connected")
        
        # First-run setup
        if self.config.is_first_run():
            self.config.setup_first_run()
    
    def _initialize_llm(self, quiet: bool = False):
        """Initialize LLM if API key is configured."""
        provider = self.config.get_llm_provider()
        api_key = self.config.get_llm_api_key()

        if provider and api_key:
            base_url = self.config.get_ollama_base_url() if provider == "ollama" else None
            if self.nl_handler.initialize_llm(provider, api_key, base_url=base_url):
                if not quiet:
                    model = self.config.get_llm_model() or ""
                    model_hint = f", model: {model}" if model else ""
                    print_success(f"[OK] LLM initialized ({provider}{model_hint})")
            else:
                if not quiet:
                    print_warning(f"[Warning] Failed to initialize LLM ({provider})")
        else:
            pass

    def _semantic_history_worker(self):
        """Background worker: get (command, cwd, exit_code, summary_override) from queue; add to semantic store."""
        q = self._semantic_history_queue
        store = self._semantic_history
        if not q or not store:
            return
        while True:
            item = None
            try:
                item = q.get()
                if item is None:
                    break
                command, cwd, exit_code, summary_override = item
                if summary_override:
                    summary = summary_override
                else:
                    context = {
                        "cwd": cwd or str(Path.cwd()),
                        "os": platform.system(),
                        "shell": self.shell_path or os.environ.get("SHELL", "bash"),
                    }
                    summary = self.nl_handler.summarize_command_for_history(command, context) or ""

                # Generate embedding when the feature is enabled
                embedding = None
                if self.config.get("semantic_history_use_embeddings", False):
                    emb_text = f"{command} {summary}".strip()
                    embedding = self.nl_handler.get_embedding(emb_text)

                store.add(
                    command=command,
                    summary=summary,
                    cwd=cwd,
                    exit_code=exit_code,
                    embedding=embedding,
                )
            except Exception:
                # Still add with empty summary so store populates (e.g. LLM timeout)
                if item is not None:
                    try:
                        command, cwd, exit_code, _ = item
                        store.add(command=command, summary="", cwd=cwd, exit_code=exit_code)
                    except Exception:
                        pass
            finally:
                try:
                    q.task_done()
                except Exception:
                    pass

    def _enqueue_semantic_add(
        self,
        command: str,
        cwd: Optional[str] = None,
        exit_code: Optional[int] = None,
    ):
        """Add command to semantic history (sync so store is never empty); enqueue for background summary."""
        if not self._semantic_history:
            return
        summary_override = None
        if self._last_explained_command is not None and command.strip() == self._last_explained_command.strip():
            summary_override = self._last_explained_summary or ""
            self._last_explained_command = None
            self._last_explained_summary = None
        # Add synchronously so store is always populated even if worker fails or is slow
        try:
            self._semantic_history.add(
                command=command,
                summary=summary_override or "",
                cwd=cwd,
                exit_code=exit_code,
            )
        except Exception:
            pass
        # Enqueue for background summary (worker will add again with summary; dedupe replaces)
        if not self._semantic_history_queue:
            return
        if not self.config.get("semantic_history_summary_on_add", True):
            return
        try:
            self._semantic_history_queue.put((command, cwd, exit_code, summary_override))
        except Exception:
            pass

    # Rotating "did you know?" tips shown on startup.
    # Each entry may contain {nl} which is replaced by the configured nl_prefix.
    _STARTUP_TIPS: List[str] = [
        "Try '{nl} fix' right after a failed command — Cliara diagnoses the error and suggests a fix.",
        "'{nl} why' runs a regression deep-dive: it compares the current failure to past successes.",
        "'{nl} find <phrase>' searches your command history by intent, not just text.",
        "Prefix any command with '{nl}' to translate plain English to shell — e.g. '{nl} kill port 3000'.",
        "Use 'explain <cmd>' to get a plain-English breakdown of any shell command.",
        "Run 'macro add <name>' to save any command (or a series) as a reusable macro.",
        "Run 'macro add <name> --nl' to define a macro entirely in plain English.",
        "Type just a macro name to run it — no prefix needed.",
        "Risky commands (rm -rf, format …) always pause for approval, even when piped.",
        "'push' automatically writes your commit message and selects the right branch.",
        "'session start <name>' groups your work so you can review what you shipped later.",
        "'session end' closes the session; 'session list' shows all past ones.",
        "Run 'theme <name>' to switch colour themes — try dracula, nord, or catppuccin.",
        "'history' shows recent commands; '{nl} when did I <phrase>' finds them by meaning.",
        "Cliara watches long-running commands and notifies you when they finish.",
        "Set OPENAI_API_KEY in a .env file and Cliara picks it up automatically.",
        "Use '{nl} deploy' to get guided deployment steps for your current project.",
        "The diff preview shows what a destructive command will affect before it runs.",
        "'macro list' shows all your saved macros with descriptions and run counts.",
        "Press Ctrl+C to cancel a running command; Cliara will offer to diagnose failures.",
    ]

    def _pick_tip(self) -> str:
        """Return a random startup tip, substituting the configured nl_prefix."""
        nl = self.config.get("nl_prefix", "?")
        tip = random.choice(self._STARTUP_TIPS)
        return tip.replace("{nl}", nl)

    def print_banner(self):
        """Print welcome banner as a Rich Panel."""
        from cliara import __version__
        from rich.panel import Panel
        nl = self.config.get('nl_prefix', '?')
        lines = [
            f"Shell: {self.shell_path}",
            "",
        ]
        if self.nl_handler.llm_enabled:
            lines.append(f"LLM: {self.nl_handler.provider.upper()} (Ready)")
        else:
            lines.append("LLM: Not configured (set OPENAI_API_KEY in .env)")
        lines.extend([
            "",
            "Quick tips:",
            f"  • {nl} <query>             Ask in plain English" + (" (e.g. " + nl + " list large files)" if self.nl_handler.llm_enabled else " (requires API key)"),
            f"  • {nl} fix                 Diagnose & fix the last failed command",
            f"  • {nl} why                 Regression deep-dive after a failure",
            f"  • {nl} find / when did I   Search history by meaning",
            "  • session start <name>   Start a task session",
            "  • session end [note]     End session — session help for more",
            "  • session help           More session commands (notes, list, show)",
            "  • push                    Smart git push — auto-commit message & branch",
            "  • explain <cmd>           Understand any command  (e.g. explain git rebase)",
            "  • macro add <name>        Create a reusable macro",
            "  • macro add <name> --nl   Create a macro from plain English",
            "  • <macro-name>            Run a saved macro",
            "  • help                    Show all commands",
            "  • version                 Show Cliara version",
            "  • theme [name]            List or set color theme (e.g. dracula, nord)",
            "  • exit                    Quit Cliara",
        ])
        lines.extend([
            "",
            f"[dim]💡 Did you know? {self._pick_tip()}[/dim]",
        ])
        content = "\n".join(lines)
        panel = Panel(content, title=f"Cliara {__version__} — AI-Powered Shell", border_style="cyan")
        _cliara_console().print(panel)
        _cliara_console().print()
    
    # ------------------------------------------------------------------
    # Highlighted prompt (prompt_toolkit + Pygments)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_system_clipboard() -> str:
        """
        Read text from the OS clipboard without extra dependencies.

        Windows: ctypes with safe pointer handling
        macOS:   pbpaste
        Linux:   xclip / xsel
        """
        if platform.system() == "Windows":
            try:
                import ctypes
                import ctypes.wintypes as wt

                CF_UNICODETEXT = 13

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

                user32.OpenClipboard.argtypes = [wt.HWND]
                user32.OpenClipboard.restype = wt.BOOL
                user32.GetClipboardData.argtypes = [wt.UINT]
                user32.GetClipboardData.restype = wt.HANDLE
                user32.CloseClipboard.argtypes = []
                user32.CloseClipboard.restype = wt.BOOL

                kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
                kernel32.GlobalLock.restype = ctypes.c_void_p
                kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
                kernel32.GlobalUnlock.restype = wt.BOOL
                kernel32.GlobalSize.argtypes = [wt.HGLOBAL]
                kernel32.GlobalSize.restype = ctypes.c_size_t

                if not user32.OpenClipboard(None):
                    return ""
                try:
                    handle = user32.GetClipboardData(CF_UNICODETEXT)
                    if not handle:
                        return ""
                    size = kernel32.GlobalSize(handle)
                    if size == 0:
                        return ""
                    ptr = kernel32.GlobalLock(handle)
                    if not ptr:
                        return ""
                    try:
                        max_chars = size // 2
                        text = ctypes.wstring_at(ptr, max_chars)
                        return text.rstrip("\x00")
                    finally:
                        kernel32.GlobalUnlock(handle)
                finally:
                    user32.CloseClipboard()
            except Exception:
                return ""
        elif platform.system() == "Darwin":
            try:
                r = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=2,
                )
                return r.stdout if r.returncode == 0 else ""
            except Exception:
                return ""
        else:
            for tool in (
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
            ):
                try:
                    r = subprocess.run(
                        tool, capture_output=True, text=True, timeout=2,
                    )
                    if r.returncode == 0:
                        return r.stdout
                except Exception:
                    continue
            return ""

    @staticmethod
    def _write_system_clipboard(text: str) -> bool:
        """
        Write text to the OS clipboard without extra dependencies.

        Windows: ctypes
        macOS:   pbcopy
        Linux:   xclip / xsel

        Returns True on success, False on failure.
        """
        if platform.system() == "Windows":
            try:
                import ctypes
                import ctypes.wintypes as wt

                CF_UNICODETEXT = 13
                GMEM_MOVEABLE = 0x0002

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

                user32.OpenClipboard.argtypes = [wt.HWND]
                user32.OpenClipboard.restype = wt.BOOL
                user32.EmptyClipboard.argtypes = []
                user32.EmptyClipboard.restype = wt.BOOL
                user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
                user32.SetClipboardData.restype = wt.HANDLE
                user32.CloseClipboard.argtypes = []
                user32.CloseClipboard.restype = wt.BOOL

                kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
                kernel32.GlobalAlloc.restype = wt.HGLOBAL
                kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
                kernel32.GlobalLock.restype = ctypes.c_void_p
                kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
                kernel32.GlobalUnlock.restype = wt.BOOL

                encoded = text.encode("utf-16-le") + b"\x00\x00"
                if not user32.OpenClipboard(None):
                    return False
                try:
                    user32.EmptyClipboard()
                    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
                    if not handle:
                        return False
                    ptr = kernel32.GlobalLock(handle)
                    if not ptr:
                        return False
                    ctypes.memmove(ptr, encoded, len(encoded))
                    kernel32.GlobalUnlock(handle)
                    if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                        return False
                    return True
                finally:
                    user32.CloseClipboard()
            except Exception:
                return False
        elif platform.system() == "Darwin":
            try:
                r = subprocess.run(
                    ["pbcopy"], input=text, text=True, timeout=2,
                )
                return r.returncode == 0
            except Exception:
                return False
        else:
            for tool in (
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ):
                try:
                    r = subprocess.run(
                        tool, input=text, text=True, timeout=2,
                    )
                    if r.returncode == 0:
                        return True
                except Exception:
                    continue
            return False

    def _create_prompt_session(self):
        """
        Build a prompt_toolkit PromptSession with syntax highlighting.

        Returns the session, or *None* if prompt_toolkit / pygments are
        unavailable (falls back to plain ``input()``).
        """
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.lexers import PygmentsLexer
            from prompt_toolkit.styles import merge_styles, Style as PTStyle
            from prompt_toolkit.styles.pygments import style_from_pygments_cls
            from cliara.highlighting import ShellLexer, get_style_for_theme

            # ── Custom key bindings ──
            kb = KeyBindings()

            @kb.add("c-v", eager=True)
            def _paste(event):
                """Paste from the system clipboard (Ctrl+V)."""
                try:
                    text = self._read_system_clipboard()
                    if text:
                        event.current_buffer.insert_text(text)
                        self._source_detector.mark_paste()
                except Exception:
                    pass

            try:
                from prompt_toolkit.keys import Keys
                @kb.add(Keys.BracketedPaste)
                def _bracketed_paste(event):
                    """Bracket-paste: terminal wraps pasted text in escape sequences."""
                    event.current_buffer.insert_text(event.data)
                    self._source_detector.mark_paste()
            except (ImportError, Exception):
                pass

            @kb.add("tab", eager=True)
            def _accept_fix(event):
                """Tab on empty prompt → fill in the pending fix suggestion."""
                buf = event.current_buffer
                if buf.text == "" and self._pending_fix:
                    buf.insert_text(self._pending_fix)
                    self._pending_fix = None
                else:
                    # Default Tab behaviour (completion)
                    buf.complete_next()

            # Seed prompt history from existing command history so
            # arrow-up recalls previous sessions' commands.
            pt_history = InMemoryHistory()
            for cmd in self.history.history:
                pt_history.store_string(cmd)

            theme_name = self.config.get("theme", "monokai")
            style_cls, prompt_style = get_style_for_theme(theme_name)
            style = merge_styles([
                style_from_pygments_cls(style_cls),
                PTStyle.from_dict(prompt_style),
            ])

            return PromptSession(
                lexer=PygmentsLexer(ShellLexer),
                style=style,
                history=pt_history,
                key_bindings=kb,
                auto_suggest=AutoSuggestFromHistory(),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Main REPL
    # ------------------------------------------------------------------
    def run(self):
        """Main shell loop."""
        self.print_banner()

        # Try to set up the highlighted prompt; fall back to plain input
        self._prompt_session = self._create_prompt_session()
        if self._prompt_session is None:
            # prompt_toolkit unavailable — use readline instead
            self.history.setup_readline()

        # Use safe prompt character for Windows
        prompt_arrow = ">" if platform.system() == "Windows" else ">"

        while self.running:
            try:
                cwd = str(Path.cwd())

                if self._prompt_session is not None:
                    # Coloured, syntax-highlighted prompt (uses current theme from config)
                    message = []
                    # Exit code indicator: ✓ for success (exit code 0), ✗ N for failure
                    if self.last_exit_code != 0:
                        message.append(("class:prompt-exit-fail", f"✗ {self.last_exit_code}"))
                        message.append(("class:prompt-sep", " "))
                    elif self.last_command:
                        message.append(("class:prompt-exit-success", "✓"))
                        message.append(("class:prompt-sep", " "))
                    message.append(("class:prompt-name", "[cliara]"))
                    message.append(("class:prompt-sep", " "))
                    if self.current_session:
                        message.append(("class:prompt-path", f"[{self.current_session.name}]"))
                        message.append(("class:prompt-sep", " "))
                    message.extend([
                        ("class:prompt-path", cwd),
                        ("", " "),
                        ("class:prompt-arrow", f"{prompt_arrow} "),
                    ])
                    user_input = self._prompt_session.prompt(message).strip()
                else:
                    # Plain fallback
                    exit_indicator = ""
                    if self.last_exit_code != 0:
                        exit_indicator = f"X {self.last_exit_code} "
                    elif self.last_command:
                        exit_indicator = "OK "
                    if self.current_session:
                        prompt = f"{exit_indicator}[cliara] [{self.current_session.name}] {cwd} {prompt_arrow} "
                    else:
                        prompt = f"{exit_indicator}[cliara] {cwd} {prompt_arrow} "
                    user_input = input(prompt).strip()

                if not user_input:
                    continue

                self.handle_input(user_input)

            except KeyboardInterrupt:
                print("\nGoodbye!")
                break
            except EOFError:
                print("\nGoodbye!")
                break
            except Exception as e:
                print_error(f"[Error] {e}")
                if os.getenv("DEBUG"):
                    import traceback
                    traceback.print_exc()
    
    def run_single_command(self, command: str) -> int:
        """
        Run a single command through the risk gate then exit.
        Used by ``cliara -c "command"``.

        When stdin is not a TTY (e.g. agent/CI), risky commands are denied
        without prompting so the run does not block.

        Returns the process exit code (0 = success).
        """
        import sys
        from cliara.safety import DangerLevel as _DL

        assessment = self._risk_engine.assess(command)
        non_interactive = not sys.stdin.isatty()

        if not self._inline_gate(command, assessment, non_interactive=non_interactive):
            return 130  # cancelled, same as Ctrl+C convention

        success = self.execute_shell_command(command, capture=False)
        return 0 if success else self.last_exit_code or 1

    def handle_input(self, user_input: str):
        """
        Route user input to appropriate handler.
        
        Args:
            user_input: Raw user input
        """
        # Any new input clears a pending fix suggestion
        self._pending_fix = None

        # --- @run bypass: skip the Copilot Gate for this command ---
        if user_input.startswith("@run "):
            user_input = user_input[5:].strip()
            if not user_input:
                return

        # --- Copilot Gate: intercept AI-generated commands ---
        elif self.config.get("copilot_gate", True):
            gate_mode = self.config.get("copilot_gate_mode", "auto")
            source = self._source_detector.classify(
                user_input, self.last_command, mode=gate_mode)
            if self._source_detector.is_ai_generated(source):
                command = user_input[4:].strip() if source == InputSource.AI_TAGGED else user_input
                if not command:
                    return
                approved = self._copilot_gate.evaluate(command, source)
                if not approved:
                    print_warning("  [Cancelled]")
                    return
                user_input = command

        # Check for exit commands
        if user_input.lower() in ['exit', 'quit', 'q']:
            print("Goodbye!")
            self.running = False
            return
        
        # Check for help
        if user_input.lower() in ['help', '?help']:
            self.show_help()
            return

        # Check for version
        if user_input.lower() == 'version':
            from cliara import __version__
            print_info(f"Cliara {__version__}")
            return

        # Command history — history [N]
        if user_input.lower() == 'history' or user_input.lower().startswith('history '):
            self.handle_history(user_input[7:].strip() if len(user_input) > 7 else "")
            return

        # Check for explain command
        if user_input.lower().startswith('explain '):
            self.handle_explain(user_input[8:].strip())
            return

        # Smart push — auto-commit-message + branch detection
        if user_input.lower() == 'push':
            self.handle_push()
            return

        # Task sessions — start, resume, end, list, show, note
        if user_input.lower() == 'session' or user_input.lower().startswith('session '):
            subcommand = user_input[7:].strip() if len(user_input) > 7 else ""
            self.handle_session(subcommand)
            return

        # Smart deploy — detect project type and deploy in one word
        if user_input.lower() == 'deploy' or user_input.lower().startswith('deploy '):
            subcommand = user_input[6:].strip() if len(user_input) > 6 else ""
            self.handle_deploy(subcommand)
            return

        # Check for NL prefix (Phase 2 - stubbed for now)
        nl_prefix = self.config.get('nl_prefix', '?')
        if user_input.startswith(nl_prefix):
            query_rest = user_input[len(nl_prefix):].strip()
            if not query_rest:
                print_dim(f"Type {nl_prefix} <question> for natural language (e.g. {nl_prefix} kill process on port 3000).")
                return
            self.handle_nl_query(query_rest)
            return
        
        # Check for macro commands
        if user_input.startswith('macro '):
            self.handle_macro_command(user_input[6:].strip())
            return

        # Theme: list or set color scheme
        if user_input.strip() == 'theme' or user_input.startswith('theme '):
            self._handle_theme_command(user_input[5:].strip())
            return

        # Config — read/write persistent settings
        if user_input.strip() == 'config' or user_input.lower().startswith('config '):
            self._handle_config_command(user_input[6:].strip() if len(user_input) > 6 else "")
            return

        # Ollama setup wizard
        if user_input.lower().strip() == 'setup-ollama':
            self._handle_setup_ollama()
            return
        
        # Check if it's a macro name (exact match)
        if self.macros.exists(user_input):
            self.run_macro(user_input)
            return

        # Check for "macroname key=value ..." parameterised invocation
        _first_token = user_input.split()[0] if user_input.split() else ""
        if _first_token and _first_token != user_input and self.macros.exists(_first_token):
            self.run_macro(user_input)   # run_macro splits name from args
            return

        # Bare "fix" (without ?) — shortcut when there's a recent failure
        if _looks_like_fix(user_input) and self.last_exit_code != 0 and self.last_command:
            self.handle_fix()
            return

        # Try fuzzy match for macros
        fuzzy_match = self.macros.find_fuzzy(user_input)
        if fuzzy_match:
            response = input(f"Did you mean macro '{fuzzy_match}'? (y/n): ").strip().lower()
            if response in ['y', 'yes']:
                self.run_macro(fuzzy_match)
                return
        
        # Intercept cd commands so they change Cliara's own working directory
        if user_input == 'cd' or user_input.startswith('cd '):
            self._handle_cd(user_input)
            return

        # Intercept clear/cls so the host terminal is cleared properly
        if user_input.lower() in ('clear', 'cls'):
            os.system('cls' if platform.system() == 'Windows' else 'clear')
            if self.config.get('clear_show_header', True):
                print_dim("Cliara ready. Type 'help' for commands.")
            return

        # Diff preview: show exactly what destructive commands will affect
        if self.config.get("diff_preview", True) and self.diff_preview.should_preview(user_input):
            if not self._confirm_with_preview(user_input):
                return

        # Risk gate: assess every command and require confirmation for risky ones
        if self.config.get("copilot_gate", True):
            assessment = self._risk_engine.assess(user_input)
            if not self._inline_gate(user_input, assessment):
                return

        # Default: pass through to underlying shell
        success = self.execute_shell_command(user_input)
        if not success:
            # If the executable doesn't exist, try cross-platform
            # translation (it returns early when the command *is* found).
            self._check_cross_platform(user_input)

            # Auto-suggest a fix right below the error
            self._auto_suggest_fix()

            # Regression: compare to last success, minimal one-line hint
            self._regression_check_failure(user_input)
    
    def handle_nl_query(self, query: str):
        """
        Handle natural language query using LLM.
        
        Supports --save-as <name> flag to save generated commands as a macro
        instead of executing them immediately.
        
        Args:
            query: Natural language query (may contain --save-as <name>)
        """
        if not query:
            print_error("[Error] Please provide a query after '?'")
            return

        # ── "? fix" — context-aware error repair (typo-tolerant) ──
        # Catches: ? fix, ? fox, ? fxi, ? fiz, ? fixe, etc.
        if _looks_like_fix(query):
            self.handle_fix()
            return

        # ── "? why" — regression deep-dive (typo-tolerant) ──
        if _looks_like_why(query):
            self.handle_why()
            return

        # ── Semantic history search: ? find ... / ? when did I ... / ? what did I run ... ──
        if _is_semantic_history_search_intent(query):
            self.handle_semantic_history_search(query)
            return

        # Check for --save-as <name> flag
        save_as_name = None
        if '--save-as' in query:
            parts = query.split('--save-as', 1)
            query = parts[0].strip()
            save_as_name = parts[1].strip()
            if not save_as_name:
                print_error("[Error] Macro name required after --save-as")
                return
            if not query:
                print_error("[Error] Please provide a query before --save-as")
                return
        
        print_info(f"\n[Processing] {query}")
        print_dim("Generating commands...\n")
        
        # Build context
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash")
        }
        
        # Process with LLM (no streaming for ? queries — we show only the parsed explanation and commands)
        commands, explanation, danger_level = self.nl_handler.process_query(query, context, stream_callback=None)
        
        if not commands:
            print_error(f"[Error] {explanation}")
            return
        
        # Show generated commands first, then explanation
        _cliara_console().print("Generated commands:", style="magenta")
        for i, cmd in enumerate(commands, 1):
            _cliara_console().print(f"  {i}. {cmd}", style="magenta")
        _cliara_console().print(f"\n[Explanation] {explanation}\n", style="magenta")
        
        # --save-as: save as macro instead of executing
        if save_as_name:
            confirm = input(f"\nSave as macro '{save_as_name}'? (y/n): ").strip().lower()
            if confirm not in ['y', 'yes']:
                print_warning("[Cancelled]")
                return
            if not self._check_macro_name_conflict(save_as_name):
                print_warning("[Cancelled]")
                return
            description = input("Description (optional): ").strip() or query
            self.macros.add(save_as_name, commands, description)
            print_success(f"[OK] Macro '{save_as_name}' saved with {len(commands)} command(s)")
            return
        
        # Safety check with copy-to-clipboard option
        if danger_level != DangerLevel.SAFE:
            print(self.safety.get_warning_message(commands, danger_level))
        
        # Show interactive prompt with copy option
        import sys
        # Use direct print with ANSI dim code to ensure it displays before prompt_toolkit takes over
        print("\033[2m[c] copy  [Enter] run  [Esc/n] cancel\033[0m", flush=True)
        action = self._confirm_with_copy_option(commands, danger_level)
        
        if action == "copy":
            commands_text = "\n".join(commands)
            if self._write_system_clipboard(commands_text):
                print_success("[Copied to clipboard]")
            else:
                print_warning("[Could not copy to clipboard]")
                print_dim("Commands:")
                print(commands_text)
            return
        elif action == "cancel":
            print_warning("[Cancelled]")
            return
        
        # Execute commands
        print_header("\n" + "="*60)
        print_header("EXECUTING COMMANDS")
        print_header("="*60 + "\n")
        
        for i, cmd in enumerate(commands, 1):
            print_info(f"[{i}/{len(commands)}] {cmd}")
            print("-" * 60)
            success = self.execute_shell_command(cmd, capture=False)
            print()
            
            if not success:
                print_error(f"[X] Command {i} failed")
                self._auto_suggest_fix()
                break
        else:
            print_header("="*60)
            print_success("[OK] All commands completed successfully")
            print_header("="*60 + "\n")
        
        # Save to history for "save last"
        self.history.set_last_execution(commands)
    
    def handle_fix(self):
        """
        Context-aware error repair: '? fix'

        Uses the last failed command's stderr, exit code, and the command
        itself to ask the LLM (or stub patterns) how to fix the error.
        No copy-pasting needed — Cliara already has all the context.
        """
        # Guard: is there anything to fix?
        if not self.last_command:
            print_error("[Cliara] Nothing to fix — no commands have been run yet.")
            return

        if self.last_exit_code == 0:
            print_info(
                f"[Cliara] Last command succeeded (exit 0): {self.last_command}"
            )
            print_dim("         Nothing to fix!")
            return

        stderr = self.last_stderr.strip()
        if not stderr:
            print_warning(
                f"[Cliara] Last command failed (exit {self.last_exit_code}): "
                f"{self.last_command}"
            )
            print_dim("         No stderr captured — nothing to analyse.")
            return

        # We have a failed command with stderr — hand off to the error
        # translation pipeline (which already handles LLM + stub fallback,
        # displays the explanation, and offers to run the fix).
        print_info(
            f"\n[Cliara] Diagnosing last failure..."
        )
        print_dim(f"         Command:   {self.last_command}")
        print_dim(f"         Exit code: {self.last_exit_code}")
        print()

        self._handle_error_translation(self.last_command, stderr)

    def handle_why(self):
        """
        Regression deep-dive: show why the last failure might be a regression.
        Uses stored report from automatic check, or runs comparison on the fly.
        """
        from rich.panel import Panel
        if self._last_regression_report:
            causes, last_snap, current_snap = self._last_regression_report
            text = regression.format_expanded_report(causes, last_snap, current_snap)
            _cliara_console().print(Panel(text, title="Regression (vs last success)", border_style="dim"))
            return
        if not self.last_command or self.last_exit_code == 0:
            print_dim("No recent failure to explain. Run a command that fails, then ? why")
            return
        key = self._regression_workflow_key(self.last_command)
        if not key:
            print_dim("No previous success for this workflow.")
            return
        store_path = self.config.config_dir / "regression_snapshots.json"
        last = regression.load_last_success(key, store_path)
        if not last:
            print_dim("No previous success for this workflow.")
            return
        cwd = Path.cwd()
        current = regression.gather_current_snapshot(cwd)
        diff_result = regression.diff_snapshots(last, current)
        causes = regression.rank_causes(diff_result, last, current)
        if not causes:
            print_dim("No snapshot diff (git/deps/env/runtime) — failure may be unrelated.")
            return
        self._last_regression_report = (causes, last, current)
        text = regression.format_expanded_report(causes, last, current)
        _cliara_console().print(Panel(text, title="Regression (vs last success)", border_style="dim"))

    def handle_semantic_history_search(self, query: str):
        """
        Search command history by intent. Called for ? find ... / ? when did I ... / ? what did I run ...
        """
        if not self.config.get("semantic_history_enabled", True):
            print_dim("Semantic history search is disabled. Use 'history [N]' for a plain list.")
            return
        store = self._semantic_history
        if not store or store.is_empty():
            print_dim("No semantic history yet. Run some commands, then try again.")
            print_dim("Use 'history [N]' for a plain list of recent commands.")
            return
        if not self.nl_handler.llm_enabled:
            print_dim("LLM not configured. Semantic search requires OPENAI_API_KEY.")
            print_dim("Use 'history [N]' for a plain list.")
            return
        use_embeddings = self.config.get("semantic_history_use_embeddings", False)
        entries = store.get_all() if use_embeddings else store.get_recent(100)
        if not entries:
            print_dim("No matching commands found. Try a different phrase or run more commands.")
            return
        print_info(f"\n[Searching] {query.strip()}\n")

        matches: list = []
        if use_embeddings:
            matches = self.nl_handler.search_history_by_embeddings(entries, query.strip())
            if not matches:
                # No embeddings stored yet — fall back to summary-based search
                entries_recent = store.get_recent(100)
                matches = self.nl_handler.search_history_by_intent(entries_recent, query.strip())
        else:
            matches = self.nl_handler.search_history_by_intent(entries, query.strip())
        if not matches:
            print_dim("No matching commands found. Try a different phrase or run more commands.")
            return
        print_info(f"Found {len(matches)} matching command(s):\n")
        for i, e in enumerate(matches, 1):
            cmd = e.get("command", "")
            summary = e.get("summary", "").strip()
            ts = e.get("timestamp", "").strip()
            cwd = e.get("cwd", "").strip()
            line = f"  {i}. {cmd}"
            if summary:
                line += f"\n     {summary}"
            if ts:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    # Convert UTC timestamp to local time and show 12-hour clock with AM/PM
                    local_dt = dt.astimezone()
                    line += f"\n     {local_dt.strftime('%Y-%m-%d %I:%M %p')}"
                except Exception:
                    line += f"\n     {ts}"
            if cwd:
                line += f"  |  {cwd}"
            print(line)
            print()
        # Offer to run the first match
        first_cmd = matches[0].get("command", "").strip()
        if first_cmd:
            try:
                run_again = input("Run the first command again? (y/n): ").strip().lower()
                if run_again in ("y", "yes"):
                    self.execute_shell_command(first_cmd)
            except (EOFError, KeyboardInterrupt):
                print()

    # ------------------------------------------------------------------
    # Parameterized-macro helpers
    # ------------------------------------------------------------------

    _PARAM_PATTERN = re.compile(r'\{(\w+)\}')

    @staticmethod
    def _extract_param_names(commands: List[str]) -> List[str]:
        """Return unique {param} names found across all commands, in order."""
        seen: set = set()
        result: List[str] = []
        for cmd in commands:
            for m in re.finditer(r'\{(\w+)\}', cmd):
                p = m.group(1)
                if p not in seen:
                    seen.add(p)
                    result.append(p)
        return result

    @staticmethod
    def _parse_inline_args(args_str: str) -> Dict[str, str]:
        """Parse 'key=value key2=value2 ...' into a dict.  Values may be quoted."""
        values: Dict[str, str] = {}
        for token in args_str.split():
            if '=' in token:
                k, _, v = token.partition('=')
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    values[k] = v
        return values

    @staticmethod
    def _substitute_params(cmd: str, values: Dict[str, str]) -> str:
        """Replace {param} placeholders in *cmd* with values from *values*."""
        for param, value in values.items():
            cmd = cmd.replace(f'{{{param}}}', value)
        return cmd

    def _collect_param_values(self, params: List[str],
                               prefilled: Dict[str, str]) -> Optional[Dict[str, str]]:
        """
        Prompt the user for any params not already in *prefilled*.
        Returns the completed dict, or None if the user cancels.
        """
        values = dict(prefilled)
        for p in params:
            if p not in values:
                try:
                    val = input(f"  {p}: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None
                if not val:
                    print_warning(f"[Cancelled] No value provided for '{p}'")
                    return None
                values[p] = val
        return values

    # ------------------------------------------------------------------

    def handle_macro_command(self, args: str):
        """
        Handle macro subcommands.

        Args:
            args: Command arguments after 'macro '
        """
        parts = args.split(maxsplit=1)
        if not parts:
            print("Usage: macro <command> [args]")
            print("Commands: add, edit, list, stats, search, show, run, chain, delete, rename, save, help")
            return
        
        cmd = parts[0].lower()
        args_rest = parts[1] if len(parts) > 1 else ""
        
        if cmd == 'add':
            # Check for --nl flag
            if args_rest.startswith('--nl') or '--nl' in args_rest:
                # Remove --nl flag and get name
                name = args_rest.replace('--nl', '').strip()
                if not name:
                    name = None
                self.macro_add_nl(name)
            else:
                self.macro_add(args_rest)
        elif cmd == 'list':
            self.macro_list()
        elif cmd == 'stats':
            self.macro_stats()
        elif cmd == 'search':
            self.macro_search(args_rest)
        elif cmd == 'show':
            self.macro_show(args_rest)
        elif cmd == 'run':
            # args_rest may be "name key=val ..." — run_macro handles the split
            self.run_macro(args_rest)
        elif cmd == 'edit':
            self.macro_edit(args_rest)
        elif cmd == 'delete':
            self.macro_delete(args_rest)
        elif cmd == 'rename':
            self.macro_rename(args_rest)
        elif cmd == 'chain':
            self.macro_chain(args_rest)
        elif cmd == 'save':
            self.macro_save_last(args_rest)
        elif cmd == 'help':
            self.macro_help()
        else:
            print_error(f"Unknown macro command: {cmd}")
            print_dim("Type 'macro help' for available commands")
    
    def macro_add(self, raw: str):
        """Create a new macro interactively.

        Accepts an optional ``--params name1,name2`` flag so the macro can
        declare typed placeholders.  Example::

            macro add deploy-to --params env,tag
        """
        # ── Parse --params flag ─────────────────────────────────────────
        params: List[str] = []
        name = raw
        if '--params' in raw:
            before, _, after = raw.partition('--params')
            name = before.strip()
            params_token = after.strip().split()[0] if after.strip() else ""
            params = [p.strip() for p in params_token.split(',') if p.strip()]

        if not name:
            name = input("Macro name: ").strip()
            if not name:
                print_error("[Error] Macro name required")
                return

        print_info(f"\nCreating macro '{name}'")
        if params:
            print_dim(f"Parameters declared: {', '.join(params)}")
            print_dim("Use {param} in commands to reference them, e.g.  kubectl apply -n {env}")
        else:
            print_dim("Tip: use {param} placeholders to make commands reusable, e.g.  echo {msg}")
        print_dim("Enter commands (one per line, empty line to finish):")

        commands = []
        while True:
            cmd = input("  > ").strip()
            if not cmd:
                break
            commands.append(cmd)

        if not commands:
            print_error("[Error] At least one command required")
            return

        # Auto-detect any {var} in commands and merge with declared params
        detected = self._extract_param_names(commands)
        for p in detected:
            if p not in params:
                params.append(p)

        if params:
            print_dim(f"\nParams: {', '.join(params)}")

        description = input("Description (optional): ").strip()

        # Safety check
        level, dangerous = self.safety.check_commands(commands)
        if level in [DangerLevel.DANGEROUS, DangerLevel.CRITICAL]:
            print_warning(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
            confirm = input("\nSave anyway? (yes/no): ").strip().lower()
            if confirm not in ['yes', 'y']:
                print_warning("[Cancelled]")
                return

        if not self._check_macro_name_conflict(name):
            print_warning("[Cancelled]")
            return
        self.macros.add(name, commands, description, params=params or None)
        param_hint = f" [{', '.join(params)}]" if params else ""
        print_success(f"\n[OK] Macro '{name}' created with {len(commands)} command(s){param_hint}")
    
    def macro_add_nl(self, name: Optional[str] = None):
        """Create a macro using natural language description."""
        if not self.nl_handler.llm_enabled:
            print_error("[Error] LLM not configured. Set OPENAI_API_KEY in .env file.")
            return
        
        if not name:
            name = input("Macro name: ").strip()
            if not name:
                print_error("[Error] Macro name required")
                return
        
        print_info(f"\nCreating macro '{name}' from natural language")
        print("Describe what this macro should do:")
        nl_description = input("  > ").strip()
        
        if not nl_description:
            print_error("[Error] Description required")
            return
        
        print_info("\n[Generating commands...]")
        
        # Build context
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash")
        }
        
        # Generate commands from NL
        commands = self.nl_handler.generate_commands_from_nl(nl_description, context)
        
        if not commands or (len(commands) == 1 and commands[0].startswith("#")):
            print_error(f"[Error] Could not generate commands: {commands[0] if commands else 'Unknown error'}")
            return
        
        # Show generated commands
        print("\nGenerated commands:")
        for i, cmd in enumerate(commands, 1):
            print(f"  {i}. {cmd}")
        
        # Allow user to edit
        edit = input("\nEdit commands? (y/n): ").strip().lower()
        if edit in ['y', 'yes']:
            print("\nEnter commands (one per line, empty line to finish):")
            new_commands = []
            for i, cmd in enumerate(commands, 1):
                new_cmd = input(f"  {i}. [{cmd}] ").strip()
                if new_cmd:
                    new_commands.append(new_cmd)
                else:
                    new_commands.append(cmd)
            
            # Allow adding more
            while True:
                extra = input("  > ").strip()
                if not extra:
                    break
                new_commands.append(extra)
            
            commands = new_commands
        
        # Safety check
        level, dangerous = self.safety.check_commands(commands)
        if level in [DangerLevel.DANGEROUS, DangerLevel.CRITICAL]:
            print_warning(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
            confirm = input("\nSave anyway? (yes/no): ").strip().lower()
            if confirm not in ['yes', 'y']:
                print_warning("[Cancelled]")
                return
        
        if not self._check_macro_name_conflict(name):
            print_warning("[Cancelled]")
            return
        description = input("Description (optional): ").strip() or nl_description
        
        self.macros.add(name, commands, description)
        print_success(f"\n[OK] Macro '{name}' created with {len(commands)} command(s) from natural language")
    
    def _macro_table(self, macros_iter, title: str):
        """Render a Rich table for a collection of macros.

        Args:
            macros_iter: iterable of ``(name, Macro)`` pairs, already sorted.
            title:       header line printed above the table.
        """
        from rich.table import Table
        from rich import box
        from rich.text import Text

        console = _cliara_console()

        table = Table(
            box=box.ROUNDED,
            border_style="dim",
            header_style="bold dim",
            show_edge=True,
            padding=(0, 1),
        )
        table.add_column("Macro",       style="bold bright_cyan",  no_wrap=True)
        table.add_column("Params",      style="yellow",            no_wrap=True)
        table.add_column("Steps",       justify="right",           style="green")
        table.add_column("Runs",        justify="right")
        table.add_column("Description", style="dim")

        rows = 0
        for name, macro in macros_iter:
            # Effective params: declared ∪ auto-detected {var} patterns
            eff_params = list(macro.params) if macro.params else []
            for p in self._extract_param_names(macro.commands):
                if p not in eff_params:
                    eff_params.append(p)
            param_str = "  ".join(f"{{{p}}}" for p in eff_params) if eff_params else ""

            run_text = Text()
            if macro.run_count == 0:
                run_text.append("—", style="dim")
            elif macro.run_count >= 10:
                run_text.append(str(macro.run_count), style="bold green")
            else:
                run_text.append(str(macro.run_count), style="cyan")

            table.add_row(
                name,
                param_str,
                str(len(macro.commands)),
                run_text,
                macro.description or "",
            )
            rows += 1

        console.print()
        console.print(f"  {title}")
        console.print()
        console.print(table)
        console.print()

    def macro_list(self):
        """List all macros."""
        macros = self.macros.list_all()

        if not macros:
            print_dim("\nNo macros yet.")
            print_dim("Create one with: macro add <name>")
            return

        self._macro_table(
            sorted(macros.items()),
            f"[cyan][Macros][/cyan]  [bold]{len(macros)}[/bold] total",
        )
    
    def macro_stats(self):
        """Show macro statistics (total, most used, last used, total commands)."""
        stats = self.macros.get_stats()
        if stats["total"] == 0:
            print_dim("\nNo macros yet.")
            print_dim("Create one with: macro add <name>")
            return
        macros = self.macros.list_all()
        total_commands = sum(len(m.commands) for m in macros.values())
        print_info("\n[Macro stats]\n")
        print(f"  Macros:        {stats['total']}")
        print(f"  Total steps:  {total_commands}")
        if stats.get("most_used"):
            print(f"  Most used:     {stats['most_used']}")
        if stats.get("recently_used"):
            print(f"  Last run:      {stats['recently_used']}")
        print()
    
    def macro_search(self, keyword: str):
        """Search macros by name, description, or tags."""
        if not keyword or not keyword.strip():
            print_error("[Error] Search keyword required")
            print_dim("Usage: macro search <keyword>")
            return
        
        results = self.macros.search(keyword.strip())
        
        if not results:
            print_dim(f"\nNo macros matching '{keyword.strip()}'.")
            return
        
        kw = keyword.strip()
        self._macro_table(
            [(m.name, m) for m in sorted(results, key=lambda m: m.name)],
            f"[cyan][Search: '{kw}'][/cyan]  [bold]{len(results)}[/bold] result{'s' if len(results) != 1 else ''}",
        )
    
    def macro_show(self, name: str):
        """Show details of a macro."""
        if not name:
            print_error("[Error] Macro name required")
            return

        macro = self.macros.get(name)
        if not macro:
            print_error(f"[Error] Macro '{name}' not found")
            return

        print_info(f"\n[Macro] {name}")
        print(f"Description: {macro.description or 'None'}")

        # Show declared / auto-detected params
        effective_params = list(macro.params) if macro.params else []
        detected = self._extract_param_names(macro.commands)
        for p in detected:
            if p not in effective_params:
                effective_params.append(p)
        if effective_params:
            print(f"Parameters: {', '.join(effective_params)}")
            print_dim(f"  Usage: {name} " + " ".join(f"{p}=<value>" for p in effective_params))

        print(f"Commands ({len(macro.commands)}):")
        for i, cmd in enumerate(macro.commands, 1):
            print(f"  {i}. {cmd}")
        print(f"\nCreated: {macro.created}")
        print(f"Run count: {macro.run_count}")
        if macro.last_run:
            print(f"Last run: {macro.last_run}")
        print()
    
    def macro_edit(self, name: str):
        """Edit an existing macro's commands and description."""
        if not name:
            name = input("Macro name: ").strip()
            if not name:
                print_error("[Error] Macro name required")
                return

        macro = self.macros.get(name)
        if not macro:
            print_error(f"[Error] Macro '{name}' not found")
            return

        # Show current commands
        print_info(f"\n[Editing] {name}")
        print(f"Current description: {macro.description or 'None'}")
        print(f"Current commands ({len(macro.commands)}):")
        for i, cmd in enumerate(macro.commands, 1):
            print(f"  {i}. {cmd}")

        print("\nEnter new commands (one per line, empty line to finish).")
        print("Press Enter on the first prompt with no input to keep existing commands.\n")

        commands = []
        first = True
        while True:
            cmd = input("  > ").strip()
            if not cmd:
                if first:
                    # User pressed Enter immediately — keep existing commands
                    commands = macro.commands
                    print("  (keeping existing commands)")
                break
            first = False
            commands.append(cmd)

        # Update description
        new_desc = input(f"New description (Enter to keep '{macro.description or ''}'): ").strip()
        description = new_desc if new_desc else macro.description

        # Update params
        existing_params = macro.params or []
        detected_params = self._extract_param_names(commands)
        all_params = list(existing_params)
        for p in detected_params:
            if p not in all_params:
                all_params.append(p)
        current_params_str = ','.join(all_params)
        new_params_input = input(
            f"Parameters (comma-separated, Enter to keep '{current_params_str}'): "
        ).strip()
        if new_params_input:
            params = [p.strip() for p in new_params_input.split(',') if p.strip()]
        else:
            params = all_params

        # Safety check on the (possibly new) commands
        level, dangerous = self.safety.check_commands(commands)
        if level in [DangerLevel.DANGEROUS, DangerLevel.CRITICAL]:
            print_warning(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
            confirm = input("\nSave anyway? (yes/no): ").strip().lower()
            if confirm not in ['yes', 'y']:
                print_warning("[Cancelled]")
                return

        self.macros.add(name, commands, description, params=params or None)
        param_hint = f" [{', '.join(params)}]" if params else ""
        print_success(f"\n[OK] Macro '{name}' updated with {len(commands)} command(s){param_hint}")

    def macro_delete(self, name: str):
        """Delete a macro."""
        if not name:
            print_error("[Error] Macro name required")
            return
        
        if not self.macros.exists(name):
            print_error(f"[Error] Macro '{name}' not found")
            return
        
        confirm = input(f"Delete macro '{name}'? (y/n): ").strip().lower()
        if confirm in ['y', 'yes']:
            self.macros.delete(name)
            print_success(f"[OK] Macro '{name}' deleted")
        else:
            print_warning("[Cancelled]")
    
    def macro_rename(self, args: str):
        """Rename a macro."""
        parts = args.split()
        if len(parts) != 2:
            print_dim("Usage: macro rename <old_name> <new_name>")
            return

        old_name, new_name = parts

        macro = self.macros.get(old_name)
        if not macro:
            print_error(f"[Error] Macro '{old_name}' not found")
            return

        if self.macros.exists(new_name):
            print_error(f"[Error] Macro '{new_name}' already exists")
            return

        if not self._check_macro_name_conflict(new_name):
            print_warning("[Cancelled]")
            return

        # Re-create under new name, then delete old
        self.macros.add(new_name, macro.commands, macro.description, tags=macro.tags)
        self.macros.delete(old_name)
        print_success(f"[OK] Macro '{old_name}' renamed to '{new_name}'")

    def macro_save_last(self, args: str):
        """Save last executed commands as a macro."""
        # Parse "save last as <name>"
        if not args.startswith("last as "):
            print_dim("Usage: macro save last as <name>")
            return
        
        name = args[8:].strip()  # Remove "last as "
        if not name:
            print_error("[Error] Macro name required")
            return
        
        last_commands = self.history.get_last()
        if not last_commands:
            print_error("[Error] No recent commands to save")
            return
        
        print_info(f"\nSaving last execution as '{name}':")
        for i, cmd in enumerate(last_commands, 1):
            print(f"  {i}. {cmd}")
        
        confirm = input("\nSave these commands? (y/n): ").strip().lower()
        if confirm not in ['y', 'yes']:
            print_warning("[Cancelled]")
            return
        
        if not self._check_macro_name_conflict(name):
            print_warning("[Cancelled]")
            return
        description = input("Description (optional): ").strip()
        self.macros.add(name, last_commands, description)
        print_success(f"[OK] Macro '{name}' saved!")
    
    def macro_help(self):
        """Show macro help."""
        print_info("\n[Macro Commands]\n")
        print("  macro add <name>                    Create a new macro")
        print("  macro add <name> --params p1,p2     Create a parameterised macro")
        print("  macro add <name> --nl               Create macro from natural language")
        print("  macro edit <name>                   Edit an existing macro")
        print("  macro list                          List all macros")
        print("  macro stats                         Show macro statistics")
        print("  macro search <keyword>              Search macros by name, description, or tags")
        print("  macro show <name>                   Show macro details")
        print("  macro run <name>                    Run a macro (prompts for params if needed)")
        print("  macro run <name> p1=v1 p2=v2        Run a macro with inline param values")
        print("  macro chain <n1> <n2> [n3 …]        Run macros in sequence")
        print_dim('      "my macro", "other macro"          — quoted names (multi-word)')
        print_dim("      my macro, other macro              — comma-separated (multi-word)")
        print("  macro delete <name>                 Delete a macro")
        print("  macro rename <old> <new>            Rename a macro")
        print("  macro save last as <name>           Save last commands as macro")
        print_dim("\nParameterised macros:")
        print_dim("  Use {param} placeholders in commands, e.g.  kubectl apply -n {env}")
        print_dim("  Declare them with --params: macro add deploy --params env,tag")
        print_dim("  Run with inline values:     deploy env=prod tag=v1.2")
        print_dim("  Or just type the name and Cliara will prompt for each value.")
        print("\nYou can also run macros by just typing their name:")
        print("  cliara > my-macro")
        print("  cliara > my-macro param=value\n")
    
    def run_macro(self, name_and_args: str):
        """Execute a macro, optionally with inline parameter values.

        Accepts either:
          • a plain macro name:                  ``deploy-to``
          • a name followed by key=value pairs:  ``deploy-to env=prod tag=v1.2``

        If the macro declares parameters that are not supplied inline, the user
        is prompted for each missing value interactively.
        """
        # ── Split name from optional inline key=value args ──────────────
        parts = name_and_args.split(maxsplit=1)
        name = parts[0]
        inline_str = parts[1] if len(parts) > 1 else ""

        macro = self.macros.get(name)
        if not macro:
            print_error(f"[Error] Macro '{name}' not found")
            return

        # ── Resolve parameter values ─────────────────────────────────────
        # Effective param list: declared params ∪ {var} patterns in commands
        effective_params = list(macro.params) if macro.params else []
        detected = self._extract_param_names(macro.commands)
        for p in detected:
            if p not in effective_params:
                effective_params.append(p)

        inline_values = self._parse_inline_args(inline_str) if inline_str else {}
        param_values: Dict[str, str] = {}

        if effective_params:
            missing = [p for p in effective_params if p not in inline_values]
            if missing:
                print_info(f"\n[Macro] {name}")
                if macro.description:
                    print_dim(macro.description)
                print_dim(f"\nProvide values for: {', '.join(effective_params)}")
                if inline_values:
                    for k, v in inline_values.items():
                        print_dim(f"  {k} = {v}  (from command line)")
                collected = self._collect_param_values(effective_params, inline_values)
                if collected is None:
                    return
                param_values = collected
            else:
                param_values = dict(inline_values)
        else:
            # No params — any inline tokens are ignored (pass-through)
            param_values = {}

        # ── Build the final commands with substituted values ─────────────
        resolved_commands = [
            self._substitute_params(cmd, param_values)
            for cmd in macro.commands
        ]

        # ── Show preview ─────────────────────────────────────────────────
        print_info(f"\n[Macro] {name}")
        if macro.description:
            print(f"{macro.description}\n")
        if param_values:
            print_dim("Parameters:")
            for k, v in param_values.items():
                print_dim(f"  {k} = {v}")
            print()
        print("Commands:")
        for i, cmd in enumerate(resolved_commands, 1):
            print(f"  {i}. {cmd}")

        # ── Safety check (on resolved commands) ──────────────────────────
        level, dangerous = self.safety.check_commands(resolved_commands)
        if level != DangerLevel.SAFE:
            print(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
            prompt = self.safety.get_confirmation_prompt(level)
            response = input(prompt).strip()
            if not self.safety.validate_confirmation(response, level):
                print_warning("[Cancelled]")
                return
        else:
            confirm = input("\nRun? (y/n): ").strip().lower()
            if confirm not in ['y', 'yes']:
                print_warning("[Cancelled]")
                return

        # ── Execute ───────────────────────────────────────────────────────
        print_header("\n" + "="*60)
        print_header(f"EXECUTING: {name}")
        print_header("="*60 + "\n")

        for i, cmd in enumerate(resolved_commands, 1):
            print_info(f"[{i}/{len(resolved_commands)}] {cmd}")
            print("-" * 60)
            success = self.execute_shell_command(cmd, capture=False)
            print()

            if not success:
                print_error(f"[X] Command {i} failed")
                self._auto_suggest_fix()
                break
        else:
            print_header("="*60)
            print_success(f"[OK] Macro '{name}' completed successfully")
            print_header("="*60 + "\n")
            macro.mark_run()
            self.macros.storage.add(macro, user_id=self.macros.user_id)

        # Save to history for "save last" (store template commands, not resolved)
        self.history.set_last_execution(macro.commands)

    def _parse_chain_names(self, args: str) -> List[str]:
        """Parse a list of (possibly multi-word) macro names for macro chain.

        Two syntaxes are accepted:

        1. Comma-separated  (works for any name, no quoting needed):
               my name is, greet, deploy to prod
           Each token between commas is one macro name.

        2. Shell-quoted  (standard approach):
               "my name is" greet "deploy to prod"
           ``shlex.split`` handles the quoting.

        If neither a comma nor any quote character is present the raw words are
        returned as-is (single-word names, backward-compatible).
        """
        # ── Comma-separated ───────────────────────────────────────────────────
        if ',' in args:
            return [n.strip() for n in args.split(',') if n.strip()]

        # ── Shell-quoted ──────────────────────────────────────────────────────
        if '"' in args or "'" in args:
            try:
                return shlex.split(args)
            except ValueError:
                pass  # malformed quotes — fall through to plain split

        # ── Plain split (single-word names, backward-compatible) ──────────────
        return args.split()

    def macro_chain(self, args: str):
        """Run multiple macros in sequence.

        Usage:
          macro chain <name1> <name2> [name3 …]

        Multi-word macro names are supported in two ways:
          • Quoted:           macro chain "my name is", greet, deploy
          • Comma-separated:  macro chain my name is, greet, deploy

        All macros are validated and param values are collected up-front before
        any execution starts.  The chain halts on the first failed command and
        reports exactly which step caused the failure.
        """
        names = self._parse_chain_names(args)
        if len(names) < 2:
            print_error("[Error] 'macro chain' requires at least two macro names")
            print_dim('Usage: macro chain "name one", "name two" [...]')
            print_dim("   or: macro chain name one, name two, name three")
            return

        # ── Validate every macro exists before touching anything ──────────────
        macros: List = []
        for name in names:
            macro = self.macros.get(name)
            if not macro:
                print_error(f"[Error] Macro '{name}' not found")
                fuzzy = self.macros.find_fuzzy(name)
                if fuzzy:
                    print_dim(f"  Did you mean: {fuzzy}?")
                return
            macros.append(macro)

        # ── Collect param values for every macro that needs them ──────────────
        chain_params: List[Dict[str, str]] = []
        for macro in macros:
            effective_params = list(macro.params) if macro.params else []
            detected = self._extract_param_names(macro.commands)
            for p in detected:
                if p not in effective_params:
                    effective_params.append(p)

            if effective_params:
                print_info(f"\n[Params for '{macro.name}']")
                if macro.description:
                    print_dim(macro.description)
                collected = self._collect_param_values(effective_params, {})
                if collected is None:
                    print_warning("[Cancelled]")
                    return
                chain_params.append(collected)
            else:
                chain_params.append({})

        # ── Resolve placeholders in every macro ───────────────────────────────
        chain_resolved: List[List[str]] = []
        for macro, param_values in zip(macros, chain_params):
            resolved = [
                self._substitute_params(cmd, param_values)
                for cmd in macro.commands
            ]
            chain_resolved.append(resolved)

        # ── Show full chain preview ───────────────────────────────────────────
        total_cmds = sum(len(cmds) for cmds in chain_resolved)
        print_info(f"\n[Chain] {len(macros)} macros  ·  {total_cmds} commands total\n")
        for i, (macro, resolved, param_values) in enumerate(
            zip(macros, chain_resolved, chain_params), 1
        ):
            print(f"  Step {i}: {macro.name}")
            if macro.description:
                print_dim(f"           {macro.description}")
            if param_values:
                kv = "  ".join(f"{k}={v}" for k, v in param_values.items())
                print_dim(f"           [{kv}]")
            for j, cmd in enumerate(resolved, 1):
                print_dim(f"    {j}. {cmd}")
            print()

        # ── Safety-check across all resolved commands ─────────────────────────
        all_resolved = [cmd for cmds in chain_resolved for cmd in cmds]
        level, dangerous = self.safety.check_commands(all_resolved)
        if level != DangerLevel.SAFE:
            print(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
            prompt = self.safety.get_confirmation_prompt(level)
            response = input(prompt).strip()
            if not self.safety.validate_confirmation(response, level):
                print_warning("[Cancelled]")
                return
        else:
            confirm = input("Run chain? (y/n): ").strip().lower()
            if confirm not in ['y', 'yes']:
                print_warning("[Cancelled]")
                return

        # ── Execute each macro in sequence ────────────────────────────────────
        chain_label = " → ".join(m.name for m in macros)
        print_header("\n" + "=" * 60)
        print_header(f"CHAIN: {chain_label}")
        print_header("=" * 60 + "\n")

        for step_idx, (macro, resolved) in enumerate(
            zip(macros, chain_resolved), 1
        ):
            print_info(f"[Step {step_idx}/{len(macros)}] {macro.name}")
            if macro.description:
                print_dim(f"  {macro.description}")
            print("-" * 60)

            step_failed = False
            for cmd_idx, cmd in enumerate(resolved, 1):
                print_info(f"  [{cmd_idx}/{len(resolved)}] {cmd}")
                success = self.execute_shell_command(cmd, capture=False)
                print()
                if not success:
                    print_error(
                        f"[X] Command {cmd_idx} failed in macro '{macro.name}'"
                    )
                    print_error(
                        f"[X] Chain halted at step {step_idx}/{len(macros)}"
                    )
                    self._auto_suggest_fix()
                    step_failed = True
                    break

            if step_failed:
                return

            macro.mark_run()
            self.macros.storage.add(macro, user_id=self.macros.user_id)
            print_success(f"[OK] {macro.name} completed")
            print()

        print_header("=" * 60)
        print_success(
            f"[OK] Chain complete — {len(macros)} macros, {total_cmds} commands"
        )
        print_header("=" * 60 + "\n")

    def _handle_config_command(self, args: str):
        """Built-in config command: get/set/list persistent cliara settings.

        Usage:
          config list              — show all current settings
          config get <key>         — print one value
          config set <key> <value> — persist a value to ~/.cliara/config.json
        """
        # Read-only keys that must never be set via this command
        _READONLY = {"llm_api_key", "llm_provider", "postgres"}

        parts = args.split(None, 2)
        sub = parts[0].lower() if parts else ""

        if sub in ("list", "show", ""):
            print_info("[Cliara] Current config (~/.cliara/config.json):\n")
            skip = {"llm_api_key", "connection_string"}
            for k, v in sorted(self.config.settings.items()):
                if k in skip:
                    continue
                if isinstance(v, dict):
                    continue
                display = str(v) if v is not None else "(not set)"
                print(f"  {k:<32} {display}")
            print()
            print_dim("  Use 'config set <key> <value>' to change a setting.")
            return

        if sub == "get":
            if len(parts) < 2:
                print_error("[Cliara] Usage: config get <key>")
                return
            key = parts[1]
            val = self.config.get(key)
            if val is None:
                print_dim(f"  {key} = (not set)")
            else:
                print_info(f"  {key} = {val}")
            return

        if sub == "set":
            if len(parts) < 3:
                print_error("[Cliara] Usage: config set <key> <value>")
                return
            key = parts[1]
            raw_val = parts[2]

            if key in _READONLY:
                print_error(f"[Cliara] '{key}' is read-only — set it via your .env file instead.")
                return

            # Type-coerce: booleans and integers
            val: Any
            if raw_val.lower() in ("true", "yes", "on"):
                val = True
            elif raw_val.lower() in ("false", "no", "off"):
                val = False
            elif raw_val.lower() in ("none", "null", ""):
                val = None
            else:
                try:
                    val = int(raw_val)
                except ValueError:
                    try:
                        val = float(raw_val)
                    except ValueError:
                        val = raw_val

            self.config.set(key, val)
            print_success(f"[Cliara] {key} = {val!r}  (saved)")

            # Live-apply a small set of settings without restart
            if key == "llm_model" and self.nl_handler.llm_enabled:
                print_dim(f"  Model will be used on next LLM call.")
            return

        print_error(f"[Cliara] Unknown config subcommand: '{sub}'")
        print_dim("  Usage: config list | config get <key> | config set <key> <value>")

    # ------------------------------------------------------------------
    # Ollama setup wizard
    # ------------------------------------------------------------------

    def _handle_setup_ollama(self):
        """Delegate to the dedicated setup_ollama module."""
        from cliara import setup_ollama
        setup_ollama.run(self)

    def _handle_theme_command(self, arg: str):
        """Show scrollable theme picker (up/down to select, Enter to apply) or set theme by name."""
        from cliara.highlighting import list_themes, get_style_for_theme
        themes = list_themes()
        current = self.config.get("theme", "monokai")
        # If they passed a name, set directly
        if arg:
            name = arg.strip().lower()
            if name not in themes:
                print_error(f"[Error] Unknown theme: {arg}. Available: {', '.join(themes)}")
                return
            self._apply_theme(name)
            return
        # No arg: show scrollable picker
        selected = self._run_theme_picker(themes, current)
        if selected is not None:
            self._apply_theme(selected)
    
    def _apply_theme(self, name: str):
        """Set theme in config, refresh prompt session, and print an instant colored preview."""
        from cliara.highlighting import get_theme_preview_markup
        from rich.panel import Panel
        self.config.set("theme", name)
        session = self._create_prompt_session()
        if session is not None:
            self._prompt_session = session
        console = _cliara_console()
        try:
            markup = get_theme_preview_markup(name)
            console.print(Panel(
                markup,
                title=f"[bold]Theme applied: {name}[/bold]",
                border_style="green",
                padding=(0, 1),
            ))
        except Exception:
            console.print(f"[green]✓ Theme set to '{name}'.[/green]")
        console.print()
    
    def _run_theme_picker(self, themes: list, current: str):
        """
        Run an interactive theme picker with up/down arrows and Enter.
        Returns the selected theme name or None if cancelled.
        Uses Rich for the header panel and prompt_toolkit for the live list.
        """
        try:
            from prompt_toolkit import Application
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.layout import Layout, HSplit, Window
            from prompt_toolkit.layout.controls import FormattedTextControl
            from rich.panel import Panel
            from cliara.highlighting import THEMES as _THEMES
        except ImportError:
            # Fallback: plain text list
            print_info("[Cliara] Color themes — type a name to set")
            for name in themes:
                mark = " (active)" if name == current else ""
                print_dim(f"  {name}{mark}")
            try:
                choice = input("\nTheme name (Enter to cancel): ").strip().lower()
                return choice if choice in themes else None
            except (EOFError, KeyboardInterrupt):
                return None

        # ── Rich header panel ─────────────────────────────────────────
        console = _cliara_console()
        console.print()
        console.print(Panel(
            "[bold]↑ / ↓[/bold]  navigate   [bold]Enter[/bold]  select   [bold]Escape[/bold]  cancel",
            title="[bold cyan]✦ Theme Selector[/bold cyan]",
            border_style="cyan",
            padding=(0, 2),
        ))
        console.print()

        # ── State ─────────────────────────────────────────────────────
        selected_index = [themes.index(current) if current in themes else 0]
        n = len(themes)

        def _fg(theme_name: str) -> str:
            """Return the plain ANSI fg color for a theme (strips 'bold')."""
            ps = _THEMES.get(theme_name, _THEMES["monokai"])["prompt_style"]
            return ps["prompt-name"].replace("bold", "").strip()

        # ── Live list renderer ─────────────────────────────────────────
        def get_rows():
            rows = []
            for i, name in enumerate(themes):
                is_sel = i == selected_index[0]
                is_cur = name == current
                fg = _fg(name)
                bg = "bg:ansibrightblack " if is_sel else ""

                rows.append((f"{bg}fg:ansiwhite bold" if is_sel else "", " ❯ " if is_sel else "   "))
                rows.append((f"{bg}fg:{fg} bold", "████"))
                rows.append((f"{bg}bold" if is_sel else f"fg:{fg}", f"  {name:<13}"))
                if is_cur:
                    rows.append((f"{bg}fg:ansiyellow bold", " ✓ active"))
                rows.append(("", "\n"))
            return rows

        list_control = FormattedTextControl(text=get_rows)

        footer_text = [
            ("fg:ansibrightblack", "  "),
            ("fg:ansicyan bold", "↑"),
            ("fg:ansibrightblack", "/"),
            ("fg:ansicyan bold", "↓"),
            ("fg:ansibrightblack", " move   "),
            ("fg:ansicyan bold", "Enter"),
            ("fg:ansibrightblack", " select   "),
            ("fg:ansicyan bold", "Esc"),
            ("fg:ansibrightblack", " cancel\n"),
        ]

        # ── Key bindings ──────────────────────────────────────────────
        kb = KeyBindings()

        @kb.add("up")
        def _up(event):
            selected_index[0] = (selected_index[0] - 1) % n

        @kb.add("down")
        def _down(event):
            selected_index[0] = (selected_index[0] + 1) % n

        @kb.add("enter")
        def _enter(event):
            event.app.exit(result=themes[selected_index[0]])

        @kb.add("c-c")
        @kb.add("escape")
        def _cancel(event):
            event.app.exit(result=None)

        # ── Layout ────────────────────────────────────────────────────
        layout = Layout(HSplit([
            Window(content=list_control, height=n),
            Window(height=1),
            Window(FormattedTextControl(footer_text), height=1),
            Window(height=1),
        ]))

        app = Application(layout=layout, key_bindings=kb, full_screen=False)
        return app.run()
    
    def _handle_cd(self, user_input: str):
        """
        Handle cd commands by changing Cliara's own working directory.
        
        subprocess.run spawns a child shell, so cd in a subprocess has no
        effect on the parent process. We intercept it here and use os.chdir()
        so the prompt reflects the real working directory.
        """
        # Parse target directory
        args = user_input[2:].strip()
        if not args:
            # Bare "cd" goes to home directory
            target = Path.home()
        elif args == '-':
            # "cd -" is not supported without tracking OLDPWD
            print_error("[Error] cd - is not supported")
            return
        else:
            target = Path(args).expanduser()

        try:
            os.chdir(target)
        except FileNotFoundError:
            print_error(f"[Error] cd: no such directory: {args}")
        except PermissionError:
            print_error(f"[Error] cd: permission denied: {args}")
        except Exception as e:
            print_error(f"[Error] cd: {e}")

    # ------------------------------------------------------------------
    # NL query confirmation with copy option
    # ------------------------------------------------------------------
    def _confirm_with_copy_option(self, commands: list, danger_level) -> str:
        """
        Interactive confirmation for NL-generated commands with copy option.
        
        Returns:
            "run" - user wants to execute
            "copy" - user wants to copy to clipboard
            "cancel" - user cancelled
        """
        try:
            # Try to use prompt_toolkit for single-key input
            from prompt_toolkit import prompt
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.keys import Keys
            
            result = {"action": None}
            kb = KeyBindings()
            
            @kb.add("c")
            def _copy(event):
                result["action"] = "copy"
                event.app.exit(result="copy")
            
            @kb.add("enter")
            @kb.add("y")
            def _run(event):
                result["action"] = "run"
                event.app.exit(result="run")
            
            @kb.add("escape")
            @kb.add("n")
            @kb.add("q")
            def _cancel(event):
                result["action"] = "cancel"
                event.app.exit(result="cancel")
            
            @kb.add(Keys.ControlC)
            def _ctrl_c(event):
                result["action"] = "cancel"
                event.app.exit(result="cancel")
            
            action = prompt("", key_bindings=kb)
            return action if action else "cancel"
        except (ImportError, Exception):
            # Fallback to simple input
            if danger_level != DangerLevel.SAFE:
                confirm_prompt = self.safety.get_confirmation_prompt(danger_level)
            else:
                confirm_prompt = "Action (c=copy, y=run, n=cancel): "
            
            try:
                response = input(confirm_prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "cancel"
            
            if response == "c":
                return "copy"
            elif response in ("y", "yes"):
                if danger_level != DangerLevel.SAFE:
                    if not self.safety.validate_confirmation(response, danger_level):
                        return "cancel"
                return "run"
            else:
                return "cancel"

    # ------------------------------------------------------------------
    # Diff preview — show impact before destructive commands
    # ------------------------------------------------------------------
    def _confirm_with_preview(self, command: str) -> bool:
        """
        Show a diff preview for a destructive command and ask for
        confirmation.

        Returns *True* if the user wants to proceed, *False* to cancel.
        """
        preview = self.diff_preview.generate_preview(command)

        if preview is None:
            # Could not generate a preview (no matching files, etc.)
            # — let the command through without blocking.
            return True

        print()
        print_warning(preview)

        try:
            response = input("\n  Proceed? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if response in ("y", "yes"):
            return True

        print_warning("  [Cancelled]")
        return False

    # ------------------------------------------------------------------
    # Inline risk gate — warn and confirm risky commands in the terminal
    # ------------------------------------------------------------------
    def _inline_gate(self, command: str, assessment, *, non_interactive: bool = False) -> bool:
        """
        Render an inline risk warning and ask for y/n confirmation.
        Returns *True* to proceed, *False* to cancel.
        SAFE commands always proceed with a dim explanation line.

        When *non_interactive* is True (e.g. stdin not a TTY), risky commands
        are denied without prompting so the process does not block.
        """
        from cliara.copilot_gate import RiskAssessment

        ra: RiskAssessment = assessment
        level = ra.danger_level

        if level == DangerLevel.SAFE:
            print_dim(f"  -> {ra.explanation}")
            return True

        # Build detail lines
        details: List[str] = []
        if ra.blast_radius != "local":
            details.append(f"Scope: {ra.blast_radius}")
        details.extend(ra.risk_factors)
        details.extend(ra.context_warnings)

        if level == DangerLevel.CAUTION:
            print_warning(f"  [!] CAUTION: {ra.explanation}")
            for d in details:
                print_warning(f"  | {d}")
        elif level == DangerLevel.DANGEROUS:
            print_error(f"  [!!] DANGEROUS: {ra.explanation}")
            for d in details:
                print_error(f"  | {d}")
        elif level == DangerLevel.CRITICAL:
            print_error(f"  [!!!] CRITICAL: {ra.explanation}")
            for d in details:
                print_error(f"  | {d}")
            print_error("  This action is potentially destructive and irreversible.")

        if non_interactive:
            print_warning("  [Skipped] Non-interactive (no TTY); risky commands are not run.")
            return False

        try:
            response = input("  Proceed? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if response in ("y", "yes"):
            return True

        print_warning("  [Cancelled]")
        return False

    # ------------------------------------------------------------------
    # Cross-platform command translation
    # ------------------------------------------------------------------
    def _check_cross_platform(self, command: str):
        """
        After a command fails, check whether it failed because the
        executable doesn't exist on this platform.  If a known
        cross-platform translation is available, offer it to the user.
        """
        base_cmd = get_base_command(command)
        if not base_cmd:
            return

        # If the executable is actually on the system, the failure was
        # caused by something else (bad args, permissions, …) — skip.
        if command_exists(base_cmd):
            return

        os_name = platform.system()
        shell = self.shell_path or ""

        # Try to translate the full pipeline; fall back to single command.
        translated = translate_pipeline(command, os_name, shell)
        if not translated:
            return

        # Present the suggestion
        label = "PowerShell" if (os_name == "Windows" and is_powershell(shell)) else os_name
        print_info(f"\n[Cliara] {label} equivalent: {translated}")
        try:
            response = input("         Run? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if response in ("y", "yes"):
            self._execute_translated_command(translated)

    def _execute_translated_command(self, command: str) -> bool:
        """
        Execute a translated command using the appropriate shell.

        On Windows with PowerShell, translated commands may be PowerShell
        cmdlets that ``cmd.exe`` doesn't understand, so we invoke
        ``powershell`` / ``pwsh`` directly.
        """
        # Record in history
        self.history.add(command)
        self.history.set_last_execution([command])
        self._enqueue_semantic_add(command, str(Path.cwd()), None)

        if platform.system() == "Windows" and is_powershell(self.shell_path or ""):
            try:
                ps_exe = (
                    "pwsh"
                    if "pwsh" in (self.shell_path or "").lower()
                    else "powershell"
                )
                result = subprocess.run(
                    [ps_exe, "-NoProfile", "-Command", command],
                    timeout=300,
                )
                return result.returncode == 0
            except subprocess.TimeoutExpired:
                print_error("[Error] Command timed out (5 minutes)")
                return False
            except Exception as e:
                print_error(f"[Error] {e}")
                return False
        else:
            # CMD or Unix — just pass through to the regular shell
            return self.execute_shell_command(command, capture=False)

    # ------------------------------------------------------------------
    # Error Translator — plain-English stderr explanations + fixes
    # ------------------------------------------------------------------
    def _auto_suggest_fix(self):
        """
        After a failed command, automatically run the error translator and
        show a non-intrusive one-liner hint.  If a fix command is available,
        store it so the user can press Tab on an empty prompt to fill it in.

        Example output:
            hint: try 'pip install requests' (Tab to use)
        """
        if not self.config.get("error_translation", True):
            return
        stderr = self.last_stderr.strip()
        if not stderr:
            return
        # Don't suggest if the executable is missing — cross-platform
        # translation already handles that case.
        base_cmd = get_base_command(self.last_command)
        if base_cmd and not command_exists(base_cmd):
            return

        # Build context for the error translator
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }

        result = self.nl_handler.translate_error(
            self.last_command,
            self.last_exit_code,
            stderr,
            context,
        )

        explanation = result.get("explanation", "")
        fix_commands = result.get("fix_commands", [])

        if fix_commands:
            fix_display = " && ".join(fix_commands)
            self._pending_fix = fix_display
            print_dim(f"\n  hint: try '{fix_display}' (Tab to use)")
        elif explanation:
            # No concrete fix, but we have a useful explanation
            # Keep it short — truncate to one line
            short = explanation.split(".")[0].strip()
            if short:
                print_dim(f"\n  hint: {short}")
        print()  # trailing blank line for readability

    def _maybe_translate_error(self, command: str):
        """
        After a failed command, decide whether to invoke the Error
        Translator and, if so, display the result.

        Skipped when:
        - The feature is disabled in config
        - There is no captured stderr
        - The command's base executable doesn't exist (cross-platform
          translation handles that case instead)
        """
        if not self.config.get("error_translation", True):
            return

        stderr = self.last_stderr.strip()
        if not stderr:
            return

        # If the executable itself is missing, _check_cross_platform will
        # handle it — don't double-up with an error translation.
        base_cmd = get_base_command(command)
        if base_cmd and not command_exists(base_cmd):
            return

        self._handle_error_translation(command, stderr)

    def _handle_error_translation(self, command: str, stderr: str):
        """
        Send stderr to the NL handler for analysis and display the
        plain-English result.  If a fix is suggested, offer to run it.
        """
        print()  # visual separator after the raw error output

        # Build context identical to NL queries
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }

        # fix agent returns JSON — do not stream raw JSON to the console
        result = self.nl_handler.translate_error(
            command,
            self.last_exit_code,
            stderr,
            context,
            stream_callback=None,
        )

        explanation = result.get("explanation", "")
        fix_commands = result.get("fix_commands", [])
        fix_explanation = result.get("fix_explanation", "")

        print_info(f"[Cliara] {explanation}")

        if fix_commands:
            # Show the suggested fix
            fix_display = " && ".join(fix_commands)
            print_info(f"         Fix: {fix_display}")

            if fix_explanation:
                print_dim(f"         ({fix_explanation})")

            # Offer to run
            try:
                response = input("         Run fix? (y/n): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if response in ("y", "yes"):
                # Safety check on the fix commands
                level, dangerous = self.safety.check_commands(fix_commands)
                if level != DangerLevel.SAFE:
                    print(self.safety.get_warning_message(
                        [cmd for cmd, _ in dangerous], level
                    ))
                    prompt = self.safety.get_confirmation_prompt(level)
                    confirm = input(prompt).strip()
                    if not self.safety.validate_confirmation(confirm, level):
                        print_warning("[Cancelled]")
                        return

                # Link fix commands to the failed command in the execution graph
                if self.current_session and self.current_session.commands:
                    self._next_command_parent_id = self.current_session.commands[-1].id

                print()
                for i, fix_cmd in enumerate(fix_commands, 1):
                    if len(fix_commands) > 1:
                        print_info(f"[Fix {i}/{len(fix_commands)}] {fix_cmd}")
                    success = self.execute_shell_command(fix_cmd, capture=False)
                    if not success:
                        print_error(f"[Cliara] Fix command failed: {fix_cmd}")
                        break
                else:
                    print_success("[Cliara] Fix applied successfully!")
        print()

    # ------------------------------------------------------------------
    # Long-running command notification
    # ------------------------------------------------------------------
    def _notify_completion(self, command: str, elapsed: float, success: bool):
        """
        Send a desktop notification when a command exceeds the configured
        threshold.  Uses the terminal bell (\\a) as the primary mechanism
        — zero dependencies, works everywhere.  On Windows 10+ we also
        attempt a toast notification via a PowerShell one-liner.
        """
        threshold = self.config.get("notify_after_seconds", 30)
        if threshold <= 0 or elapsed < threshold:
            return

        status = "completed" if success else "failed"
        elapsed_str = f"{elapsed:.0f}s"

        # Shorten command for display
        short_cmd = command if len(command) <= 40 else command[:37] + "..."

        # Always print a summary line
        if success:
            print_success(f"\n[Cliara] {short_cmd} {status} ({elapsed_str})")
        else:
            print_error(f"\n[Cliara] {short_cmd} {status} ({elapsed_str})")

        # Terminal bell — works on virtually every terminal
        sys.stdout.write("\a")
        sys.stdout.flush()

        # Windows toast notification (best-effort, silent failure)
        if platform.system() == "Windows":
            try:
                title = "Cliara"
                body = f"{short_cmd} {status} ({elapsed_str})"
                # PowerShell one-liner using BurntToast or built-in
                ps_cmd = (
                    f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
                    f'$n = New-Object System.Windows.Forms.NotifyIcon; '
                    f'$n.Icon = [System.Drawing.SystemIcons]::Information; '
                    f'$n.Visible = $true; '
                    f'$n.ShowBalloonTip(5000, "{title}", "{body}", '
                    f'[System.Windows.Forms.ToolTipIcon]::Info); '
                    f'Start-Sleep -Seconds 6; $n.Dispose()'
                )
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
            except Exception:
                pass  # Toast is a nice-to-have, not critical
        else:
            # macOS / Linux: try notify-send or osascript
            try:
                if platform.system() == "Darwin":
                    subprocess.Popen(
                        ["osascript", "-e",
                         f'display notification "{short_cmd} {status} ({elapsed_str})" '
                         f'with title "Cliara"'],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    subprocess.Popen(
                        ["notify-send", "Cliara",
                         f"{short_cmd} {status} ({elapsed_str})"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            except Exception:
                pass

        print_dim("  [Desktop notification sent]")

    def execute_shell_command(self, command: str, capture: bool = False) -> bool:
        """
        Execute a command in the underlying shell.

        Stderr is always captured (in addition to being displayed in
        real-time) so the Error Translator can analyse it when the
        command fails.  A live spinner with elapsed time is shown for
        long-running commands, and a desktop notification fires when a
        command exceeds ``notify_after_seconds``.

        Args:
            command: Shell command to execute
            capture: Whether to capture stdout as well (vs. stream to console)

        Returns:
            True if command succeeded (exit code 0)
        """
        # Reset per-command error state
        self.last_stderr = ""
        self.last_exit_code = 0
        self.last_command = command

        start_time = time.time()

        # Build a timer (or a no-op stub when spinners are disabled).
        # In capture mode nothing prints, so the inline spinner is safe.
        # In streaming mode the child's stdout is inherited, so we only
        # update the terminal title bar to avoid garbled output.
        spinner_delay = self.config.get("spinner_delay_seconds", 3)
        timer = None

        try:
            # Add to history
            self.history.add(command)
            self.history.set_last_execution([command])
            self._enqueue_semantic_add(command, str(Path.cwd()), None)

            if capture:
                # ── Capture mode: both stdout and stderr captured ──
                if spinner_delay > 0:
                    timer = _LiveTimer(
                        command, delay=spinner_delay, inline=True,
                    )
                else:
                    timer = _NullTimer()
                timer.start()
                try:
                    # On Windows with a PowerShell-configured shell, run via PowerShell
                    # so cmdlets like Get-ChildItem work as expected.
                    if platform.system() == "Windows" and is_powershell(self.shell_path or ""):
                        ps_exe = (
                            "pwsh"
                            if "pwsh" in (self.shell_path or "").lower()
                            else "powershell"
                        )
                        result = subprocess.run(
                            [ps_exe, "-NoProfile", "-Command", command],
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                    else:
                        result = subprocess.run(
                            command,
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                finally:
                    timer.stop()

                print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                self.last_stderr = result.stderr or ""
                self.last_exit_code = result.returncode
                success = result.returncode == 0
                elapsed = time.time() - start_time
                self._notify_completion(command, elapsed, success)
                self._session_record_command(command, success)
                if success and self.config.get("regression_snapshots", True):
                    self._regression_save_success(command, elapsed)
                return success
            else:
                # ── Streaming mode: stdout AND stderr piped ──
                # Both streams are relayed to the terminal by background
                # threads that coordinate with the inline spinner via
                # output_lock().  Piping stdout (instead of inheriting
                # it) means the spinner and command output never fight
                # over the same cursor.
                if spinner_delay > 0:
                    # In streaming mode, keep the spinner to the title bar only
                    # to avoid choppy inline updates fighting with command output.
                    timer = _LiveTimer(
                        command, delay=spinner_delay, inline=False,
                    )
                else:
                    timer = _NullTimer()

                # On Windows + PowerShell shell, invoke PowerShell directly so
                # that PowerShell cmdlets are available. Otherwise, fall back
                # to the platform's default shell.
                if platform.system() == "Windows" and is_powershell(self.shell_path or ""):
                    ps_exe = (
                        "pwsh"
                        if "pwsh" in (self.shell_path or "").lower()
                        else "powershell"
                    )
                    popen_cmd = [ps_exe, "-NoProfile", "-Command", command]
                    popen_kwargs = {
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "encoding": "utf-8",
                        "errors": "replace",
                    }
                    proc = subprocess.Popen(popen_cmd, **popen_kwargs)
                else:
                    proc = subprocess.Popen(
                        command,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                        errors="replace",
                    )

                stderr_lines: List[str] = []

                def _drain_stdout():
                    """Read stdout line-by-line, display via timer lock."""
                    try:
                        assert proc.stdout is not None
                        for line in proc.stdout:
                            with timer.output_lock():
                                sys.stdout.write(line)
                                sys.stdout.flush()
                    except Exception:
                        pass

                def _drain_stderr():
                    """Read stderr line-by-line, display and buffer."""
                    try:
                        assert proc.stderr is not None
                        for line in proc.stderr:
                            stderr_lines.append(line)
                            with timer.output_lock():
                                sys.stderr.write(line)
                                sys.stderr.flush()
                    except Exception:
                        pass

                stdout_reader = threading.Thread(
                    target=_drain_stdout, daemon=True,
                )
                stderr_reader = threading.Thread(
                    target=_drain_stderr, daemon=True,
                )
                stdout_reader.start()
                stderr_reader.start()
                timer.start()

                timed_out = False
                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    timed_out = True

                # Stop timer first (clears spinner), then join readers
                timer.stop()
                stdout_reader.join(timeout=5)
                stderr_reader.join(timeout=5)

                if timed_out:
                    print_error("[Error] Command timed out (5 minutes)")
                    self.last_exit_code = -1
                    self._notify_completion(command, time.time() - start_time, False)
                    self._session_record_command(command, False)
                    return False

                self.last_stderr = "".join(stderr_lines)
                self.last_exit_code = proc.returncode
                success = proc.returncode == 0
                elapsed = time.time() - start_time
                self._notify_completion(command, elapsed, success)
                self._session_record_command(command, success)
                if success and self.config.get("regression_snapshots", True):
                    self._regression_save_success(command, elapsed)
                return success

        except Exception as e:
            try:
                if timer is not None:
                    timer.stop()
            except Exception:
                pass
            print_error(f"[Error] {e}")
            self.last_exit_code = -1
            self._session_record_command(command, False)
            return False

    def _session_record_command(self, command: str, success: bool):
        """If a task session is active, record this command to it."""
        if not self.current_session:
            return
        cwd = str(Path.cwd())
        root = _get_project_root(Path(cwd))
        branch = _get_branch(Path(cwd))
        parent_id = self._next_command_parent_id
        self._next_command_parent_id = None  # consume once
        self.session_store.add_command(
            self.current_session.id,
            command=command,
            cwd=cwd,
            exit_code=0 if success else (self.last_exit_code if self.last_exit_code != 0 else 1),
            branch=branch,
            project_root=root,
            parent_id=parent_id,
        )
        # Refresh in-memory session so prompt and list stay in sync
        updated = self.session_store.get_by_id(self.current_session.id)
        if updated:
            self.current_session = updated

    def _regression_workflow_key(self, command: str) -> Optional[str]:
        """Compute workflow key for regression snapshot (project_root or cwd + base command)."""
        cwd = Path.cwd()
        root = _get_project_root(cwd)
        base = get_base_command(command)
        if not base:
            return None
        return f"{root or 'cwd:' + str(cwd)}::{base}"

    def _regression_save_success(self, command: str, elapsed: Optional[float] = None) -> None:
        """Capture and save a success snapshot for this workflow (called after successful run).

        To keep common, fast commands snappy, we only record a snapshot when the
        command ran for at least ``regression_min_success_seconds`` (default: 3s).
        """
        # Skip very fast commands so regression tracking doesn't add noticeable latency.
        try:
            min_seconds = float(self.config.get("regression_min_success_seconds", 3.0))
        except Exception:
            min_seconds = 3.0
        if elapsed is not None and elapsed < max(min_seconds, 0.0):
            return

        key = self._regression_workflow_key(command)
        if not key:
            return
        cwd = Path.cwd()
        store_path = self.config.config_dir / "regression_snapshots.json"
        snap = regression.capture_snapshot(cwd)
        regression.save_success_snapshot(key, snap, store_path)

    def _regression_is_invalid_command(self) -> bool:
        """Return True when the failure is a bad/unknown subcommand, not an env issue."""
        stderr = (getattr(self, "last_stderr", "") or "").lower()
        # Patterns emitted by common tools when the subcommand itself doesn't exist
        invalid_patterns = [
            "is not a git command",
            "is not a npm command",
            "is not a yarn command",
            "unknown command",
            "unrecognized command",
            "invalid command",
            "no such subcommand",
            "command not found",
            "is not recognized as",
        ]
        return any(p in stderr for p in invalid_patterns)

    def _regression_check_failure(self, command: str) -> None:
        """On failure: compare to last success, print minimal report, store for ? why."""
        if not self.config.get("regression_snapshots", True):
            return
        # Skip when the failure is clearly a typo / bad subcommand — the
        # environment didn't cause this, so a regression report would be noise.
        if self._regression_is_invalid_command():
            return
        key = self._regression_workflow_key(command)
        if not key:
            return
        store_path = self.config.config_dir / "regression_snapshots.json"
        last = regression.load_last_success(key, store_path)
        if not last:
            return
        cwd = Path.cwd()
        current = regression.gather_current_snapshot(cwd)
        diff_result = regression.diff_snapshots(last, current)
        causes = regression.rank_causes(diff_result, last, current)
        if not causes:
            return
        self._last_regression_report = (causes, last, current)
        from rich.panel import Panel
        line = regression.format_minimal_report(causes)
        panel = Panel(line, title="Regression", border_style="dim")
        _cliara_console().print(panel)

    # ------------------------------------------------------------------
    # Smart Push — auto-commit-message + branch detection
    # ------------------------------------------------------------------
    def handle_push(self):
        """
        Built-in smart push: detect branch, stage changes, generate a
        conventional commit message via LLM, commit, and push.
        """
        # ── 1. Are we in a git repo? ──
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print_error("[Cliara] Not inside a git repository.")
            return

        # ── 2. Current branch ──
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        branch = (result.stdout or "").strip()
        if not branch:
            print_error("[Cliara] Detached HEAD state — checkout a branch first.")
            return

        # ── 3. Anything to commit? ──
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        status_output = (result.stdout or "").strip()

        if not status_output:
            # Nothing to commit — maybe there are unpushed commits?
            result = subprocess.run(
                ["git", "log", f"origin/{branch}..HEAD", "--oneline"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            unpushed = (result.stdout or "").strip()
            if unpushed:
                count = len(unpushed.splitlines())
                print_info(
                    f"\n[Cliara] {count} unpushed commit(s) on '{branch}':\n"
                )
                print(unpushed)
                try:
                    confirm = input(
                        f"\nPush to '{branch}'? (y/n): "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if confirm in ("y", "yes"):
                    print()
                    self.execute_shell_command(f"git push origin {branch}")
                else:
                    print_warning("[Cancelled]")
            else:
                print_info(
                    f"[Cliara] Everything up to date on '{branch}'. "
                    "Nothing to commit or push."
                )
            return

        # ── 4. Show what changed ──
        print_info(f"\n[Cliara] Changes detected on '{branch}':\n")
        # Coloured status from git
        subprocess.run(["git", "-c", "color.status=always", "status", "--short"])

        # ── 5. Stage everything ──
        print_dim("\nStaging all changes...")
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )

        # ── 6. Gather diff for message generation ──
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        diff_stat = (result.stdout or "").strip()

        result = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        diff_content = (result.stdout or "").strip()

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        files = [f for f in (result.stdout or "").strip().splitlines() if f]

        # ── 7. Generate commit message ──
        print_dim("Generating commit message...\n")

        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
            "branch": branch,
        }
        stream_cb = self._stream_callback_for_console() if self.config.get("stream_llm", True) else None
        commit_msg = self.nl_handler.generate_commit_message(
            diff_stat, diff_content, files, context, stream_callback=stream_cb
        )
        if not commit_msg or not commit_msg.strip():
            print_error("[Cliara] Could not generate commit message. Try again or use: git commit -m \"your message\"")
            self._unstage_all()
            return

        # ── 8. Show message and confirm ──
        if stream_cb is None:
            print_info("[Cliara] Commit message:")
            print(f"\n  {commit_msg}\n")
        else:
            print()  # newline after streamed output
        print_dim(f"  Branch: {branch}")
        print_dim(f"  Files:  {len(files)} changed")
        print()

        try:
            response = input(
                "Accept? (y)es / (e)dit / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            self._unstage_all()
            return

        if response in ("e", "edit"):
            try:
                from prompt_toolkit import prompt as pt_prompt
                custom = pt_prompt("Edit commit message: ", default=commit_msg).strip()
            except Exception:
                custom = input("Edit commit message: ").strip() or commit_msg
            if not custom:
                print_warning("[Cancelled]")
                self._unstage_all()
                return
            commit_msg = custom
        elif response not in ("y", "yes"):
            print_warning("[Cancelled]")
            self._unstage_all()
            return

        # ── 9. Commit (use subprocess list form to safely handle quotes) ──
        print()
        proc = subprocess.run(
            ["git", "commit", "-m", commit_msg],
        )
        if proc.returncode != 0:
            print_error("[Cliara] Commit failed.")
            return

        # ── 10. Push ──
        # Check if the remote branch already exists
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if (result.stdout or "").strip():
            success = self.execute_shell_command(f"git push origin {branch}")
        else:
            print_dim(f"Branch '{branch}' is new on remote — setting up tracking...")
            success = self.execute_shell_command(
                f"git push -u origin {branch}"
            )

        if success:
            print_success(f"\n[Cliara] Successfully pushed to '{branch}'!")

    def _unstage_all(self):
        """Reset the staging area (undo git add -A)."""
        subprocess.run(["git", "reset"], capture_output=True)

    # ------------------------------------------------------------------
    # Task sessions — named, resumable workflow context
    # ------------------------------------------------------------------

    def handle_session(self, subcommand: str = ""):
        """
        Task session subcommands: start, resume, end, list, show, note, help.
        """
        parts = subcommand.split(maxsplit=1)
        sub = (parts[0].lower() if parts else "").strip()
        rest = (parts[1] if len(parts) > 1 else "").strip()

        if sub == "start":
            self._session_start(rest)
            return
        if sub == "resume":
            self._session_resume(rest)
            return
        if sub == "end":
            self._session_end(rest)
            return
        if sub == "list":
            self._session_list()
            return
        if sub == "show":
            self._session_show(rest)
            return
        if sub == "graph":
            self._session_graph(rest)
            return
        if sub == "note":
            self._session_note(rest)
            return
        if sub in ("help", ""):
            self._session_help()
            return
        print_error(f"[Cliara] Unknown session subcommand: '{sub}'")
        print_dim("  session start <name> [ -- <intent>]   Name can be multi-word")
        print_dim("  session resume <name>          Resume a session and show summary")
        print_dim("  session end [note]              End current session")
        print_dim("  session list                    List sessions")
        print_dim("  session show <name>             Show session summary (no resume)")
        print_dim("  session graph [name]            Show execution graph (tree)")
        print_dim("  session note <text>             Add a note to current session")
        print_dim("  session help                    Show this help")

    def _session_start(self, args: str):
        """Start a new named task session. If already in a session, end it first.
        Session name can be multi-word. Use ' -- ' to add an optional intent.
        E.g. 'session start fix login bug' or 'session start fix login bug -- get redirect working'.
        """
        if not args:
            print_error("[Cliara] Usage: session start <name> [ -- <intent>]")
            return
        if " -- " in args:
            name, intent = args.split(" -- ", 1)
            name = name.strip()
            intent = intent.strip()
        else:
            name = args.strip()
            intent = ""
        if not name:
            print_error("[Cliara] Session name cannot be empty.")
            return

        cwd = Path.cwd()
        project_root = _get_project_root(cwd)
        branch = _get_branch(cwd)

        if self.current_session:
            print_info(f"[Cliara] Ending current session '{self.current_session.name}'.")
            self.session_store.end_session(self.current_session.id)
            self.current_session = None

        existing = self.session_store.get_by_key(name, project_root)
        if existing and not existing.is_ended:
            print_warning(f"[Cliara] Session '{name}' already exists and is in progress.")
            try:
                r = input("Resume it instead? (y/n): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if r in ("y", "yes"):
                self._session_resume(name)
            return
        if existing and existing.is_ended:
            # Allow starting again with same name — we create a new session (replace)
            pass

        session = self.session_store.create(
            name=name,
            intent=intent,
            project_root=project_root,
            branch=branch,
        )
        self.current_session = session
        print_success(f"[Cliara] Session started: '{name}'")
        if intent:
            print_dim(f"  Intent: {intent}")

    def _session_resume(self, name: str):
        """Resume a session by name (current project). Show summary and suggested next step."""
        if not name:
            print_error("[Cliara] Usage: session resume <name>")
            print_dim("  Use 'session list' to see session names.")
            return
        cwd = Path.cwd()
        project_root = _get_project_root(cwd)
        session = self.session_store.get_by_key(name, project_root)
        if session is None:
            print_error(f"[Cliara] No session named '{name}' in this project.")
            print_dim("  Use 'session list' to see sessions (or start in the right directory).")
            return
        if self.current_session and self.current_session.id != session.id:
            self.session_store.end_session(self.current_session.id)
        self.current_session = session
        if session.is_ended:
            # Re-open for more work
            session.ended_at = None
            session.end_note = None
            self.session_store.update(session)
            self.current_session = self.session_store.get_by_id(session.id)
        self._session_print_resume_summary(self.current_session)

    def _session_print_resume_summary(self, s: TaskSession, resumed: bool = True):
        """Print structured summary and suggested next step."""
        if resumed:
            print_info("\n[Cliara] Session resumed: " + s.name)
        else:
            print_info("\n[Cliara] Session: " + s.name)
        print_header("-" * 50)
        if s.intent:
            print(f"  Intent:   {s.intent}")
        try:
            from datetime import datetime, timezone
            updated = datetime.fromisoformat(s.updated.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - updated
            if delta.days > 0:
                ago = f"{delta.days}d ago"
            elif delta.seconds >= 3600:
                ago = f"{delta.seconds // 3600}h ago"
            else:
                ago = f"{max(1, delta.seconds // 60)}m ago"
            print_dim(f"  Last active: {ago}")
        except Exception:
            print_dim(f"  Last active: {s.updated}")
        if s.cwds:
            print_dim("  Where you worked:")
            for d in s.cwds[-5:]:
                print_dim(f"    {d}")
        if s.branch:
            print_dim(f"  Branch: {s.branch}")
        if s.commands:
            print_dim("  Last commands:")
            for c in s.commands[-8:]:
                status = "✓" if c.exit_code == 0 else "✗"
                short = c.command[:60] + "..." if len(c.command) > 60 else c.command
                print(f"    {status} {short}")
        if s.notes:
            print_dim("  Notes:")
            for n in s.notes[-5:]:
                print_dim(f"    {n.text[:70]}{'...' if len(n.text) > 70 else ''}")
        if s.end_note:
            print_dim(f"  End note: {s.end_note[:70]}{'...' if len(s.end_note) > 70 else ''}")

        next_step = self._session_suggest_next_step(s)
        if next_step:
            print()
            print_info("  Suggested next: " + next_step)
        print_header("-" * 50 + "\n")

    def _session_suggest_next_step(self, s: TaskSession) -> Optional[str]:
        """Heuristic: suggest what to do next based on last command and notes."""
        if not s.commands:
            if s.cwds:
                return f"Continue from last directory: cd {s.cwds[-1]}"
            return "Start running commands — they'll be recorded in this session."
        last = s.commands[-1]
        if last.exit_code != 0:
            return "Last command failed (exit %d). Re-run or debug, then continue." % last.exit_code
        return "Last command succeeded. Continue from here or add a note: session note <text>."

    def _session_end(self, end_note: str):
        """End the current session with optional note."""
        if not self.current_session:
            print_info("[Cliara] No active session to end.")
            return
        name = self.current_session.name
        self.session_store.end_session(self.current_session.id, end_note=end_note or None)
        self.current_session = None
        print_success(f"[Cliara] Session '{name}' ended.")
        if end_note:
            print_dim(f"  Note: {end_note[:80]}{'...' if len(end_note) > 80 else ''}")

    def _session_list(self):
        """List all sessions, or for current project only."""
        cwd = Path.cwd()
        project_root = _get_project_root(cwd)
        sessions = self.session_store.list_by_project(project_root)
        if not sessions:
            print_info("[Cliara] No task sessions yet.")
            print_dim("  session start <name> [intent]   to start one")
            return
        print_info(f"\n[Cliara] Task sessions ({len(sessions)}):\n")
        for s in sessions:
            status = "ended" if s.is_ended else "active"
            intent_preview = (s.intent[:40] + "...") if len(s.intent or "") > 40 else (s.intent or "")
            print(f"  {s.name}")
            print_dim(f"    {status} — {s.updated} — {intent_preview}")
        print()

    def _session_show(self, name: str):
        """Show full summary of a session without resuming."""
        if not name:
            print_error("[Cliara] Usage: session show <name>")
            return
        cwd = Path.cwd()
        project_root = _get_project_root(cwd)
        session = self.session_store.get_by_key(name, project_root)
        if session is None:
            print_error(f"[Cliara] No session named '{name}' in this project.")
            return
        self._session_print_resume_summary(session, resumed=False)
        if session.id != getattr(self.current_session, "id", None):
            print_dim("  (Not resumed — use 'session resume %s' to continue.)" % name)

    def _session_note(self, text: str):
        """Add a note to the current session."""
        if not self.current_session:
            print_error("[Cliara] No active session. Start one with 'session start <name>'.")
            return
        if not text:
            print_error("[Cliara] Usage: session note <text>")
            return
        self.session_store.add_note(self.current_session.id, text)
        updated = self.session_store.get_by_id(self.current_session.id)
        if updated:
            self.current_session = updated
        print_success("[Cliara] Note added.")

    def _session_help(self):
        """Show session command help."""
        print_info("\n[Cliara] Task sessions — persistent, resumable workflow context\n")
        print("  session start <name> [ -- <intent>]   Name can be multi-word (e.g. fix login bug)")
        print("  session resume <name>          Resume and see summary + suggested next step")
        print("  session end [note]             End current session (optional closing note)")
        print("  session list                   List sessions for this project")
        print("  session show <name>             Show session summary without resuming")
        print("  session graph [name]            Show execution graph (tree); optional: export [file], export --json <file>")
        print("  session note <text>            Add a note to the current session")
        print("  session help                   Show this help")
        print_dim("\n  Sessions are keyed by name + project (git root). Close the terminal")
        print_dim("  and run 'session resume <name>' later to continue.\n")

    def _session_graph(self, rest: str):
        """Show execution graph for current or named session. Optional: export [path] or export --json <path>."""
        cwd = Path.cwd()
        project_root = _get_project_root(cwd)

        # Parse: rest can be "", "<name>", "export [path]", "export --json <path>", or "<name> export ..."
        export_json = False
        export_path: Optional[Path] = None
        do_export = False
        name_part = rest

        if rest.strip().startswith("export"):
            # Current session: "export" or "export path" or "export --json path"
            name_part = ""
            do_export = True
            tokens = rest.split()
            if len(tokens) >= 2 and tokens[1] == "--json":
                export_json = True
                export_path = Path(tokens[2]) if len(tokens) > 2 else None
            else:
                export_path = Path(tokens[1]) if len(tokens) > 1 else None
        elif " export " in rest:
            name_part, _, export_rest = rest.partition(" export ")
            name_part = name_part.strip()
            do_export = True
            tokens = export_rest.split()
            if tokens and tokens[0] == "--json":
                export_json = True
                export_path = Path(tokens[1]) if len(tokens) > 1 else None
            else:
                export_path = Path(tokens[0]) if tokens else None

        session: Optional[TaskSession] = None
        if name_part:
            session = self.session_store.get_by_key(name_part, project_root)
            if session is None:
                print_error(f"[Cliara] No session named '{name_part}' in this project.")
                return
        else:
            session = self.current_session
            if session is None:
                print_error("[Cliara] No active session. Start one with 'session start <name>' or use 'session graph <name>'.")
                return

        if not session.commands:
            print_info(f"[Cliara] Session '{session.name}' has no commands yet.")
            return

        tree = build_execution_tree(session.commands)
        text = render_execution_tree(tree)

        if do_export or export_path is not None or export_json:
            if export_path is None:
                safe_name = session.name.replace(" ", "-")[:30]
                export_path = Path(f"cliara-graph-{safe_name}.json" if export_json else f"cliara-graph-{safe_name}.txt")
            export_path = Path(export_path)
            if export_json:
                export_tree_json(session.commands, export_path)
                print_success(f"[Cliara] Graph exported to {export_path} (JSON)")
            else:
                export_path.write_text(text, encoding="utf-8")
                print_success(f"[Cliara] Graph exported to {export_path}")
        else:
            print_info(f"\n[Cliara] Execution graph — {session.name}\n")
            print(text)
            print()

    # ------------------------------------------------------------------
    # Smart Deploy — detect project type and deploy in one word
    # ------------------------------------------------------------------

    def handle_deploy(self, subcommand: str = ""):
        """
        Built-in smart deploy: detect the project's deployment target,
        show the plan, confirm, and execute step-by-step.

        Supports subcommands:
            deploy              Run the deploy flow
            deploy config       Show / edit saved deploy config
            deploy history      Show past deploys for this project
            deploy reset        Forget saved config and re-detect
        """
        sub = subcommand.strip().lower()
        if sub == "config":
            self._deploy_show_config()
            return
        if sub == "history":
            self._deploy_show_history()
            return
        if sub == "reset":
            self._deploy_reset()
            return
        if sub == "help":
            self._deploy_help()
            return
        if sub:
            print_error(f"[Cliara] Unknown deploy subcommand: '{sub}'")
            print_dim("  Available: deploy, deploy config, deploy history, deploy reset, deploy help")
            return

        cwd = Path.cwd()

        # ── 1. Check for saved config first ──
        saved = self.deploy_store.get(cwd)
        if saved is not None:
            self._deploy_from_saved(cwd, saved)
            return

        # ── 2. Auto-detect deploy targets ──
        plans = detect_deploy_targets(cwd)

        if not plans:
            # Nothing detected — fall back to NL
            self._deploy_nl_fallback(cwd)
            return

        if len(plans) == 1:
            plan = plans[0]
        else:
            plan = self._deploy_choose_target(plans)
            if plan is None:
                return

        # ── 3. Pre-deploy checks ──
        if not self._deploy_pre_checks(cwd, plan):
            return

        # ── 4. Show plan and confirm ──
        self._deploy_show_plan(plan, cwd)
        action = self._deploy_confirm()
        if action is None:
            return

        if action == "edit":
            steps = self._deploy_edit_steps(plan.steps)
            if steps is None:
                return
            plan.steps = steps

        # ── 5. Save config for next time ──
        self.deploy_store.save(
            cwd,
            platform=plan.platform,
            steps=plan.steps,
            project_name=plan.project_name,
            framework=plan.framework,
        )

        # ── 6. Execute ──
        self._deploy_execute(cwd, plan.steps, plan.platform)

    # -- Saved config flow ---------------------------------------------------

    def _deploy_from_saved(self, cwd: Path, saved):
        """Run a previously saved deploy config."""
        # Time-since-last-deploy hint
        age_hint = ""
        if saved.last_deployed:
            try:
                from datetime import datetime, timezone
                last = datetime.fromisoformat(saved.last_deployed)
                delta = datetime.now(timezone.utc) - last
                if delta.days > 0:
                    age_hint = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    age_hint = f"{delta.seconds // 3600}h ago"
                else:
                    age_hint = f"{delta.seconds // 60}m ago"
            except Exception:
                pass

        platform_label = saved.platform.title()
        if saved.framework:
            platform_label += f" ({saved.framework})"

        count_label = f"deployed {saved.deploy_count} time(s)" if saved.deploy_count else "never deployed"
        time_label = f"last: {age_hint}" if age_hint else ""
        meta = ", ".join(filter(None, [count_label, time_label]))

        print_info(f"\n[Cliara] Deploy to {platform_label}  ({meta})")
        print()
        for i, step in enumerate(saved.steps, 1):
            print(f"  {i}. {step}")
        print()

        try:
            response = input(
                "  Continue? (y)es / (e)dit / (r)edetect / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if response in ("r", "redetect"):
            self.deploy_store.remove(cwd)
            print_dim("  Saved config cleared — re-detecting...\n")
            self.handle_deploy()
            return

        if response in ("e", "edit"):
            steps = self._deploy_edit_steps(saved.steps)
            if steps is None:
                return
            self.deploy_store.save(
                cwd,
                platform=saved.platform,
                steps=steps,
                project_name=saved.project_name,
                framework=saved.framework,
            )
            self._deploy_execute(cwd, steps, saved.platform)
            return

        if response not in ("y", "yes"):
            print_warning("  [Cancelled]")
            return

        # Pre-deploy checks
        plan = DeployPlan(
            platform=saved.platform,
            steps=saved.steps,
            project_name=saved.project_name,
            framework=saved.framework,
        )
        if not self._deploy_pre_checks(cwd, plan):
            return

        self._deploy_execute(cwd, saved.steps, saved.platform)

    # -- Multiple targets ----------------------------------------------------

    def _deploy_choose_target(self, plans: list) -> "Optional[DeployPlan]":
        """Let the user pick from multiple detected deploy targets."""
        print_info("\n[Cliara] Multiple deploy targets detected:\n")
        for i, plan in enumerate(plans, 1):
            print(f"  {i}. {plan.summary_line}")
        print()

        try:
            choice = input("  Which target? (number, or 'n' to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if choice.lower() in ("n", "no", ""):
            print_warning("  [Cancelled]")
            return None

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(plans):
                return plans[idx]
        except ValueError:
            pass

        print_error("  Invalid choice.")
        return None

    # -- NL fallback ---------------------------------------------------------

    def _deploy_nl_fallback(self, cwd: Path):
        """
        When auto-detection finds nothing, ask the user to describe
        their deploy process in natural language and generate a plan.
        """
        print_warning("\n[Cliara] No deployment platform detected.\n")

        if not self.nl_handler.llm_enabled:
            print_dim(
                "  No deploy config files found (Vercel, Fly.io, Netlify, "
                "Dockerfile, etc.).\n"
                "  Set OPENAI_API_KEY in your .env to describe your deploy "
                "process in plain English.\n"
            )
            return

        print(
            "  Describe how you deploy this project (or press Enter to cancel):"
        )
        try:
            description = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not description:
            return

        print_dim("\n  Generating deploy steps...\n")
        context = {
            "cwd": str(cwd),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }
        # deploy agent returns JSON — do not stream raw JSON to the console
        commands = self.nl_handler.generate_deploy_steps(description, context, stream_callback=None)

        if not commands or (len(commands) == 1 and commands[0].startswith("#")):
            print_error("  Could not generate deploy steps.")
            return

        print_info("  Generated steps:")
        for i, cmd in enumerate(commands, 1):
            print(f"    {i}. {cmd}")
        print()

        try:
            response = input(
                "  Run these steps? (y)es / (e)dit / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if response in ("e", "edit"):
            commands = self._deploy_edit_steps(commands)
            if commands is None:
                return

        if response not in ("y", "yes", "e", "edit"):
            print_warning("  [Cancelled]")
            return

        # Offer to save
        try:
            save_resp = input(
                "  Save as default deploy for this project? (y/n): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            save_resp = "n"

        if save_resp in ("y", "yes"):
            self.deploy_store.save(
                cwd,
                platform="custom",
                steps=commands,
                project_name=cwd.name,
            )
            print_dim("  Saved!\n")

        self._deploy_execute(cwd, commands, "custom")

    # -- Pre-deploy checks ---------------------------------------------------

    def _deploy_pre_checks(self, cwd: Path, plan: DeployPlan) -> bool:
        """
        Run sanity checks before deploying.
        Returns True if OK to proceed, False to abort.
        """
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
            cwd=str(cwd),
        )
        if result.returncode == 0 and result.stdout.strip():
            print_warning(
                "\n  [Warning] You have uncommitted changes."
            )
            try:
                resp = input(
                    "  Run 'push' first to commit & push? (y/n): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if resp in ("y", "yes"):
                self.handle_push()
                print()

        # Check branch (warn if not main/master)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch not in ("main", "master"):
                print_warning(
                    f"\n  [Warning] You're on branch '{branch}', not main/master."
                )
                try:
                    resp = input(
                        "  Deploy from this branch? (y/n): "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return False
                if resp not in ("y", "yes"):
                    print_warning("  [Cancelled]")
                    return False

        return True

    # -- Plan display & confirmation -----------------------------------------

    def _deploy_show_plan(self, plan: DeployPlan, cwd: Path):
        """Print the detected deploy plan."""
        print_info(f"\n[Cliara] Deploy detected for this project:\n")
        print(f"  Platform:  {plan.platform.title()}")
        if plan.project_name:
            print(f"  Project:   {plan.project_name}")
        if plan.framework:
            print(f"  Framework: {plan.framework}")
        if plan.detected_from:
            print(f"  Detected:  {plan.detected_from}")
        print()
        print_dim("  Steps:")
        for i, step in enumerate(plan.steps, 1):
            print(f"    {i}. {step}")
        print()

    def _deploy_confirm(self) -> Optional[str]:
        """
        Prompt the user: (y)es / (e)dit / (n)o.
        Returns 'yes', 'edit', or None for cancel.
        """
        try:
            response = input(
                "  Continue? (y)es / (e)dit / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if response in ("y", "yes"):
            return "yes"
        if response in ("e", "edit"):
            return "edit"

        print_warning("  [Cancelled]")
        return None

    def _deploy_edit_steps(self, steps: list) -> Optional[list]:
        """Let the user edit the deploy steps interactively."""
        print_dim(
            "\n  Edit steps (one command per line, empty line to finish):"
        )
        new_steps = []
        for i, step in enumerate(steps, 1):
            try:
                edited = input(f"  [{i}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            # If user just presses Enter, keep the original
            if not edited:
                # But if they entered nothing on a *new* slot, stop
                if i > len(steps):
                    break
                new_steps.append(step)
            else:
                new_steps.append(edited)

        # Allow adding extra steps
        extra_idx = len(steps) + 1
        while True:
            try:
                extra = input(f"  [{extra_idx}] (Enter to finish): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not extra:
                break
            new_steps.append(extra)
            extra_idx += 1

        if not new_steps:
            print_warning("  No steps — cancelled.")
            return None

        print()
        return new_steps

    # -- Execution -----------------------------------------------------------

    def _deploy_execute(self, cwd: Path, steps: list, platform_name: str):
        """Execute each deploy step sequentially with progress feedback."""
        total = len(steps)
        print()
        all_ok = True

        for i, step in enumerate(steps, 1):
            print_info(f"  [{i}/{total}] {step}")
            success = self.execute_shell_command(step)

            if success:
                print_success(f"  [{i}/{total}] Done")
            else:
                print_error(f"\n  [{i}/{total}] Failed: {step}")
                if i < total:
                    try:
                        resp = input(
                            "\n  Continue with remaining steps? (y/n): "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        all_ok = False
                        break
                    if resp not in ("y", "yes"):
                        all_ok = False
                        break
                else:
                    all_ok = False

        if all_ok:
            self.deploy_store.record_deploy(cwd)
            print_success(
                f"\n[Cliara] Deploy complete! ({platform_name.title()})"
            )
        else:
            print_warning(
                "\n[Cliara] Deploy did not complete successfully."
            )

    # -- Subcommands ---------------------------------------------------------

    def _deploy_show_config(self):
        """Show saved deploy config for the current project."""
        saved = self.deploy_store.get(Path.cwd())
        if saved is None:
            print_info("[Cliara] No saved deploy config for this project.")
            print_dim("  Run 'deploy' to auto-detect and configure.")
            return

        print_info(f"\n[Cliara] Deploy config for {Path.cwd().name}:\n")
        print(f"  Platform:  {saved.platform}")
        if saved.project_name:
            print(f"  Project:   {saved.project_name}")
        if saved.framework:
            print(f"  Framework: {saved.framework}")
        print(f"  Deploys:   {saved.deploy_count}")
        if saved.last_deployed:
            print(f"  Last:      {saved.last_deployed}")
        print()
        print_dim("  Steps:")
        for i, step in enumerate(saved.steps, 1):
            print(f"    {i}. {step}")
        print()

    def _deploy_show_history(self):
        """Show all saved deploy configs across projects."""
        all_configs = self.deploy_store.list_all()
        if not all_configs:
            print_info("[Cliara] No deploy history yet.")
            return

        print_info(f"\n[Cliara] Deploy history ({len(all_configs)} project(s)):\n")
        for path, saved in all_configs.items():
            deploys = f"{saved.deploy_count} deploy(s)" if saved.deploy_count else "never deployed"
            print(f"  {path}")
            print_dim(f"    {saved.platform.title()} — {deploys}")
            if saved.last_deployed:
                print_dim(f"    Last: {saved.last_deployed}")
            print()

    def _deploy_reset(self):
        """Forget saved deploy config for the current project."""
        cwd = Path.cwd()
        saved = self.deploy_store.get(cwd)
        if saved is None:
            print_info("[Cliara] No saved deploy config for this project.")
            return

        self.deploy_store.remove(cwd)
        print_success(
            f"[Cliara] Deploy config for '{cwd.name}' cleared. "
            "Next 'deploy' will re-detect."
        )

    def _deploy_help(self):
        """Show deploy subcommand help."""
        print_info("\n[Cliara] Deploy Commands\n")
        print("  deploy               Auto-detect and deploy this project")
        print("  deploy config        Show saved deploy config")
        print("  deploy history       Show deploy history across all projects")
        print("  deploy reset         Forget saved config and re-detect")
        print("  deploy help          Show this help")
        print()
        print_dim("  First run: Cliara detects your project type and proposes a plan.")
        print_dim("  After confirming, the plan is saved — next time it's instant.")
        print()

    # ------------------------------------------------------------------
    # Macro conflict detection
    # ------------------------------------------------------------------

    # Built-in names that a macro would shadow
    _BUILTIN_NAMES = frozenset({
        "exit", "quit", "q", "help", "version", "explain", "push", "session", "deploy",
        "macro", "cd", "clear", "cls", "fix", "config", "theme", "setup-ollama",
    })

    def _check_macro_name_conflict(self, name: str) -> bool:
        """
        Warn if a macro name would shadow a system command or Cliara
        built-in.  Returns True if it's OK to proceed, False if the
        user declined.
        """
        reason = None

        if name.lower() in self._BUILTIN_NAMES:
            reason = f"'{name}' is a Cliara built-in command"
        elif command_exists(name):
            reason = f"'{name}' is a system command on this machine"

        if reason is None:
            return True  # no conflict

        print_warning(f"\n[Warning] {reason}.")
        print_dim(
            "  Creating a macro with this name will shadow it — "
            "the original command\n  won't be reachable by name."
        )
        try:
            confirm = input("  Create macro anyway? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        return confirm in ("y", "yes")

    def _stream_callback_for_console(self):
        """Return a callable that prints each streamed LLM chunk to the console and flushes stdout."""
        def callback(chunk: str) -> None:
            _cliara_console().print(chunk, end="")
            sys.stdout.flush()
        return callback

    def handle_history(self, arg: str = ""):
        """
        Show recent command history. Usage: history [N]
        Default: last 20 commands.
        """
        default_n = 20
        max_n = min(500, self.config.get("history_size", 1000))
        n = default_n
        if arg:
            try:
                n = int(arg.strip())
                n = max(1, min(n, max_n))
            except ValueError:
                print_error("[Error] history expects an optional number")
                print_dim("Usage: history   or   history 10")
                return
        commands = self.history.get_recent(n)
        if not commands:
            print_dim("No command history yet.")
            return
        print_info(f"\nLast {len(commands)} command(s):\n")
        for i, cmd in enumerate(reversed(commands), 1):
            print(f"  {i:4}  {cmd}")
        print()

    def handle_explain(self, command: str):
        """
        Explain a shell command in plain English using the LLM.

        Args:
            command: The shell command to explain (e.g. "git rebase -i HEAD~3")
        """
        if not command:
            print_error("[Error] Please provide a command to explain")
            print_dim("Usage: explain <command>")
            print_dim("Example: explain git rebase -i HEAD~3")
            return

        print_info(f"\n[Explain] {command}")
        print_dim("Analyzing command...\n")

        # Build context
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }

        stream_cb = self._stream_callback_for_console() if self.config.get("stream_llm", True) else None
        explanation = self.nl_handler.explain_command(command, context, stream_callback=stream_cb)

        # Display the explanation with a nice header/footer (skip body when streamed — already shown)
        print_header("-" * 60)
        if stream_cb is None:
            print(explanation)
        else:
            print()  # newline after streamed output
        print_header("-" * 60)

        # Cache for semantic history: if user runs this command next, use explanation as summary
        one_line = (explanation or "").strip().split("\n")[0].strip()
        if len(one_line) > 150:
            one_line = one_line[:147] + "..."
        self._last_explained_command = command.strip()
        self._last_explained_summary = one_line if one_line else None
        if self._semantic_history and one_line:
            self._semantic_history.update_summary_for_command(
                command.strip(), one_line, str(Path.cwd())
            )

        # Offer to run the command
        print()
        run = input("Run this command? (y/n): ").strip().lower()
        if run in ['y', 'yes']:
            # Safety check first
            level, dangerous = self.safety.check_commands([command])
            if level != DangerLevel.SAFE:
                print(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
                prompt = self.safety.get_confirmation_prompt(level)
                response = input(prompt).strip()
                if not self.safety.validate_confirmation(response, level):
                    print_warning("[Cancelled]")
                    return

            print()
            self.execute_shell_command(command, capture=False)

    def show_help(self):
        """Show main help message."""
        nl = self.config.get('nl_prefix', '?')

        print_header("\n" + "=" * 60)
        print_info("  Cliara Help")
        print_header("=" * 60)

        print_info("\n  Normal Commands")
        print_dim("  ─────────────────────────────────────")
        print("  Just type any command — it passes through to your shell")
        print("  Examples: ls, cd, git status, npm install\n")

        print_info("  Natural Language")
        print_dim("  ─────────────────────────────────────")
        if self.nl_handler.llm_enabled:
            print(f"  {nl} <query>                Use natural language")
        else:
            print(f"  {nl} <query>                Use natural language (requires API key)")
        print(f"  {nl} <query> --save-as <n>  Generate & save as macro")
        print_dim(f"  Example: {nl} kill process on port 3000\n")

        print_info("  Explain")
        print_dim("  ─────────────────────────────────────")
        print("  explain <command>          Plain-English explanation of any command")
        print_dim("  Example: explain git rebase -i HEAD~3\n")

        print_info("  Semantic History Search")
        print_dim("  ─────────────────────────────────────")
        print(f"  {nl} find <what>             Search past commands by meaning")
        print(f"  {nl} when did I ...          e.g. when did I fix the login bug")
        print(f"  {nl} what did I run ...      e.g. what did I run to deploy last time")
        print_dim("  Requires LLM; uses stored summaries of your commands.\n")

        print_info("  Macros")
        print_dim("  ─────────────────────────────────────")
        print("  macro add <name>           Create a macro")
        print("  macro add <name> --nl      Create macro from plain English")
        print("  macro edit <name>          Edit an existing macro")
        print("  macro list                 List all macros")
        print("  macro search <word>        Search macros")
        print("  <macro-name>               Run a saved macro\n")

        print_info("  Quick Fix")
        print_dim("  ─────────────────────────────────────")
        print("  When a command fails, Cliara automatically shows a fix hint:")
        print_dim("    hint: try 'python3 script.py' (Tab to use)")
        print("  Press Tab on an empty prompt to fill in the fix, then Enter.")
        print(f"  {nl} fix                    Full interactive diagnosis\n")

        print_info("  Smart Push")
        print_dim("  ─────────────────────────────────────")
        print("  push                       Stage, auto-commit, and push")
        print("  Detects branch, generates a conventional commit message")
        print("  (feat:, fix:, docs:, …) from the diff. Accept, edit, or cancel.\n")

        print_info("  Task Sessions")
        print_dim("  ─────────────────────────────────────")
        print("  session start <name> [ -- <intent>]   Start a task (name can be multi-word)")
        print("  session resume <name>            Resume and see summary + next step")
        print("  session end [note]               End current session")
        print("  session list / show / note        List, show, or add notes")
        print_dim("  Sessions persist across terminal closes — resume anytime.\n")

        print_info("  Smart Deploy")
        print_dim("  ─────────────────────────────────────")
        print("  deploy                     Auto-detect project and deploy")
        print("  deploy config              Show saved deploy config")
        print("  deploy history             Show deploy history")
        print("  deploy reset               Re-detect deploy target")
        print("  Detects Vercel, Netlify, Fly.io, Docker, npm, PyPI, and more.")
        print("  Remembers your config — second deploy is just 'deploy' + 'y'.\n")

        print_info("  Theme")
        print_dim("  ─────────────────────────────────────")
        print("  theme                      List color themes and show current")
        print("  theme <name>               Set theme (monokai, dracula, nord, solarized, catppuccin, light)")
        print("  Stored in ~/.cliara/config.json — applies immediately.\n")

        print_info("  Diff Preview")
        print_dim("  ─────────────────────────────────────")
        print("  Destructive commands (rm, git checkout, git clean,")
        print("  git reset) show exactly what will be affected first.")
        print_dim("  Example: rm *.log → shows each file and total size\n")

        print_info("  Cross-Platform Translation")
        print_dim("  ─────────────────────────────────────")
        print("  If a command doesn't exist on your OS, Cliara suggests")
        print("  the equivalent automatically.")
        print_dim("  Example: grep on Windows → Select-String (PowerShell)\n")

        print_info("  Other")
        print_dim("  ─────────────────────────────────────")
        print("  help                       Show this help")
        print("  history [N]                Show last N commands (default 20)")
        print(f"  {nl} find / when did I ...   Search history by meaning (semantic)")
        print("  version                    Show Cliara version")
        print("  exit / Ctrl+C              Quit Cliara")

        print_header("\n" + "=" * 60 + "\n")

"""
Shell wrapper/proxy for Cliara.
Handles command pass-through, NL routing, and macro execution.
"""

import subprocess
import sys
import os
import platform
import threading
import time
from contextlib import contextmanager
from typing import Optional, List, Tuple, Union
from pathlib import Path

from cliara.config import Config
from cliara.macros import MacroManager
from cliara.safety import SafetyChecker, DangerLevel
from cliara.nl_handler import NLHandler
from cliara.diff_preview import DiffPreview
from cliara.deploy_detector import detect_all as detect_deploy_targets, DeployPlan
from cliara.deploy_store import DeployStore
from cliara.session_store import (
    SessionStore,
    TaskSession,
    _get_project_root,
    _get_branch,
)
from cliara.cross_platform import (
    get_base_command,
    command_exists,
    is_powershell,
    translate_command,
    translate_pipeline,
)


# ---------------------------------------------------------------------------
# Colorized output helpers
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Check if the terminal supports ANSI colors."""
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
    """Wrap *text* with an ANSI escape if colors are enabled."""
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def print_success(msg: str):
    """Print a green success message."""
    print(_c("32", msg))


def print_error(msg: str, **kw):
    """Print a red error message."""
    print(_c("31", msg), **kw)


def print_warning(msg: str):
    """Print a yellow warning message."""
    print(_c("33", msg))


def print_info(msg: str):
    """Print a cyan informational message."""
    print(_c("36", msg))


def print_header(msg: str):
    """Print a bold header message."""
    print(_c("1", msg))


def print_dim(msg: str):
    """Print a dimmed/muted message."""
    print(_c("2", msg))


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
        self.nl_handler = NLHandler(self.safety)

        progress.step("Loading history...")
        history_file = self.config.config_dir / "history.txt"
        self.history = CommandHistory(
            max_size=self.config.get("history_size", 1000),
            history_file=history_file,
        )

        self.running = True
        self.shell_path = self.config.get("shell")

        # Deploy store — persisted per-project deploy configs
        self.deploy_store = DeployStore()

        # Task sessions — named, resumable workflow context
        sessions_path = self.config.config_dir / "sessions.json"
        self.session_store = SessionStore(store_path=sessions_path)
        self.current_session: Optional[TaskSession] = None

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
            if self.nl_handler.initialize_llm(provider, api_key):
                if not quiet:
                    print_success(f"[OK] LLM initialized ({provider})")
            else:
                if not quiet:
                    print_warning(f"[Warning] Failed to initialize LLM ({provider})")
        else:
            # LLM not configured, will use stub responses
            pass
    
    def print_banner(self):
        """Print welcome banner."""
        print_header("\n" + "="*60)
        print_info("  Cliara - AI-Powered Shell")
        print(f"  Shell: {self.shell_path}")
        if self.nl_handler.llm_enabled:
            print_success(f"  LLM: {self.nl_handler.provider.upper()} (Ready)")
        else:
            print_dim("  LLM: Not configured (set OPENAI_API_KEY in .env)")
        print_header("="*60)
        nl = self.config.get('nl_prefix', '?')
        print("\nQuick tips:")
        if self.nl_handler.llm_enabled:
            print_dim(f"  • {nl} <query>             Ask in plain English  (e.g. {nl} list large files)")
        else:
            print_dim(f"  • {nl} <query>             Ask in plain English  (requires API key)")
        print_dim(f"  • {nl} fix                 Diagnose & fix the last failed command")
        print_dim(f"  • session start <name>   Start a task session")
        print_dim(f"  • session end [note]     End session — session help for more")
        print_dim(f"  • session help           More session commands (notes, list, show)")
        print_dim(f"  • push                    Smart git push — auto-commit message & branch")
        print_dim(f"  • explain <cmd>           Understand any command  (e.g. explain git rebase)")
        print_dim(f"  • macro add <name>        Create a reusable macro")
        print_dim(f"  • macro add <name> --nl   Create a macro from plain English")
        print_dim(f"  • <macro-name>            Run a saved macro")
        print_dim(f"  • help                    Show all commands")
        print_dim(f"  • exit                    Quit Cliara")
        print()
    
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

    def _create_prompt_session(self):
        """
        Build a prompt_toolkit PromptSession with syntax highlighting.

        Returns the session, or *None* if prompt_toolkit / pygments are
        unavailable (falls back to plain ``input()``).
        """
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.lexers import PygmentsLexer
            from prompt_toolkit.styles import merge_styles, Style as PTStyle
            from prompt_toolkit.styles.pygments import style_from_pygments_cls
            from cliara.highlighting import ShellLexer, CliaraStyle, PROMPT_STYLE

            # ── Custom key bindings ──
            kb = KeyBindings()

            @kb.add("c-v", eager=True)
            def _paste(event):
                """Paste from the system clipboard (Ctrl+V)."""
                try:
                    text = self._read_system_clipboard()
                    if text:
                        event.current_buffer.insert_text(text)
                except Exception:
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

            style = merge_styles([
                style_from_pygments_cls(CliaraStyle),
                PTStyle.from_dict(PROMPT_STYLE),
            ])

            return PromptSession(
                lexer=PygmentsLexer(ShellLexer),
                style=style,
                history=pt_history,
                key_bindings=kb,
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
        session = self._create_prompt_session()
        self._prompt_session = session  # store for _auto_suggest_fix
        if session is None:
            # prompt_toolkit unavailable — use readline instead
            self.history.setup_readline()

        # Use safe prompt character for Windows
        prompt_arrow = ">" if platform.system() == "Windows" else ">"

        while self.running:
            try:
                cwd = str(Path.cwd())

                if session is not None:
                    # Coloured, syntax-highlighted prompt
                    message = [
                        ("class:prompt-name", "cliara"),
                        ("class:prompt-sep", ":"),
                    ]
                    if self.current_session:
                        message.append(("class:prompt-path", f"[{self.current_session.name}]"))
                        message.append(("class:prompt-sep", ":"))
                    message.extend([
                        ("class:prompt-path", cwd),
                        ("", " "),
                        ("class:prompt-arrow", f"{prompt_arrow} "),
                    ])
                    user_input = session.prompt(message).strip()
                else:
                    # Plain fallback
                    if self.current_session:
                        prompt = f"cliara [{self.current_session.name}]:{cwd} {prompt_arrow} "
                    else:
                        prompt = f"cliara:{cwd} {prompt_arrow} "
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
    
    def handle_input(self, user_input: str):
        """
        Route user input to appropriate handler.
        
        Args:
            user_input: Raw user input
        """
        # Any new input clears a pending fix suggestion
        self._pending_fix = None

        # Check for exit commands
        if user_input.lower() in ['exit', 'quit', 'q']:
            print("Goodbye!")
            self.running = False
            return
        
        # Check for help
        if user_input.lower() in ['help', '?help']:
            self.show_help()
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
            self.handle_nl_query(user_input[len(nl_prefix):].strip())
            return
        
        # Check for macro commands
        if user_input.startswith('macro '):
            self.handle_macro_command(user_input[6:].strip())
            return
        
        # Check if it's a macro name
        if self.macros.exists(user_input):
            self.run_macro(user_input)
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
            return

        # Diff preview: show exactly what destructive commands will affect
        if self.config.get("diff_preview", True) and self.diff_preview.should_preview(user_input):
            if not self._confirm_with_preview(user_input):
                return

        # Default: pass through to underlying shell
        success = self.execute_shell_command(user_input)
        if not success:
            # If the executable doesn't exist, try cross-platform
            # translation (it returns early when the command *is* found).
            self._check_cross_platform(user_input)

            # Auto-suggest a fix right below the error
            self._auto_suggest_fix()
    
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
        
        # Process with LLM
        commands, explanation, danger_level = self.nl_handler.process_query(query, context)
        
        if not commands:
            print_error(f"[Error] {explanation}")
            return
        
        # Show generated commands
        print_info(f"[Explanation] {explanation}\n")
        print("Generated commands:")
        for i, cmd in enumerate(commands, 1):
            print(f"  {i}. {cmd}")
        
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
        
        # Safety check
        if danger_level != DangerLevel.SAFE:
            print(self.safety.get_warning_message(commands, danger_level))
            prompt = self.safety.get_confirmation_prompt(danger_level)
            response = input(prompt).strip()
            if not self.safety.validate_confirmation(response, danger_level):
                print_warning("[Cancelled]")
                return
        else:
            confirm = input("\nRun these commands? (y/n): ").strip().lower()
            if confirm not in ['y', 'yes']:
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

    def handle_macro_command(self, args: str):
        """
        Handle macro subcommands.
        
        Args:
            args: Command arguments after 'macro '
        """
        parts = args.split(maxsplit=1)
        if not parts:
            print("Usage: macro <command> [args]")
            print("Commands: add, edit, list, search, show, run, delete, rename, save, help")
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
        elif cmd == 'search':
            self.macro_search(args_rest)
        elif cmd == 'show':
            self.macro_show(args_rest)
        elif cmd == 'run':
            self.run_macro(args_rest)
        elif cmd == 'edit':
            self.macro_edit(args_rest)
        elif cmd == 'delete':
            self.macro_delete(args_rest)
        elif cmd == 'rename':
            self.macro_rename(args_rest)
        elif cmd == 'save':
            self.macro_save_last(args_rest)
        elif cmd == 'help':
            self.macro_help()
        else:
            print_error(f"Unknown macro command: {cmd}")
            print_dim("Type 'macro help' for available commands")
    
    def macro_add(self, name: str):
        """Create a new macro interactively."""
        if not name:
            name = input("Macro name: ").strip()
            if not name:
                print_error("[Error] Macro name required")
                return
        
        print_info(f"\nCreating macro '{name}'")
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
        self.macros.add(name, commands, description)
        print_success(f"\n[OK] Macro '{name}' created with {len(commands)} command(s)")
    
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
    
    def macro_list(self):
        """List all macros."""
        macros = self.macros.list_all()
        
        if not macros:
            print_dim("\nNo macros yet.")
            print_dim("Create one with: macro add <name>")
            return
        
        print_info(f"\n[Macros] {len(macros)} total\n")
        for name, macro in sorted(macros.items()):
            desc = macro.description or "No description"
            cmd_count = len(macro.commands)
            print(f"  • {name}")
            print(f"    {desc} ({cmd_count} command{'s' if cmd_count != 1 else ''})")
            if macro.run_count > 0:
                print(f"    Run {macro.run_count} time{'s' if macro.run_count != 1 else ''}")
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
        
        print_info(f"\n[Search: '{keyword.strip()}'] {len(results)} result(s)\n")
        for macro in sorted(results, key=lambda m: m.name):
            desc = macro.description or "No description"
            cmd_count = len(macro.commands)
            print(f"  • {macro.name}")
            print(f"    {desc} ({cmd_count} command{'s' if cmd_count != 1 else ''})")
            if macro.run_count > 0:
                print(f"    Run {macro.run_count} time{'s' if macro.run_count != 1 else ''}")
        print()
    
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

        # Safety check on the (possibly new) commands
        level, dangerous = self.safety.check_commands(commands)
        if level in [DangerLevel.DANGEROUS, DangerLevel.CRITICAL]:
            print_warning(self.safety.get_warning_message([cmd for cmd, _ in dangerous], level))
            confirm = input("\nSave anyway? (yes/no): ").strip().lower()
            if confirm not in ['yes', 'y']:
                print_warning("[Cancelled]")
                return

        self.macros.add(name, commands, description)
        print_success(f"\n[OK] Macro '{name}' updated with {len(commands)} command(s)")

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
        print("  macro add <name>          Create a new macro")
        print("  macro add <name> --nl      Create macro from natural language")
        print("  macro edit <name>         Edit an existing macro")
        print("  macro list                List all macros")
        print("  macro search <keyword>    Search macros by name, description, or tags")
        print("  macro show <name>         Show macro details")
        print("  macro run <name>          Run a macro")
        print("  macro delete <name>       Delete a macro")
        print("  macro rename <old> <new>  Rename a macro")
        print("  macro save last as <name> Save last commands as macro")
        print("\nYou can also run macros by just typing their name:")
        print("  cliara > my-macro\n")
    
    def run_macro(self, name: str):
        """Execute a macro."""
        macro = self.macros.get(name)
        if not macro:
            print_error(f"[Error] Macro '{name}' not found")
            return
        
        # Show preview
        print_info(f"\n[Macro] {name}")
        if macro.description:
            print(f"{macro.description}\n")
        print("Commands:")
        for i, cmd in enumerate(macro.commands, 1):
            print(f"  {i}. {cmd}")
        
        # Safety check
        level, dangerous = self.safety.check_commands(macro.commands)
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
        
        # Execute
        print_header("\n" + "="*60)
        print_header(f"EXECUTING: {name}")
        print_header("="*60 + "\n")
        
        for i, cmd in enumerate(macro.commands, 1):
            print_info(f"[{i}/{len(macro.commands)}] {cmd}")
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
            # Save updated macro back to storage
            self.macros.storage.add(macro, user_id=self.macros.user_id)
        
        # Save to history for "save last"
        self.history.set_last_execution(macro.commands)
    
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

        result = self.nl_handler.translate_error(
            command,
            self.last_exit_code,
            stderr,
            context,
        )

        explanation = result.get("explanation", "")
        fix_commands = result.get("fix_commands", [])
        fix_explanation = result.get("fix_explanation", "")

        # ── Display explanation ──
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

            if capture:
                # ── Capture mode: both stdout and stderr captured ──
                if spinner_delay > 0:
                    timer: Union[_LiveTimer, _NullTimer] = _LiveTimer(
                        command, delay=spinner_delay, inline=True,
                    )
                else:
                    timer = _NullTimer()
                timer.start()
                try:
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
                self._notify_completion(command, time.time() - start_time, success)
                self._session_record_command(command, success)
                return success
            else:
                # ── Streaming mode: stdout AND stderr piped ──
                # Both streams are relayed to the terminal by background
                # threads that coordinate with the inline spinner via
                # output_lock().  Piping stdout (instead of inheriting
                # it) means the spinner and command output never fight
                # over the same cursor.
                if spinner_delay > 0:
                    timer = _LiveTimer(
                        command, delay=spinner_delay, inline=True,
                    )
                else:
                    timer = _NullTimer()

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
                self._notify_completion(command, time.time() - start_time, success)
                self._session_record_command(command, success)
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
        self.session_store.add_command(
            self.current_session.id,
            command=command,
            cwd=cwd,
            exit_code=0 if success else (self.last_exit_code if self.last_exit_code != 0 else 1),
            branch=branch,
            project_root=root,
        )
        # Refresh in-memory session so prompt and list stay in sync
        updated = self.session_store.get_by_id(self.current_session.id)
        if updated:
            self.current_session = updated

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
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print_error("[Cliara] Not inside a git repository.")
            return

        # ── 2. Current branch ──
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
        )
        branch = result.stdout.strip()
        if not branch:
            print_error("[Cliara] Detached HEAD state — checkout a branch first.")
            return

        # ── 3. Anything to commit? ──
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
        )
        status_output = result.stdout.strip()

        if not status_output:
            # Nothing to commit — maybe there are unpushed commits?
            result = subprocess.run(
                ["git", "log", f"origin/{branch}..HEAD", "--oneline"],
                capture_output=True, text=True,
            )
            unpushed = result.stdout.strip()
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
        subprocess.run(["git", "add", "-A"], capture_output=True)

        # ── 6. Gather diff for message generation ──
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True,
        )
        diff_stat = result.stdout.strip()

        result = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True, text=True,
        )
        diff_content = result.stdout.strip()

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True,
        )
        files = [f for f in result.stdout.strip().splitlines() if f]

        # ── 7. Generate commit message ──
        print_dim("Generating commit message...\n")

        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
            "branch": branch,
        }
        commit_msg = self.nl_handler.generate_commit_message(
            diff_stat, diff_content, files, context
        )

        # ── 8. Show message and confirm ──
        print_info("[Cliara] Commit message:")
        print(f"\n  {commit_msg}\n")
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
            custom = input("Enter commit message: ").strip()
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
            capture_output=True, text=True,
        )
        if result.stdout.strip():
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
        print("  session note <text>            Add a note to the current session")
        print("  session help                   Show this help")
        print_dim("\n  Sessions are keyed by name + project (git root). Close the terminal")
        print_dim("  and run 'session resume <name>' later to continue.\n")

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
        commands = self.nl_handler.generate_commands_from_nl(description, context)

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
        "exit", "quit", "q", "help", "explain", "push", "session", "deploy",
        "macro", "cd", "clear", "cls", "fix",
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

        explanation = self.nl_handler.explain_command(command, context)

        # Display the explanation with a nice header/footer
        print_header("-" * 60)
        print(explanation)
        print_header("-" * 60)

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
        print("  exit / Ctrl+C              Quit Cliara")

        print_header("\n" + "=" * 60 + "\n")

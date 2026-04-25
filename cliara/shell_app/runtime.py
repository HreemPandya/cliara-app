"""Shared runtime helpers for Cliara shell orchestration."""

import os
import platform
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cliara import icons
from cliara.file_lock import with_file_lock
from cliara.safety import DangerLevel

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
    prefixes = [f"[{icons.FAIL}]", "[Cliara]", "[X]"]
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
    """Print a neutral informational message using the active Cliara theme."""
    from cliara.console import get_ui_theme
    from cliara.highlighting import get_ui_info_style

    # Brackets like [Cliara] are labels, not Rich markup  -  markup would split styling
    # (e.g. quoted 'main' picking up stray tags) and fight the single ui_info color.
    _cliara_console().print(
        msg,
        style=get_ui_info_style(get_ui_theme()),
        markup=False,
        highlight=False,
    )


def print_header(msg: str):
    """Print a bold header message; render ASCII rulers as Rich rules."""
    text = str(msg)
    m = re.fullmatch(r"(\n*)([=-])\2{2,}(\n*)", text)
    if m:
        from rich.rule import Rule

        lead, char, tail = m.groups()
        console = _cliara_console()
        if lead:
            console.print("\n" * lead.count("\n"), end="")
        style = _ui_accent_style() if char == "=" else "dim"
        console.print(Rule(style=style))
        if tail:
            console.print("\n" * tail.count("\n"), end="")
        return

    _cliara_console().print(msg, style="bold")


def print_dim(msg: str):
    """Print a dimmed/muted message."""
    _cliara_console().print(msg, style="dim")


def _ui_accent_style() -> str:
    """Rich style for the active theme accent (same as ``print_info``); reflects theme switches."""
    from cliara.console import get_ui_theme
    from cliara.highlighting import get_ui_info_style

    return get_ui_info_style(get_ui_theme())


def _rich_help_with_placeholders(
    text: str, base_style: str, placeholder_style: str
) -> "Text":
    """Split *text* on ``<...>`` tokens: *base_style* outside, *placeholder_style* inside."""
    from rich.text import Text

    if "<" not in text or ">" not in text:
        return Text(text, style=base_style)
    out = Text()
    pos = 0
    for m in re.finditer(r"<[^>]+>", text):
        if m.start() > pos:
            out.append(text[pos:m.start()], style=base_style)
        out.append(m.group(), style=placeholder_style)
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:], style=base_style)
    return out


def print_help_example(body: str, *, label: str = "Example") -> None:
    """Print a help example: dim label, wide gap, bold cyan body (stands out from command rows)."""
    from rich.text import Text

    accent = _ui_accent_style()
    gap = max(4, 14 - len(label))
    line = Text("  ")
    line.append(label, style="dim italic")
    line.append(" " * gap)
    line.append_text(_rich_help_with_placeholders(body, "bold cyan", accent))
    _cliara_console().print(line)


def print_help_cmd(command: str, description: str = "", *, pad_to: int = 34) -> None:
    """Help reference row: bold white command; ``<placeholders>`` use the theme accent."""
    from rich.text import Text

    accent = _ui_accent_style()
    line = Text("  ")
    line.append_text(_rich_help_with_placeholders(command, "bold white", accent))
    if description:
        gap = max(2, pad_to - len(command))
        line.append(" " * gap)
        line.append_text(_rich_help_with_placeholders(description, "dim", accent))
    _cliara_console().print(line)


def _print_safety_panel(safety, commands, level):
    """Render safety warning as a Rich Panel (CRITICAL=red, DANGEROUS=orange, CAUTION=yellow)."""
    from rich.panel import Panel

    data = safety.get_warning_panel_data(commands, level)
    if not data:
        return
    lvl, title, desc, prompt = data
    reason = commands[0] if commands else ""
    body = f"{desc}\n  Reason: {reason}" if reason else desc
    body += "\n\nCommands:\n"
    for cmd in commands:
        body += f"  * {cmd}\n"
    body += f"\n{prompt}"
    if lvl == DangerLevel.CRITICAL:
        border_style = "bold red"
        title_str = f"{icons.DANGER}  {title}"
    elif lvl == DangerLevel.DANGEROUS:
        border_style = "bold orange1"
        title_str = f"{icons.WARN}  {title}"
    else:
        border_style = "bold yellow"
        title_str = f"{icons.WARN}  {title}"
    _cliara_console().print(
        Panel(body, title=title_str, border_style=border_style, padding=(0, 1))
    )


def _fmt_path(cwd: str, max_segments: int = 3) -> str:
    """
    Format the current working directory for the prompt.

    - Compresses the home directory to "~".
    - Shows only the last 2 - 3 segments with an ellipsis when the path is deep.
    """
    home = str(Path.home())
    p = cwd.replace(home, "~")
    parts = Path(p).parts
    # Only apply the smart truncation when we're under home (starts with "~")
    if parts and parts[0] == "~" and len(parts) > max_segments:
        # Keep "~", insert an ellipsis, then the last (max_segments - 1) parts
        tail = "/".join(parts[-(max_segments - 1):]) if max_segments > 1 else ""
        return "~/.../" + tail if tail else "~"
    return p


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

    Catches: fox, fxi, fiz, fux, fi, fixe, etc.  -  without ever prompting
    the user.  Only considers single short words so normal NL queries
    like 'fix the deploy script' are NOT caught.
    """
    word = query.strip().lower()
    if word == "fix":
        return True
    # Multi-word  ->  real NL query, not a typo
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


def _is_explain_last_rest(rest: str) -> bool:
    """True if ``explain `` + *rest* is exactly ``explain last`` (case-insensitive)."""
    return rest.strip().lower() == "last"


def _nl_query_plain_history_arg(query: str) -> Optional[str]:
    """
    If *query* is the built-in ``history [N]`` form (after the NL prefix), return
    the argument for :meth:`CliaraShell.handle_history` (``\"\"`` or a numeric
    string). Otherwise return None.

    Keeps ``? history 20`` as a plain list, not semantic search or LLM routing.
    """
    q = query.strip()
    if not q:
        return None
    low = q.lower()
    if low == "history":
        return ""
    if low.startswith("history "):
        rest = q[len("history "):].strip()
        if rest.isdigit():
            return rest
        if rest.lower() == "clear":
            return "clear"
    return None


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
    # ``history <N>`` is the plain list command; semantic forms use search/find.
    if q.startswith("history "):
        rest = q[len("history "):].strip()
        if not rest or rest.isdigit():
            return False
        if rest.startswith("search ") or rest.startswith("find "):
            return True
        return False
    return False


# ---------------------------------------------------------------------------
# Startup progress bar
# ---------------------------------------------------------------------------

class _StartupProgress:
    """
    Pip/npm-style progress bar for startup initialization.

    Renders a single updating line like:
        Initializing Cliara...  ########·············  Loading macros
    """

    BAR_WIDTH = 30  # characters in the bar

    def __init__(self, total_steps: int):
        self.total = total_steps
        self.current = 0
        self._label = ""
        self._finished = False

    # -- internal helpers ---------------------------------------------------
    def _render(self):
        """Redraw the progress line in-place, respecting terminal width."""
        frac = self.current / self.total if self.total else 1
        filled = int(frac * self.BAR_WIDTH)
        empty = self.BAR_WIDTH - filled

        # Prefer Unicode blocks for a polished bar; fall back on legacy encodings.
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        use_unicode_bar = ("utf" in enc or "65001" in enc)
        bar_filled_char = "█" if use_unicode_bar else "#"
        bar_empty_char = "░" if use_unicode_bar else "."

        bar_filled = _c("36", bar_filled_char * filled) if _COLOR else bar_filled_char * filled
        bar_empty = _c("2", bar_empty_char * empty) if _COLOR else bar_empty_char * empty
        pct = f"{int(frac * 100):>3}%"

        # Fixed-width prefix:  "  " + 30-char bar + " NNN%  " = 39 visible chars
        prefix = f"  {bar_filled}{bar_empty} {pct}  "
        prefix_visible_len = 2 + self.BAR_WIDTH + 1 + 4 + 2  # 39

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
        # Tiny pause so the user can actually see the bar move  -  without
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

    # "?"? public API "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?

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

    # "?"? internals "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?

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

                # Inline spinner  -  only in capture mode where nothing
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

    # Type for (exit_code, timestamp) per entry; None means unknown (e.g. before meta was added)
    _Meta = Tuple[Optional[int], Optional[float]]

    def __init__(self, max_size: int = 1000, history_file: Optional[Path] = None):
        self.history: List[str] = []
        self.exit_meta: List["CommandHistory._Meta"] = []  # (exit_code, timestamp) per entry
        self.max_size = max_size
        self.last_commands: List[str] = []  # Commands from last execution
        self.history_file: Optional[Path] = history_file
        self._meta_file: Optional[Path] = (
            (history_file.parent / "history_meta.json") if history_file else None
        )
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
            # readline completely unavailable  -  arrow keys won't work,
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
            with with_file_lock(self.history_file):
                with open(self.history_file, "r", encoding="utf-8") as f:
                    lines = [line.rstrip("\n") for line in f if line.strip()]
                # Keep only the last max_size entries
                self.history = lines[-self.max_size:]
        except Exception:
            # Corrupt / unreadable file  -  start fresh
            self.history = []
        self._load_meta()

    def _load_meta(self):
        """Load exit code and timestamp meta; must match length of history."""
        self.exit_meta = [(None, None)] * len(self.history)
        if not self._meta_file or not self._meta_file.exists():
            return
        try:
            import json
            with with_file_lock(self._meta_file):
                with open(self._meta_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            if not isinstance(raw, list):
                return
            # Meta is stored same order as history (oldest first); take last len(history)
            loaded = []
            for item in raw[-len(self.history):]:
                if isinstance(item, dict):
                    e, t = item.get("e"), item.get("t")
                    loaded.append((
                        int(e) if e is not None else None,
                        float(t) if t is not None else None,
                    ))
                else:
                    loaded.append((None, None))
            # Align: pad at front if we have fewer meta than history
            pad = len(self.history) - len(loaded)
            self.exit_meta = [(None, None)] * max(0, pad) + loaded
        except Exception:
            self.exit_meta = [(None, None)] * len(self.history)

    def _save_meta(self):
        """Persist exit_meta to history_meta.json (last max_size entries)."""
        if not self._meta_file or len(self.exit_meta) != len(self.history):
            return
        try:
            self._meta_file.parent.mkdir(parents=True, exist_ok=True)
            data = [{"e": e, "t": t} for e, t in self.exit_meta]
            import json
            with with_file_lock(self._meta_file):
                with open(self._meta_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
        except Exception:
            pass
    
    def _append_to_file(self, command: str):
        """Append a single command to the on-disk history file."""
        if not self.history_file:
            return
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with with_file_lock(self.history_file):
                with open(self.history_file, "a", encoding="utf-8") as f:
                    f.write(command + "\n")
        except Exception:
            pass  # Non-critical  -  don't crash the shell
    
    def _trim_file(self):
        """Trim the on-disk file to max_size lines (called occasionally)."""
        if not self.history_file or not self.history_file.exists():
            return
        try:
            with with_file_lock(self.history_file):
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
        self.exit_meta.append((None, None))
        if len(self.history) > self.max_size:
            self.history.pop(0)
            self.exit_meta.pop(0)

        # Persist to disk
        self._append_to_file(command)
        self._trim_file()
        self._save_meta()

        # Push into readline buffer so arrow-up sees it immediately
        if self._readline:
            try:
                self._readline.add_history(command)
            except Exception:
                pass

    def set_last_exit_ts(self, exit_code: int, timestamp: float):
        """Set exit code and timestamp for the most recently added command."""
        if not self.exit_meta:
            return
        self.exit_meta[-1] = (exit_code, timestamp)
        self._save_meta()

    def get_recent_with_meta(
        self, n: int
    ) -> List[Tuple[str, Optional[int], Optional[float]]]:
        """Get last n commands with (exit_code, timestamp); (None, None) if unknown."""
        commands = self.history[-n:] if n < len(self.history) else self.history.copy()
        start = len(self.history) - len(commands)
        result = []
        for i, cmd in enumerate(commands):
            idx = start + i
            meta = self.exit_meta[idx] if idx < len(self.exit_meta) else (None, None)
            result.append((cmd, meta[0], meta[1]))
        return result

    def set_last_execution(self, commands: List[str]):
        """Store commands from last execution."""
        self.last_commands = commands.copy()
    
    def get_last(self) -> List[str]:
        """Get last executed commands."""
        return self.last_commands.copy()
    
    def get_recent(self, n: int = 10) -> List[str]:
        """Get n most recent commands."""
        return self.history[-n:] if n < len(self.history) else self.history.copy()

    def clear_all(self) -> None:
        """Remove all command history from memory, disk, and readline (if active)."""
        self.history.clear()
        self.exit_meta.clear()
        self.last_commands.clear()
        if self.history_file:
            try:
                self.history_file.parent.mkdir(parents=True, exist_ok=True)
                with with_file_lock(self.history_file):
                    self.history_file.write_text("", encoding="utf-8")
            except Exception:
                pass
        if self._meta_file:
            try:
                self._meta_file.parent.mkdir(parents=True, exist_ok=True)
                with with_file_lock(self._meta_file):
                    self._meta_file.write_text("[]", encoding="utf-8")
            except Exception:
                pass
        if self._readline:
            try:
                self._readline.clear_history()
            except Exception:
                pass

    def __len__(self) -> int:
        """Number of commands in history (so len(shell.history) works)."""
        return len(self.history)

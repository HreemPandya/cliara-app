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
from typing import Optional, List, Tuple
from pathlib import Path

from cliara.config import Config
from cliara.macros import MacroManager
from cliara.safety import SafetyChecker, DangerLevel
from cliara.nl_handler import NLHandler
from cliara.diff_preview import DiffPreview
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
        """Redraw the progress line in-place."""
        frac = self.current / self.total if self.total else 1
        filled = int(frac * self.BAR_WIDTH)
        empty = self.BAR_WIDTH - filled

        bar_filled = _c("36", "#" * filled) if _COLOR else "#" * filled
        bar_empty = _c("2", "." * empty) if _COLOR else "." * empty
        pct = f"{int(frac * 100):>3}%"

        line = f"\r  [{bar_filled}{bar_empty}] {pct}  {self._label}"
        # Pad with spaces to overwrite any leftover chars from a longer label
        sys.stdout.write(f"{line:<80}")
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
        
        # Error translator state — populated by execute_shell_command()
        self.last_stderr: str = ""
        self.last_exit_code: int = 0
        self.last_command: str = ""  # Last shell command that was executed
        
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
    def _create_prompt_session(self):
        """
        Build a prompt_toolkit PromptSession with syntax highlighting.

        Returns the session, or *None* if prompt_toolkit / pygments are
        unavailable (falls back to plain ``input()``).
        """
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.lexers import PygmentsLexer
            from prompt_toolkit.styles import merge_styles, Style as PTStyle
            from prompt_toolkit.styles.pygments import style_from_pygments_cls
            from cliara.highlighting import ShellLexer, CliaraStyle, PROMPT_STYLE

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
                        ("class:prompt-path", cwd),
                        ("", " "),
                        ("class:prompt-arrow", f"{prompt_arrow} "),
                    ]
                    user_input = session.prompt(message).strip()
                else:
                    # Plain fallback
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

            # Nudge the user toward '? fix' instead of auto-diagnosing
            self._nudge_fix()
    
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
                self._nudge_fix()
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
                self._nudge_fix()
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
    def _nudge_fix(self):
        """
        After a failed command, print a short one-line hint pointing the
        user to '? fix' instead of running the full diagnosis automatically.
        """
        if not self.config.get("error_translation", True):
            return
        stderr = self.last_stderr.strip()
        if not stderr:
            return
        # Don't nudge if the executable is missing — cross-platform
        # translation already handles that case.
        base_cmd = get_base_command(self.last_command)
        if base_cmd and not command_exists(base_cmd):
            return
        nl_prefix = self.config.get("nl_prefix", "?")
        print_dim(f"\n  Tip: type '{nl_prefix} fix' to diagnose this error\n")

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
        command fails.  If the command takes longer than the configured
        ``notify_after_seconds`` threshold, a desktop notification is
        sent when it finishes.

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

        try:
            # Add to history
            self.history.add(command)
            self.history.set_last_execution([command])

            if capture:
                # ── Capture mode: both stdout and stderr captured ──
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                self.last_stderr = result.stderr or ""
                self.last_exit_code = result.returncode
                success = result.returncode == 0
                self._notify_completion(command, time.time() - start_time, success)
                return success
            else:
                # ── Streaming mode: stdout inherited, stderr piped ──
                # We pipe stderr through a background thread so it is
                # still displayed in real-time *and* buffered for the
                # Error Translator.
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )

                stderr_lines: List[str] = []

                def _drain_stderr():
                    """Read stderr line-by-line, display and buffer."""
                    try:
                        assert proc.stderr is not None
                        for line in proc.stderr:
                            stderr_lines.append(line)
                            sys.stderr.write(line)
                            sys.stderr.flush()
                    except Exception:
                        pass  # Don't crash the shell on read errors

                reader = threading.Thread(target=_drain_stderr, daemon=True)
                reader.start()

                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    reader.join(timeout=5)
                    print_error("[Error] Command timed out (5 minutes)")
                    self.last_exit_code = -1
                    self._notify_completion(command, time.time() - start_time, False)
                    return False

                # Wait for the reader thread to finish flushing
                reader.join(timeout=5)

                self.last_stderr = "".join(stderr_lines)
                self.last_exit_code = proc.returncode
                success = proc.returncode == 0
                self._notify_completion(command, time.time() - start_time, success)
                return success

        except Exception as e:
            print_error(f"[Error] {e}")
            return False
    
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
    # Macro conflict detection
    # ------------------------------------------------------------------

    # Built-in names that a macro would shadow
    _BUILTIN_NAMES = frozenset({
        "exit", "quit", "q", "help", "explain", "push",
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
        print(f"  {nl} fix                    Diagnose the last failed command")
        print("  Cliara already knows what failed, the exit code, and stderr.")
        print_dim(f"  Example: pip install fails → type '{nl} fix' → get the fix\n")

        print_info("  Smart Push")
        print_dim("  ─────────────────────────────────────")
        print("  push                       Stage, auto-commit, and push")
        print("  Detects branch, generates a conventional commit message")
        print("  (feat:, fix:, docs:, …) from the diff. Accept, edit, or cancel.\n")

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

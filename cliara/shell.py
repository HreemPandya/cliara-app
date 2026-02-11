"""
Shell wrapper/proxy for Cliara.
Handles command pass-through, NL routing, and macro execution.
"""

import subprocess
import sys
import os
import platform
from typing import Optional, List, Tuple
from pathlib import Path

from cliara.config import Config
from cliara.macros import MacroManager
from cliara.safety import SafetyChecker, DangerLevel
from cliara.nl_handler import NLHandler


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
        self.config = config or Config()
        # Pass config dict to MacroManager for storage backend selection
        config_dict = {
            "storage_backend": self.config.get("storage_backend", "json"),
            "storage_path": str(self.config.get_macros_path()),
            "macro_storage": str(self.config.get_macros_path()),
            "postgres": self.config.get("postgres", {}),
            "connection_string": self.config.get("connection_string"),
        }
        self.macros = MacroManager(config=config_dict)
        self.safety = SafetyChecker()
        self.nl_handler = NLHandler(self.safety)
        history_file = self.config.config_dir / "history.txt"
        self.history = CommandHistory(
            max_size=self.config.get("history_size", 1000),
            history_file=history_file,
        )
        self.running = True
        self.shell_path = self.config.get("shell")
        
        # Initialize LLM if API key is available
        self._initialize_llm()
        
        # First-run setup
        if self.config.is_first_run():
            self.config.setup_first_run()
    
    def _initialize_llm(self):
        """Initialize LLM if API key is configured."""
        provider = self.config.get_llm_provider()
        api_key = self.config.get_llm_api_key()
        
        if provider and api_key:
            if self.nl_handler.initialize_llm(provider, api_key):
                print_success(f"[OK] LLM initialized ({provider})")
            else:
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
        print("\nQuick tips:")
        print_dim("  • Normal commands work as usual")
        if self.nl_handler.llm_enabled:
            print_dim(f"  • Use '{self.config.get('nl_prefix')}' for natural language")
        else:
            print_dim(f"  • Use '{self.config.get('nl_prefix')}' for natural language (requires API key)")
        print_dim("  • Type 'macro help' for macro commands")
        print_dim("  • Type 'help' for all commands")
        print_dim("  • Type 'exit' to quit")
        print()
    
    def run(self):
        """Main shell loop."""
        self.print_banner()
        
        # Enable arrow-key history recall
        self.history.setup_readline()
        
        # Use safe prompt character for Windows
        prompt_arrow = ">" if platform.system() == "Windows" else "❯"
        
        while self.running:
            try:
                # Get current directory for prompt
                cwd = Path.cwd().name
                prompt = f"cliara:{cwd} {prompt_arrow} "
                
                user_input = input(prompt).strip()
                
                if not user_input:
                    continue
                
                self.handle_input(user_input)
            
            except KeyboardInterrupt:
                print("\n(Use 'exit' to quit)")
                continue
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

        # Default: pass through to underlying shell
        self.execute_shell_command(user_input)
    
    def handle_nl_query(self, query: str):
        """
        Handle natural language query using LLM.
        
        Args:
            query: Natural language query
        """
        if not query:
            print_error("[Error] Please provide a query after '?'")
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
                break
        else:
            print_header("="*60)
            print_success("[OK] All commands completed successfully")
            print_header("="*60 + "\n")
        
        # Save to history for "save last"
        self.history.set_last_execution(commands)
    
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

    def execute_shell_command(self, command: str, capture: bool = False) -> bool:
        """
        Execute a command in the underlying shell.
        
        Args:
            command: Shell command to execute
            capture: Whether to capture output (vs. stream to console)
        
        Returns:
            True if command succeeded (exit code 0)
        """
        try:
            # Add to history
            self.history.add(command)
            self.history.set_last_execution([command])
            
            # Execute with shell
            if capture:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                print(result.stdout, end='')
                if result.stderr:
                    print(result.stderr, end='', file=sys.stderr)
                return result.returncode == 0
            else:
                # Stream output directly
                result = subprocess.run(
                    command,
                    shell=True,
                    timeout=300
                )
                return result.returncode == 0
        
        except subprocess.TimeoutExpired:
            print_error("[Error] Command timed out (5 minutes)")
            return False
        except Exception as e:
            print_error(f"[Error] {e}")
            return False
    
    def show_help(self):
        """Show main help message."""
        print_info("\n[Cliara Help]\n")
        print("Normal Commands:")
        print("  Just type any command - it passes through to your shell")
        print("  Examples: ls, cd, git status, npm install\n")
        if self.nl_handler.llm_enabled:
            print("Natural Language:")
            print(f"  {self.config.get('nl_prefix')} <query>  - Use natural language")
            print(f"  Example: {self.config.get('nl_prefix')} kill process on port 3000\n")
        else:
            print("Natural Language:")
            print(f"  {self.config.get('nl_prefix')} <query>  - Use natural language (requires OPENAI_API_KEY)")
            print(f"  Example: {self.config.get('nl_prefix')} kill process on port 3000\n")
        print("Macros:")
        print("  macro add <name>    - Create a macro")
        print("  macro add <name> --nl - Create macro from natural language")
        print("  macro edit <name>   - Edit an existing macro")
        print("  macro list          - List all macros")
        print("  macro search <word> - Search macros")
        print("  macro help          - Show macro commands")
        print("  <macro-name>        - Run a macro\n")
        print("Other:")
        print("  help                - Show this help")
        print("  exit                - Quit Cliara\n")

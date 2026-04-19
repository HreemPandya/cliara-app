"""Macro command mixin for Cliara shell."""

import os
import platform
import re
import shlex
from pathlib import Path
from typing import Dict, List, Optional

from cliara.safety import DangerLevel
from cliara.translation.core import command_exists
from cliara.shell_app.runtime import (
    _cliara_console,
    _print_safety_panel,
    print_dim,
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
)


class MacroCommandMixin:
    """Macro command handlers and helpers mixed into CliaraShell."""

    @staticmethod
    def _expand_macro_alias(user_input: str) -> Optional[str]:
        """
        Map short tokens to the argument string for ``handle_macro_command`` (text after ``macro ``).

        Returns None if the line is not a macro alias (e.g. ``mkdir``).
        """
        stripped = user_input.strip()
        if not stripped:
            return None
        parts = stripped.split(maxsplit=1)
        head = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        cmd = head.lower()

        # Longer / specific tokens before short prefixes (e.g. mst before ms)
        if cmd == "mch":
            return f"chain {rest}".strip() if rest else "chain"
        if cmd == "msh":
            return f"show {rest}".strip() if rest else "show"
        if cmd == "msr":
            return f"search {rest}".strip() if rest else "search"
        if cmd == "mrn":
            return f"rename {rest}".strip() if rest else "rename"
        if cmd == "mst":
            return f"stats {rest}".strip() if rest else "stats"
        if cmd == "ms":
            return f"save last as {rest}".strip() if rest else "save last as"
        if cmd == "mr":
            return f"run {rest}".strip() if rest else "run"
        if cmd == "mc":
            return f"create {rest}".strip() if rest else "create"
        if cmd == "ml":
            return f"list {rest}".strip() if rest else "list"
        if cmd == "ma":
            return f"add {rest}".strip() if rest else "add"
        if cmd == "me":
            return f"edit {rest}".strip() if rest else "edit"
        if cmd == "md":
            return f"delete {rest}".strip() if rest else "delete"
        if cmd == "mh":
            return f"help {rest}".strip() if rest else "help"
        if cmd == "m":
            return rest.strip()
        return None

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
            args: Subcommand + rest (after ``mc`` / ``m`` / ``macro``, etc.)
        """
        parts = args.split(maxsplit=1)
        if not parts:
            print("Usage: mc, ml, ma, mr, ...   -  same as macro create, list, add, run, ...  (type mh for full list)")
            print("Optional full form: macro <command> [args]")
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
            # args_rest may be "name key=val ..."  -  run_macro handles the split
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
        elif cmd == 'create':
            self.macro_create(args_rest)
        elif cmd == 'help':
            self.macro_help()
        else:
            print_error(f"Unknown macro command: {cmd}")
            print_dim("Type mh (or macro help) for available commands")
    
    def macro_add(self, raw: str):
        """Create a new macro interactively.

        Accepts an optional ``--params name1,name2`` flag so the macro can
        declare typed placeholders.  Example::

            ma deploy-to --params env,tag
        """
        # "?"? Parse --params flag "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
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
            _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
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

    def macro_create(self, raw: str):
        """Create a macro from plain English; LLM suggests name, description, and ordered commands."""
        if not self.nl_handler.llm_enabled:
            print_error("[Error] LLM not configured. Run 'setup-llm' to configure a free AI provider.")
            return
        nl_description = (raw or "").strip()
        if not nl_description:
            print_info("\nDescribe the workflow  -  Cliara will suggest a name and shell commands:")
            try:
                nl_description = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not nl_description:
                print_error("[Error] Description required")
                return
        self._macro_from_nl_auto(nl_description)

    def _macro_from_nl_auto(self, nl_description: str) -> None:
        """LLM proposes macro name + commands + description; user confirms then saves."""
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }
        from rich.status import Status

        with Status("[dim]Designing macro...[/dim]", spinner="dots", console=_cliara_console()):
            name, commands, desc, expl = self.nl_handler.propose_macro_from_nl(nl_description, context)

        if not commands:
            print_error(f"[Error] {expl or 'Could not generate commands'}")
            return

        print(f"\nProposed macro name: {name or '(choose a name)'}")
        if desc:
            print_dim(f"Description: {desc}")
        if expl:
            print_dim(f"Notes: {expl}")
        print("\nCommands:")
        for i, cmd in enumerate(commands, 1):
            print(f"  {i}. {cmd}")

        final_name = name
        try:
            if final_name:
                ok = input(f"\nKeep macro name '{final_name}'? (y/n): ").strip().lower()
                if ok in ("n", "no"):
                    final_name = input("Macro name: ").strip()
            else:
                final_name = input("\nMacro name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print_warning("[Cancelled]")
            return

        if not final_name:
            print_error("[Error] Macro name required")
            return

        suggested = (desc or expl or "").strip()
        self._macro_nl_finalize(
            final_name, commands, suggested, nl_description, commands_already_listed=True
        )

    def _macro_nl_finalize(
        self,
        name: str,
        commands: List[str],
        suggested_description: str,
        nl_source: str,
        *,
        commands_already_listed: bool = False,
    ) -> None:
        """Optional command edit, safety check, description prompt, save."""
        if not commands_already_listed:
            print("\nCommands to save:")
            for i, cmd in enumerate(commands, 1):
                print(f"  {i}. {cmd}")
        try:
            edit = input("\nEdit commands? (y/n): ").strip().lower()
            if edit in ("y", "yes"):
                print("\nEnter commands (one per line, empty line to finish):")
                new_commands: List[str] = []
                for i, cmd in enumerate(commands, 1):
                    new_cmd = input(f"  {i}. [{cmd}] ").strip()
                    new_commands.append(new_cmd if new_cmd else cmd)
                while True:
                    extra = input("  > ").strip()
                    if not extra:
                        break
                    new_commands.append(extra)
                commands = new_commands
        except (EOFError, KeyboardInterrupt):
            print()
            print_warning("[Cancelled]")
            return

        level, dangerous = self.safety.check_commands(commands)
        if level in (DangerLevel.DANGEROUS, DangerLevel.CRITICAL):
            _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
            confirm = input("\nSave anyway? (yes/no): ").strip().lower()
            if confirm not in ("yes", "y"):
                print_warning("[Cancelled]")
                return

        default_desc = (suggested_description or "").strip() or nl_source
        try:
            desc_in = input(f"\nDescription [{default_desc}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print_warning("[Cancelled]")
            return
        description = desc_in or default_desc

        if not self._check_macro_name_conflict(name):
            print_warning("[Cancelled]")
            return
        self.macros.add(name, commands, description)
        print_success(f"\n[OK] Macro '{name}' saved with {len(commands)} command(s)")

    def macro_add_nl(self, name: Optional[str] = None):
        """Create a macro using natural language.

        ``ma --nl`` (no name) infers the macro name and commands from one description.
        ``ma <name> --nl`` keeps the given name and only generates commands from NL.
        """
        if not self.nl_handler.llm_enabled:
            print_error("[Error] LLM not configured. Run 'setup-llm' to configure a free AI provider.")
            return

        if not name:
            self.macro_create("")
            return

        print_info(f"\nCreating macro '{name}' from natural language")
        print("Describe what this macro should do:")
        try:
            nl_description = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not nl_description:
            print_error("[Error] Description required")
            return

        print_info("\n[Generating commands...]")
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }
        commands = self.nl_handler.generate_commands_from_nl(nl_description, context)

        if not commands or (len(commands) == 1 and commands[0].startswith("#")):
            print_error(f"[Error] Could not generate commands: {commands[0] if commands else 'Unknown error'}")
            return

        self._macro_nl_finalize(name, commands, "", nl_description)
    
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
            # Effective params: declared ^ auto-detected {var} patterns
            eff_params = list(macro.params) if macro.params else []
            for p in self._extract_param_names(macro.commands):
                if p not in eff_params:
                    eff_params.append(p)
            param_str = "  ".join(f"{{{p}}}" for p in eff_params) if eff_params else ""

            run_text = Text()
            if macro.run_count == 0:
                run_text.append(" - ", style="dim")
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
            print_dim("Create one with: ma <name>  (or mc from English)")
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
            print_dim("Create one with: ma <name>  (or mc from English)")
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
                    # User pressed Enter immediately  -  keep existing commands
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
            _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
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
        """Show macro help (short forms are the default; ``macro ...`` is optional)."""
        print_info("\n[Macros]\n")
        print_dim("  Default  -  short commands:")
        print("  mc [description...]                 Create: English in  ->  suggested name + shell steps")
        print("  ma <name>                           Add: type commands line by line")
        print("  ma <name> --params p1,p2            Add with {p1} placeholders in commands")
        print("  ma <name> --nl                      Add: keep name; generate steps from English")
        print("  ma --nl                             Same as mc (infer name + steps)")
        print("  ml                                  List all macros")
        print("  mst                                 Macro statistics")
        print("  msr <keyword>                       Search macros")
        print("  msh <name>                          Show macro details")
        print("  mr <name>                           Run (prompts for params if needed)")
        print("  mr <name> p1=v1 p2=v2               Run with inline parameter values")
        print("  mch <n1> <n2> [n3 ...]                Run macros in sequence")
        print_dim('        "my macro", "other macro"         -  quoted names (multi-word)')
        print_dim("        my macro, other macro             -  comma-separated (multi-word)")
        print("  me <name>                           Edit a macro")
        print("  md <name>                           Delete a macro")
        print("  mrn <old> <new>                     Rename a macro")
        print("  ms <name>                           Save last executed commands as this macro")
        print("  m <subcommand> [args]               Same as the macro word: macro <subcommand> ...")
        print("  mh                                  This help")
        print_dim("\n  Optional full word (same behavior): macro create, macro add, macro list, ...")
        print_dim("\nParameterised macros:")
        print_dim("  Use {param} placeholders in commands, e.g.  kubectl apply -n {env}")
        print_dim("  Declare: ma deploy --params env,tag")
        print_dim("  Run: type the macro name with values, e.g.  deploy env=prod tag=v1.2")
        print_dim("  Or run mr <name> and Cliara will prompt for each value.")
        print("\nRun a saved macro by typing its name (no prefix):")
        print("  cliara > my-macro")
        print("  cliara > my-macro param=value\n")
    
    def run_macro(self, name_and_args: str):
        """Execute a macro, optionally with inline parameter values.

        Accepts either:
          ... a plain macro name:                  ``deploy-to``
          ... a name followed by key=value pairs:  ``deploy-to env=prod tag=v1.2``

        If the macro declares parameters that are not supplied inline, the user
        is prompted for each missing value interactively.
        """
        # "?"? Split name from optional inline key=value args "?"?"?"?"?"?"?"?"?"?"?"?"?"?
        parts = name_and_args.split(maxsplit=1)
        name = parts[0]
        inline_str = parts[1] if len(parts) > 1 else ""

        macro = self.macros.get(name)
        if not macro:
            print_error(f"[Error] Macro '{name}' not found")
            return

        # "?"? Resolve parameter values "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        # Effective param list: declared params ^ {var} patterns in commands
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
            # No params  -  any inline tokens are ignored (pass-through)
            param_values = {}

        # "?"? Build the final commands with substituted values "?"?"?"?"?"?"?"?"?"?"?"?"?
        resolved_commands = [
            self._substitute_params(cmd, param_values)
            for cmd in macro.commands
        ]

        # "?"? Show preview "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
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

        # "?"? Safety check (on resolved commands) "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        level, dangerous = self.safety.check_commands(resolved_commands)
        if level != DangerLevel.SAFE:
            _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
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

        # "?"? Execute "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
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
        # "?"? Comma-separated "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        if ',' in args:
            return [n.strip() for n in args.split(',') if n.strip()]

        # "?"? Shell-quoted "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        if '"' in args or "'" in args:
            try:
                return shlex.split(args)
            except ValueError:
                pass  # malformed quotes  -  fall through to plain split

        # "?"? Plain split (single-word names, backward-compatible) "?"?"?"?"?"?"?"?"?"?"?"?"?"?
        return args.split()

    def macro_chain(self, args: str):
        """Run multiple macros in sequence.

        Usage:
          macro chain <name1> <name2> [name3 ...]

        Multi-word macro names are supported in two ways:
          ... Quoted:           macro chain "my name is", greet, deploy
          ... Comma-separated:  macro chain my name is, greet, deploy

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

        # "?"? Validate every macro exists before touching anything "?"?"?"?"?"?"?"?"?"?"?"?"?"?
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

        # "?"? Collect param values for every macro that needs them "?"?"?"?"?"?"?"?"?"?"?"?"?"?
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

        # "?"? Resolve placeholders in every macro "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        chain_resolved: List[List[str]] = []
        for macro, param_values in zip(macros, chain_params):
            resolved = [
                self._substitute_params(cmd, param_values)
                for cmd in macro.commands
            ]
            chain_resolved.append(resolved)

        # "?"? Show full chain preview "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
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

        # "?"? Safety-check across all resolved commands "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        all_resolved = [cmd for cmds in chain_resolved for cmd in cmds]
        level, dangerous = self.safety.check_commands(all_resolved)
        if level != DangerLevel.SAFE:
            _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
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

        # "?"? Execute each macro in sequence "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        chain_label = "  ->  ".join(m.name for m in macros)
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
            f"[OK] Chain complete  -  {len(macros)} macros, {total_cmds} commands"
        )
        print_header("=" * 60 + "\n")

    # ------------------------------------------------------------------
    # Macro conflict detection
    # ------------------------------------------------------------------

    # Built-in names that a macro would shadow
    _BUILTIN_NAMES = frozenset({
        "exit", "quit", "q", "help", "version", "status", "readme", "last", "doctor", "upgrade-cliara",
        "explain", "lint", "push", "session", "deploy",
        "macro", "cd", "clear", "cls", "fix", "config", "theme", "themes", "setup-ollama", "setup-llm",
        "cliara-login", "cliara login", "cliara-logout", "cliara logout", "use",
        # Macro CLI shortcuts (see _expand_macro_alias)
        "m", "mc", "ml", "mr", "ma", "me", "md", "ms", "mst", "msh", "msr", "mch", "mrn", "mh",
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
            "  Creating a macro with this name will shadow it  -  "
            "the original command\n  won't be reachable by name."
        )
        try:
            confirm = input("  Create macro anyway? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        return confirm in ("y", "yes")

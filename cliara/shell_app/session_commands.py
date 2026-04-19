"""Session, reflection, chat, and graph command mixin for Cliara shell."""

import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cliara import regression
from cliara.chat_export import (
    default_shell_label,
    format_last_run_bundle,
    format_session_for_chat,
)
from cliara.execution_graph import build_execution_tree, export_tree_json, render_execution_tree
from cliara.session_store import CLOSEOUT_KEYS, TaskSession, _get_branch, _get_project_root
from cliara.shell_app.runtime import (
    _cliara_console,
    print_dim,
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
)


class SessionCommandMixin:
    """Session/chat/graph command handlers."""
    def handle_session(self, subcommand: str = ""):
        """
        Task session subcommands: start, resume, end (optional --reflect), list, show, note, help.
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
        if sub == "snapshot":
            self._session_snapshot(rest)
            return
        if sub == "note":
            self._session_note(rest)
            return
        if sub in ("help", ""):
            self._session_help()
            return
        print_error(f"[Cliara] Unknown session subcommand: '{sub}'")
        print_dim("  ss <name> / session start ...     Start a task (ss = shortcut)")
        print_dim("  session resume <name>          Resume a session and show summary")
        print_dim("  se [note] / session end ...       End session (se = shortcut)")
        print_dim("  se --reflect / session end --reflect   Closeout prompts (LLM-tailored if configured)")
        print_dim("  session list                    List sessions")
        print_dim("  session show <name>             Show session summary (no resume)")
        print_dim("  session graph [name]            Show execution graph (tree)")
        print_dim("  session snapshot --chat [name]  Copy session for Copilot/Cursor chat")
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
            # Allow starting again with same name  -  we create a new session (replace)
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
            session.closeout = None
            session.closeout_prompts = None
            session.reflection = None
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
                status = "OK" if c.exit_code == 0 else "X"
                short = c.command[:60] + "..." if len(c.command) > 60 else c.command
                print(f"    {status} {short}")
        if s.notes:
            print_dim("  Notes:")
            for n in s.notes[-5:]:
                print_dim(f"    {n.text[:70]}{'...' if len(n.text) > 70 else ''}")
        if s.end_note:
            print_dim(f"  End note: {s.end_note[:70]}{'...' if len(s.end_note) > 70 else ''}")
        if s.reflection:
            print_dim("  Reflection (session_reflect):")
            for i, ent in enumerate(s.reflection, 1):
                kind = ent.get("kind", "?")
                q = ent.get("question", "")
                ql = q[:100] + "..." if len(q) > 100 else q
                print_dim(f"    [{i}] ({kind}) {ql}")
                hint = ent.get("hint")
                if hint:
                    print_dim(f"        hint: {hint[:80]}{'...' if len(hint) > 80 else ''}")
                if kind == "choice" and ent.get("selected_label"):
                    print_dim(f"         ->  {ent['selected_label']}")
                elif ent.get("answer"):
                    ans = ent["answer"]
                    for line in str(ans).split("\n")[:12]:
                        print_dim(f"        {line[:120]}{'...' if len(line) > 120 else ''}")
                    if str(ans).count("\n") > 11:
                        print_dim("        ...")
        elif s.closeout or s.closeout_prompts:
            print_dim("  Closeout:")
            _fallback = {"blocked": "Blocked", "decided": "Decided", "next": "Next"}
            for key in CLOSEOUT_KEYS:
                q = (s.closeout_prompts or {}).get(key) if s.closeout_prompts else None
                ans = (s.closeout or {}).get(key) if s.closeout else None
                if q:
                    ql = q[:120] + "..." if len(q) > 120 else q
                    print_dim(f"    Q: {ql}")
                    if ans:
                        al = ans[:200] + "..." if len(ans) > 200 else ans
                        print_dim(f"       {al}")
                    else:
                        print_dim("       (skipped)")
                elif ans:
                    short = ans[:200] + "..." if len(ans) > 200 else ans
                    print_dim(f"    {_fallback[key]}: {short}")

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
            return "Start running commands  -  they'll be recorded in this session."
        last = s.commands[-1]
        if last.exit_code != 0:
            return "Last command failed (exit %d). Re-run or debug, then continue." % last.exit_code
        return "Last command succeeded. Continue from here or add a note: session note <text>."

    def _build_session_closeout_briefing(self, s: TaskSession) -> str:
        """Compact text for LLM to tailor closeout questions."""
        lines = [
            f"Session name: {s.name}",
            f"Intent: {s.intent or '(none)'}",
            f"Git branch: {s.branch or '(none)'}",
            f"Command count: {len(s.commands)}",
            "",
        ]
        if s.commands:
            lines.append("Recent commands (newest last):")
            for c in s.commands[-25:]:
                st = "ok" if c.exit_code == 0 else f"exit {c.exit_code}"
                cmd = c.command[:160] + "..." if len(c.command) > 160 else c.command
                lines.append(f"  [{st}] {cmd}")
        if s.notes:
            lines.append("User notes:")
            for n in s.notes[-10:]:
                lines.append(f"  - {(n.text or '')[:300]}")
        return "\n".join(lines)

    def _reflect_read_choice(self, options: List[str], console) -> Tuple[Optional[int], Optional[str], str]:
        """Return (index, label, raw_input) for a choice; index None if skipped."""
        for i, opt in enumerate(options, 1):
            console.print(f"  [cyan]{i}.[/cyan] {opt}")
        print_dim("  Enter a number, part of an option, or leave empty to skip.")
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            raise
        if not line:
            return None, None, ""
        if line.isdigit():
            idx = int(line) - 1
            if 0 <= idx < len(options):
                return idx, options[idx], line
        low = line.lower()
        for i, opt in enumerate(options):
            if low in opt.lower():
                return i, opt, line
        return None, None, line

    def _reflect_read_long_text(self, console) -> str:
        print_dim("  Long answer  -  type lines; finish with a line containing only END")
        lines: List[str] = []
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                raise
            if line.strip() == "END":
                break
            lines.append(line)
        return "\n".join(lines).strip()

    def _session_run_reflect(
        self,
    ) -> Tuple[bool, Optional[List[Dict[str, Any]]]]:
        """
        Run session_reflect skill: multi-step reflection (choice / text / long_text).
        Returns (aborted, reflection_log).
        """
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.status import Status
        from rich.text import Text

        if not self.current_session:
            return (True, None)
        s = self.current_session
        briefing = self._build_session_closeout_briefing(s)
        console = _cliara_console()
        plan: List[Dict[str, Any]] = []
        with Status(
            "[dim]Running session_reflect skill...[/dim]",
            spinner="dots",
            console=console,
        ):
            plan = self.nl_handler.session_reflect_plan(briefing)
        offline = not self.nl_handler.llm_enabled

        summary_body = Text()
        summary_body.append(f"Session -o{s.name}...", style="bold")
        summary_body.append(f" · {len(s.commands)} commands")
        if s.branch:
            summary_body.append(f" · {s.branch}", style="dim")
        summary_body.append("\n")
        if s.intent:
            summary_body.append(f"Intent: {s.intent[:120]}\n", style="dim")
        if s.commands:
            summary_body.append("\nLast commands:\n", style="dim")
            for c in s.commands[-5:]:
                mark = "OK " if c.exit_code == 0 else "X "
                cmd = c.command[:76] + "..." if len(c.command) > 76 else c.command
                summary_body.append(mark, style="green" if c.exit_code == 0 else "yellow")
                summary_body.append(cmd + "\n", style="dim")

        console.print()
        console.print(Rule("[bold cyan]Session reflection[/bold cyan]", style="cyan"))
        console.print(
            Panel(
                summary_body,
                title="Context",
                border_style="dim",
                padding=(0, 1),
            )
        )
        src = "session_reflect (offline defaults)" if offline else "session_reflect skill (LLM)"
        print_dim(f"  Plan: {src}")

        log: List[Dict[str, Any]] = []
        n = len(plan)
        for si, step in enumerate(plan, 1):
            kind = step.get("kind")
            q = step.get("question", "")
            hint = step.get("hint")
            entry: Dict[str, Any] = {
                "id": step.get("id", "step_%d" % si),
                "kind": kind,
                "question": q,
            }
            if hint:
                entry["hint"] = hint
            console.print()
            console.print(f"[bold]{si}/{n}[/bold] [cyan]{kind}[/cyan]")
            console.print(f"[bold]{q}[/bold]")
            if hint:
                print_dim(f"  {hint}")
            try:
                if kind == "choice":
                    opts = step.get("options") or []
                    entry["options"] = list(opts)
                    idx, label, raw_in = self._reflect_read_choice(opts, console)
                    entry["answer"] = raw_in
                    if idx is not None and label is not None:
                        entry["selected_index"] = idx
                        entry["selected_label"] = label
                elif kind == "long_text":
                    ans = self._reflect_read_long_text(console)
                    entry["answer"] = ans
                else:
                    line = input("> ")
                    entry["answer"] = (line or "").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                print_dim("  Reflection cancelled  -  session still active.")
                return (True, None)
            log.append(entry)

        return (False, log)

    def _session_end(self, rest: str):
        """End the current session with optional note, or --reflect for reflection."""
        if not self.current_session:
            print_info("[Cliara] No active session to end.")
            return
        name = self.current_session.name
        rest_stripped = (rest or "").strip()
        if rest_stripped == "--reflect" or rest_stripped.startswith("--reflect "):
            aborted, refl = self._session_run_reflect()
            if aborted:
                return
            self.session_store.end_session(
                self.current_session.id,
                end_note=None,
                reflection=refl,
            )
            self.current_session = None
            print_success(f"[Cliara] Session '{name}' ended.")
            if refl:
                print_dim("  Reflection saved (session show / list to review).")
            return
        self.session_store.end_session(
            self.current_session.id,
            end_note=rest_stripped or None,
            closeout=None,
            closeout_prompts=None,
            reflection=None,
        )
        self.current_session = None
        print_success(f"[Cliara] Session '{name}' ended.")
        if rest_stripped:
            print_dim(
                f"  Note: {rest_stripped[:80]}{'...' if len(rest_stripped) > 80 else ''}"
            )

    def _session_list(self):
        """List all sessions, or for current project only."""
        cwd = Path.cwd()
        project_root = _get_project_root(cwd)
        sessions = self.session_store.list_by_project(project_root)
        if not sessions:
            print_info("[Cliara] No task sessions yet.")
            print_dim("  ss <name> or session start ...   to start one")
            return
        print_info(f"\n[Cliara] Task sessions ({len(sessions)}):\n")
        for s in sessions:
            status = "ended" if s.is_ended else "active"
            intent_preview = (s.intent[:40] + "...") if len(s.intent or "") > 40 else (s.intent or "")
            print(f"  {s.name}")
            print_dim(f"    {status}  -  {s.updated}  -  {intent_preview}")
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
            print_dim("  (Not resumed  -  use 'session resume %s' to continue.)" % name)

    def _session_note(self, text: str):
        """Add a note to the current session."""
        if not self.current_session:
            print_error("[Cliara] No active session. Start one with 'ss <name>' or 'session start <name>'.")
            return
        if not text:
            print_error("[Cliara] Usage: session note <text>")
            return
        self.session_store.add_note(self.current_session.id, text)
        updated = self.session_store.get_by_id(self.current_session.id)
        if updated:
            self.current_session = updated
        print_success("[Cliara] Note added.")

    def _build_chat_bundle_text(self) -> str:
        """Markdown for last shell run + cwd (for Copilot/Cursor)."""
        cwd = str(Path.cwd())
        branch = _get_branch(Path(cwd))
        reg_snap = None
        if self.config.get("chat_export_include_regression_snapshot"):
            reg_snap = regression.gather_current_snapshot(Path(cwd))
        try:
            mx = int(self.config.get("chat_export_max_stderr_chars", 12000))
        except (TypeError, ValueError):
            mx = 12000
        try:
            mxo = int(self.config.get("chat_export_max_stdout_chars", 8000))
        except (TypeError, ValueError):
            mxo = 8000
        try:
            rm = int(self.config.get("chat_export_regression_max_chars", 2000))
        except (TypeError, ValueError):
            rm = 2000
        return format_last_run_bundle(
            cwd=cwd,
            shell=default_shell_label(self.shell_path),
            os_name=platform.system(),
            branch=branch,
            last_command=self.last_command,
            last_exit_code=self.last_exit_code,
            last_stderr=self.last_stderr or "",
            last_stdout=getattr(self, "last_stdout", "") or "",
            session_name=self.current_session.name if self.current_session else None,
            session_id=self.current_session.id if self.current_session else None,
            max_stderr=mx,
            max_stdout=mxo,
            include_stdout=bool(self.config.get("chat_export_include_stdout", False)),
            regression_snapshot=reg_snap,
            regression_max_chars=rm,
        )

    def _handle_chat_command(self, rest: str):
        """chat copy | chat polish  -  Copilot/Cursor integration."""
        parts = rest.split(maxsplit=1)
        sub = (parts[0].lower() if parts else "").strip()
        if sub in ("", "help"):
            print_info("\n[Cliara] chat  -  context for Copilot or Cursor\n")
            print("  chat copy              Copy last-run markdown (cwd, command, exit, stderr) to clipboard")
            print("  chat polish            LLM-compress clipboard (needs LLM; enable chat_polish_enabled)")
            print_dim("  Tip: use `session snapshot --chat` for full session + last run.\n")
            return
        if sub == "copy":
            text = self._build_chat_bundle_text()
            if self._write_system_clipboard(text):
                print_success("[Copied to clipboard  -  paste into Copilot or Cursor chat]")
            else:
                print_error("[Cliara] Could not copy to clipboard")
            return
        if sub == "polish":
            if not self.config.get("chat_polish_enabled"):
                print_error(
                    "[Cliara] chat polish is disabled. Set chat_polish_enabled to true in config."
                )
                return
            raw = self._read_system_clipboard() or ""
            if not raw.strip():
                print_error("[Cliara] Clipboard is empty. Run chat copy (or session snapshot --chat) first.")
                return
            if not self.nl_handler.llm_enabled:
                print_error("[Cliara] LLM not configured. Run setup-llm.")
                return
            try:
                out = self.nl_handler.chat_polish_bundle(raw)
            except Exception as e:
                print_error(f"[Cliara] chat polish failed: {e}")
                return
            if self._write_system_clipboard(out):
                print_success("[Copied polished summary to clipboard]")
            else:
                print_error("[Cliara] Could not copy to clipboard")
            return
        print_error(f"[Cliara] Unknown chat subcommand: {sub!r}. Try: chat copy, chat help")

    def _session_snapshot(self, rest: str):
        """session snapshot --chat [name]  -  full session + last-run bundle for IDE chat."""
        tokens = [t for t in rest.split() if t]
        if "--chat" not in tokens:
            print_error("[Cliara] Usage: session snapshot --chat [session-name]")
            print_dim("  Copies markdown for the current or named session plus last-run context.")
            return
        name_tokens = [t for t in tokens if t != "--chat"]
        name = " ".join(name_tokens).strip() if name_tokens else None
        cwd = Path.cwd()
        project_root = _get_project_root(cwd)
        if name:
            session = self.session_store.get_by_key(name, project_root)
        else:
            session = self.current_session
        if session is None:
            print_error(
                "[Cliara] No session. Start with ss <name> or: session snapshot --chat <name>"
            )
            return
        bundle = self._build_chat_bundle_text()
        text = format_session_for_chat(session, bundle, max_commands=40)
        if self._write_system_clipboard(text):
            print_success("[Copied session snapshot to clipboard  -  paste into Copilot or Cursor]")
        else:
            print_error("[Cliara] Could not copy to clipboard")

    def _session_help(self):
        """Show session command help."""
        print_info("\n[Cliara] Task sessions  -  persistent, resumable workflow context\n")
        print("  ss <name> [ -- <intent>]       Short for session start (name can be multi-word)")
        print("  session start <name> [ -- <intent>]   Same as ss")
        print("  session resume <name>          Resume and see summary + suggested next step")
        print("  se [note]                      Short for session end (optional closing note)")
        print("  se --reflect                   Short for session end --reflect")
        print("  session end [note]             Same as se")
        print("  session end --reflect          Closeout prompts (blocked / decided / next; LLM-tailored if configured)")
        print("  session list                   List sessions for this project")
        print("  session show <name>             Show session summary without resuming")
        print("  session graph [name]            Show execution graph (tree); optional: export [file], export --json <file>")
        print("  session snapshot --chat [name]  Copy session + last-run markdown for Copilot/Cursor")
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
                print_error("[Cliara] No active session. Start one with 'ss <name>' or use 'session graph <name>'.")
                return

        if not session.commands:
            print_info(f"[Cliara] Session '{session.name}' has no commands yet.")
            return

        tree = build_execution_tree(session.commands)
        text = render_execution_tree(tree)

        if do_export or export_path is not None or export_json:
            if export_path is None:
                safe_name = session.name.replace(" ", "-")[:30]
                export_path = Path(f"cliara-graph-{safe_name}.json" if export_json else f"cliara-graph-{safe_name}.md")
            export_path = Path(export_path)
            if export_json:
                export_tree_json(session.commands, export_path)
                print_success(f"[Cliara] Graph exported to {export_path} (JSON)")
            else:
                export_path.write_text(text, encoding="utf-8")
                print_success(f"[Cliara] Graph exported to {export_path}")
        else:
            print_info(f"\n[Cliara] Execution graph  -  {session.name}\n")
            print(text)
            print()

    # ------------------------------------------------------------------
    # Smart Deploy  -  detect project type and deploy in one word
    # ------------------------------------------------------------------




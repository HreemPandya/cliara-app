"""
Shell wrapper/proxy for Cliara.
Handles command pass-through, NL routing, and macro execution.
"""

import collections
import shutil
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
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple, Union
from pathlib import Path

from cliara.config import Config
from cliara.macros import MacroManager
from cliara.safety import SafetyChecker, DangerLevel
from cliara.nl.service import NLHandler
from cliara.diff_preview import DiffPreview
from cliara.deploy_store import DeployStore
from cliara.semantic_history import SemanticHistoryStore
from cliara.session_store import (
    SessionStore,
    TaskSession,
    _get_project_root,
    _get_branch,
    CLOSEOUT_KEYS,
)
from cliara.execution_graph import (
    build_execution_tree,
    render_execution_tree,
    export_tree_json,
)
from cliara.file_lock import with_file_lock
from cliara.translation.core import (
    get_base_command,
    is_powershell,
    translate_command,
    translate_pipeline,
)
from cliara import regression
from cliara.chat_export import (
    format_last_run_bundle,
    format_session_for_chat,
    default_shell_label,
    truncate_text,
)
from cliara.copilot_gate import (
    SourceDetector,
    InputSource,
    RiskEngine,
    CopilotGate,
)
from cliara.shell_app.jump import JumpDirectoryStore
from cliara import icons


from cliara.shell_app.session_commands import SessionCommandMixin
from cliara.shell_app.deploy_commands import DeployCommandMixin
from cliara.shell_app.macro_commands import MacroCommandMixin
from cliara.shell_app.input_routing import InputRoutingMixin
from cliara.shell_app.execution_engine import ExecutionEngineMixin
from cliara.shell_app.gate_flow import GateFlowMixin
from cliara.shell_app.codebase_commands import CodebaseCommandMixin

from cliara.shell_app.runtime import (
    CommandHistory,
    _StartupProgress,
    _NullTimer,
    _LiveTimer,
    _cliara_console,
    _ui_accent_style,
    _fmt_path,
    _is_codebase_question_intent,
    _is_explain_last_rest,
    _is_semantic_history_search_intent,
    _looks_like_fix,
    _looks_like_why,
    _nl_query_plain_history_arg,
    _print_safety_panel,
    StreamingThinkingAnimation,
    pick_thinking_word,
    thinking_status,
    print_dim,
    print_error,
    print_header,
    print_help_cmd,
    print_help_example,
    print_info,
    print_success,
    print_warning,
    safe_input,
)


def _git_run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_ok(args: List[str]) -> bool:
    return subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0

class CliaraShell(
    InputRoutingMixin,
    SessionCommandMixin,
    MacroCommandMixin,
    DeployCommandMixin,
    ExecutionEngineMixin,
    GateFlowMixin,
    CodebaseCommandMixin,
):
    """Main Cliara shell - wraps user's real shell."""
    
    def __init__(self, config: Optional[Config] = None, quiet: bool = False):
        """
        Initialize Cliara shell.

        Args:
            config: Configuration object (creates default if None)
            quiet:  Suppress banner, pulse, and startup hints (tmux/minimal terminals).
                    Also honoured via ``quiet: true`` in ~/.cliara/config.json.
        """
        _cfg = config or Config()
        self.quiet = quiet or bool(_cfg.get("quiet", False))

        # --- Startup progress bar ---
        progress = _StartupProgress(total_steps=6, silent=self.quiet)

        if not self.quiet:
            print()  # blank line before the bar
        progress.step("Loading config...")
        self.config = _cfg
        self._config_undo_stack: collections.deque = collections.deque(maxlen=20)
        from cliara.console import set_ui_theme

        set_ui_theme(self.config.get("theme"))

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

        # Copilot Gate  -  AI-command interception
        self._source_detector = SourceDetector()
        self._risk_engine = RiskEngine(self.safety, self.diff_preview)
        self._copilot_gate = CopilotGate(
            self._risk_engine,
            auto_approve_safe=self.config.get("copilot_gate_auto_approve_safe", True),
            auto_approve_caution=self.config.get("copilot_gate_auto_approve_caution", False),
        )

        progress.step("Loading history...")
        history_file = self.config.config_dir / "history.txt"
        self.history = CommandHistory(
            max_size=self.config.get("history_size", 1000),
            history_file=history_file,
        )

        # Jump store  -  zoxide-style directory learning
        try:
            jump_store_path = self.config.config_dir / "jump_dirs.json"
            self._jump_store = JumpDirectoryStore(store_path=jump_store_path)
        except Exception:
            self._jump_store = None

        self.running = True
        self.shell_path = self.config.get("shell")
        if not self.shell_path:
            self.shell_path = self.config._detect_shell()

        # Deploy store  -  persisted per-project deploy configs
        self.deploy_store = DeployStore()

        # Task sessions  -  named, resumable workflow context
        sessions_path = self.config.config_dir / "sessions.json"
        self.session_store = SessionStore(store_path=sessions_path)

        # Ambient pulse glyph (prompt-only; details via `cliara pulse`).
        try:
            from cliara.pulse import PulseComputer

            self._pulse = PulseComputer(self.config)
        except Exception:
            self._pulse = None
        self.current_session: Optional[TaskSession] = None
        # When set, the next recorded command is linked as child of this id (e.g. fix after failure)
        self._next_command_parent_id: Optional[str] = None

        # Error translator state  -  populated by execute_shell_command()
        self.last_stderr: str = ""
        self.last_stdout: str = ""
        self.last_exit_code: int = 0
        self.last_command: str = ""  # Last shell command that was executed
        # When False, hide OK/X in the prompt (e.g. user just ran theme/help). last_exit_code
        # is unchanged so ? fix still sees the last real shell failure. Starts True so the first
        # prompt matches prior behaviour; handle_input clears it until a shell run completes.
        self.show_shell_exit_in_prompt: bool = True
        # When True, next handle_input skips Copilot Gate (replay via last/retry)
        self._gate_force_typed: bool = False
        # After CopilotGate approves pasted/AI input, skip duplicate _inline_gate (see handle_input).
        # Cleared at the next handle_input if the prior line exited before reaching _inline_gate.
        self._inline_skip_once: bool = False
        self._prev_cwd: Optional[str] = None  # Previous directory for "cd -"
        # Load persisted last command so "last" works after restart
        _last_cmd_file = self.config.config_dir / "last_command.txt"
        if _last_cmd_file.exists():
            try:
                with with_file_lock(_last_cmd_file):
                    with open(_last_cmd_file, "r", encoding="utf-8") as f:
                        self.last_command = f.read().strip()
            except Exception:
                pass

        # Prompt session reference  -  set in run().
        self._prompt_session = None

        # Pending fix command  -  set by _auto_suggest_fix(), consumed by
        # the Tab key binding in prompt_toolkit.  Pressing Tab on an empty
        # prompt fills in this command; any other input clears it.
        self._pending_fix: Optional[str] = None

        # Inline fix offer  -  set by _auto_suggest_fix() after a non-zero exit.
        # The REPL consumes exactly one keypress: 'f' applies, anything else dismisses.
        self._inline_fix_offer: Optional[str] = None
        self._inline_fix_offer_active: bool = False

        # Regression detection  -  last report (ranked_causes, last_snapshot, current_snapshot)
        # for ? why after an automatic regression check on failure.
        self._last_regression_report: Optional[Tuple[List[Tuple[str, str]], dict, dict]] = None

        # Semantic history  -  store + background worker for ? find / ? when did I ...
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
                self._semantic_history_queue = queue.Queue(maxsize=200)
                self._semantic_history_thread = threading.Thread(
                    target=self._semantic_history_worker,
                    daemon=True,
                )
                self._semantic_history_thread.start()

        # Elapsed time for the last executed command (for prompt duration display)
        self._last_command_elapsed: Optional[float] = None

        # IDE bridge (silent, bidirectional) - shares active editor file + last run
        self._ide_bridge = None
        try:
            from cliara.ide_bridge import get_bridge

            if self.config.get("ide_bridge_enabled", True):
                self._ide_bridge = get_bridge(config_dir=self.config.config_dir, enabled=True)
                self._ide_bridge.start()
        except Exception:
            self._ide_bridge = None

        progress.step("Connecting LLM...")
        # Initialize LLM if API key is available
        self._initialize_llm(quiet=True)
        # Optional: also initialize a secondary local Ollama backend for the
        # transparent local/cloud router (never interrupts; silent if unavailable).
        try:
            self._initialize_local_llm_router(quiet=True)
        except Exception:
            pass

        progress.step("Detecting environment...")
        # Finish the progress bar
        progress.finish()
        
        # Show LLM status after the progress bar (single clean line)
        if self.nl_handler.llm_enabled:
            if not self.quiet:
                print_success(f"  LLM: {self._llm_status_provider_label()} connected")
        else:
            # Auto-detect Ollama first (silent, no prompt)
            from cliara import setup_wizard as _wiz
            if not _wiz.auto_detect_ollama(self):
                # Show setup wizard if LLM has never been configured and user hasn't dismissed
                wizard_dismissed = self.config.get("llm_wizard_dismissed", False)
                if not wizard_dismissed:
                    # Zero-friction: if no provider at all, auto-run Cliara Cloud login
                    provider = self.config.get_llm_provider()
                    api_key = self.config.get_llm_api_key()
                    if provider is None and api_key is None:
                        login_ok = self._handle_cliara_login(auto_run=True)
                        if not login_ok:
                            _wiz.run_wizard(self)
                    else:
                        _wiz.run_wizard(self)
        
        # First-run setup
        if self.config.is_first_run():
            self.config.setup_first_run()

    def _get_right_prompt(self):
        """
        Build a right-side prompt showing the last command duration when it was slow.
        Returns formatted text for prompt_toolkit or None when nothing should be shown.
        """
        elapsed = self._last_command_elapsed
        if elapsed is None:
            return None
        try:
            threshold = float(self.config.get("prompt_duration_threshold", 2.0))
        except Exception:
            threshold = 2.0
        if elapsed <= max(threshold, 0.0):
            return None

        # Format seconds with one decimal for sub-minute commands, mm:ss for longer ones
        if elapsed < 60:
            text = f"[{elapsed:.1f}s]"
        else:
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            text = f"[{minutes}m{seconds:02d}s]"

        return [("class:prompt-duration", text)]

    def _llm_status_provider_label(self) -> str:
        """UPPERCASE provider, with Ollama model name when applicable."""
        prov = (self.nl_handler.provider or "").upper()
        if self.nl_handler.provider == "ollama":
            return f"{prov} · {self.nl_handler.resolved_model_for_display()}"
        return prov

    def _initialize_llm(self, quiet: bool = False):
        """Initialize LLM if API key is configured."""
        provider = self.config.get_llm_provider()
        api_key = self.config.get_llm_api_key()

        if provider and api_key:
            # Clear any cloud model stored from a previous provider when switching to Ollama
            if provider == "ollama":
                from cliara.setup_wizard import _clear_incompatible_model
                _clear_incompatible_model(self)
            base_url = self.config.get_ollama_base_url() if provider == "ollama" else None
            if self.nl_handler.initialize_llm(provider, api_key, base_url=base_url):
                if not quiet:
                    model = self.config.get_llm_model() or ""
                    model_hint = f", model: {model}" if model else ""
                    print_success(f"[{icons.OK}] LLM initialized ({provider}{model_hint})")
            else:
                if not quiet:
                    print_warning(f"[{icons.WARN}] Failed to initialize LLM ({provider})")
        else:
            pass

    def _initialize_local_llm_router(self, quiet: bool = False) -> None:
        """Attempt to enable a secondary local Ollama backend.

        This does not change the configured cloud provider; it only enables
        transparent routing/redaction when Ollama is available.
        """
        # If the primary provider is already Ollama, routing isn't needed.
        if self.nl_handler.provider == "ollama":
            return
        base_url = self.config.get_ollama_base_url()
        ok = self.nl_handler.initialize_local_ollama(base_url=base_url)
        if ok and not quiet:
            print_success(f"[{icons.OK}] Local LLM (Ollama) ready")

    def _flush_semantic_history(self) -> None:
        """Persist semantic history (including in-memory-only stubs) to disk."""
        if self._semantic_history:
            try:
                self._semantic_history.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Git context helper (cached, used by semantic history enrichment)
    # ------------------------------------------------------------------

    _git_ctx_cache: Tuple[float, Dict[str, str]] = (0.0, {})

    def _get_quick_git_context(self) -> Dict[str, str]:
        """Return {git_branch, git_repo} with a 15 s TTL to avoid subprocess overhead.

        Uses a single `git rev-parse` call (fast) rather than the full snapshot
        used for NL grounding. Called from the hot path after every command, so
        the TTL keeps cumulative cost negligible.
        """
        now = time.monotonic()
        ts, cached = self._git_ctx_cache
        if (now - ts) < 15.0:
            return cached
        ctx: Dict[str, str] = {}
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1.5,
                cwd=str(Path.cwd()),
            )
            if r.returncode == 0:
                lines = r.stdout.strip().splitlines()
                if len(lines) >= 2:
                    ctx["git_repo"] = Path(lines[0].strip()).name
                    ctx["git_branch"] = lines[1].strip()
        except Exception:
            pass
        self._git_ctx_cache = (now, ctx)
        return ctx

    def _semantic_history_worker(self):
        """Background worker: dequeue (command, cwd, exit_code, summary_override, git_ctx, session_name)."""
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
                command, cwd, exit_code, summary_override, git_ctx, session_name = item
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
                    git_branch=git_ctx.get("git_branch"),
                    git_repo=git_ctx.get("git_repo"),
                    session_name=session_name,
                    persist=True,
                )
            except Exception:
                if item is not None:
                    try:
                        command, cwd, exit_code = item[0], item[1], item[2]
                        store.add(command=command, summary="", cwd=cwd, exit_code=exit_code, persist=True)
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
        """Add command to semantic history; enqueue for background summary + enrichment."""
        if not self._semantic_history:
            return
        summary_override = None
        if self._last_explained_command is not None and command.strip() == self._last_explained_command.strip():
            summary_override = self._last_explained_summary or ""
            self._last_explained_command = None
            self._last_explained_summary = None
        will_enqueue = (
            self._semantic_history_queue is not None
            and self.config.get("semantic_history_summary_on_add", True)
        )
        # Lightweight git + session context captured synchronously (cached, ~0 ms).
        git_ctx = self._get_quick_git_context()
        session_name = self.current_session.name if self.current_session else None

        try:
            self._semantic_history.add(
                command=command,
                summary=summary_override or "",
                cwd=cwd,
                exit_code=exit_code,
                git_branch=git_ctx.get("git_branch"),
                git_repo=git_ctx.get("git_repo"),
                session_name=session_name,
                persist=not will_enqueue,
            )
        except Exception:
            pass
        if not self._semantic_history_queue:
            return
        if not self.config.get("semantic_history_summary_on_add", True):
            return
        item = (command, cwd, exit_code, summary_override, git_ctx, session_name)
        try:
            self._semantic_history_queue.put_nowait(item)
        except queue.Full:
            # Queue is at capacity (Ollama unresponsive) — drop oldest to make room.
            try:
                self._semantic_history_queue.get_nowait()
                self._semantic_history_queue.task_done()
            except queue.Empty:
                pass
            try:
                self._semantic_history_queue.put_nowait(item)
            except queue.Full:
                pass
        except Exception:
            pass

    # Rotating "did you know?" tips shown on startup.
    # Each entry may contain {nl} which is replaced by the configured nl_prefix.
    _STARTUP_TIPS: List[str] = [
        "Try '{nl} fix' right after a failed command  -  Cliara diagnoses the error and suggests a fix.",
        "'{nl} why' runs a regression deep-dive: it compares the current failure to past successes.",
        "'{nl} find <phrase>' searches your command history by intent, not just text.",
        "Prefix any command with '{nl}' to translate plain English to shell  -  e.g. '{nl} kill port 3000'.",
        "Use 'explain <cmd>' or 'explain last' to break down a command or the last run's output.",
        "Use 'ma <name>' to save command chains as a macro, or 'mc' to create one from plain English (name + steps suggested).",
        "'mc' opens macro create  -  describe the workflow and Cliara suggests a name and multi-step shell commands.",
        "'ma <name> --nl' keeps your chosen name and generates commands from English; 'ma --nl' is the same as 'mc'.",
        "Type just a macro name to run it  -  no prefix needed.",
        "Risky commands (rm -rf, format ...) always pause for approval, even when piped.",
        "'push' automatically writes your commit message and selects the right branch.",
        "'ss <name>' (or session start) groups your work; 'se' / 'session end' closes; 'se --reflect' saves optional closeout; 'session list' shows past ones.",
        "Use 'theme' or 'themes' to list or switch colour schemes  -  try dracula, nord, or catppuccin.",
        "'history' shows recent commands; '{nl} when did I <phrase>' finds them by meaning.",
        "Cliara watches long-running commands and notifies you when they finish.",
        "Set OPENAI_API_KEY in a .env file and Cliara picks it up automatically.",
        "Use '{nl} deploy' to get guided deployment steps for your current project.",
        "The diff preview shows what a destructive command will affect before it runs.",
        "'ml' lists your macros (full form: macro list).",
        "Press Ctrl+C to cancel a running command; Cliara will offer to diagnose failures.",
    ]

    def _pick_tip(self) -> str:
        """Return a random startup tip, substituting the configured nl_prefix."""
        nl = self.config.get("nl_prefix", "?")
        tip = random.choice(self._STARTUP_TIPS)
        return tip.replace("{nl}", nl)

    def _print_full_banner(self):
        """Print the full quick-tips banner (Rich Panel). Used at startup when appropriate and by the 'tips' command."""
        from cliara import __version__
        from cliara.console import get_ui_theme
        from cliara.highlighting import get_tips_panel_styles
        from rich.markup import escape
        from rich.panel import Panel
        from rich.text import Text

        nl = self.config.get("nl_prefix", "?")
        theme = get_ui_theme()
        s = get_tips_panel_styles(theme)

        def M(key: str, text: str) -> str:
            """Wrap *text* (user-facing; escaped) in Rich style *key* from theme."""
            raw = (text or "").replace("\n", " ")
            st = (s.get(key) or "").strip()
            if not st:
                return escape(raw)
            return f"[{st}]{escape(raw)}[/]"

        shell_line = f"Shell: {self.shell_path}"
        if self.nl_handler.llm_enabled:
            llm_line = f"LLM: {self._llm_status_provider_label()} (Ready)"
        else:
            llm_line = "LLM: Not configured (set OPENAI_API_KEY in .env)"

        _nl_hint = (
            f"e.g. {nl} kill port 3000"
            if self.nl_handler.llm_enabled
            else "(configure LLM  -  see help)"
        )

        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        rule_char = "─" if ("utf" in enc or "65001" in enc) else "-"
        rule = f"[{s['rule']}]{rule_char * 52}[/]"
        show_tips = self.config.get("show_tips", True)

        blocks = [
            M("meta", shell_line),
            M("meta", llm_line),
            "",
            rule,
            "",
            M("heading", "Quick tips"),
            "",
            f"  {M('kbd', f'{nl} <query>')}{M('body', '   Plain English  ->  shell commands  ')}{M('hint', _nl_hint)}",
            f"  {M('kbd', f'{nl} fix')}{M('body', '            After error  -  what broke + how to fix')}",
            f"  {M('kbd', 'explain last')}{M('body', '     Last run  -  output + exit code  ')}{M('hint', f'({nl} explain last)')}",
            f"  {M('kbd', f'{nl} find ...')}{M('body', '     Search history by meaning  ')}{M('hint', f'({nl} when did I ...)')}",
            f"  {M('kbd', 'mc · ma · ml · ms')}{M('body', ' Default macro commands  -  create, add, list, save last run')}",
            f"  {M('kbd', 'push')}{M('body', '               Smart git  -  suggest commit + push')}",
            f"  {M('kbd', 'ss / se · chat copy')}{M('body', ' Start/end task sessions; copy last run for AI editors')}",
            f"  {M('kbd', 'doctor')}{M('body', '              Health check  -  shell, LLM, macros, config')}",
            f"  {M('kbd', 'help · tips · exit')}{M('body', ' Full command list · show tips again · quit')}",
            "",
            rule,
        ]
        if show_tips:
            tip_footer = self._pick_tip()
            blocks += [
                "",
                f"{M('footer_icon', '*')} {M('footer', f'Did you know? {tip_footer}')}",
            ]
        content = "\n".join(blocks)

        title = (
            f"[{s['title_brand']}]Cliara {__version__}[/] "
            f"[{s['title_sep']}] - [/] "
            f"[{s['title_tagline']}]AI-Powered Shell[/]"
        )

        panel = Panel(
            Text.from_markup(content, overflow="fold"),
            title=Text.from_markup(title, overflow="fold"),
            border_style=s.get("border", "cyan"),
            padding=(1, 2),
            highlight=False,
        )
        _cliara_console().print(panel)
        _cliara_console().print()

    def print_banner(self, force_full: bool = False):
        """
        Print welcome: full quick-tips panel or a compact one-liner.
        After launch #3, show compact unless full was shown today or --verbose.
        """
        from datetime import date
        launch_count = int(self.config.get("launch_count") or 0) + 1
        self.config.settings["launch_count"] = launch_count
        today = date.today().isoformat()
        last_banner = self.config.get("last_banner_date")

        if force_full or launch_count <= 3 or last_banner != today:
            self._print_full_banner()
            self.config.settings["last_banner_date"] = today
            self.config.save()
            return
        # Compact one-liner
        from cliara import __version__
        parts = [f"cliara {__version__}"]
        if self.nl_handler.llm_enabled:
            if self.nl_handler.provider == "ollama":
                parts.append(
                    f" {self.nl_handler.provider} · {self.nl_handler.resolved_model_for_display()} (ready)"
                )
            else:
                parts.append(f" {self.nl_handler.provider} (ready)")
        else:
            parts.append(" LLM not configured")
        n_macros = len(self.macros.list_all()) if hasattr(self.macros, "list_all") else 0
        if n_macros > 0:
            parts.append(f" · {n_macros} macros")
        if self.current_session:
            parts.append(f" · session: {self.current_session.name}")
        parts.append("Type help or tips")
        print_dim("  " + " · ".join(parts))
        print_dim("")
        self.config.save()
    
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
            from cliara.highlighting import ShellLexer, get_style_for_theme, list_themes

            # "?"? Theme: always use a valid one (user's choice or default dracula) "?"?
            theme_name = (self.config.get("theme") or "dracula").strip().lower()
            if theme_name not in list_themes():
                theme_name = "dracula"

            # "?"? Custom key bindings "?"?
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
                    # Mark BEFORE insert_text so that if insert_text raises the
                    # flag is never set on a failed paste (avoids false positives
                    # on the next typed command).
                    try:
                        event.current_buffer.insert_text(event.data)
                    except Exception:
                        return  # failed paste — do not mark
                    self._source_detector.mark_paste()
            except (ImportError, Exception):
                pass

            @kb.add("tab", eager=True)
            def _accept_fix(event):
                """Tab: accept ghost text (like Right arrow), or pending fix, or completion."""
                buf = event.current_buffer
                # Ghost text: accept auto-suggestion if available (same as Right arrow)
                suggestion = getattr(buf, "suggestion", None)
                if suggestion and suggestion.text and buf.document.is_cursor_at_the_end:
                    buf.insert_text(suggestion.text)
                    return
                if buf.text == "" and self._pending_fix:
                    buf.insert_text(self._pending_fix)
                    self._pending_fix = None
                else:
                    buf.complete_next()

            @kb.add("c-g", eager=True)
            def _force_local_next(event):
                """Force the next LLM request to local (Ctrl+G)."""
                try:
                    self.nl_handler.force_local_next_request()
                    try:
                        event.app.invalidate()
                    except Exception:
                        pass
                except Exception:
                    pass

            # Seed prompt history from existing command history so
            # arrow-up recalls previous sessions' commands.
            pt_history = InMemoryHistory()
            for cmd in self.history.history:
                pt_history.store_string(cmd)

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
    def run(self, verbose_banner: bool = False):
        """Main shell loop. Set verbose_banner=True to always show full quick-tips panel (e.g. cliara --verbose)."""
        if not self.quiet:
            self.print_banner(force_full=verbose_banner)

        # Try to set up the highlighted prompt; fall back to plain input
        self._prompt_session = self._create_prompt_session()
        if self._prompt_session is None:
            # prompt_toolkit unavailable  -  use readline instead
            self.history.setup_readline()

        # Prompt arrow glyph (kept simple and readable across terminals)
        prompt_arrow = "❯"

        while self.running:
            try:
                # Inline fix offer: consume one key between command output and next prompt.
                if self._inline_fix_offer_active and self._inline_fix_offer:
                    self._handle_inline_fix_offer()

                raw_cwd = str(Path.cwd())
                cwd = _fmt_path(raw_cwd)

                # Compute pulse once per prompt (avoid work per keystroke).
                pulse_snap = None
                try:
                    if getattr(self, "_pulse", None) is not None:
                        pulse_snap = self._pulse.get(fetch_ci=True)
                except Exception:
                    pulse_snap = None

                if self._prompt_session is not None:
                    # Coloured, syntax-highlighted prompt (uses current theme from config)
                    nl_p = (self.config.get("nl_prefix", "?") or "?")

                    def _message():
                        message = []

                        # Ambient pulse glyph: synthesis-only, always glyph-only.
                        try:
                            if pulse_snap is not None and not self.quiet:
                                from cliara.pulse import prompt_style_class

                                message.append((prompt_style_class(pulse_snap.color), f"{pulse_snap.glyph} "))
                        except Exception:
                            pass

                        # Exit code indicator: only after a line that ran the shell (not theme/help).
                        if self.show_shell_exit_in_prompt:
                            if self.last_exit_code != 0:
                                message.append(("class:prompt-exit-fail", f"X {self.last_exit_code}"))
                                message.append(("class:prompt-sep", " "))
                            elif self.last_command:
                                message.append(("class:prompt-exit-success", icons.OK))
                                message.append(("class:prompt-sep", " "))
                        message.append(("class:prompt-name", "cliara"))
                        message.append(("class:prompt-sep", " "))
                        if self.current_session:
                            message.append(("class:prompt-path", f"[{self.current_session.name}]"))
                            message.append(("class:prompt-sep", " "))
                        message.extend([
                            ("class:prompt-path", cwd),
                            ("", " "),
                            ("class:prompt-arrow", f"{prompt_arrow} "),
                        ])
                        return message

                    user_input = self._prompt_session.prompt(
                        _message,
                        rprompt=self._get_right_prompt(),
                    ).strip()
                else:
                    # Plain fallback: still apply theme to "cliara" via ANSI so a theme is always visible
                    from cliara.highlighting import get_prompt_name_ansi
                    pfx, suf = get_prompt_name_ansi(self.config.get("theme") or "dracula")
                    exit_indicator = ""
                    if self.show_shell_exit_in_prompt:
                        if self.last_exit_code != 0:
                            exit_indicator = f"X {self.last_exit_code} "
                        elif self.last_command:
                            exit_indicator = f"{icons.OK} "

                    # Duration in plain mode: show on its own line (we can't right-align without prompt_toolkit)
                    if self._last_command_elapsed is not None:
                        try:
                            threshold = float(self.config.get("prompt_duration_threshold", 2.0))
                        except Exception:
                            threshold = 2.0
                        if self._last_command_elapsed > max(threshold, 0.0):
                            if self._last_command_elapsed < 60:
                                duration_str = f"[{self._last_command_elapsed:.1f}s]"
                            else:
                                minutes = int(self._last_command_elapsed // 60)
                                seconds = int(self._last_command_elapsed % 60)
                                duration_str = f"[{minutes}m{seconds:02d}s]"
                            print_dim(f"  {duration_str}")
                    # Prompt line stays clean so cursor is right after "> "
                    pulse_prefix = ""
                    if not self.quiet:
                        try:
                            if pulse_snap is not None:
                                from cliara.pulse import ansi_color_prefix, ansi_color_suffix

                                pulse_prefix = f"{ansi_color_prefix(pulse_snap.color)}{pulse_snap.glyph}{ansi_color_suffix()} "
                        except Exception:
                            pulse_prefix = ""
                    if self.current_session:
                        prompt = f"{pulse_prefix}{exit_indicator}{pfx}cliara{suf} [{self.current_session.name}] {cwd} {prompt_arrow} "
                    else:
                        prompt = f"{pulse_prefix}{exit_indicator}{pfx}cliara{suf} {cwd} {prompt_arrow} "
                    user_input = input(prompt).strip()

                if not user_input:
                    continue

                self.handle_input(user_input)

            except KeyboardInterrupt:
                self._print_exit_message()
                break
            except EOFError:
                self._print_exit_message()
                break
            except Exception as e:
                print_error(f"[Error] {e}")
                if os.getenv("DEBUG"):
                    import traceback
                    traceback.print_exc()


    def _handle_inline_fix_offer(self) -> None:
        """Apply or dismiss the pending inline fix offer with one keypress."""
        from cliara.shell_app.runtime import read_single_key_no_echo

        fix_cmd = self._inline_fix_offer
        # Clear first so failures don't re-trigger.
        self._inline_fix_offer = None
        self._inline_fix_offer_active = False

        if not fix_cmd:
            return

        ch = read_single_key_no_echo()
        if not ch:
            return
        if ch.lower() != "f":
            return

        # Link fix to the last command in the current task session.
        try:
            if self.current_session and self.current_session.commands:
                self._next_command_parent_id = self.current_session.commands[-1].id
        except Exception:
            pass

        # Execute immediately - no prompts.
        self.execute_shell_command(fix_cmd, capture=False)
    
    
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

        # "?"? "? fix"  -  context-aware error repair (typo-tolerant) "?"?
        # Catches: ? fix, ? fox, ? fxi, ? fiz, ? fixe, etc.
        if _looks_like_fix(query):
            self.handle_fix()
            return

        # "?"? "? why"  -  regression deep-dive (typo-tolerant) "?"?
        if _looks_like_why(query):
            self.handle_why()
            return

        # "?"? "? explain last"  -  same as bare explain last
        q_low = query.strip().lower()
        if q_low.startswith("explain ") and _is_explain_last_rest(q_low[8:].strip()):
            self.handle_explain_last()
            return

        # "?"? ``? history`` / ``? history N``  -  same as built-in history (not NL / not semantic)
        _hist_arg = _nl_query_plain_history_arg(query)
        if _hist_arg is not None:
            self.handle_history(_hist_arg)
            return

        # "?"? Semantic history search: ? find ... / ? when did I ... / ? what did I run ... "?"?
        if _is_semantic_history_search_intent(query):
            self.handle_semantic_history_search(query)
            return

        # "?"? RAG over the codebase: ? how does auth work / ? where is X defined "?"?
        # Only when a non-empty index exists for this repo — otherwise fall
        # through to the normal answer/commands routing below.
        if (
            "--save-as" not in query
            and _is_codebase_question_intent(query)
            and self.has_codebase_index()
        ):
            self.handle_codebase_question(query)
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
        
        # Build context
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash")
        }

        route = "commands"
        if self.nl_handler.llm_enabled:
            with thinking_status(query):
                route = self.nl_handler.route_query_mode(query, context)

        # Informational intent: answer directly, don't force command execution.
        if route == "answer":
            if save_as_name:
                print_warning("[Cancelled] --save-as is only valid for executable command generation.")
                return

            stream_cb = None
            ql = query if len(query) <= 48 else (query[:45] + "...")
            with thinking_status(ql) as status:
                answer_chars = 0

                if self.config.get("stream_llm", True):
                    def _answer_stream_cb(chunk: str) -> None:
                        nonlocal answer_chars
                        answer_chars += len(chunk or "")
                        if answer_chars and answer_chars % 120 == 0:
                            status.update(f"[dim]{answer_chars} chars[/dim]")

                    stream_cb = _answer_stream_cb

                answer = self.nl_handler.answer_query(query, context, stream_callback=stream_cb)

            from rich.panel import Panel
            from rich.markdown import Markdown
            from rich.text import Text

            accent = _ui_accent_style()
            body = (answer or "").strip()
            if body:
                renderable = Markdown(body)
                _cliara_console().print(
                    Panel(
                        renderable,
                        title=Text("Answer", style=accent),
                        subtitle=Text(f"? {query}", style="dim"),
                        border_style=accent,
                        padding=(0, 1),
                    )
                )
            else:
                print_error("[Error] No answer content returned from the LLM.")
            return

        if not self.nl_handler.llm_enabled:
            print_warning("[LLM not configured  -  run 'setup-llm' to enable natural language commands]")
            return

        ql = query if len(query) <= 48 else (query[:45] + "...")
        with thinking_status(ql) as status:
            progress_chars = 0
            progress_tick = 0

            def _nl_progress_callback(chunk: str) -> None:
                nonlocal progress_chars, progress_tick
                progress_chars += len(chunk or "")
                next_tick = progress_chars // 120
                if next_tick > progress_tick:
                    progress_tick = next_tick
                    status.update(f"[dim]{progress_chars} chars[/dim]")

            # Safe for JSON agents: only updates spinner label, never prints tokens.
            setattr(_nl_progress_callback, "__cliara_json_safe__", True)

            commands, explanation, danger_level = self.nl_handler.process_query(
                query,
                context,
                stream_callback=_nl_progress_callback,
            )
        
        if not commands:
            print_error(f"[Error] {explanation}")
            return

        # Show generated commands and explanation with themed Rich UI.
        from rich.panel import Panel
        from rich.table import Table
        from rich.syntax import Syntax
        from rich.text import Text
        from cliara.highlighting import ShellLexer

        accent = _ui_accent_style()
        theme_name = (self.config.get("theme") or "dracula").strip().lower()
        _pygments_theme_map = {
            "solarized": "solarized-dark",
            "light": "native",
            "nord": "dracula",
            "catppuccin": "dracula",
        }
        pygments_theme = _pygments_theme_map.get(theme_name, theme_name)

        cmd_table = Table(show_header=True, box=None, padding=(0, 1), header_style=accent)
        cmd_table.add_column("#", style="dim", width=3)
        cmd_table.add_column("Command", min_width=20)
        for i, cmd in enumerate(commands, 1):
            cmd_table.add_row(str(i), Syntax(cmd, lexer=ShellLexer(), theme=pygments_theme))

        _cliara_console().print(
            Panel(
                cmd_table,
                title=Text("Generated Commands", style=accent),
                subtitle=Text(f"? {query}", style="dim"),
                border_style=accent,
                padding=(0, 1),
            )
        )
        _cliara_console().print(
            Panel(
                (explanation or "").strip() or "No explanation provided.",
                title=Text("Explanation", style=accent),
                border_style="dim",
                padding=(0, 1),
            )
        )
        
        # --save-as: save as macro instead of executing
        if save_as_name:
            confirm = (safe_input(f"\nSave as macro '{save_as_name}'? (y/n): ") or "").lower()
            if confirm not in ['y', 'yes']:
                print_warning("[Cancelled]")
                return
            if not self._check_macro_name_conflict(save_as_name):
                print_warning("[Cancelled]")
                return
            description = safe_input("Description (optional): ") or query
            self.macros.add(save_as_name, commands, description)
            print_success(f"[{icons.OK}] Macro '{save_as_name}' saved with {len(commands)} command(s)")
            return
        
        # Safety check with copy-to-clipboard option
        if danger_level != DangerLevel.SAFE:
            _print_safety_panel(self.safety, commands, danger_level)
        
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
        from rich.panel import Panel
        from rich.text import Text
        _cliara_console().print(
            Panel(
                Text("Executing generated commands", style="dim"),
                title=Text("Execution", style=_ui_accent_style()),
                border_style=_ui_accent_style(),
                padding=(0, 1),
            )
        )
        
        for i, cmd in enumerate(commands, 1):
            print_info(f"[{i}/{len(commands)}] {cmd}")
            _cliara_console().rule(style="dim")
            success = self._execute_nl_generated_command(cmd)
            print()
            
            if not success:
                print_error(f"[X] Command {i} failed")
                self._auto_suggest_fix()
                break
        else:
            print_header("="*60)
            print_success(f"[{icons.OK}] All commands completed successfully")
            print_header("="*60 + "\n")
        
        # Save to history for "save last"
        self.history.set_last_execution(commands)

    def handle_fix(self):
        """
        Context-aware error repair: '? fix'

        Uses the last failed command's stderr, exit code, and the command
        itself to ask the LLM (or stub patterns) how to fix the error.
        No copy-pasting needed  -  Cliara already has all the context.
        """
        # Guard: is there anything to fix?
        if not self.last_command:
            print_error("[Cliara] Nothing to fix  -  no commands have been run yet.")
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
            print_dim("         No stderr captured  -  nothing to analyse.")
            return

        # We have a failed command with stderr  -  hand off to the error
        # translation pipeline (which already handles LLM + stub fallback,
        # displays the explanation, and offers to run the fix).
        print_info(
            f"\n[Cliara] Diagnosing last failure..."
        )
        print_dim(f"         Command:   {self.last_command}")
        print_dim(f"         Exit code: {self.last_exit_code}")
        print()

        self._handle_error_translation(self.last_command, stderr)

    def handle_explain_last(self):
        """
        Explain the last run: command line + exit code + captured stdout/stderr
        in one narrative. If nothing was captured, falls back to ``explain <cmd>``
        for the last command (no re-run prompt).
        """
        if not self.last_command:
            print_error("[Cliara] Nothing to explain  -  no command run yet.")
            return

        stdout = self.last_stdout or ""
        stderr = self.last_stderr or ""
        if not stdout.strip() and not stderr.strip():
            print_warning(
                "[Cliara] No captured stdout/stderr for that run  -  "
                "showing command-line explanation instead.\n"
            )
            self.handle_explain(self.last_command, offer_run=False)
            return

        from rich.console import Group
        from rich.panel import Panel
        from rich.status import Status
        from rich.syntax import Syntax
        from rich.text import Text
        from cliara.console import get_ui_theme
        from cliara.highlighting import ShellLexer, get_tips_panel_styles

        theme_name = get_ui_theme()
        styles = get_tips_panel_styles(theme_name)
        console = _cliara_console()

        # Keep syntax rendering consistent with other command views.
        _pygments_theme_map = {
            "solarized": "solarized-dark",
            "light": "native",
            "nord": "dracula",
            "catppuccin": "dracula",
        }
        pygments_theme = _pygments_theme_map.get(theme_name, theme_name)

        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }

        # Stream explanation token-by-token; animated spinner shows until first token.
        _explain_chunks: List[str] = []
        _explain_anim = StreamingThinkingAnimation(self.last_command[:55]).start()

        def _on_explain_token(piece: str) -> None:
            _explain_chunks.append(piece)

        explanation = self.nl_handler.explain_terminal_output(
            self.last_command,
            self.last_exit_code,
            stdout,
            stderr,
            context,
            stream_callback=_explain_anim.wrap(on_token=_on_explain_token),
        )
        _explain_anim.stop()
        if _explain_chunks:
            print()  # end streamed line
            explanation = "".join(_explain_chunks).strip()

        raw_lines = [ln.strip() for ln in (explanation or "").splitlines() if ln.strip()]
        if not raw_lines:
            raw_lines = ["No explanation was returned."]

        has_bullets = any(ln.startswith(("-", "*", "•")) for ln in raw_lines)
        if not has_bullets and len(raw_lines) == 1:
            sentence_parts = [
                s.strip()
                for s in re.split(r"(?<=[.!?])\s+", raw_lines[0])
                if s.strip()
            ]
            if len(sentence_parts) > 1:
                raw_lines = [f"- {s}" for s in sentence_parts]

        explanation_text = Text()
        for idx, line in enumerate(raw_lines):
            is_bullet = line.startswith(("-", "*", "•"))
            if is_bullet:
                cleaned = line.lstrip("-*• ").strip()
                explanation_text.append("  • ", style=styles["kbd"])
                explanation_text.append(cleaned, style=styles["body"])
            else:
                explanation_text.append(line, style=styles["body"])
            if idx < len(raw_lines) - 1:
                explanation_text.append("\n")

        short_cmd = self.last_command.strip()
        if len(short_cmd) > 88:
            short_cmd = short_cmd[:85] + "..."

        title = Text()
        title.append(f"{icons.INFO} ", style=styles["title_brand"])
        title.append("Explain last", style=styles["title_tagline"])

        body = Group(
            Text("Command", style=styles["heading"]),
            Syntax(self.last_command, lexer=ShellLexer(), theme=pygments_theme, word_wrap=True),
            Text(""),
            Text("What it means", style=styles["heading"]),
            explanation_text,
        )

        console.print()
        console.print(
            Panel(
                body,
                title=title,
                subtitle=Text(short_cmd, style=styles["hint"]),
                border_style=styles["border"],
                padding=(0, 1),
            )
        )
        console.print()

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
            print_dim("No snapshot diff (git/deps/env/runtime)  -  failure may be unrelated.")
            return
        self._last_regression_report = (causes, last, current)
        text = regression.format_expanded_report(causes, last, current)
        _cliara_console().print(Panel(text, title="Regression (vs last success)", border_style="dim"))

    def handle_semantic_history_search(self, query: str):
        """Search command history by intent — routes to prose answer or command table.

        Routing:
          * Questions about past activity ("when did I set up Ollama?",
            "did I ever run docker-compose?") → RAG prose answer + sources table.
          * Command-seeking queries ("find the docker command I used last week",
            "? find port command") → traditional command table + run prompt.
        """
        if not self.config.get("semantic_history_enabled", True):
            print_dim("Semantic history search is disabled. Use 'history [N]' for a plain list.")
            return
        store = self._semantic_history
        if not store or store.is_empty():
            print_dim("No semantic history yet — run some commands first.")
            return

        can_embed = self.nl_handler.supports_embedding_api()
        llm = self.nl_handler.llm_enabled
        if not can_embed and not llm:
            print_dim("History search needs an LLM or embedding API. Run 'setup-llm'.")
            return

        def _cfg_int(key: str, default: int) -> int:
            try:
                return int(self.config.get(key, default))
            except (TypeError, ValueError):
                return default

        def _cfg_float(key: str, default: float) -> float:
            try:
                return float(self.config.get(key, default))
            except (TypeError, ValueError):
                return default

        use_embeddings = self.config.get("semantic_history_use_embeddings", False)
        top_k       = max(1, min(_cfg_int("semantic_history_top_k", 10), 100))
        min_score   = max(0.0, min(_cfg_float("semantic_history_embedding_min_score", 0.28), 1.0))
        adaptive    = bool(self.config.get("semantic_history_embedding_adaptive", True))
        adaptive_frac = max(0.1, min(_cfg_float("semantic_history_embedding_adaptive_frac", 0.82), 1.0))
        hybrid_kw   = bool(self.config.get("semantic_history_hybrid_keyword", True))
        kw_pool     = max(4, min(_cfg_int("semantic_history_hybrid_keyword_pool", 24), 200))
        backfill_n  = max(0, min(_cfg_int("semantic_history_backfill_per_search", 32), 200))
        intent_max  = max(20, min(_cfg_int("semantic_history_intent_max_entries", 200), 500))

        qstrip = (query or "").strip()

        # --- Classify intent FIRST — before any expensive I/O ---
        # This lets us skip the embedding backfill entirely for prose-answer
        # mode (where search_history_rag handles retrieval itself).
        mode = self.nl_handler._classify_history_query_mode(qstrip) if llm else "commands"

        # --- Load entries ---
        entries = store.get_all()

        # --- Embedding backfill: ONLY for command-table mode ---
        # Backfilling up to 32 entries via the embedding API can take 30-60 s on
        # Ollama (nomic-embed-text). Prose/answer mode uses search_history_rag
        # which handles its own retrieval without needing pre-computed vectors.
        if mode == "commands" and use_embeddings and can_embed and backfill_n > 0:
            store.backfill_missing_embeddings(self.nl_handler.get_embedding, max_entries=backfill_n)
            entries = store.get_all()

        from rich.table import Table
        from rich import box
        from datetime import datetime

        def _fmt_ts(ts: str) -> str:
            ts = (ts or "").strip()
            if not ts:
                return ""
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.astimezone().strftime("%b %d, %H:%M")
            except Exception:
                return ts[:10]

        def _show_sources_table(sources: List[Dict], header: str = "") -> None:
            """Theme-aware Rich evidence table (used in both prose and command modes)."""
            from rich.text import Text
            accent = _ui_accent_style()

            if header:
                print()
                print_dim(header)

            tbl = Table(
                box=box.SIMPLE,
                show_header=True,
                header_style=f"bold {accent}",
                show_lines=False,
                padding=(0, 1),
            )
            tbl.add_column("#",       style="dim",         justify="right", no_wrap=True)
            tbl.add_column("When",    style="dim",         no_wrap=True)
            tbl.add_column("Command", style="bold white",  overflow="fold")
            tbl.add_column("Context", overflow="fold")
            tbl.add_column("",        justify="right",     no_wrap=True)

            for i, e in enumerate(sources, 1):
                cmd     = (e.get("command") or "").strip()
                when    = _fmt_ts(e.get("timestamp", ""))
                branch  = (e.get("git_branch") or "").strip()
                session = (e.get("session_name") or "").strip()
                ec      = e.get("exit_code")

                # Build context cell: branch in accent color, session italic dim
                ctx = Text()
                if branch:
                    ctx.append(f"[{branch}]", style=accent)
                if session:
                    if branch:
                        ctx.append("  ")
                    ctx.append(f"#{session}", style="dim italic")

                # Colored status icon
                if ec is None:
                    status = Text("", style="dim")
                elif ec == 0:
                    status = Text("✓", style="green")
                else:
                    status = Text("✗", style="red")

                tbl.add_row(str(i), when, cmd, ctx, status)
            _cliara_console().print(tbl)

        def _render_answer_divider() -> None:
            """Print a slim themed divider between the streamed answer and sources.

            We don't re-render the answer in a panel — the streamed text IS the
            answer. A subtle accent-colored rule separates it from the sources
            table below so the visual hierarchy stays clear.
            """
            from rich.rule import Rule

            accent = _ui_accent_style()
            print()  # spacer
            _cliara_console().print(Rule(style=accent, characters="─"))

        def _run_prompt(sources: List[Dict]) -> None:
            """Offer to re-execute one of the source commands."""
            if not sources:
                return
            try:
                choice = input(f"Run? (1-{len(sources)} / Enter to skip): ").strip().lower()
                if not choice or choice in ("n", "no"):
                    return
                idx = 1 if choice in ("y", "yes") else (int(choice) if choice.isdigit() else None)
                if idx and 1 <= idx <= len(sources):
                    picked = (sources[idx - 1].get("command") or "").strip()
                    if picked:
                        self.execute_shell_command(picked)
            except (EOFError, KeyboardInterrupt):
                print()

        # ==================================================================
        # MODE A: Prose narrative answer (RAG)
        # ==================================================================
        if mode == "answer" and llm:
            # Animated spinner cycles through words until first token; then clears
            # so the answer streams directly on a clean line below.
            _hist_anim = StreamingThinkingAnimation(qstrip[:55]).start()
            streamed: List[str] = []

            def _on_hist_token(piece: str) -> None:
                streamed.append(piece)

            answer, sources = self.nl_handler.search_history_rag(
                entries, qstrip,
                top_k=top_k,
                min_score=min_score,
                stream_callback=_hist_anim.wrap(on_token=_on_hist_token),
            )
            _hist_anim.stop()  # no-op if already stopped by first token
            if streamed:
                print()  # end streamed line

            # Trust the retrieval. The sources returned by search_history_rag are
            # exactly the entries the LLM used as context — show them all so the
            # user sees what informed the answer. Re-filtering here would hide
            # entries the LLM legitimately reasoned over.
            #
            # Display cap: keep to ≤5 for clean output.
            if sources and len(sources) > 5:
                # Already chronologically sorted; bias toward most recent on tie.
                sources = sources[-5:]

            # Prose / answer mode: the streamed text IS the result. Do NOT clutter
            # the terminal with a sources table or a "Run?" prompt — the user asked
            # a question, not for a command to re-run. The LLM is instructed to
            # name specific commands in the answer itself when relevant.
            #
            # If the model returned nothing AND there's no retrieval, surface that
            # explicitly so the user knows the history search didn't find anything.
            if not answer:
                if not sources:
                    print_dim("  No relevant history found for that question.")
                else:
                    print_dim("  No answer could be synthesized from the retrieved entries.")
            return

        # ==================================================================
        # MODE B: Command table (traditional, with optional embedding search)
        # ==================================================================
        matches: List[Dict] = []
        if use_embeddings and can_embed:
            matches = self.nl_handler.search_history_by_embeddings(
                entries, qstrip, top_k=top_k,
                min_score=min_score, adaptive=adaptive, adaptive_frac=adaptive_frac,
            )
            if hybrid_kw:
                matches = self.nl_handler.merge_embedding_keyword_results(
                    matches, entries, qstrip, target_k=top_k, keyword_pool=kw_pool,
                )
            matches = self.nl_handler.rerank_history_matches(matches, qstrip)
            if not matches and llm:
                matches = self.nl_handler.search_history_by_intent(
                    store.get_recent(intent_max), qstrip, max_entries_in_prompt=intent_max,
                )
        elif llm:
            matches = self.nl_handler.search_history_by_intent(
                store.get_recent(intent_max), qstrip, max_entries_in_prompt=intent_max,
            )
        else:
            matches = self.nl_handler.keyword_history_candidates(entries, qstrip, top_m=top_k)

        if not matches:
            print_dim(f"  No matching commands found for '{qstrip}'.")
            print_dim("  Try rephrasing, or run more commands to build up history.")
            return

        print()
        print_info(f"  Found {len(matches)} matching command(s) for: {qstrip}\n")
        _show_sources_table(matches)
        print()
        _run_prompt(matches)


    def _handle_config_command(self, args: str):
        """Built-in config command: get/set/list persistent cliara settings.

        Usage:
          config list               -  show all current settings
          config get <key>          -  print one value
          config set <key> <value>  -  persist a value to ~/.cliara/config.json
          config undo               -  revert the last config set (up to 20 levels)
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

        if sub == "undo":
            if not self._config_undo_stack:
                print_warning("  Nothing to undo.")
                return
            key, old_val = self._config_undo_stack.pop()
            cur_val = self.config.get(key)
            if old_val is None:
                self.config.settings.pop(key, None)
            else:
                self.config.settings[key] = old_val
            self.config.save()
            cur_str = repr(cur_val) if cur_val is not None else "(not set)"
            old_str = repr(old_val) if old_val is not None else "(not set)"
            print_success(f"  Reverted {key}: {cur_str}  ->  {old_str}  {icons.OK}")
            return

        if sub == "set":
            if len(parts) < 3:
                print_error("[Cliara] Usage: config set <key> <value>")
                return
            key = parts[1]
            raw_val = parts[2]

            if key in _READONLY:
                print_error(f"[Cliara] '{key}' is read-only  -  set it via your .env file instead.")
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

            old_val = self.config.get(key)
            self._config_undo_stack.append((key, old_val))
            self.config.set(key, val)
            old_str = repr(old_val) if old_val is not None else "(not set)"
            print_success(f"  {key}: {old_str}  ->  {val!r}  {icons.OK}")

            # Live-apply a small set of settings without restart
            if key == "llm_model" and self.nl_handler.llm_enabled:
                print_dim(f"  Model will be used on next LLM call.")
            return

        print_error(f"[Cliara] Unknown config subcommand: '{sub}'")
        print_dim("  Usage: config list | config get <key> | config set <key> <value> | config undo")

    # ------------------------------------------------------------------
    # Ollama setup wizard
    # ------------------------------------------------------------------

    def _handle_setup_ollama(self):
        """Delegate to the dedicated setup_ollama module."""
        from cliara import setup_ollama
        setup_ollama.run(self)

    def _handle_setup_llm(self):
        """Run the multi-provider LLM setup wizard."""
        from cliara import setup_wizard
        # Reset dismissed flag so the wizard shows fully
        self.config.settings["llm_wizard_dismissed"] = False
        setup_wizard.run_wizard(self)

    # ------------------------------------------------------------------
    # API key management
    # ------------------------------------------------------------------

    _PROVIDER_KEY_ENV_VARS: Dict[str, str] = {
        "openai":    "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "groq":      "GROQ_API_KEY",
        "gemini":    "GEMINI_API_KEY",
    }

    @staticmethod
    def _mask_api_key(key: Optional[str]) -> str:
        """Mask a key for display: 'sk-pro...XYZ4' (never log full key)."""
        if not key:
            return "(none)"
        if len(key) <= 12:
            return key[:3] + "***" + key[-2:] if len(key) > 5 else "*****"
        return f"{key[:6]}...{key[-4:]}"

    def _key_storage_path(self) -> Path:
        """Return path to the user's persistent key store (~/.cliara/.env)."""
        return self.config.config_dir / ".env"

    def _write_key_to_env_file(self, env_var: str, value: Optional[str]) -> Path:
        """Upsert KEY=VALUE in ~/.cliara/.env; if value is None, remove the key line.

        Returns the path written.
        """
        env_path = self._key_storage_path()
        existing_lines: List[str] = []
        if env_path.exists():
            try:
                existing_lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
            except Exception:
                existing_lines = []

        new_lines: List[str] = []
        replaced = False
        for line in existing_lines:
            bare = line.lstrip("#").lstrip().split("=", 1)[0].strip()
            if bare == env_var:
                if value is not None:
                    new_lines.append(f"{env_var}={value}\n")
                    replaced = True
                # if value is None, omit (= delete)
            else:
                new_lines.append(line)

        if value is not None and not replaced:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(f"{env_var}={value}\n")

        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("".join(new_lines), encoding="utf-8")
        return env_path

    def _handle_key_command(self, args: str) -> None:
        """Manage API keys: show / set / remove / test / path.

        Usage:
            key                          Show active provider, masked key, and storage path.
            key show                     Same as above.
            key set <provider> <key>     Save a key for <provider> (openai, anthropic, groq, gemini).
            key remove <provider>        Delete the saved key for <provider>.
            key test [provider]          Make a tiny test request to verify connectivity.
            key path                     Show where keys are stored.

        Keys are persisted to ``~/.cliara/.env`` (a file readable only by the
        current user on POSIX; on Windows it lives in the user profile).
        """
        parts = (args or "").split(maxsplit=2)
        sub = parts[0].lower() if parts else "show"
        env_path = self._key_storage_path()

        # ---------- show ----------
        if sub in ("", "show", "ls", "list", "status"):
            print()
            current = self.nl_handler.provider or "(none)"
            key = self.config.get_llm_api_key()
            print_info(f"  Active provider: [{current.upper()}]")
            print_dim(f"  Key:    {self._mask_api_key(key)}")
            print_dim(f"  Stored: {env_path}{' (exists)' if env_path.exists() else ' (not yet created)'}")
            print()
            print_dim("  All configured providers:")
            any_set = False
            for pid, env_var in self._PROVIDER_KEY_ENV_VARS.items():
                val = os.environ.get(env_var)
                marker = "  <- active" if pid == current else ""
                if val:
                    print(f"    {pid:<10}  {env_var:<20}  {self._mask_api_key(val)}{marker}")
                    any_set = True
                else:
                    print_dim(f"    {pid:<10}  {env_var:<20}  (unset)")
            if not any_set:
                print_dim("    (no BYOK keys set; run `key set <provider> <key>`)")
            print()
            print_dim("  Commands:")
            print_dim("    key set <provider> <key>   key remove <provider>   key test")
            print()
            return

        # ---------- path ----------
        if sub == "path":
            print()
            print_info(f"  {env_path}")
            print_dim(f"  Exists: {env_path.exists()}")
            print()
            return

        # ---------- set <provider> <key> ----------
        if sub == "set":
            if len(parts) < 3:
                print_error("[Error] Usage: key set <provider> <key>")
                return
            provider = parts[1].lower().strip()
            key = parts[2].strip()
            if provider not in self._PROVIDER_KEY_ENV_VARS:
                print_error(f"[Error] Unknown provider '{provider}'. Options: {', '.join(self._PROVIDER_KEY_ENV_VARS)}")
                return
            if not key or len(key) < 8:
                print_error("[Error] Key looks too short — paste the full key (no quotes).")
                return
            env_var = self._PROVIDER_KEY_ENV_VARS[provider]
            try:
                path = self._write_key_to_env_file(env_var, key)
            except Exception as exc:
                print_error(f"[Error] Could not write {env_path}: {exc}")
                return
            # Apply in-process so the next query uses it
            os.environ[env_var] = key
            # Set provider in config so credential resolution picks it up
            self.config.settings["llm_provider"] = provider
            self.config._load_env_vars()
            try:
                self.config.save()
            except Exception:
                pass
            # Reinit client
            ok = self.nl_handler.initialize_llm(provider, key)
            print()
            if ok:
                print_success(f"  Key saved for [{provider.upper()}]  ({self._mask_api_key(key)})")
                print_dim(f"  Storage: {path}")
                print_dim("  Run `key test` to verify, or just try `? hello world`.")
            else:
                print_warning(f"  Key saved to {path} but client init failed.")
                print_dim("  Run `key test` for the error details.")
            print()
            return

        # ---------- remove <provider> ----------
        if sub in ("remove", "rm", "delete", "del", "unset"):
            if len(parts) < 2:
                print_error("[Error] Usage: key remove <provider>")
                return
            provider = parts[1].lower().strip()
            if provider not in self._PROVIDER_KEY_ENV_VARS:
                print_error(f"[Error] Unknown provider '{provider}'. Options: {', '.join(self._PROVIDER_KEY_ENV_VARS)}")
                return
            env_var = self._PROVIDER_KEY_ENV_VARS[provider]
            try:
                self._write_key_to_env_file(env_var, None)  # None = delete line
            except Exception as exc:
                print_error(f"[Error] Could not update {env_path}: {exc}")
                return
            # Remove from process env so it doesn't leak into the current session
            os.environ.pop(env_var, None)
            print()
            print_success(f"  Removed key for [{provider.upper()}]")
            # If that was the active provider, clear in-process LLM state so
            # the next call surfaces a clear "not configured" error rather
            # than using a stale client.
            if self.nl_handler.provider == provider:
                self.nl_handler.llm_enabled = False
                self.nl_handler.llm_client = None
                self.nl_handler.provider = None
                self.config.settings["llm_provider"] = None
                try:
                    self.config.save()
                except Exception:
                    pass
                print_dim("  No active LLM provider now. Run `setup-llm` or `use <provider>`.")
            print()
            return

        # ---------- test [provider] ----------
        if sub == "test":
            provider = parts[1].lower().strip() if len(parts) > 1 else (self.nl_handler.provider or "")
            if not provider:
                print_error("[Error] No active provider. Usage: key test <provider>")
                return
            if provider != (self.nl_handler.provider or ""):
                # Switch first
                api_key = os.environ.get(self._PROVIDER_KEY_ENV_VARS.get(provider, ""), "")
                if not api_key and provider != "ollama":
                    print_error(f"[Error] No key configured for {provider}. Run `key set {provider} <key>` first.")
                    return
                if not self.nl_handler.initialize_llm(provider, api_key or "ollama"):
                    print_error(f"[Error] Failed to initialize {provider} client.")
                    return
            # Make a tiny test call
            print()
            print_dim(f"  Testing {self.nl_handler.provider} with a 5-token ping...")
            try:
                # cliara_qa is a streaming-safe text agent — use it for the ping
                reply = self.nl_handler._call_llm("cliara_qa", "Reply with exactly the single word: OK")
                snippet = (reply or "").strip()[:80] or "(empty)"
                print_success(f"  [{icons.OK}] {self.nl_handler.provider} responded: {snippet!r}")
            except Exception as e:
                err = self.nl_handler._brief_llm_error(e)
                print_error(f"  [{icons.FAIL}] {self.nl_handler.provider} failed: {err}")
                print_dim("  Fix: `key set <provider> <new-key>` or `use <other-provider>`.")
            print()
            return

        print_error(f"[Error] Unknown subcommand 'key {sub}'.")
        print_dim("  Usage: key [show|set|remove|test|path]")

    def _handle_secret_scan_command(self) -> None:
        """Run secret scan on staged files on demand (same engine as push gate).

        Usage: secret-scan
        """
        from cliara.secret_scan import get_staged_files

        staged = get_staged_files(Path.cwd())
        if not staged:
            print_dim("  No staged files. Run `git add` first, or use `push` to stage and scan automatically.")
            return

        print()
        print_dim(f"  Scanning {len(staged)} staged file(s)...")
        ok = self._run_secret_scan()
        if ok:
            print()
        # _run_secret_scan already prints the result panel

    def _handle_use_provider(self, provider_arg: str) -> None:
        """Switch the active LLM provider for this session.

        Usage:
            use             -  show current provider + available options
            use openai      -  switch to OpenAI (requires OPENAI_API_KEY in env/config)
            use ollama      -  switch to Ollama (requires Ollama running)
            use groq        -  switch to Groq (requires GROQ_API_KEY)
            use gemini      -  switch to Gemini (requires GEMINI_API_KEY)
            use anthropic   -  switch to Anthropic (requires ANTHROPIC_API_KEY)
        """
        from cliara.nl.service import _PROVIDER_DEFAULT_MODELS, _PROVIDER_BASE_URLS
        from cliara.setup_wizard import _clear_incompatible_model

        _ENV_VAR_MAP = {
            "openai":    "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "groq":      "GROQ_API_KEY",
            "gemini":    "GEMINI_API_KEY",
            "ollama":    "OLLAMA_BASE_URL",
        }

        current = self.nl_handler.provider or "none"

        if not provider_arg:
            # Show status + available options
            print()
            print_info("  Active provider: " + current.upper())
            print()
            print_dim("  Available providers:")
            for pid, evar in _ENV_VAR_MAP.items():
                val = os.getenv(evar)
                if pid == "ollama":
                    from cliara.setup_wizard import _ollama_running
                    status = "running" if _ollama_running() else "not running"
                else:
                    status = "key set" if val else "no key"
                model = _PROVIDER_DEFAULT_MODELS.get(pid, "")
                active = "  <- active" if pid == current else ""
                print(f"    use {pid:<12}  {status:<12}  default model: {model}{active}")
            print()
            print_dim("  Example: use groq   or   use ollama")
            print()
            return

        target = provider_arg.lower().strip()

        if target not in _ENV_VAR_MAP:
            print_error(f"[Error] Unknown provider '{target}'. Options: {', '.join(_ENV_VAR_MAP)}")
            return

        if target == current:
            print_info(f"  Already using {target.upper()}.")
            return

        # Resolve credentials for the target provider
        if target == "ollama":
            from cliara.setup_wizard import _ollama_running
            base_url = self.config.get_ollama_base_url()
            if not _ollama_running(base_url):
                print_error(f"[Error] Ollama is not running at {base_url}.")
                print_dim("  Start Ollama, then run 'use ollama' again.")
                return
            _clear_incompatible_model(self)
            ok = self.nl_handler.initialize_llm("ollama", "ollama", base_url=base_url)
        else:
            api_key = os.getenv(_ENV_VAR_MAP[target])
            if not api_key:
                print_error(f"[Error] {_ENV_VAR_MAP[target]} is not set.")
                print_dim(f"  Add it to ~/.cliara/.env or run 'setup-llm' to configure {target}.")
                return
            if current == "ollama":
                _clear_incompatible_model(self)
            ok = self.nl_handler.initialize_llm(target, api_key)

        if ok:
            self.config.settings["llm_provider"] = target
            self.config._load_env_vars()
            # Clear incompatible stored model overrides (e.g. gemma4 on OpenAI).
            try:
                self.config._normalize_models_for_provider(target)
            except Exception:
                pass
            self.config.save()
            model = self.nl_handler._resolve_model("nl_to_commands")
            print_success(f"  Switched to {target.upper()}  (model: {model})")
        else:
            print_error(f"[Error] Failed to connect to {target}.")

    def _handle_cliara_login(self, auto_run: bool = False) -> bool:
        """Authenticate with the Cliara Cloud gateway via GitHub OAuth (PKCE).

        When auto_run is True (startup with no provider), uses a shorter prompt.
        Returns True if login succeeded and LLM is ready, False otherwise.
        """
        from rich import box
        from rich.panel import Panel
        from rich.text import Text

        from cliara.console import get_console

        _login_console = get_console()
        _login_console.print()
        if auto_run:
            _login_console.print(
                Panel(
                    Text.from_markup(
                        "[bold white]No AI provider yet.[/]\n\n"
                        "[dim]Next: sign in with GitHub for free Cliara Cloud "
                        "(150 queries/month, no card).[/]\n\n"
                        "[dim]Press[/] [bold]Ctrl+C[/] [dim]to skip and choose Groq, Gemini, or Ollama in the menu.[/]"
                    ),
                    title=Text("Cliara Cloud", style=_ui_accent_style()),
                    subtitle=Text.from_markup("[dim]Zero-friction setup[/]"),
                    border_style=_ui_accent_style(),
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        else:
            _login_console.print(
                Panel(
                    Text.from_markup(
                        "[dim]Free tier:[/] 150 queries/month · no credit card · GPT-4o-mini\n\n"
                        "[dim]A browser window will open for GitHub sign-in.[/]"
                    ),
                    title=Text("Cliara Login", style=_ui_accent_style()),
                    border_style=_ui_accent_style(),
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        _login_console.print()

        from cliara import auth as _auth
        try:
            result = _auth.login()
        except KeyboardInterrupt:
            print()
            print_warning("  Login cancelled.")
            return False
        except RuntimeError as exc:
            print()
            print_error(f"  [Error] {exc}")
            print_dim("  Try 'setup-llm' for BYOK options (Groq/Gemini are free).")
            return False

        # login() returns (access_token, email)
        if isinstance(result, tuple):
            token, email = result
        else:
            token, email = result, ""

        # Hot-swap the LLM client to start using the gateway immediately,
        # without requiring the user to restart Cliara.
        ok = self.nl_handler.initialize_llm("cliara", token)
        if ok:
            self.config.settings["llm_provider"] = "cliara"
            self.config.settings["llm_api_key"] = token
            # Clear incompatible stored model overrides from prior providers.
            try:
                self.config._normalize_models_for_provider("cliara")
            except Exception:
                pass
            self.config.save()

        print()
        if ok:
            user_label = f"  ({email})" if email else ""
            print_success(f"  Logged in to Cliara Cloud{user_label}")
            if not auto_run:
                print_success("  Free tier · 150 queries/month · resets monthly")
                print_dim("  Token saved to ~/.cliara/token.json  -  auto-loaded on every startup.")
                print_dim("  Run 'cliara logout' to sign out.")
        else:
            print_warning("  Logged in but could not connect to the gateway right now.")
            print_dim("  Your token is saved  -  it will be used automatically on the next start.")
        return ok

    def _handle_status(self):
        """Show auth and LLM status."""
        from cliara.auth import load_token, get_valid_token

        print()
        print_dim("  Cliara Status")
        print_dim("  ------------")
        print()

        token_data = load_token()
        if token_data and get_valid_token():
            email = token_data.get("email", "unknown")
            print_success(f"  Cliara Cloud: logged in ({email})")
            print_dim("  Free tier · 150 queries/month · resets monthly")
        elif self.nl_handler.llm_enabled and self.nl_handler.provider:
            byok = self.nl_handler.provider
            if self.nl_handler.provider == "ollama":
                byok = f"{byok} · {self.nl_handler.resolved_model_for_display()}"
            print_success(f"  BYOK: {byok}")
            print_dim("  Using your own API key")
        else:
            print_warning("  Not configured")
            print_dim("  Run 'cliara login' for Cloud, or 'setup-llm' for BYOK")
        print()

    def _handle_readme(self):
        """Generate README from project context, save to file for review, optionally apply."""
        if not self.nl_handler.llm_enabled:
            print_warning("  LLM not configured. Run 'cliara login' or 'setup-llm' to enable readme generation.")
            return

        cwd = Path.cwd()
        readme_path = cwd / "README.md"
        preview_path = cwd / "README.generated.md"

        print()
        print_dim("  Analyzing project...")
        generated = self.nl_handler.generate_readme(cwd=cwd)
        if not generated:
            print_error("  Could not generate README. Check LLM connection.")
            return

        _glow = generated.strip().lower()
        _bad_chat_markers = (
            "how can i help",
            "the request itself is missing",
            "please let me know what you would like",
            "<channel|>",
            "self-correction during drafting",
        )
        if not generated.lstrip().startswith("#") and any(m in _glow for m in _bad_chat_markers):
            print_warning(
                "  Model replied like a chat assistant instead of README markdown (common with some local models)."
            )
            print_dim(
                "  Try: `use openai` / Cliara Cloud, or another Ollama model; then run `readme` again."
            )

        existing = readme_path.read_text(encoding="utf-8", errors="replace") if readme_path.exists() else ""
        if existing.strip() == generated.strip():
            print_success("  README is already up to date.")
            return

        # Write generated README to .md file for review
        preview_path.write_text(generated, encoding="utf-8")
        print_success(f"  Preview saved to {preview_path.name}")

        # Open in current IDE window via URL protocol (opens in same window)
        abs_path = str(preview_path.resolve())
        path_url = abs_path.replace("\\", "/")
        opened = False
        for protocol in ("cursor", "vscode"):
            try:
                url = f"{protocol}://file/{path_url}"
                if platform.system() == "Windows":
                    os.startfile(url)
                elif platform.system() == "Darwin":
                    subprocess.run(["open", url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(["xdg-open", url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            try:
                if platform.system() == "Windows":
                    os.startfile(abs_path)
                elif platform.system() == "Darwin":
                    subprocess.run(["open", abs_path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(["xdg-open", abs_path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        print_dim("  Review the file, then apply to replace README.md")
        print()
        try:
            resp = input("  Apply? (y/n): ").strip().lower()
        except EOFError:
            resp = "n"
        if resp in ("y", "yes"):
            readme_path.write_text(generated, encoding="utf-8")
            print_success(f"  Wrote {readme_path}")
            try:
                preview_path.unlink()
            except OSError:
                pass
        else:
            print_dim("  Cancelled. Preview remains at " + str(preview_path.name))

    def _handle_cliara_logout(self):
        """Sign out of Cliara Cloud and clear the stored token."""
        from cliara import auth as _auth

        token_data = _auth.load_token()
        if token_data is None:
            print()
            print_warning("  Not currently logged in to Cliara Cloud.")
            print_dim("  Run 'cliara login' to sign in.")
            return

        email = token_data.get("email", "")
        _auth.logout()

        print()
        label = f" ({email})" if email else ""
        print_success(f"  Logged out of Cliara Cloud{label}.")
        print_dim("  Token deleted from ~/.cliara/token.json.")
        print_dim("  Run 'cliara login' to sign in again, or 'setup-llm' to configure a BYOK provider.")

        # If we were using the cliara gateway, clear it out so the REPL
        # doesn't keep trying to call a gateway with no valid credentials.
        if self.nl_handler.provider == "cliara":
            self.nl_handler.llm_enabled = False
            self.nl_handler.llm_client = None
            self.nl_handler.provider = None
            self.config.settings["llm_provider"] = None
            self.config.settings["llm_api_key"] = None
            print_dim("  LLM disabled. Run 'setup-llm' to configure a provider.")

    def _handle_theme_command(self, arg: str):
        """Show scrollable theme picker (up/down to select, Enter to apply) or set theme by name."""
        from cliara.highlighting import list_themes, get_style_for_theme
        themes = list_themes()
        current = self.config.get("theme") or "dracula"
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
        from cliara.console import set_ui_theme
        from cliara.highlighting import get_theme_preview_markup, get_tips_panel_styles
        from rich.panel import Panel
        from rich.text import Text
        self.config.set("theme", name)
        set_ui_theme(name)
        session = self._create_prompt_session()
        if session is not None:
            self._prompt_session = session
        console = _cliara_console()
        tp = get_tips_panel_styles(name)
        try:
            from rich.markup import escape as _rich_esc
            markup = get_theme_preview_markup(name)
            title = (
                f"[{tp['title_brand']}]Theme applied[/] "
                f"[{tp['title_sep']}] - [/] "
                f"[{tp['title_tagline']}]{_rich_esc(name)}[/]"
            )
            console.print(Panel(
                Text.from_markup(markup, overflow="fold"),
                title=Text.from_markup(title, overflow="fold"),
                border_style=tp.get("border", "green"),
                padding=(0, 1),
            ))
        except Exception:
            console.print(f"[green]{icons.OK} Theme set to '{name}'.[/green]")
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
            print_info("[Cliara] Color themes  -  type a name to set")
            for name in themes:
                mark = " (active)" if name == current else ""
                print_dim(f"  {name}{mark}")
            try:
                choice = input("\nTheme name (Enter to cancel): ").strip().lower()
                return choice if choice in themes else None
            except (EOFError, KeyboardInterrupt):
                return None

        # "?"? Rich header panel "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        console = _cliara_console()
        console.print()
        console.print(Panel(
            "[bold] ->  /  - [/bold]  navigate   [bold]Enter[/bold]  select   [bold]Escape[/bold]  cancel",
            title=f"[{_ui_accent_style()}]o Theme Selector[/]",
            border_style=_ui_accent_style(),
            padding=(0, 2),
        ))
        console.print()

        # "?"? State "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        selected_index = [themes.index(current) if current in themes else 0]
        n = len(themes)
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        swatch = "██████" if ("utf" in enc or "65001" in enc) else "******"

        def _fg(theme_name: str) -> str:
            """Return the plain ANSI fg color for a theme (strips 'bold')."""
            ps = _THEMES.get(theme_name, _THEMES["dracula"])["prompt_style"]
            return ps["prompt-name"].replace("bold", "").strip()

        # "?"? Live list renderer "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
        def get_rows():
            rows = []
            for i, name in enumerate(themes):
                is_sel = i == selected_index[0]
                is_cur = name == current
                fg = _fg(name)
                bg = "bg:ansibrightblack " if is_sel else ""

                rows.append((f"{bg}fg:ansiwhite bold" if is_sel else "", " ❯ " if is_sel else "   "))
                rows.append((f"{bg}fg:{fg} bold", swatch))
                rows.append((f"{bg}bold" if is_sel else f"fg:{fg}", f" {name:<13}"))
                if is_cur:
                    rows.append((f"{bg}fg:ansiyellow bold", f" {icons.OK} active"))
                rows.append(("", "\n"))
            return rows

        list_control = FormattedTextControl(text=get_rows)

        footer_text = [
            ("fg:ansibrightblack", "  "),
            ("fg:ansicyan bold", " -> "),
            ("fg:ansibrightblack", "/"),
            ("fg:ansicyan bold", " - "),
            ("fg:ansibrightblack", " move   "),
            ("fg:ansicyan bold", "Enter"),
            ("fg:ansibrightblack", " select   "),
            ("fg:ansicyan bold", "Esc"),
            ("fg:ansibrightblack", " cancel\n"),
        ]

        # "?"? Key bindings "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
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

        # "?"? Layout "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?
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
        "cd -" switches to the previous working directory (bash/zsh style).
        """
        args = user_input[2:].strip()
        if args == '-':
            if self._prev_cwd is None:
                print_error("[Error] cd -: no previous directory")
                return
            target = Path(self._prev_cwd)
            self._prev_cwd = str(Path.cwd())
        else:
            if not args:
                target = Path.home()
            else:
                target = Path(args).expanduser()
            self._prev_cwd = str(Path.cwd())

        try:
            os.chdir(target)
            try:
                store = getattr(self, "_jump_store", None)
                if store is not None:
                    store.record_visit(Path.cwd(), persist=True)
            except Exception:
                pass
        except FileNotFoundError:
            print_error(f"[Error] cd: no such directory: {args}")
        except PermissionError:
            print_error(f"[Error] cd: permission denied: {args}")
        except Exception as e:
            print_error(f"[Error] cd: {e}")

    def handle_jump(self, query: str = ""):
        """Jump to a learned directory (zoxide-style).

        Usage:
          jump <query>   Jump to best matching directory
          jump           Jump to your top directory
        """
        store = getattr(self, "_jump_store", None)
        if store is None:
            print_error("[Error] jump store not initialized")
            return

        q = (query or "").strip()

        # No query: jump to most-used/most-recent top.
        if not q:
            top = store.top(limit=1)
            if top:
                self._handle_cd(f"cd {top[0].path}")
                return
            print_warning("[No matches] jump")
            return

        # Path shortcut: if they provided a real directory, just cd there.
        try:
            p = Path(q).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p)
            if p.exists() and p.is_dir():
                self._handle_cd(f"cd {str(p)}")
                return
        except Exception:
            pass

        # Exact subfolder scan: jump to the best matching directory path with
        # trailing components exactly equal to the query segments.
        try:
            from cliara.shell_app.jump import find_best_exact_subdir

            cwd = Path.cwd()
            root_str = _get_project_root(cwd)
            root = Path(root_str) if root_str else cwd
            roots = [cwd]
            if str(root) != str(cwd):
                roots.append(root)

            exact = find_best_exact_subdir(q, roots=roots)
            if exact:
                self._handle_cd(f"cd {exact}")
                return
        except Exception:
            pass

        # Learned fallback: auto-jump only when the top match is confident.
        candidates = store.search(q, limit=2)
        if candidates:
            top = candidates[0]
            second = candidates[1] if len(candidates) > 1 else None
            second_match = second.match if second else 0
            if top.match >= 88 and (second is None or (top.match - second_match) >= 10):
                self._handle_cd(f"cd {top.path}")
                return

        print_warning(f"[No matches] jump {q}")

    def _handle_doctor(self):
        """Run setup health check: shell, LLM, macros, history, semantic history, config."""
        console = _cliara_console()
        print_info("\n  System check:")
        # Shell
        shell_path = self.shell_path or (os.environ.get("SHELL") if platform.system() != "Windows" else os.environ.get("COMSPEC", "?"))
        if shell_path:
            print_success(f"  {icons.OK} Shell: {shell_path}")
        else:
            console.print(f"  {icons.FAIL} Shell: not detected", style="red")
        # LLM
        if self.nl_handler.llm_enabled:
            key = self.config.get_llm_api_key()
            masked = f" (...{key[-4:]})" if key and len(key) >= 4 else " (configured)"
            model_bit = ""
            if self.nl_handler.provider == "ollama":
                model_bit = f" · {self.nl_handler.resolved_model_for_display()}"
            print_success(
                f"  {icons.OK} LLM: {self.nl_handler.provider or '?'}{model_bit}{masked}"
            )
        else:
            console.print(f"  {icons.FAIL} LLM: not configured (run setup-llm)", style="red")
        # Macros
        macro_path = Path(self.config.get("macro_storage", "~/.cliara/macros.json")).expanduser()
        try:
            n = self.macros.count()
            print_success(f"  {icons.OK} Macros: {n} loaded  ({macro_path})")
        except Exception:
            console.print(f"  {icons.FAIL} Macros: error loading ({macro_path})", style="red")
        # History
        count = len(self.history.history)
        print_success(f"  {icons.OK} History: {count} entries")
        # Semantic history embeddings
        use_emb = self.config.get("semantic_history_use_embeddings", False)
        if use_emb:
            print_success(f"  {icons.OK} Semantic history: embeddings enabled")
        else:
            console.print(f"  {icons.FAIL} Semantic history: embeddings disabled (set semantic_history_use_embeddings: true)", style="red")
        # Config
        cfg_path = self.config.config_file
        if cfg_path and cfg_path.exists():
            print_success(f"  {icons.OK} Config: {cfg_path}")
        else:
            print_success(f"  {icons.OK} Config: {cfg_path} (defaults)")
        print()

    def _persist_last_command(self):
        """Write last executed command to disk so 'last' works after restart."""
        if not self.last_command:
            return
        path = self.config.config_dir / "last_command.txt"
        try:
            self.config.config_dir.mkdir(parents=True, exist_ok=True)
            with with_file_lock(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.last_command)
        except Exception:
            pass

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
    # Smart Push  -  auto-commit-message + branch detection
    # ------------------------------------------------------------------
    def handle_push(self):
        """
        Built-in smart push: detect branch, stage changes, generate a
        conventional commit message via LLM, commit, and push.
        """
        # "?"? 1. Are we in a git repo? "?"?
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print_error("[Cliara] Not inside a git repository.")
            return

        # "?"? 2. Current branch "?"?
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        branch = (result.stdout or "").strip()
        if not branch:
            print_error("[Cliara] Detached HEAD state  -  checkout a branch first.")
            return

        # "?"? 3. Anything to commit? "?"?
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        status_output = (result.stdout or "").strip()

        if not status_output:
            # Nothing to commit  -  maybe there are unpushed commits?
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

        # "?"? 4. Show what changed "?"?
        print_info(f"\n[Cliara] Changes detected on '{branch}':\n")
        # Coloured status from git
        subprocess.run(["git", "-c", "color.status=always", "status", "--short"])

        # "?"? 5. Stage everything "?"?
        print_dim("\nStaging all changes...")
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )

        # "?"? 5b. Secret scan on staged files "?"?
        if self.config.get("secret_scan_on_push", True):
            scan_ok = self._run_secret_scan()
            if not scan_ok:
                self._unstage_all()
                return

        # "?"? 6. Gather diff for message generation "?"?
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

        # "?"? 7. Generate commit message with animated spinner "?"?
        # Don't stream to stdout — the message will be displayed formatted in a
        # confirmation panel below. Showing the same text twice (raw stream +
        # panel) looks redundant for a one-line output.
        _commit_anim = StreamingThinkingAnimation().start()

        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
            "branch": branch,
        }
        commit_msg = self.nl_handler.generate_commit_message(
            diff_stat, diff_content, files, context,
            stream_callback=_commit_anim.wrap(print_to_stdout=False),
        )
        _commit_anim.stop()
        if not commit_msg or not commit_msg.strip():
            print_error("[Cliara] Could not generate commit message. Try again or use: git commit -m \"your message\"")
            self._unstage_all()
            return

        # "?"? 8. Show message and confirm "?"?
        # Always print the commit message so it's visible even when streaming
        # output was buffered or didn't display (e.g. rapid successive runs)
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

        # "?"? 9. Commit (use subprocess list form to safely handle quotes) "?"?
        print()
        proc = subprocess.run(
            ["git", "commit", "-m", commit_msg],
        )
        if proc.returncode != 0:
            print_error("[Cliara] Commit failed.")
            return

        # "?"? 10. Push "?"?
        # Check if the remote branch already exists
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if (result.stdout or "").strip():
            success = self.execute_shell_command(f"git push origin {branch}")
        else:
            print_dim(f"Branch '{branch}' is new on remote  -  setting up tracking...")
            success = self.execute_shell_command(
                f"git push -u origin {branch}"
            )

        if success:
            print_success(f"\n[Cliara] Successfully pushed to '{branch}'!")

    # ------------------------------------------------------------------
    # Prune branches  -  delete merged local branches + prune remotes
    # ------------------------------------------------------------------
    def handle_prune_branches(self) -> None:
        """Delete merged local branches and prune stale remote-tracking branches."""
        import sys
        from cliara.shell_app.prune_branches import parse_selection_spec

        if not sys.stdin.isatty():
            print_error("[Cliara] 'prune branches' requires an interactive terminal.")
            return

        # 1) Must be in a git repo
        if _git_run(["git", "rev-parse", "--is-inside-work-tree"]).returncode != 0:
            print_error("[Cliara] Not inside a git repository.")
            return

        # 2) Determine remotes / default remote
        remotes = [r for r in (_git_run(["git", "remote"]).stdout or "").splitlines() if r.strip()]
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "")

        # 3) Determine a safe base ref to check merges against
        base_ref, base_label = self._git_detect_default_base(remote)

        # 4) Fetch/prune remote refs (non-destructive; improves accuracy)
        if remote:
            print_dim(f"Fetching latest refs from '{remote}'...")
            subprocess.run(["git", "fetch", remote, "--prune"], check=False)

        current_branch = ( _git_run(["git", "branch", "--show-current"]).stdout or "").strip()
        if not current_branch:
            print_error("[Cliara] Detached HEAD state  -  checkout a branch first.")
            return

        branches = self._git_list_local_branches()
        candidates = [
            b
            for b in branches
            if b["name"]
            and b["name"] != current_branch
            and b["name"] != base_label
            and not self._git_branch_is_protected(b["name"])
            and self._git_is_merged_into(b["name"], base_ref)
        ]

        from rich import box
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich.prompt import Prompt, Confirm

        console = _cliara_console()

        if not candidates:
            print_info(f"[Cliara] No merged local branches found to delete (base: {base_label}).")
            if remote and Confirm.ask("Prune remotes (remove stale remote-tracking branches)?", default=True):
                self._git_prune_remotes(remotes)
            return

        # UI: show a numbered table
        table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style=_ui_accent_style(),
            border_style=_ui_accent_style(),
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("#", style="dim", justify="center", width=3)
        table.add_column("Branch", style="bold white", no_wrap=True)
        table.add_column("Upstream", style=_ui_accent_style(), no_wrap=True)
        table.add_column("Last commit", min_width=18)
        table.add_column("When", style="dim", width=16, no_wrap=True)

        for i, b in enumerate(candidates, 1):
            subj = (b.get("subject") or "").strip()
            subj_t = Text(subj if subj else "(no subject)", style="white")
            if b.get("sha"):
                subj_t.append(f"  ({b['sha']})", style="dim")
            table.add_row(
                str(i),
                b["name"],
                b.get("upstream") or "",
                subj_t,
                b.get("when") or "",
            )

        panel = Panel(
            table,
            title=Text.from_markup(f"[bold white]Prune branches[/] [dim]·[/] [{_ui_accent_style()}]base[/] {base_label}"),
            border_style=_ui_accent_style(),
            box=box.ROUNDED,
            padding=(0, 1),
        )
        console.print()
        console.print(panel)
        console.print()
        print_dim("Select branches to delete: 'all' or '1-3,5' (Enter to cancel).")

        spec = Prompt.ask("Delete", default="")
        picks = parse_selection_spec(spec, max_index=len(candidates))
        if not picks:
            print_warning("[Cancelled]")
            return

        to_delete = [candidates[i] for i in picks]
        names = [b["name"] for b in to_delete]

        console.print()
        console.print(Panel(
            "\n".join(f"- {n}" for n in names),
            title="Will delete",
            border_style=_ui_accent_style(),
            box=box.ROUNDED,
        ))
        console.print()

        if not Confirm.ask(f"Delete {len(names)} local branch(es)?", default=False):
            print_warning("[Cancelled]")
            return

        deleted: List[str] = []
        failed: List[Tuple[str, str]] = []
        for n in names:
            r = _git_run(["git", "branch", "-d", n])
            if r.returncode == 0:
                deleted.append(n)
            else:
                err = (r.stderr or r.stdout or "").strip()
                failed.append((n, err or "Failed"))

        console.print()
        if deleted:
            print_success(f"[Cliara] Deleted {len(deleted)} branch(es).")
        if failed:
            print_warning(f"[Cliara] Could not delete {len(failed)} branch(es).")
            for n, msg in failed:
                print_dim(f"  - {n}: {msg}")

        # Remotes prune (stale remote-tracking branches)
        if remotes and Confirm.ask("Prune remotes (remove stale remote-tracking branches)?", default=True):
            self._git_prune_remotes(remotes)

    def _unstage_all(self):
        """Reset the staging area (undo git add -A)."""
        subprocess.run(["git", "reset"], capture_output=True)

    # ------------------------------------------------------------------
    # Pre-push secret scan
    # ------------------------------------------------------------------

    def _run_secret_scan(self) -> bool:
        """Run detect-secrets on staged files via pre-commit.

        Returns True → safe to push.
        Returns False → secrets found; push is blocked (staged changes are
                        left intact so the user can inspect them).

        Disable: config set secret_scan_on_push false
        Bypass a line: add  # cliara-noscan  as an inline comment.
        """
        from cliara.secret_scan import scan, BYPASS_COMMENT

        console = _cliara_console()
        repo_root = Path.cwd()

        print()
        with thinking_status("Scanning staged files for secrets..."):
            result = scan(repo_root=repo_root, auto_install_precommit=True)

        # ── Clean ────────────────────────────────────────────────────
        if result.passed:
            if result.new_config_created:
                print_dim("  [Scan] Created .cliara-secret-scan.yaml")
            engine = "pre-commit + inline" if result.precommit_used else "inline"
            n = len(result.findings)
            if n:
                print_success(
                    f"  Secret scan passed ({engine}) — "
                    f"{n} finding(s) acknowledged with # {BYPASS_COMMENT}"
                )
            else:
                print_success(f"  Secret scan passed ({engine}) — no secrets found")
            return True

        # ── Blocked ──────────────────────────────────────────────────
        blocked  = [f for f in result.findings if not f.bypassed]
        bypassed = [f for f in result.findings if f.bypassed]

        from rich.panel import Panel
        from rich.text import Text
        from rich.table import Table
        from rich import box

        tbl = Table(
            box=box.SIMPLE, show_header=True, header_style="bold red",
            padding=(0, 1), show_lines=False,
        )
        tbl.add_column("File",    style="bold white", overflow="fold")
        tbl.add_column("Line",    style="dim",        justify="right", no_wrap=True)
        tbl.add_column("Type",    style="yellow",     no_wrap=True)
        tbl.add_column("Content", style="dim",        overflow="fold")
        tbl.add_column("",        justify="center",   no_wrap=True)

        for f in result.findings:
            status  = "✓ bypassed" if f.bypassed else "[bold red]✗ BLOCKED[/bold red]"
            snippet = f.line_content[:72] + ("…" if len(f.line_content) > 72 else "")
            label   = (f.pattern_label or "secret")[:28]
            tbl.add_row(f.file, str(f.line_number), label, snippet, status)

        console.print()
        console.print(
            Panel(
                tbl,
                title=Text(
                    f"  Secret scan blocked {len(blocked)} finding(s)  ", style="bold red"
                ),
                subtitle=Text("push aborted — fix secrets or add bypass comment", style="dim"),
                border_style="red",
                padding=(0, 1),
            )
        )

        console.print()
        if bypassed:
            print_dim(f"  {len(bypassed)} finding(s) already bypassed with # {BYPASS_COMMENT}")
        print_error(
            f"  Add  [bold]# {BYPASS_COMMENT}[/bold]  to the end of any line you've "
            "intentionally reviewed as safe, then re-run push."
        )
        print_dim(f"  Example:  API_KEY = 'sk-proj-...'  # {BYPASS_COMMENT}") #pragma: allowlist secret
        print_dim( "  Disable scanning:  config set secret_scan_on_push false")

        if result.precommit_output:
            low = result.precommit_output.lower()
            if "failed" in low or "error" in low:
                print_dim(f"\n  pre-commit output:\n{result.precommit_output[:400]}")

        console.print()
        return False

    def _git_detect_default_base(self, remote: str) -> Tuple[str, str]:
        """Return (base_ref, base_label).

        base_ref is a commit-ish used for merge checks; base_label is a human label.
        """
        # Prefer remote HEAD, e.g. refs/remotes/origin/main
        if remote:
            r = _git_run(["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"])
            ref = (r.stdout or "").strip()
            if r.returncode == 0 and ref.startswith(f"refs/remotes/{remote}/"):
                name = ref.split(f"refs/remotes/{remote}/", 1)[-1]
                # Use origin/<name> if available, else fall back to local <name>
                if _git_ok(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{name}"]):
                    return f"{remote}/{name}", name
                if _git_ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]):
                    return name, name

        # Fallback common branch names
        for name in ("main", "master", "develop"):
            if remote and _git_ok(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{name}"]):
                return f"{remote}/{name}", name
            if _git_ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]):
                return name, name

        # Last resort: whatever HEAD is right now
        return "HEAD", "HEAD"

    def _git_list_local_branches(self) -> List[Dict[str, str]]:
        """Return local branches with minimal metadata for UI."""
        fmt = "%(refname:short)\t%(objectname:short)\t%(committerdate:iso8601)\t%(upstream:short)\t%(subject)"
        r = _git_run(["git", "for-each-ref", "refs/heads", f"--format={fmt}"])
        out: List[Dict[str, str]] = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t", 4)
            if not parts:
                continue
            name = parts[0].strip() if len(parts) > 0 else ""
            sha = parts[1].strip() if len(parts) > 1 else ""
            when = parts[2].strip() if len(parts) > 2 else ""
            upstream = parts[3].strip() if len(parts) > 3 else ""
            subject = parts[4].strip() if len(parts) > 4 else ""
            out.append(
                {
                    "name": name,
                    "sha": sha,
                    "when": when,
                    "upstream": upstream,
                    "subject": subject,
                }
            )
        return out

    def _git_is_merged_into(self, branch: str, base_ref: str) -> bool:
        """True if branch tip is an ancestor of base_ref."""
        if branch == base_ref:
            return False
        return _git_ok(["git", "merge-base", "--is-ancestor", branch, base_ref])

    @staticmethod
    def _git_branch_is_protected(name: str) -> bool:
        low = (name or "").strip().lower()
        if low in {"main", "master", "develop", "dev", "release"}:
            return True
        if low.startswith("release/") or low.startswith("hotfix/"):
            return True
        return False

    def _git_prune_remotes(self, remotes: List[str]) -> None:
        if not remotes:
            return
        for r in remotes:
            print_dim(f"Pruning remote '{r}'...")
            subprocess.run(["git", "remote", "prune", r], check=False)
        print_success("[Cliara] Remotes pruned.")

    # ------------------------------------------------------------------
    # Task sessions  -  named, resumable workflow context
    # ------------------------------------------------------------------

    @staticmethod
    def _expand_session_shortcut(user_input: str) -> Optional[str]:
        """
        Map ss -> session start, se -> session end.
        Returns session subcommand string, or None to run input as a normal shell command.
        System ``ss`` with flags (e.g. ``ss -tuln``) is not hijacked.
        """
        s = user_input.strip()
        if not s:
            return None
        low = s.lower()
        if low == "se" or low.startswith("se "):
            rest = s[2:].strip()
            return ("end " + rest).strip() if rest else "end"
        if low == "ss" or low.startswith("ss "):
            if low == "ss":
                return "start"
            tail = s[2:].strip()
            first = tail.split(None, 1)[0] if tail else ""
            if first.startswith("-"):
                return None
            return ("start " + tail).strip() if tail else "start"
        return None


    def _stream_callback_for_console(self):
        """Return a callable that prints each streamed LLM chunk to the console and flushes stdout."""
        def callback(chunk: str) -> None:
            _cliara_console().print(chunk, end="")
            sys.stdout.flush()
        return callback

    def _format_history_ts(self, ts: Optional[float]) -> str:
        """Format timestamp for history: 'today 14:32', 'yesterday 14:32', or 'M/D HH:MM'."""
        if ts is None:
            return ""
        try:
            dt = datetime.fromtimestamp(ts)
            now = datetime.now()
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if dt >= today:
                return dt.strftime("today %H:%M")
            if dt >= today - timedelta(days=1):
                return dt.strftime("yesterday %H:%M")
            return dt.strftime("%-m/%-d %H:%M") if platform.system() != "Windows" else dt.strftime("%#m/%#d %H:%M")
        except Exception:
            return ""

    def _print_clear_status_line(self):
        """After clear/cls, show a contextual one-liner: version, LLM, path, session or macros + hint."""
        from cliara import __version__
        parts = [f"{icons.INFO} cliara {__version__}"]
        if self.nl_handler.llm_enabled:
            if self.nl_handler.provider == "ollama":
                parts.append(
                    f"{self.nl_handler.provider} · {self.nl_handler.resolved_model_for_display()} ready"
                )
            else:
                parts.append(f"{self.nl_handler.provider} ready")
        else:
            parts.append("no LLM")
        cwd = _fmt_path(str(Path.cwd()))
        parts.append(cwd)
        if self.current_session:
            parts.append(f"session: {self.current_session.name}")
        else:
            try:
                n = self.macros.count()
                parts.append(f"{n} macros")
            except Exception:
                parts.append("macros")
            nl = self.config.get("nl_prefix", "?")
            parts.append(f"{nl} to ask, help for all commands")
        print_dim("  " + "  ·  ".join(parts))

    def _print_exit_message(self):
        """Styled exit message: 2 lines, plus session resume hint if a session is active."""
        self._flush_semantic_history()
        console = _cliara_console()
        console.print()
        console.print("[dim]Session ended. See you next time.[/dim]")
        if self.current_session:
            console.print(
                f"[dim]Session '{self.current_session.name}' is saved  -  "
                f"resume with 'session resume {self.current_session.name}'[/dim]"
            )

    def _print_empty_nl_suggestions(self, nl_prefix: str):
        """When user types ? with no query, show three context-aware prompt suggestions."""
        suggestions = []
        if self.last_command and self.last_exit_code != 0:
            suggestions.append(f"{nl_prefix} explain last")
        elif self.last_command:
            short = self.last_command if len(self.last_command) <= 45 else self.last_command[:42] + "..."
            suggestions.append(f"{nl_prefix} explain {short}")
        if self.current_session:
            suggestions.append(f"{nl_prefix} what was I doing with git last session")
        static_tips = [
            f"{nl_prefix} kill whatever is on port 8080",
            f"{nl_prefix} list all python files modified today",
            f"{nl_prefix} find when I last ran tests",
        ]
        while len(suggestions) < 3:
            pick = random.choice(static_tips)
            if pick not in suggestions:
                suggestions.append(pick)
        print_dim("Try:")
        for s in suggestions[:3]:
            print_dim(f"      {s}")

    def _handle_clear_command_history(self) -> None:
        """Clear persisted command history (~/.cliara/history.txt) and in-session recall."""
        n = len(self.history.history)
        self.history.clear_all()
        if self._prompt_session is not None:
            try:
                self._prompt_session = self._create_prompt_session()
            except Exception:
                pass
        print_success(
            f"  {icons.OK} Cleared command history"
            + (f" ({n} entries removed)." if n else ".")
        )

    def handle_history(self, arg: str = ""):
        """
        Show recent command history with exit codes, timestamps, and syntax highlighting.
        Usage: history [N]. Default: last 20 commands. Use ``history clear`` to wipe history.
        """
        if (arg or "").strip().lower() == "clear":
            self._handle_clear_command_history()
            return

        default_n = 20
        max_n = min(500, self.config.get("history_size", 1000))
        n = default_n
        if arg:
            try:
                n = int(arg.strip())
                n = max(1, min(n, max_n))
            except ValueError:
                print_error("[Error] history expects a number, 'clear', or no argument")
                print_dim("Usage: history   or   history 10   or   history clear")
                return
        rows = self.history.get_recent_with_meta(n)
        if not rows:
            print_dim("No command history yet.")
            return
        # Build colorized table: index, OK/X, command (syntax-highlighted), timestamp, [exit N]
        from rich.table import Table
        from rich.syntax import Syntax
        from cliara.highlighting import ShellLexer

        # Use Pygments theme by name so Rich gets a full Style (with style_for_token)
        theme_name = (self.config.get("theme") or "dracula").strip().lower()
        _pygments_theme_map = {
            "solarized": "solarized-dark",
            # light theme = white/snow on dark  -  use a dark Pygments bg for history Syntax
            "light": "native",
            "nord": "dracula",
            "catppuccin": "dracula",
        }
        pygments_theme = _pygments_theme_map.get(theme_name, theme_name)
        console = _cliara_console()
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style="dim", width=5)   # index
        table.add_column(style="dim", width=2)   # OK/X
        table.add_column(min_width=20)           # command
        table.add_column(style="dim", justify="right", width=18)  # timestamp
        table.add_column(style="dim", width=10) # [exit N] for failures

        # Show 1 = most recent, 2 = second most recent, ... (clearer than global index)
        for i, (cmd, exit_code, ts) in enumerate(reversed(rows), 1):
            num_str = f"  {i}"
            if exit_code == 0:
                icon = f"[dim green]{icons.OK}[/]"
            elif exit_code is not None:
                icon = f"[dim red]{icons.FAIL}[/]"
            else:
                icon = " "
            syntax = Syntax(cmd, lexer=ShellLexer(), theme=pygments_theme)
            ts_str = self._format_history_ts(ts)
            exit_str = f"[red][exit {exit_code}][/]" if (exit_code is not None and exit_code != 0) else ""
            table.add_row(num_str, icon, syntax, ts_str, exit_str)
        print_info(f"\nLast {len(rows)} command(s):\n")
        console.print(table)
        print()

    def _handle_lint(self, command: str):
        """
        Lint a command: show AI explanation + diff preview (if any), then ask to run.
        Like a dry run  -  explain before running.
        """
        context = {
            "cwd": str(Path.cwd()),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }
        explanation = self.nl_handler.explain_command(command, context, stream_callback=None)
        one_line = (explanation or "").strip().split("\n")[0].strip()
        if len(one_line) > 200:
            one_line = one_line[:197] + "..."
        print_warning(f" ->  {icons.WARN}  {one_line}")
        preview = self.diff_preview.generate_preview(command)
        if preview:
            for line in preview.strip().split("\n"):
                print_dim(f" ->  {line.strip()}")
        try:
            response = input(" ->  Run it anyway? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print_warning("  [Cancelled]")
            return
        if response in ("y", "yes"):
            self.execute_shell_command(command, capture=False)
        else:
            print_warning("  [Cancelled]")

    def handle_explain(self, command: str, offer_run: bool = True):
        """
        Explain a shell command in plain English using the LLM.

        Args:
            command: The shell command to explain (e.g. "git rebase -i HEAD~3")
            offer_run: If False, skip the "Run this command?" prompt (e.g. after
                ``explain last`` when output was empty and we fall back to the command line).
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

        # Display the explanation with a nice header/footer (skip body when streamed  -  already shown)
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
            embedding = None
            if self.config.get("semantic_history_use_embeddings", False):
                emb_text = f"{command.strip()} {one_line}".strip()
                embedding = self.nl_handler.get_embedding(emb_text)
            self._semantic_history.update_summary_for_command(
                command.strip(),
                one_line,
                str(Path.cwd()),
                embedding=embedding,
            )

        # Offer to run the command (skip when explaining something already executed)
        if offer_run:
            print()
            run = (safe_input("Run this command? (y/n): ") or "").lower()
            if run in ['y', 'yes']:
                # Safety check first
                level, dangerous = self.safety.check_commands([command])
                if level != DangerLevel.SAFE:
                    _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
                    prompt = self.safety.get_confirmation_prompt(level)
                    response = safe_input(prompt) or ""
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
        print_dim("  " + "-" * 38)
        print_dim("  Just type any command  -  it passes through to your shell")
        print()
        print_help_example("ls, cd, git status, npm install", label="Examples")
        print()

        print_info("  Ambient")
        print_dim("  " + "-" * 38)
        print_help_cmd("pulse", "Explain the prompt pulse glyph")
        print()
        print_help_example("pulse")
        print()

        print_info("  Natural Language")
        print_dim("  " + "-" * 38)
        if self.nl_handler.llm_enabled:
            print_help_cmd(f"{nl} <query>", "Use natural language")
        else:
            print_help_cmd(f"{nl} <query>", "Use natural language (requires API key)")
        print_help_cmd(f"{nl} <query> --save-as <n>", "Generate & save as macro")
        print()
        print_help_example(f"{nl} kill process on port 3000")
        print()

        print_info("  Explain & Lint")
        print_dim("  " + "-" * 38)
        print_help_cmd("explain <command>", "Plain-English explanation of any command")
        print_help_cmd(
            "explain last",
            "Last run: command + output + exit code (one explanation)",
        )
        print_help_cmd(f"{nl} explain last", "Same as explain last")
        print_help_cmd(
            "lint <command>",
            "Explain + show impact, then ask to run (dry run)",
        )
        print()
        print_help_example("explain git rebase -i HEAD~3")
        print_help_example("lint find . -name '*.py' -exec rm {} \\;")
        print()

        print_info("  Semantic History Search")
        print_dim("  " + "-" * 38)
        print_help_cmd(f"{nl} find <what>", "Search past commands by meaning")
        print_help_cmd(f"{nl} when did I ...", "e.g. when did I fix the login bug")
        print_help_cmd(f"{nl} what did I run ...", "e.g. what did I run to deploy last time")
        print_dim("  Requires LLM; uses stored summaries of your commands.\n")

        print_info("  Codebase RAG")
        print_dim("  " + "-" * 38)
        print_help_cmd("index", "Index git-tracked files into a local vector store")
        print_help_cmd("index rebuild", "Full re-index (e.g. after switching models)")
        print_help_cmd("index status", "Show files/chunks indexed and embedding model")
        print_help_cmd("index clear", "Delete the index for this repo")
        print_help_cmd("ask <question>", "Answer from the code, with file:line citations")
        print_help_cmd(f"{nl} how does <X> work", "Same, when an index exists")
        print()
        print_help_example("index")
        print_help_example("? how does auth work")
        print_dim("  Incremental: re-running `index` only re-embeds changed files.\n")

        print_info("  Macros")
        print_dim("  " + "-" * 38)
        print_dim("  Short commands are the default; macro ... does the same with full words (e.g. macro list = ml).")
        print_help_cmd("mc [description]", "Create from English  -  suggested name + steps")
        print_help_cmd("ma <name>", "Add macro (line-by-line commands)")
        print_help_cmd("ma <name> --nl", "Keep name; steps from English")
        print_help_cmd("ma --nl", "Same as mc")
        print_help_cmd("ml [--tag <tag>]", "List macros  (filter by tag)")
        print_help_cmd("mr <name>", "Run a macro")
        print_help_cmd("ms <name>", "Save last run as macro")
        print_help_cmd("m <sub> [args]", "Passthrough  -  same as macro <sub> ...")
        print_help_cmd("<macro-name>", "Run  -  type the saved name alone")
        print_dim("  More commands: type mh in the shell (mst, msh, msr, mch, mrn, me, md).\n")
        print()

        print_info("  Quick Fix")
        print_dim("  " + "-" * 38)
        print_dim("  When a command fails, Cliara automatically shows a fix hint:")
        print_dim("    hint: try 'python3 script.py' (Tab to use)")
        print_dim("  Press Tab on an empty prompt to fill in the fix, then Enter.")
        print_help_cmd(f"{nl} fix", "Full interactive diagnosis")
        print()

        print_info("  Smart Push")
        print_dim("  " + "-" * 38)
        print_help_cmd("push", "Stage, scan for secrets, auto-commit, and push")
        print_dim("  Runs detect-secrets on staged files before committing.")
        print_dim("  Bypass a line: add  # cliara-noscan  inline.")
        print_dim("  Disable scan: config set secret_scan_on_push false")
        print_help_cmd("secret-scan", "Scan staged files for secrets on demand")
        print_dim("  (feat:, fix:, docs:, ...) from the diff. Accept, edit, or cancel.\n")

        print_info("  Prune Branches")
        print_dim("  " + "-" * 38)
        print_help_cmd("prune branches", "Delete merged local branches + prune remotes")
        print_dim("  Shows a numbered list; pick 'all' or ranges like 1-3,5.\n")

        print_info("  Task Sessions")
        print_dim("  " + "-" * 38)
        print_help_cmd(
            "ss <name> [ -- <intent>]",
            "Start a task (shortcut for session start)",
            pad_to=36,
        )
        print_help_cmd("session resume <name>", "Resume and see summary + next step")
        print_help_cmd("se [note]", "End session (shortcut)")
        print_help_cmd("se --reflect", "End with closeout prompts")
        print_help_cmd(
            "session list / show / note",
            "List, show, or add notes",
            pad_to=36,
        )
        print_help_cmd(
            "session snapshot --chat [name]",
            "Copy session for Copilot/Cursor",
            pad_to=36,
        )
        print_dim("  Sessions persist across terminal closes  -  resume anytime.\n")

        print_info("  Copilot / Cursor")
        print_dim("  " + "-" * 38)
        print_help_cmd(
            "chat copy",
            "Copy last-run markdown (cwd, exit, stderr) to clipboard",
        )
        print_help_cmd(
            "chat polish",
            "Optional: LLM-compress clipboard (chat_polish_enabled)",
        )
        print_help_cmd(
            "last / retry",
            "Re-run the last shell command (skip Copilot Gate)",
        )
        print()

        print_info("  Smart Deploy")
        print_dim("  " + "-" * 38)
        print_help_cmd("deploy", "Auto-detect project and deploy")
        print_help_cmd("deploy config", "Show saved deploy config")
        print_help_cmd("deploy history", "Show deploy history")
        print_help_cmd("deploy reset", "Re-detect deploy target")
        print_dim("  Detects Vercel, Netlify, Fly.io, Docker, npm, PyPI, and more.")
        print_dim("  Remembers your config  -  second deploy is just 'deploy' + 'y'.\n")

        print_info("  Theme")
        print_dim("  " + "-" * 38)
        print_help_cmd("theme", "List color themes and show current (alias: themes)")
        print_help_cmd(
            "theme <name>",
            "Set theme (same as themes <name>; light = white/snow on dark)",
        )
        print_dim("  Stored in ~/.cliara/config.json  -  applies immediately.\n")

        print_info("  Diff Preview")
        print_dim("  " + "-" * 38)
        print_dim("  Destructive commands (rm, git checkout, git clean,")
        print_dim("  git reset) show exactly what will be affected first.")
        print()
        print_help_example("rm *.log  ->  shows each file and total size")
        print()

        print_info("  Cross-Platform Translation")
        print_dim("  " + "-" * 38)
        print_dim("  If a command doesn't exist on your OS, Cliara suggests")
        print_dim("  the equivalent automatically.")
        print()
        print_help_example("grep on Windows  ->  Select-String (PowerShell)")
        print()

        print_info("  AI Provider Setup")
        print_dim("  " + "-" * 38)
        print_help_cmd("use", "Show active provider and all available options")
        print_help_cmd(
            "use <provider>",
            "Switch provider live: use openai / use ollama / use groq",
        )
        print_help_cmd("key", "Show / set / remove / test API keys (key set openai sk-...)")
        print_help_cmd(
            "setup-llm",
            "Configure an AI provider (Groq, Gemini, Ollama, OpenAI...)",
        )
        print_help_cmd("setup-ollama", "Set up a local Ollama model")
        print_help_cmd(
            "cliara login",
            "Log in to Cliara Cloud (GitHub OAuth, free tier)",
        )
        print_help_cmd("cliara logout", "Sign out and clear stored token")
        print_dim("  Free options: Groq (groq.com) · Gemini (aistudio.google.com) · Ollama (local)\n")

        print_info("  Other")
        print_dim("  " + "-" * 38)
        print_help_cmd("help", "Show this help")
        print_help_cmd("tips", "Show quick-tips panel (startup banner)")
        print_help_cmd("tips off / tips on", "Disable or re-enable the 'Did you know?' tip footer")
        print_help_cmd("last", "Repeat the last command")
        print_help_cmd("doctor", "Setup health check (shell, LLM, macros, config)")
        print_help_cmd("history [N]", "Show last N commands (default 20)")
        print_help_cmd("history clear", "Wipe command history (also: clear-history)")
        print_help_cmd(
            f"{nl} find / when did I ...",
            "Search history by meaning (semantic)",
            pad_to=36,
        )
        print_help_cmd(
            "config set semantic_history_enabled false",
            "disable semantic history & ? find",
            pad_to=42,
        )
        print_help_cmd("config undo", "Revert the last config set (up to 20 levels)")
        print_help_cmd("version / status / readme", "Show version, auth, or generate README")
        print_help_cmd("exit / Ctrl+C", "Quit Cliara")

        print_header("\n" + "=" * 60 + "\n")



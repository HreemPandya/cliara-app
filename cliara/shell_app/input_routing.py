"""Input routing mixin for Cliara shell."""

import os
import platform

from cliara.copilot_gate import InputSource
from cliara.shell_app.runtime import (
    _is_explain_last_rest,
    _looks_like_fix,
    print_dim,
    print_error,
    print_info,
    print_warning,
)


class InputRoutingMixin:
    """Prompt input dispatch and NL-command execution helpers."""

    def run_single_command(self, command: str) -> int:
        """
        Run a single command through the risk gate then exit.
        Used by ``cliara -c "command"``.

        When stdin is not a TTY (for example, CI/agent), risky commands are
        denied without prompting so the run does not block.

        Returns the process exit code (0 = success).
        """
        import sys

        try:
            assessment = self._risk_engine.assess(command)
            non_interactive = not sys.stdin.isatty()

            if not self._inline_gate(command, assessment, non_interactive=non_interactive):
                return 130

            success = self.execute_shell_command(command, capture=False)
            return 0 if success else self.last_exit_code or 1
        finally:
            self._flush_semantic_history()

    def handle_input(self, user_input: str):
        """Route one line of user input to the appropriate handler."""
        self._pending_fix = None
        self.show_shell_exit_in_prompt = False

        if self._inline_skip_once:
            self._inline_skip_once = False

        if user_input.strip():
            s = user_input.strip()
            low = s.lower()
            nl_p = (self.config.get("nl_prefix", "?") or "?").strip().lower()
            rest_after_nl = low[len(nl_p) :].lstrip() if low.startswith(nl_p) else ""
            _skip_hist = low in ("history clear", "clear-history") or rest_after_nl == "history clear"
            if not _skip_hist:
                self.history.add(s)

        if user_input.startswith("@run "):
            user_input = user_input[5:].strip()
            if not user_input:
                return
        elif self.config.get("copilot_gate", True):
            if getattr(self, "_gate_force_typed", False):
                self._gate_force_typed = False
            else:
                gate_mode = self.config.get("copilot_gate_mode", "auto")
                source = self._source_detector.classify(user_input, self.last_command, mode=gate_mode)
                if self._source_detector.is_ai_generated(source):
                    command = user_input[4:].strip() if source == InputSource.AI_TAGGED else user_input
                    if not command:
                        return
                    approved = self._copilot_gate.evaluate(command, source)
                    if not approved:
                        print_warning("  [Cancelled]")
                        return
                    user_input = command
                    self._inline_skip_once = True

        if user_input.lower() in ["exit", "quit", "q"]:
            self._print_exit_message()
            self.running = False
            return

        if user_input.lower() in ["help", "?help"]:
            self.show_help()
            return

        if user_input.lower() == "version":
            from cliara import __version__

            print_info(f"Cliara {__version__}")
            return

        _ulow = user_input.strip().lower()
        if _ulow == "last" or _ulow == "retry":
            if not self.last_command:
                print_error("[Cliara] No previous command to repeat.")
                return
            print_dim(f"-> reruns: {self.last_command}")
            self._gate_force_typed = True
            self.handle_input(self.last_command)
            return

        if user_input.lower() == "chat" or user_input.lower().startswith("chat "):
            rest = user_input[4:].strip() if len(user_input) > 4 else ""
            self._handle_chat_command(rest)
            return

        if user_input.strip().lower() == "doctor":
            self._handle_doctor()
            return

        if user_input.strip().lower() == "clear-history":
            self._handle_clear_command_history()
            return

        if user_input.strip().lower() in ("tips", "quick-tips", "quicktips"):
            self._print_full_banner()
            return

        if user_input.lower() == "history" or user_input.lower().startswith("history "):
            self.handle_history(user_input[7:].strip() if len(user_input) > 7 else "")
            return

        if user_input.lower().startswith("explain "):
            rest = user_input[8:].strip()
            if _is_explain_last_rest(rest):
                self.handle_explain_last()
            else:
                self.handle_explain(rest)
            return

        if user_input.strip().lower() == "lint" or user_input.lower().startswith("lint "):
            cmd = user_input[5:].strip() if len(user_input) > 5 else ""
            if not cmd:
                print_error("[Error] Usage: lint <command>")
                print_dim("Example: lint find . -name '*.py' -exec rm {} \\;")
                return
            self._handle_lint(cmd)
            return

        if user_input.lower() == "push":
            self.handle_push()
            return

        if user_input.strip().lower() == "prune branches":
            self.handle_prune_branches()
            return

        _sess_expanded = self._expand_session_shortcut(user_input)
        if _sess_expanded is not None:
            self.handle_session(_sess_expanded)
            return

        if user_input.lower() == "session" or user_input.lower().startswith("session "):
            subcommand = user_input[7:].strip() if len(user_input) > 7 else ""
            self.handle_session(subcommand)
            return

        if user_input.lower() == "deploy" or user_input.lower().startswith("deploy "):
            subcommand = user_input[6:].strip() if len(user_input) > 6 else ""
            self.handle_deploy(subcommand)
            return

        nl_prefix = self.config.get("nl_prefix", "?")
        if user_input.startswith(nl_prefix):
            query_rest = user_input[len(nl_prefix):].strip()
            if not query_rest:
                self._print_empty_nl_suggestions(nl_prefix)
                return
            self.handle_nl_query(query_rest)
            return

        _macro_alias = self._expand_macro_alias(user_input)
        if _macro_alias is not None:
            self.handle_macro_command(_macro_alias)
            return

        if user_input.startswith("macro "):
            self.handle_macro_command(user_input[6:].strip())
            return

        _parts = user_input.strip().split(maxsplit=1)
        if _parts and _parts[0].lower() in ("theme", "themes"):
            self._handle_theme_command(_parts[1] if len(_parts) > 1 else "")
            return

        if user_input.strip() == "config" or user_input.lower().startswith("config "):
            self._handle_config_command(user_input[6:].strip() if len(user_input) > 6 else "")
            return

        if user_input.lower().strip() == "setup-ollama":
            self._handle_setup_ollama()
            return

        if user_input.lower().strip() == "setup-llm":
            self._handle_setup_llm()
            return

        if user_input.lower().strip() in ("cliara-login", "cliara login"):
            self._handle_cliara_login()
            return

        if user_input.lower().strip() in ("cliara-logout", "cliara logout"):
            self._handle_cliara_logout()
            return

        if user_input.lower().strip() == "status":
            self._handle_status()
            return

        if user_input.lower().strip() == "readme":
            self._handle_readme()
            return

        if user_input.lower().strip() == "use" or user_input.lower().startswith("use "):
            self._handle_use_provider(user_input[3:].strip())
            return

        if self.macros.exists(user_input):
            self.run_macro(user_input)
            return

        _first_token = user_input.split()[0] if user_input.split() else ""
        if _first_token and _first_token != user_input and self.macros.exists(_first_token):
            self.run_macro(user_input)
            return

        if _looks_like_fix(user_input) and self.last_exit_code != 0 and self.last_command:
            self.handle_fix()
            return

        fuzzy_match = self.macros.find_fuzzy(user_input)
        if fuzzy_match:
            response = input(f"Did you mean macro '{fuzzy_match}'? (y/n): ").strip().lower()
            if response in ["y", "yes"]:
                self.run_macro(fuzzy_match)
                return

        if user_input == "cd" or user_input.startswith("cd "):
            self._handle_cd(user_input)
            return

        if user_input.lower() == "jump" or user_input.lower().startswith("jump "):
            query = user_input[4:].strip() if len(user_input) > 4 else ""
            self.handle_jump(query)
            return

        if user_input.lower() in ("clear", "cls"):
            os.system("cls" if platform.system() == "Windows" else "clear")
            if self.config.get("clear_show_header", True):
                self._print_clear_status_line()
            return

        if self.config.get("diff_preview", True) and self.diff_preview.should_preview(user_input):
            if not self._confirm_with_preview(user_input):
                return

        if self.config.get("copilot_gate", True):
            assessment = self._risk_engine.assess(user_input)
            if not self._inline_gate(user_input, assessment):
                return

        success = self.execute_shell_command(user_input)
        if not success:
            self._check_cross_platform(user_input)
            self._auto_suggest_fix()
            self._regression_check_failure(user_input)

    def _execute_nl_generated_command(self, cmd: str) -> bool:
        """Execute one NL-generated command, honoring Cliara built-ins first."""
        raw = (cmd or "").strip()
        if not raw:
            return True

        low = raw.lower()
        macro_alias = self._expand_macro_alias(raw)
        if macro_alias is not None:
            self.handle_macro_command(macro_alias)
            return True

        if low == "macro" or low.startswith("macro "):
            rest = raw[6:].strip() if len(raw) > 6 else ""
            self.handle_macro_command(rest)
            return True

        if low in ("help", "?help"):
            self.show_help()
            return True
        if low == "status":
            self._handle_status()
            return True
        if low == "setup-llm":
            self._handle_setup_llm()
            return True
        if low == "setup-ollama":
            self._handle_setup_ollama()
            return True

        if low == "history clear" or low == "clear-history":
            self.handle_history("clear")
            return True

        if low == "session" or low.startswith("session "):
            sub = raw[7:].strip() if len(raw) > 7 else ""
            self.handle_session(sub)
            return True

        if low == "deploy" or low.startswith("deploy "):
            sub = raw[6:].strip() if len(raw) > 6 else ""
            self.handle_deploy(sub)
            return True

        if low.startswith("explain "):
            rest = raw[8:].strip()
            if _is_explain_last_rest(rest):
                self.handle_explain_last()
            else:
                self.handle_explain(rest)
            return True

        return self.execute_shell_command(raw, capture=False)

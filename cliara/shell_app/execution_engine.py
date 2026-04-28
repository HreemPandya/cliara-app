"""Execution and error-handling mixin for Cliara shell."""

import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from cliara import regression
from cliara.chat_export import truncate_text
from cliara.safety import DangerLevel
from cliara.session_store import _get_branch, _get_project_root
from cliara.translation.core import (
    command_exists,
    get_base_command,
    is_powershell,
    translate_pipeline,
)

from cliara.shell_app.runtime import (
    _LiveTimer,
    _NullTimer,
    _cliara_console,
    _print_safety_panel,
    print_dim,
    print_error,
    print_info,
    print_success,
    print_warning,
)


class ExecutionEngineMixin:
    """Command execution, translation, and failure analysis helpers."""

    # ------------------------------------------------------------------
    # Cross-platform command translation
    # ------------------------------------------------------------------
    def _check_cross_platform(self, command: str):
        """
        After a command fails, check whether it failed because the
        executable does not exist on this platform. If a known
        cross-platform translation is available, offer it to the user.
        """
        base_cmd = get_base_command(command)
        if not base_cmd:
            return

        # If the executable is actually on the system, the failure was
        # caused by something else (bad args, permissions, etc.) - skip.
        if command_exists(base_cmd):
            return

        os_name = platform.system()
        shell = self.shell_path or ""

        # Try to translate the full pipeline.
        translated = translate_pipeline(command, os_name, shell)
        if not translated:
            return

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
        cmdlets that cmd.exe does not understand, so we invoke
        powershell/pwsh directly.
        """
        self.history.add(command)
        self.history.set_last_execution([command])

        if platform.system() == "Windows" and is_powershell(self.shell_path or ""):
            try:
                ps_exe = "pwsh" if "pwsh" in (self.shell_path or "").lower() else "powershell"
                result = subprocess.run(
                    [ps_exe, "-NoProfile", "-Command", command],
                    timeout=300,
                )
                self._enqueue_semantic_add(command, str(Path.cwd()), result.returncode)
                return result.returncode == 0
            except subprocess.TimeoutExpired:
                print_error("[Error] Command timed out (5 minutes)")
                self._enqueue_semantic_add(command, str(Path.cwd()), -1)
                return False
            except Exception as e:
                print_error(f"[Error] {e}")
                self._enqueue_semantic_add(command, str(Path.cwd()), -1)
                return False

        return self.execute_shell_command(command, capture=False)

    # ------------------------------------------------------------------
    # Error translator - plain-English stderr explanations + fixes
    # ------------------------------------------------------------------
    def _auto_suggest_fix(self):
        """
        After a failed command, run the error translator and show a one-line
        hint. If a fix command is available, store it for Tab completion.
        """
        if not self.config.get("error_translation", True):
            return
        stderr = self.last_stderr.strip()
        if not stderr:
            return

        base_cmd = get_base_command(self.last_command)
        if base_cmd and not command_exists(base_cmd):
            return

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
            short = explanation.split(".")[0].strip()
            if short:
                print_dim(f"\n  hint: {short}")
        print()

    def _maybe_translate_error(self, command: str):
        """
        After a failed command, decide whether to invoke the Error
        Translator and display the result.
        """
        if not self.config.get("error_translation", True):
            return

        stderr = self.last_stderr.strip()
        if not stderr:
            return

        base_cmd = get_base_command(command)
        if base_cmd and not command_exists(base_cmd):
            return

        self._handle_error_translation(command, stderr)

    def _handle_error_translation(self, command: str, stderr: str):
        """Translate stderr into plain English and optionally run fixes."""
        print()

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
            stream_callback=None,
        )

        explanation = result.get("explanation", "")
        fix_commands = result.get("fix_commands", [])
        fix_explanation = result.get("fix_explanation", "")

        print_info(f"[Cliara] {explanation}")

        if fix_commands:
            fix_display = " && ".join(fix_commands)
            print_info(f"         Fix: {fix_display}")

            if fix_explanation:
                print_dim(f"         ({fix_explanation})")

            try:
                response = input("         Run fix? (y/n): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if response in ("y", "yes"):
                level, dangerous = self.safety.check_commands(fix_commands)
                if level != DangerLevel.SAFE:
                    _print_safety_panel(self.safety, [cmd for cmd, _ in dangerous], level)
                    prompt = self.safety.get_confirmation_prompt(level)
                    confirm = input(prompt).strip()
                    if not self.safety.validate_confirmation(confirm, level):
                        print_warning("[Cancelled]")
                        return

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
        """Notify when a command exceeds the configured duration threshold."""
        threshold = self.config.get("notify_after_seconds", 30)
        if threshold <= 0 or elapsed < threshold:
            return

        status = "completed" if success else "failed"
        elapsed_str = f"{elapsed:.0f}s"
        short_cmd = command if len(command) <= 40 else command[:37] + "..."

        if success:
            print_success(f"\n[Cliara] {short_cmd} {status} ({elapsed_str})")
        else:
            print_error(f"\n[Cliara] {short_cmd} {status} ({elapsed_str})")

        sys.stdout.write("\a")
        sys.stdout.flush()

        if platform.system() == "Windows":
            try:
                title = "Cliara"
                body = f"{short_cmd} {status} ({elapsed_str})"
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
                    creationflags=0x08000000,
                )
            except Exception:
                pass
        else:
            try:
                if platform.system() == "Darwin":
                    subprocess.Popen(
                        [
                            "osascript",
                            "-e",
                            f'display notification "{short_cmd} {status} ({elapsed_str})" with title "Cliara"',
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    subprocess.Popen(
                        ["notify-send", "Cliara", f"{short_cmd} {status} ({elapsed_str})"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            except Exception:
                pass

        print_dim("  [Desktop notification sent]")

    def execute_shell_command(self, command: str, capture: bool = False) -> bool:
        """Execute a command in the underlying shell."""
        self.last_stderr = ""
        self.last_stdout = ""
        self.last_exit_code = 0
        self.last_command = command
        self._last_command_elapsed = None
        self._persist_last_command()

        start_time = time.time()
        spinner_delay = self.config.get("spinner_delay_seconds", 3)
        timer = None

        try:
            self.history.set_last_execution([command])

            if capture:
                if spinner_delay > 0:
                    timer = _LiveTimer(command, delay=spinner_delay, inline=True)
                else:
                    timer = _NullTimer()
                timer.start()
                try:
                    if platform.system() == "Windows" and is_powershell(self.shell_path or ""):
                        ps_exe = "pwsh" if "pwsh" in (self.shell_path or "").lower() else "powershell"
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
                self.last_stdout = result.stdout or ""
                self.last_exit_code = result.returncode
                success = result.returncode == 0
                elapsed = time.time() - start_time
                self._last_command_elapsed = elapsed
                self._notify_completion(command, elapsed, success)
                self._session_record_command(command, success)
                if success and self.config.get("regression_snapshots", True):
                    self._regression_save_success(command, elapsed)
                self.history.set_last_exit_ts(self.last_exit_code, start_time)
                return success

            if spinner_delay > 0:
                timer = _LiveTimer(command, delay=spinner_delay, inline=False)
            else:
                timer = _NullTimer()

            if platform.system() == "Windows" and is_powershell(self.shell_path or ""):
                ps_exe = "pwsh" if "pwsh" in (self.shell_path or "").lower() else "powershell"
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
            stdout_lines: List[str] = []

            def _drain_stdout():
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        stdout_lines.append(line)
                        with timer.output_lock():
                            sys.stdout.write(line)
                            sys.stdout.flush()
                except Exception:
                    pass

            def _drain_stderr():
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        stderr_lines.append(line)
                        with timer.output_lock():
                            sys.stderr.write(line)
                            sys.stderr.flush()
                except Exception:
                    pass

            stdout_reader = threading.Thread(target=_drain_stdout, daemon=True)
            stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
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

            timer.stop()
            stdout_reader.join(timeout=5)
            stderr_reader.join(timeout=5)

            if timed_out:
                print_error("[Error] Command timed out (5 minutes)")
                self.last_exit_code = -1
                elapsed = time.time() - start_time
                self._last_command_elapsed = elapsed
                self._notify_completion(command, elapsed, False)
                self._session_record_command(command, False)
                self.history.set_last_exit_ts(self.last_exit_code, start_time)
                return False

            self.last_stderr = "".join(stderr_lines)
            self.last_stdout = "".join(stdout_lines)
            self.last_exit_code = proc.returncode
            success = proc.returncode == 0
            elapsed = time.time() - start_time
            self._last_command_elapsed = elapsed
            self._notify_completion(command, elapsed, success)
            self._session_record_command(command, success)
            if success and self.config.get("regression_snapshots", True):
                self._regression_save_success(command, elapsed)
            self.history.set_last_exit_ts(self.last_exit_code, start_time)
            return success

        except Exception as e:
            try:
                if timer is not None:
                    timer.stop()
            except Exception:
                pass
            print_error(f"[Error] {e}")
            self.last_exit_code = -1
            elapsed = time.time() - start_time
            self._last_command_elapsed = elapsed
            self._session_record_command(command, False)
            self.history.set_last_exit_ts(self.last_exit_code, start_time)
            return False
        finally:
            try:
                store = getattr(self, "_jump_store", None)
                if store is not None and (command or "").strip():
                    store.record_visit(Path.cwd(), persist=True)
            except Exception:
                pass
            self._enqueue_semantic_add(command, str(Path.cwd()), self.last_exit_code)
            self.show_shell_exit_in_prompt = True

    def _session_record_command(self, command: str, success: bool):
        """If a task session is active, record this command to it."""
        if not self.current_session:
            return

        cwd = str(Path.cwd())
        root = _get_project_root(Path(cwd))
        branch = _get_branch(Path(cwd))
        parent_id = self._next_command_parent_id
        self._next_command_parent_id = None
        stderr_preview = None
        stdout_preview = None
        if self.config.get("session_persist_output"):
            try:
                smax = int(self.config.get("session_output_max_stderr_chars", 4000))
            except (TypeError, ValueError):
                smax = 4000
            try:
                omax = int(self.config.get("session_output_max_stdout_chars", 4000))
            except (TypeError, ValueError):
                omax = 4000
            if (self.last_stderr or "").strip():
                stderr_preview = truncate_text(self.last_stderr, smax)
            lo = getattr(self, "last_stdout", "") or ""
            if lo.strip():
                stdout_preview = truncate_text(lo, omax)

        self.session_store.add_command(
            self.current_session.id,
            command=command,
            cwd=cwd,
            exit_code=0 if success else (self.last_exit_code if self.last_exit_code != 0 else 1),
            branch=branch,
            project_root=root,
            parent_id=parent_id,
            stderr_preview=stderr_preview,
            stdout_preview=stdout_preview,
        )
        updated = self.session_store.get_by_id(self.current_session.id)
        if updated:
            self.current_session = updated

    def _regression_workflow_key(self, command: str) -> Optional[str]:
        """Compute workflow key for regression snapshot."""
        cwd = Path.cwd()
        root = _get_project_root(cwd)
        base = get_base_command(command)
        if not base:
            return None
        return f"{root or 'cwd:' + str(cwd)}::{base}"

    def _regression_save_success(self, command: str, elapsed: Optional[float] = None) -> None:
        """Capture and save a success snapshot for this workflow."""
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
        """Return True when the failure is a bad/unknown subcommand."""
        stderr = (getattr(self, "last_stderr", "") or "").lower()
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
        """On failure, compare to last success and show a compact report."""
        if not self.config.get("regression_snapshots", True):
            return
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

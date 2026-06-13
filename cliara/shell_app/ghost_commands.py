"""Ghost Run commands for Cliara (``ghost <command>``).

The shell-side personality of :mod:`cliara.shadow_exec`: forks the current
directory into a throwaway parallel universe, runs a destructive command
there, and renders the *real* resulting diff — deleted / created / modified
files with byte counts — before anything in your timeline changes.

Entry points:
  * ``ghost <command>``      explicit, from the prompt
  * ``[g]`` in the diff-preview gate (rm / git clean confirmations)
  * ``g`` at the DANGEROUS/CRITICAL Copilot-Gate prompt

Mixed into :class:`cliara.shell_app.orchestrator.CliaraShell`.
"""

import random
import time
from pathlib import Path
from typing import List, Optional

from cliara import icons
from cliara.shell_app.runtime import (
    _cliara_console,
    _ui_accent_style,
    print_dim,
    print_error,
    print_info,
    print_success,
    print_warning,
    safe_input,
    thinking_status,
)


# The ghost has a voice. Keep it dry, confident, and short — it reports from
# a universe it just destroyed.
_FORKING_LINES = [
    "forking reality",
    "splitting the timeline",
    "sending the ghost ahead",
    "opening a parallel universe",
    "cloning this corner of spacetime",
]

_NOTHING_LINES = [
    "The ghost reports: nothing would change. Your timeline is safe either way.",
    "The ghost came back bored — that command changes nothing here.",
    "Verdict from the parallel universe: no casualties, no survivors, no changes.",
]

_RETURN_LINES = [
    "The ghost returns from the parallel universe:",
    "Report from the timeline that no longer exists:",
    "The ghost has seen your future. Here it is:",
]

# Show at most this many rows per change category in the report table.
_MAX_ROWS_PER_KIND = 20


class GhostRunCommandMixin:
    """``ghost`` — run a destructive command in a parallel universe first."""

    _ghost_purged_stale: bool = False

    # -- config -----------------------------------------------------------

    def _ghost_enabled(self) -> bool:
        return bool(self.config.get("ghost_run_enabled", True))

    def _ghost_cfg_int(self, key: str, default: int) -> int:
        try:
            v = int(self.config.get(key, default))
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    # -- public entry points ------------------------------------------------

    def handle_ghost(self, args: str = "") -> None:
        """``ghost <command>`` — fork, run, diff, report, then offer reality."""
        from cliara import shadow_exec

        cmd = (args or "").strip()
        if not cmd or cmd.lower() in ("help", "-h", "--help"):
            self._print_ghost_usage()
            return
        if not self._ghost_enabled():
            print_warning("[Ghost Run is disabled — config set ghost_run_enabled true]")
            return

        verdict = shadow_exec.assess_shadowability(cmd, Path.cwd())
        if not verdict.eligible:
            print_warning(f"  {icons.GHOST} The ghost declines.")
            print_dim(f"  {verdict.reason}")
            return

        if not shadow_exec.expanded_targets_exist(cmd, Path.cwd()):
            print_dim(f"  {icons.GHOST} The ghost found nothing matching those targets — "
                      "running it would change nothing.")
            return

        result = self._ghost_fork_and_run(cmd)
        if result is None:
            return

        self._render_ghost_report(result)

        if result.diff.is_empty:
            return

        # The diff IS the informed consent — one keystroke makes it real.
        choice = (safe_input(
            f"\n  Make it real? [Enter] run in THIS universe · [n] keep your timeline: "
        ) or "").strip().lower()
        if choice in ("", "y", "yes", "run"):
            print_dim(f"  {icons.GHOST} Collapsing the wavefunction...")
            self._gate_force_typed = True
            self.execute_shell_command(cmd, capture=False)
        else:
            print_dim(f"  {icons.GHOST} Your timeline remains unchanged.")

    def _ghost_gate_offer(self, command: str) -> Optional[bool]:
        """Gate hook: ghost-run *command*, show the diff, return the decision.

        Returns True  → user saw the future and wants it (caller executes),
                False → user walked away (caller cancels),
                None  → couldn't ghost-run (caller falls back to its prompt).
        """
        from cliara import shadow_exec

        if not self._ghost_enabled():
            return None
        verdict = shadow_exec.assess_shadowability(command, Path.cwd())
        if not verdict.eligible:
            print_dim(f"  {icons.GHOST} {verdict.reason}")
            return None

        result = self._ghost_fork_and_run(command)
        if result is None:
            return None

        self._render_ghost_report(result)
        if result.diff.is_empty:
            # Nothing would change — running it for real is trivially safe.
            choice = (safe_input("\n  Run it for real anyway? (y/N): ") or "").strip().lower()
            return choice in ("y", "yes")
        choice = (safe_input(
            "\n  Make it real? [Enter] run in THIS universe · [n] keep your timeline: "
        ) or "").strip().lower()
        return choice in ("", "y", "yes", "run")

    def _ghost_eligible(self, command: str) -> bool:
        """Cheap check used to decide whether gates should advertise [g]."""
        from cliara import shadow_exec

        if not self._ghost_enabled():
            return False
        return shadow_exec.assess_shadowability(command, Path.cwd()).eligible

    # -- core ----------------------------------------------------------------

    def _ghost_fork_and_run(self, command: str):
        """Fork + execute + destroy. Returns ShadowResult or None on refusal."""
        from cliara import shadow_exec

        # Sweep universes left behind by crashed sessions, once per session.
        if not self._ghost_purged_stale:
            self._ghost_purged_stale = True
            try:
                shadow_exec.purge_stale_sandboxes()
            except Exception:
                pass

        max_files = self._ghost_cfg_int("ghost_run_max_files", shadow_exec.DEFAULT_MAX_FILES)
        max_copy_mb = self._ghost_cfg_int("ghost_run_max_copy_mb", 500)
        timeout_s = self._ghost_cfg_int("ghost_run_timeout_seconds", 120)

        label = command if len(command) <= 44 else command[:41] + "..."
        with thinking_status(f"{random.choice(_FORKING_LINES)} · {label}"):
            try:
                return shadow_exec.run_shadow(
                    command,
                    Path.cwd(),
                    shell_path=self.shell_path,
                    max_files=max_files,
                    max_copy_bytes=max_copy_mb * 1024 * 1024,
                    timeout_s=float(timeout_s),
                )
            except RuntimeError as e:
                print_warning(f"  {icons.GHOST} The fork failed: {e}")
                return None
            except Exception as e:
                print_error(f"[Error] Ghost Run failed unexpectedly: {e}")
                return None

    # -- rendering -------------------------------------------------------------

    def _render_ghost_report(self, result) -> None:
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        from cliara.shadow_exec import format_bytes

        accent = _ui_accent_style()
        console = _cliara_console()
        diff = result.diff

        # ── headline ────────────────────────────────────────────────────
        headline = Text()
        headline.append(f"{random.choice(_RETURN_LINES)}\n\n", style="italic dim")

        if diff.is_empty:
            headline.append(random.choice(_NOTHING_LINES), style="bold")
        else:
            bits: List[str] = []
            if diff.deleted:
                bits.append(f"{len(diff.deleted)} file(s) deleted ({format_bytes(diff.bytes_deleted)})")
            if diff.modified:
                bits.append(f"{len(diff.modified)} modified")
            if diff.created:
                bits.append(f"{len(diff.created)} created")
            headline.append("Would result in: ", style="bold")
            headline.append(" · ".join(bits), style="bold red" if diff.deleted else "bold yellow")

        exit_style = "green" if result.exit_code == 0 else "red"
        headline.append(f"\nExit code in the other universe: ", style="dim")
        headline.append(str(result.exit_code), style=exit_style)

        body = [headline]

        # ── change table ────────────────────────────────────────────────
        if not diff.is_empty:
            tbl = Table(box=box.SIMPLE, show_header=True,
                        header_style=f"bold {accent}", padding=(0, 1))
            tbl.add_column("Fate", no_wrap=True)
            tbl.add_column("File", style="bold white", overflow="fold")
            tbl.add_column("Size", style="dim", justify="right", no_wrap=True)

            def _rows(changes, label: str, style: str):
                for c in changes[:_MAX_ROWS_PER_KIND]:
                    tbl.add_row(Text(label, style=style), c.path, format_bytes(c.size))
                extra = len(changes) - _MAX_ROWS_PER_KIND
                if extra > 0:
                    tbl.add_row(Text(label, style=f"dim {style}"),
                                Text(f"... and {extra} more", style="dim"), "")

            _rows(diff.deleted, "deleted", "red")
            _rows(diff.modified, "modified", "yellow")
            _rows(diff.created, "created", "green")
            body.append(Text())
            body.append(tbl)

        # ── command output from the sandbox (usually silence) ───────────
        out = (result.stderr or "").strip() or (result.stdout or "").strip()
        if out:
            lines = out.splitlines()
            shown = "\n".join(lines[:8])
            if len(lines) > 8:
                shown += f"\n... ({len(lines) - 8} more lines)"
            body.append(Text())
            body.append(Text("It said, from the other side:", style="dim italic"))
            body.append(Text(shown, style="dim"))

        # ── fork stats footer ────────────────────────────────────────────
        c = result.clone
        fork_note = (
            f"forked {c.files:,} files in {result.elapsed_s:.1f}s "
            f"({c.linked:,} hardlinked · {c.copied:,} copied)"
        )
        subtitle = Text(
            f"{fork_note} — the parallel universe has been destroyed",
            style="dim",
        )

        console.print()
        console.print(
            Panel(
                Group(*body),
                title=Text(f"{icons.GHOST} Ghost Run", style=accent),
                subtitle=subtitle,
                border_style=accent,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        print_dim("  Nothing real was touched. Your files are exactly as they were.")

    @staticmethod
    def _print_ghost_usage() -> None:
        print_info(f"  {icons.GHOST} Ghost Run — run it in a parallel universe first")
        print_dim("  Usage: ghost <command>")
        print_dim("  Forks the current directory into a throwaway sandbox, runs the")
        print_dim("  command THERE, and shows the real resulting diff. Nothing in your")
        print_dim("  actual timeline changes until you say so.")
        print()
        print_dim("  Examples:")
        print_dim("    ghost rm -rf dist")
        print_dim("    ghost rm *.log")
        print_dim("    ghost git clean -fdx")
        print()
        print_dim("  The ghost speaks deletion magic only (rm / del / erase / rd,")
        print_dim("  git clean) and refuses anything it can't faithfully contain:")
        print_dim("  pipes, network, sudo, absolute paths, universes over the size cap.")
        print_dim("  Disable: config set ghost_run_enabled false")

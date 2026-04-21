"""Gate and risk-flow mixin for Cliara shell."""

from cliara.safety import DangerLevel
from cliara.shell_app.runtime import print_warning


class GateFlowMixin:
    """Diff preview and risk gating helpers."""

    # ------------------------------------------------------------------
    # Diff preview - show impact before destructive commands
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
            # - let the command through without blocking.
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
    # Inline risk gate - warn and confirm risky commands in the terminal
    # ------------------------------------------------------------------
    def _inline_gate(self, command: str, assessment, *, non_interactive: bool = False) -> bool:
        """
        Tiered risk gate for typed commands - same UX tiers as CopilotGate (SAFE / CAUTION /
        DANGEROUS ``RUN`` / CRITICAL ``I UNDERSTAND``).

        When *non_interactive* is True (e.g. stdin not a TTY), risky commands
        are denied without prompting so the process does not block.

        After CopilotGate already approved pasted/AI input, *inline_skip_once* avoids
        prompting twice for the same line.
        """
        from cliara.copilot_gate import RiskAssessment

        ra: RiskAssessment = assessment
        level = ra.danger_level

        if non_interactive:
            if level == DangerLevel.SAFE:
                return True
            print_warning("  [Skipped] Non-interactive (no TTY); risky commands are not run.")
            return False

        if self._inline_skip_once:
            self._inline_skip_once = False
            return True

        return self._copilot_gate.confirm_command(command, ra)

"""
Copilot Gate: AI-command interception layer.

Detects commands pasted from GitHub Copilot (or any AI tool), assesses
their risk using repo and runtime context, and gates execution
proportionally -- auto-approving safe commands and requiring explicit
confirmation only for dangerous ones.
"""

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from cliara.safety import SafetyChecker, DangerLevel
from cliara.diff_preview import DiffPreview
from cliara.self_upgrade import is_cliara_pip_install_command


# ---------------------------------------------------------------------------
# Input source detection
# ---------------------------------------------------------------------------

class InputSource(Enum):
    """How a command entered the shell."""
    TYPED = "typed"
    PASTED = "pasted"
    AI_TAGGED = "ai_tagged"       # Explicit @ai prefix
    COPILOT_CLI = "copilot_cli"   # After `gh copilot suggest`


class SourceDetector:
    """
    Classifies each command as typed, pasted, explicitly AI-tagged, or
    originating from ``gh copilot suggest``.
    """

    def __init__(self):
        self._paste_flag = False

    def mark_paste(self):
        """Called by bracket-paste / Ctrl+V handlers in the prompt session."""
        self._paste_flag = True

    def classify(self, command: str, prev_command: str = "",
                 *, mode: str = "auto") -> InputSource:
        """
        Return the most likely source of *command*.

        *mode* mirrors the ``copilot_gate_mode`` config key:
          - ``"auto"``     -- all four detection channels active
          - ``"explicit"`` -- only the ``@ai`` prefix is recognised
          - ``"all"``      -- every command is treated as AI-generated
        """
        if mode == "all":
            return InputSource.PASTED

        if command.startswith("@ai "):
            return InputSource.AI_TAGGED

        if mode == "explicit":
            return InputSource.TYPED

        if prev_command and "gh copilot" in prev_command:
            return InputSource.COPILOT_CLI

        if self._paste_flag:
            self._paste_flag = False
            return InputSource.PASTED

        return InputSource.TYPED

    @staticmethod
    def is_ai_generated(source: InputSource) -> bool:
        return source != InputSource.TYPED


# ---------------------------------------------------------------------------
# Risk assessment result
# ---------------------------------------------------------------------------

@dataclass
class RiskAssessment:
    danger_level: DangerLevel
    explanation: str                          # reserved; explicit `explain` handles command narration
    risk_factors: List[str] = field(default_factory=list)
    blast_radius: str = "local"              # "1 file" | "entire repo" | "system-wide"
    reversible: bool = True
    context_warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Irreversibility indicators
# ---------------------------------------------------------------------------

_IRREVERSIBLE_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.I) for p in [
        r'\brm\b', r'\bdel\b', r'\berase\b',
        r'\brd\s+/s', r'\brmdir\b',
        r'\bgit\s+push\s+.*--force', r'\bgit\s+push\s+.*-f\b',
        r'\bgit\s+reset\s+--hard',
        r'\bgit\s+clean\b',
        r'\bgit\s+filter-branch\b',
        r'\bnpm\s+publish\b',
        r'\bcargo\s+publish\b',
        r'\bdocker\s+push\b',
        r'\bfly\s+deploy\b',
        r'\bvercel\s+--prod\b',
        r'\bnetlify\s+deploy\s+--prod\b',
        r'\bterraform\s+destroy\b',
        r'\bheroku\s+apps:destroy\b',
        r'\bmkfs\b',
        r'\bdd\b',
    ]
]

_PROTECTED_BRANCHES = {"main", "master", "production", "prod", "release"}


# ---------------------------------------------------------------------------
# Blast-radius heuristics
# ---------------------------------------------------------------------------

_GLOBAL_SCOPE_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.I) for p in [
        r'\bnpm\s+install\s+-g\b',
        r'\bpip\s+install\b(?!.*--user)',
        r'\bsudo\b',
        r'\bsystemctl\b',
        r'\bservice\b',
        r'\bshutdown\b', r'\breboot\b',
        r'\bmkfs\b', r'\bdd\b',
        r'\bchmod\s+777\s+/',
    ]
]


# ---------------------------------------------------------------------------
# RiskEngine
# ---------------------------------------------------------------------------

class RiskEngine:
    """
    Context-aware risk assessment that wraps the existing SafetyChecker
    and augments it with blast-radius estimation, repo context probes,
    reversibility checks, and explanation generation.
    """

    def __init__(self, safety: SafetyChecker, diff_preview: DiffPreview):
        self._safety = safety
        self._diff = diff_preview

    # ── public ────────────────────────────────────────────────────────

    def assess(self, command: str, *, use_repo_context: bool = True) -> RiskAssessment:
        """Produce a full RiskAssessment for *command*."""
        sub_commands = self._split_compound(command)

        highest_level = DangerLevel.SAFE
        all_risk_factors: List[str] = []
        all_context_warnings: List[str] = []
        level_order = [DangerLevel.SAFE, DangerLevel.CAUTION,
                       DangerLevel.DANGEROUS, DangerLevel.CRITICAL]

        for sub in sub_commands:
            lvl, _ = self._safety.check_command(sub)
            if level_order.index(lvl) > level_order.index(highest_level):
                highest_level = lvl

        reversible = self._check_reversible(command)
        blast = self._estimate_blast_radius(command)
        risk_factors = self._collect_risk_factors(command, highest_level)

        if use_repo_context:
            ctx = self._gather_repo_context()
            ctx_warnings, amplified = self._apply_context_amplifiers(
                command, highest_level, ctx)
            all_context_warnings.extend(ctx_warnings)
            if level_order.index(amplified) > level_order.index(highest_level):
                highest_level = amplified

        all_risk_factors.extend(risk_factors)

        if not reversible:
            all_risk_factors.append("Irreversible")

        return RiskAssessment(
            danger_level=highest_level,
            explanation="",
            risk_factors=all_risk_factors,
            blast_radius=blast,
            reversible=reversible,
            context_warnings=all_context_warnings,
        )

    # ── compound command splitting ────────────────────────────────────

    @staticmethod
    def _split_compound(command: str) -> List[str]:
        """Split on ``&&``, ``||``, ``;``, and ``|``."""
        parts = re.split(r'\s*(?:&&|\|\|?|;)\s*', command)
        return [p.strip() for p in parts if p.strip()]

    # ── reversibility ─────────────────────────────────────────────────

    @staticmethod
    def _check_reversible(command: str) -> bool:
        for pattern in _IRREVERSIBLE_PATTERNS:
            if pattern.search(command):
                return False
        return True

    # ── blast radius ──────────────────────────────────────────────────

    def _estimate_blast_radius(self, command: str) -> str:
        # Upgrading Cliara itself is scoped to one package, not general system-wide pip.
        if is_cliara_pip_install_command(command):
            return "local"

        for pat in _GLOBAL_SCOPE_PATTERNS:
            if pat.search(command):
                return "system-wide"

        cmd = command.strip()
        tokens = cmd.split()
        if len(tokens) >= 2:
            target = tokens[-1]
            if target in ("/", "~", "C:\\", "C:/"):
                return "system-wide"
            if target.startswith("/") or target.startswith("~"):
                return "home directory"
            if target == ".":
                return "entire repo"

        try:
            targets = self._extract_file_targets(cmd)
            if targets:
                dirs = sum(1 for t in targets if Path(t).is_dir())
                files = len(targets) - dirs
                parts = []
                if dirs:
                    parts.append(f"{dirs} director{'ies' if dirs != 1 else 'y'}")
                if files:
                    parts.append(f"{files} file{'s' if files != 1 else ''}")
                if parts:
                    return ", ".join(parts)
        except Exception:
            pass

        return "local"

    @staticmethod
    def _extract_file_targets(command: str) -> List[str]:
        """Best-effort extraction of file/directory targets from rm-like commands."""
        if not re.match(r'^(rm|del|erase)\b', command, re.I):
            return []
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        return [t for t in tokens[1:] if not t.startswith("-")]

    # ── risk factors ──────────────────────────────────────────────────

    @staticmethod
    def _collect_risk_factors(command: str, level: DangerLevel) -> List[str]:
        factors: List[str] = []
        cmd = command.lower()

        if "--force" in cmd or " -f " in cmd or cmd.endswith(" -f"):
            factors.append("Uses --force flag")
        if "--no-verify" in cmd:
            factors.append("Skips verification hooks")
        if "--skip-hooks" in cmd:
            factors.append("Skips hooks")
        if re.search(r'(\.env\b|\bcredentials\b|\bsecrets?\b|\.pem\b|\.key\b)', command, re.I):
            factors.append("Touches sensitive files")
        if re.search(r'\b(curl|wget)\b.*\b(POST|PUT|DELETE)\b', command, re.I):
            factors.append("Mutating HTTP request")
        if re.search(r'\bcurl\b.*(-d|--data)\b', command, re.I):
            factors.append("Sends data via HTTP")
        if re.search(r'\bnpm\s+publish\b', command, re.I):
            factors.append("Publishes to npm registry")
        if re.search(r'\bdocker\s+push\b', command, re.I):
            factors.append("Pushes image to registry")
        if re.search(r'\bterraform\s+(apply|destroy)\b', command, re.I):
            factors.append("Modifies cloud infrastructure")

        return factors

    # ── repo context ──────────────────────────────────────────────────

    @staticmethod
    def _git(args: str) -> str:
        try:
            result = subprocess.run(
                ["git"] + args.split(),
                capture_output=True, text=True, timeout=3,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    def _gather_repo_context(self) -> dict:
        branch = self._git("rev-parse --abbrev-ref HEAD")
        if not branch:
            return {}
        dirty = bool(self._git("status --porcelain"))
        try:
            unpushed = int(self._git("rev-list @{u}..HEAD --count") or "0")
        except (ValueError, TypeError):
            unpushed = 0
        has_remote = bool(self._git("remote"))

        return {
            "branch": branch,
            "is_dirty": dirty,
            "unpushed": unpushed,
            "has_remote": has_remote,
        }

    @staticmethod
    def _apply_context_amplifiers(
        command: str, base_level: DangerLevel, ctx: dict,
    ) -> Tuple[List[str], DangerLevel]:
        """Return (context_warnings, potentially_escalated_level)."""
        warnings: List[str] = []
        level = base_level
        level_order = [DangerLevel.SAFE, DangerLevel.CAUTION,
                       DangerLevel.DANGEROUS, DangerLevel.CRITICAL]

        branch = ctx.get("branch", "")
        unpushed = ctx.get("unpushed", 0)
        is_dirty = ctx.get("is_dirty", False)

        def _escalate(new: DangerLevel):
            nonlocal level
            if level_order.index(new) > level_order.index(level):
                level = new

        if branch in _PROTECTED_BRANCHES:
            is_push = bool(re.search(r'\bgit\s+push\b', command, re.I))
            is_force = bool(re.search(r'--force|-f\b', command, re.I))
            is_rebase = bool(re.search(r'\bgit\s+rebase\b', command, re.I))
            is_deploy = bool(re.search(
                r'\b(deploy|publish|terraform\s+apply)\b', command, re.I))

            if is_push and is_force:
                warnings.append(f"Force-pushing to protected branch '{branch}'")
                _escalate(DangerLevel.CRITICAL)
            elif is_push:
                warnings.append(f"Pushing to protected branch '{branch}'")
            elif is_rebase:
                warnings.append(f"Rebasing on protected branch '{branch}'")
                _escalate(DangerLevel.DANGEROUS)
            elif is_deploy:
                warnings.append(f"Deploying from protected branch '{branch}'")

        if unpushed and re.search(r'\bgit\s+reset\b', command, re.I):
            warnings.append(f"{unpushed} unpushed commit(s) may be lost")
            _escalate(DangerLevel.DANGEROUS)

        if is_dirty and re.search(r'\bgit\s+(checkout|switch|reset|clean)\b', command, re.I):
            warnings.append("Uncommitted changes in working tree")

        return warnings, level

    # ── preview integration ───────────────────────────────────────────

    def get_preview(self, command: str) -> Optional[str]:
        """Delegate to DiffPreview for concrete impact details."""
        if self._diff.should_preview(command):
            return self._diff.generate_preview(command)
        return None


# ---------------------------------------------------------------------------
# CopilotGate (orchestrator + UX)
# ---------------------------------------------------------------------------

class CopilotGate:
    """
    Orchestrator: receives a command flagged as AI-generated, runs it
    through the RiskEngine, renders the appropriate UX tier, and
    returns whether execution is approved.
    """

    def __init__(self, risk_engine: RiskEngine, *,
                 auto_approve_safe: bool = True,
                 auto_approve_caution: bool = False,
                 nl_handler=None):
        self._risk = risk_engine
        self._auto_approve_safe = auto_approve_safe
        self._auto_approve_caution = auto_approve_caution
        self._nl_handler = nl_handler

    # ── public API ────────────────────────────────────────────────────

    def evaluate(self, command: str, source: InputSource) -> bool:
        """
        Assess *command* and prompt the user if needed.

        Returns *True* when execution is approved, *False* to cancel.
        """
        assessment = self._risk.assess(command)
        return self.confirm_command(command, assessment)

    def confirm_command(
        self, command: str, ra: RiskAssessment,
    ) -> bool:
        """
        Present the 4-tier gate UX for an already-computed assessment.

        Used for pasted/AI commands (via ``evaluate``) and for typed shell
        commands (via ``CliaraShell._inline_gate``) so behavior stays consistent.
        """
        from cliara.console import get_console
        console = get_console()

        if ra.danger_level == DangerLevel.SAFE:
            return self._gate_safe(console, ra)
        if ra.danger_level == DangerLevel.CAUTION:
            return self._gate_caution(console, ra)
        if ra.danger_level == DangerLevel.DANGEROUS:
            return self._gate_dangerous(console, command, ra)
        return self._gate_critical(console, command, ra)

    # ── Tier 1: SAFE ──

    def _gate_safe(self, console, ra: RiskAssessment) -> bool:
        if self._auto_approve_safe:
            return True
        try:
            resp = input("  Press Enter to run, or 'n' to cancel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return resp not in ("n", "no")

    # ── Tier 2: CAUTION ──

    def _gate_caution(self, console, ra: RiskAssessment) -> bool:
        parts: List[str] = []
        if ra.blast_radius != "local":
            parts.append(f"scope: {ra.blast_radius}")
        parts.extend(ra.risk_factors)
        if ra.context_warnings:
            parts.extend(ra.context_warnings)
        if not parts:
            parts.append("Potential side effects")
        detail = " [yellow]|[/yellow] ".join(parts)

        console.print(f" [yellow bold]Caution[/yellow bold]  {detail}")

        if self._auto_approve_caution:
            console.print("  [dim](caution tier auto-approved — set copilot_gate_auto_approve_caution false to confirm)[/dim]")
            return True

        try:
            resp = input("  Run? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return resp in ("y", "yes")

    # ── Tier 3: DANGEROUS ──

    def _gate_dangerous(self, console, command: str,
                        ra: RiskAssessment) -> bool:
        console.print(" [red bold]Dangerous command[/red bold]")

        details: List[str] = []
        if ra.blast_radius != "local":
            details.append(ra.blast_radius)
        details.extend(ra.risk_factors)
        details.extend(ra.context_warnings)

        for line in details:
            console.print(f"     [red]|[/red] {line}")

        preview = self._risk.get_preview(command)
        if preview:
            console.print(f"[yellow]{preview}[/yellow]")

        try:
            resp = input("  Type 'RUN' to execute: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return resp == "RUN"

    # ── Tier 4: CRITICAL ──

    def _gate_critical(self, console, command: str,
                       ra: RiskAssessment) -> bool:
        console.print(" [bold bright_red]Critical command[/bold bright_red]")

        details: List[str] = []
        details.extend(ra.context_warnings)
        details.extend(ra.risk_factors)
        if ra.blast_radius != "local":
            details.append(f"Blast radius: {ra.blast_radius}")

        for line in details:
            console.print(f"     [bright_red]![/bright_red] {line}")

        preview = self._risk.get_preview(command)
        if preview:
            console.print(f"[yellow]{preview}[/yellow]")

        try:
            resp = input("  Type 'I UNDERSTAND' to execute: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return resp == "I UNDERSTAND"

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
    explanation: str                          # 1-line explanation
    risk_factors: List[str] = field(default_factory=list)
    blast_radius: str = "local"              # "1 file" | "entire repo" | "system-wide"
    reversible: bool = True
    context_warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Explanation templates (pattern-based, no LLM)
# ---------------------------------------------------------------------------

_EXPLANATION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # ── git ──
    (re.compile(r'^git\s+status$', re.I), "Shows working tree status"),
    (re.compile(r'^git\s+status\s', re.I), "Shows working tree status"),
    (re.compile(r'^git\s+diff$', re.I), "Shows unstaged changes"),
    (re.compile(r'^git\s+diff\s+--cached', re.I), "Shows staged changes"),
    (re.compile(r'^git\s+diff\s+--staged', re.I), "Shows staged changes"),
    (re.compile(r'^git\s+diff\s', re.I), "Shows differences between revisions"),
    (re.compile(r'^git\s+log\b', re.I), "Shows commit history"),
    (re.compile(r'^git\s+show\b', re.I), "Shows commit details"),
    (re.compile(r'^git\s+branch\b', re.I), "Lists or manages branches"),
    (re.compile(r'^git\s+checkout\s+-b\s', re.I), "Creates and switches to a new branch"),
    (re.compile(r'^git\s+checkout\b', re.I), "Switches branches or restores files"),
    (re.compile(r'^git\s+switch\b', re.I), "Switches branches"),
    (re.compile(r'^git\s+add\s+\.\s*$', re.I), "Stages all changes"),
    (re.compile(r'^git\s+add\b', re.I), "Stages files for commit"),
    (re.compile(r'^git\s+commit\b', re.I), "Creates a new commit"),
    (re.compile(r'^git\s+push\s+.*--force', re.I), "Force-pushes, rewriting remote history"),
    (re.compile(r'^git\s+push\s+.*-f\b', re.I), "Force-pushes, rewriting remote history"),
    (re.compile(r'^git\s+push\b', re.I), "Pushes commits to remote"),
    (re.compile(r'^git\s+pull\s+--rebase', re.I), "Pulls and rebases local commits"),
    (re.compile(r'^git\s+pull\b', re.I), "Pulls and merges remote changes"),
    (re.compile(r'^git\s+fetch\b', re.I), "Downloads remote refs without merging"),
    (re.compile(r'^git\s+merge\b', re.I), "Merges another branch into current"),
    (re.compile(r'^git\s+rebase\b', re.I), "Replays commits onto another base"),
    (re.compile(r'^git\s+reset\s+--hard', re.I), "Hard-resets HEAD, discarding all changes"),
    (re.compile(r'^git\s+reset\b', re.I), "Resets HEAD to a previous state"),
    (re.compile(r'^git\s+stash\s+pop', re.I), "Applies and removes top stash entry"),
    (re.compile(r'^git\s+stash\s+drop', re.I), "Deletes a stash entry"),
    (re.compile(r'^git\s+stash\b', re.I), "Stashes working directory changes"),
    (re.compile(r'^git\s+clean\s+-fd', re.I), "Removes untracked files and directories"),
    (re.compile(r'^git\s+clean\b', re.I), "Removes untracked files"),
    (re.compile(r'^git\s+clone\b', re.I), "Clones a repository"),
    (re.compile(r'^git\s+remote\b', re.I), "Manages remote repositories"),
    (re.compile(r'^git\s+tag\b', re.I), "Manages tags"),
    (re.compile(r'^git\s+cherry-pick\b', re.I), "Applies a commit from another branch"),
    (re.compile(r'^git\s+revert\b', re.I), "Reverts a commit by creating a new one"),
    (re.compile(r'^git\s+filter-branch\b', re.I), "Rewrites entire branch history"),
    (re.compile(r'^git\s+restore\b', re.I), "Restores working tree files"),
    (re.compile(r'^git\s+init\b', re.I), "Initialises a new git repository"),
    # ── file operations ──
    (re.compile(r'^rm\s+-rf\s+(\S+)', re.I), "Recursively deletes {0}"),
    (re.compile(r'^rm\s+-r\s+(\S+)', re.I), "Recursively deletes {0}"),
    (re.compile(r'^rm\s+(\S+)', re.I), "Deletes {0}"),
    (re.compile(r'^del\s+', re.I), "Deletes files (Windows)"),
    (re.compile(r'^rd\s+/s', re.I), "Removes directory tree (Windows)"),
    (re.compile(r'^rmdir\s+', re.I), "Removes directory"),
    (re.compile(r'^mkdir\s+', re.I), "Creates directory"),
    (re.compile(r'^touch\s+', re.I), "Creates or updates file timestamp"),
    (re.compile(r'^cp\s+-r', re.I), "Recursively copies files"),
    (re.compile(r'^cp\s+', re.I), "Copies files"),
    (re.compile(r'^mv\s+', re.I), "Moves or renames files"),
    (re.compile(r'^chmod\s+777\b', re.I), "Sets full permissions (world-writable)"),
    (re.compile(r'^chmod\s+', re.I), "Changes file permissions"),
    (re.compile(r'^chown\s+', re.I), "Changes file ownership"),
    (re.compile(r'^ln\s+-s', re.I), "Creates a symbolic link"),
    (re.compile(r'^ln\s+', re.I), "Creates a hard link"),
    # ── reading / searching ──
    (re.compile(r'^cat\s+', re.I), "Displays file contents"),
    (re.compile(r'^less\s+', re.I), "Pages through file contents"),
    (re.compile(r'^head\s+', re.I), "Shows first lines of a file"),
    (re.compile(r'^tail\s+-f', re.I), "Follows file output in real-time"),
    (re.compile(r'^tail\s+', re.I), "Shows last lines of a file"),
    (re.compile(r'^grep\s+', re.I), "Searches text by pattern"),
    (re.compile(r'^rg\s+', re.I), "Searches text by pattern (ripgrep)"),
    (re.compile(r'^find\s+', re.I), "Finds files by criteria"),
    (re.compile(r'^fd\s+', re.I), "Finds files by name (fd)"),
    (re.compile(r'^wc\s+', re.I), "Counts lines, words, or characters"),
    (re.compile(r'^ls\b', re.I), "Lists directory contents"),
    (re.compile(r'^dir\b', re.I), "Lists directory contents (Windows)"),
    (re.compile(r'^pwd$', re.I), "Prints current directory"),
    (re.compile(r'^tree\b', re.I), "Displays directory tree"),
    # ── package managers ──
    (re.compile(r'^npm\s+install\s+-g\b', re.I), "Installs npm package globally"),
    (re.compile(r'^npm\s+install\b', re.I), "Installs npm dependencies"),
    (re.compile(r'^npm\s+run\s+(\S+)', re.I), "Runs npm script '{0}'"),
    (re.compile(r'^npm\s+publish\b', re.I), "Publishes package to npm registry"),
    (re.compile(r'^npm\s+test\b', re.I), "Runs project tests"),
    (re.compile(r'^npm\s+start\b', re.I), "Starts the application"),
    (re.compile(r'^npm\s+ci\b', re.I), "Clean-installs dependencies from lockfile"),
    (re.compile(r'^npx\s+', re.I), "Runs an npm package binary"),
    (re.compile(r'^yarn\s+add\b', re.I), "Adds a yarn dependency"),
    (re.compile(r'^yarn\s+install\b', re.I), "Installs yarn dependencies"),
    (re.compile(r'^yarn\b', re.I), "Runs a yarn command"),
    (re.compile(r'^pnpm\s+', re.I), "Runs a pnpm command"),
    (re.compile(r'^pip\s+install\s+', re.I), "Installs Python packages"),
    (re.compile(r'^pip\s+uninstall\s+', re.I), "Uninstalls Python packages"),
    (re.compile(r'^pip\s+freeze\b', re.I), "Lists installed Python packages"),
    (re.compile(r'^pip\s+', re.I), "Runs pip package manager"),
    (re.compile(r'^pipx\s+install\s+', re.I), "Installs a Python CLI tool in isolation"),
    (re.compile(r'^poetry\s+', re.I), "Runs Poetry dependency manager"),
    (re.compile(r'^cargo\s+build\b', re.I), "Builds a Rust project"),
    (re.compile(r'^cargo\s+run\b', re.I), "Builds and runs a Rust project"),
    (re.compile(r'^cargo\s+test\b', re.I), "Runs Rust tests"),
    (re.compile(r'^cargo\s+publish\b', re.I), "Publishes a Rust crate"),
    (re.compile(r'^cargo\s+', re.I), "Runs a Cargo command"),
    (re.compile(r'^go\s+build\b', re.I), "Compiles Go packages"),
    (re.compile(r'^go\s+run\b', re.I), "Compiles and runs Go program"),
    (re.compile(r'^go\s+test\b', re.I), "Runs Go tests"),
    (re.compile(r'^go\s+', re.I), "Runs a Go command"),
    # ── docker / containers ──
    (re.compile(r'^docker\s+compose\s+up\b', re.I), "Starts containers via Compose"),
    (re.compile(r'^docker\s+compose\s+down\b', re.I), "Stops and removes containers"),
    (re.compile(r'^docker\s+compose\s+build\b', re.I), "Builds Compose service images"),
    (re.compile(r'^docker\s+compose\s+', re.I), "Runs Docker Compose command"),
    (re.compile(r'^docker\s+build\b', re.I), "Builds a Docker image"),
    (re.compile(r'^docker\s+run\b', re.I), "Runs a container"),
    (re.compile(r'^docker\s+push\b', re.I), "Pushes image to registry"),
    (re.compile(r'^docker\s+pull\b', re.I), "Pulls image from registry"),
    (re.compile(r'^docker\s+stop\b', re.I), "Stops running container(s)"),
    (re.compile(r'^docker\s+rm\b', re.I), "Removes container(s)"),
    (re.compile(r'^docker\s+rmi\b', re.I), "Removes image(s)"),
    (re.compile(r'^docker\s+system\s+prune', re.I), "Removes unused Docker data"),
    (re.compile(r'^docker\s+exec\b', re.I), "Executes command in a running container"),
    (re.compile(r'^docker\s+ps\b', re.I), "Lists running containers"),
    (re.compile(r'^docker\s+images\b', re.I), "Lists Docker images"),
    (re.compile(r'^docker\s+', re.I), "Runs a Docker command"),
    # ── kubernetes ──
    (re.compile(r'^kubectl\s+apply\b', re.I), "Applies Kubernetes manifests"),
    (re.compile(r'^kubectl\s+delete\b', re.I), "Deletes Kubernetes resources"),
    (re.compile(r'^kubectl\s+get\b', re.I), "Lists Kubernetes resources"),
    (re.compile(r'^kubectl\s+', re.I), "Runs a kubectl command"),
    # ── system / admin ──
    (re.compile(r'^sudo\s+(.+)', re.I), "Runs with elevated privileges: {0}"),
    (re.compile(r'^kill\s+-9\s+', re.I), "Force-kills a process"),
    (re.compile(r'^kill\s+', re.I), "Sends signal to a process"),
    (re.compile(r'^pkill\s+', re.I), "Kills processes by name"),
    (re.compile(r'^shutdown\b', re.I), "Shuts down the system"),
    (re.compile(r'^reboot\b', re.I), "Reboots the system"),
    (re.compile(r'^systemctl\s+', re.I), "Manages systemd services"),
    (re.compile(r'^service\s+', re.I), "Manages system services"),
    # ── network ──
    (re.compile(r'^curl\s+', re.I), "Makes an HTTP request"),
    (re.compile(r'^wget\s+', re.I), "Downloads a file from the web"),
    (re.compile(r'^ssh\s+', re.I), "Opens an SSH connection"),
    (re.compile(r'^scp\s+', re.I), "Copies files over SSH"),
    (re.compile(r'^rsync\s+', re.I), "Syncs files between locations"),
    (re.compile(r'^ping\s+', re.I), "Pings a host"),
    (re.compile(r'^nslookup\s+', re.I), "Queries DNS records"),
    (re.compile(r'^dig\s+', re.I), "Queries DNS records"),
    # ── deploy / publish ──
    (re.compile(r'^fly\s+deploy\b', re.I), "Deploys to Fly.io"),
    (re.compile(r'^fly\s+', re.I), "Runs a Fly.io command"),
    (re.compile(r'^vercel\s+', re.I), "Runs a Vercel command"),
    (re.compile(r'^netlify\s+deploy\b', re.I), "Deploys to Netlify"),
    (re.compile(r'^netlify\s+', re.I), "Runs a Netlify command"),
    (re.compile(r'^heroku\s+', re.I), "Runs a Heroku command"),
    (re.compile(r'^railway\s+', re.I), "Runs a Railway command"),
    (re.compile(r'^serverless\s+deploy\b', re.I), "Deploys via Serverless Framework"),
    (re.compile(r'^terraform\s+apply\b', re.I), "Applies Terraform changes"),
    (re.compile(r'^terraform\s+destroy\b', re.I), "Destroys Terraform-managed infrastructure"),
    (re.compile(r'^terraform\s+', re.I), "Runs a Terraform command"),
    # ── python ──
    (re.compile(r'^python3?\s+-m\s+pytest\b', re.I), "Runs Python tests"),
    (re.compile(r'^python3?\s+-m\s+(\S+)', re.I), "Runs Python module {0}"),
    (re.compile(r'^python3?\s+(\S+\.py)', re.I), "Runs Python script {0}"),
    (re.compile(r'^python3?\b', re.I), "Starts Python"),
    (re.compile(r'^pytest\b', re.I), "Runs Python tests"),
    # ── misc ──
    (re.compile(r'^echo\s+', re.I), "Prints text to stdout"),
    (re.compile(r'^export\s+', re.I), "Sets an environment variable"),
    (re.compile(r'^set\s+', re.I), "Sets a shell variable"),
    (re.compile(r'^env\b', re.I), "Prints environment variables"),
    (re.compile(r'^printenv\b', re.I), "Prints environment variables"),
    (re.compile(r'^source\s+', re.I), "Sources a shell script"),
    (re.compile(r'^\.\s+', re.I), "Sources a shell script"),
    (re.compile(r'^make\s+(\S+)', re.I), "Runs make target '{0}'"),
    (re.compile(r'^make$', re.I), "Runs the default make target"),
    (re.compile(r'^cmake\s+', re.I), "Configures a CMake build"),
    (re.compile(r'^xargs\s+', re.I), "Runs a command for each input line"),
    (re.compile(r'^crontab\s+', re.I), "Edits scheduled tasks"),
    (re.compile(r'^dd\s+', re.I), "Low-level block copy (disk utility)"),
    (re.compile(r'^mkfs\b', re.I), "Formats a filesystem"),
    # ── Windows PowerShell ──
    (re.compile(r'^Get-ChildItem\b', re.I), "Lists directory contents (PowerShell)"),
    (re.compile(r'^Remove-Item\b', re.I), "Deletes files (PowerShell)"),
    (re.compile(r'^Set-Location\b', re.I), "Changes directory (PowerShell)"),
    (re.compile(r'^Invoke-WebRequest\b', re.I), "Makes an HTTP request (PowerShell)"),
    (re.compile(r'^Start-Process\b', re.I), "Starts a process (PowerShell)"),
    (re.compile(r'^Stop-Process\b', re.I), "Stops a process (PowerShell)"),
]


def _explain_by_pattern(command: str) -> Optional[str]:
    """Return a 1-line explanation using pre-built templates, or *None*."""
    cmd = command.strip()
    for pattern, template in _EXPLANATION_PATTERNS:
        m = pattern.search(cmd)
        if m:
            try:
                return template.format(*m.groups())
            except (IndexError, KeyError):
                return template
    return None


def _generic_explanation(command: str) -> str:
    """Last-resort explanation derived from the base command name."""
    base = command.strip().split()[0] if command.strip() else "unknown"
    return f"Runs '{base}'"


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

        explanation = self._build_explanation(command, sub_commands)
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
            explanation=explanation,
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

    # ── explanation ───────────────────────────────────────────────────

    def _build_explanation(self, command: str, sub_commands: List[str]) -> str:
        if len(sub_commands) == 1:
            return _explain_by_pattern(sub_commands[0]) or _generic_explanation(sub_commands[0])

        explanations = []
        for sub in sub_commands:
            exp = _explain_by_pattern(sub) or _generic_explanation(sub)
            explanations.append(exp)
        return " ; ".join(explanations)

    # ── reversibility ─────────────────────────────────────────────────

    @staticmethod
    def _check_reversible(command: str) -> bool:
        for pattern in _IRREVERSIBLE_PATTERNS:
            if pattern.search(command):
                return False
        return True

    # ── blast radius ──────────────────────────────────────────────────

    def _estimate_blast_radius(self, command: str) -> str:
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
        source_label = {
            InputSource.PASTED: "pasted",
            InputSource.AI_TAGGED: "ai",
            InputSource.COPILOT_CLI: "copilot",
        }.get(source, "ai")
        return self.confirm_command(command, assessment, source_label=source_label)

    def confirm_command(
        self, command: str, ra: RiskAssessment, *, source_label: str,
    ) -> bool:
        """
        Present the 4-tier gate UX for an already-computed assessment.

        Used for pasted/AI commands (via ``evaluate``) and for typed shell
        commands (via ``CliaraShell._inline_gate``) so behavior stays consistent.
        """
        from cliara.console import get_console
        console = get_console()

        if ra.danger_level == DangerLevel.SAFE:
            return self._gate_safe(console, ra, source_label)
        if ra.danger_level == DangerLevel.CAUTION:
            return self._gate_caution(console, ra, source_label)
        if ra.danger_level == DangerLevel.DANGEROUS:
            return self._gate_dangerous(console, command, ra, source_label)
        return self._gate_critical(console, command, ra, source_label)

    # ── Tier 1: SAFE ──

    def _gate_safe(self, console, ra: RiskAssessment, label: str) -> bool:
        console.print(
            f" [dim bold] {label} [/dim bold]  [dim]{ra.explanation}[/dim]"
        )
        if self._auto_approve_safe:
            return True
        try:
            resp = input("  Press Enter to run, or 'n' to cancel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return resp not in ("n", "no")

    # ── Tier 2: CAUTION ──

    def _gate_caution(self, console, ra: RiskAssessment, label: str) -> bool:
        parts = [ra.explanation]
        if ra.blast_radius != "local":
            parts.append(f"scope: {ra.blast_radius}")
        if ra.context_warnings:
            parts.extend(ra.context_warnings)
        detail = " [yellow]|[/yellow] ".join(parts)

        console.print(f" [yellow bold] {label} [/yellow bold]  {detail}")

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
                        ra: RiskAssessment, label: str) -> bool:
        console.print(f" [red bold] {label} [/red bold]  {ra.explanation}")

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
                       ra: RiskAssessment, label: str) -> bool:
        console.print(f" [bold bright_red] {label} [/bold bright_red]  {ra.explanation}")

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

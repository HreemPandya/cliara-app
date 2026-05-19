"""Ambient pulse glyph.

A tiny colored glyph intended for the interactive prompt.

Color meanings:
- green  = clean working tree on the default branch (tests, if recorded, are passing)
- amber  = unstaged changes / off main / explicit test failure
- red    = failing CI on this branch (HEAD)
- purple = uncommitted work older than 24h

Notes on green:
- We do NOT require a recent test run for green. Most users don't run their
  full suite before every prompt; treating "unknown test status" as
  not-green made the dot perpetually amber on otherwise pristine repos.
- An *explicit* test failure (last recorded run had non-zero exit) still
  downgrades green so the user is notified.

Notes:
- This is synthesis, not data: glyph-only in the prompt.
- Expansion is via `cliara pulse`.
- Expensive sources (GitHub checks) are cached via pulse_store.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cliara.config import Config
from cliara.gh_api import GitHubClient, resolve_repo
from cliara.pulse_store import (
    get_ci_head_status,
    get_last_test_run,
    record_ci_head_status,
)


GLYPH = "●"  # tiny, readable in most terminals


@dataclass(frozen=True)
class PulseSnapshot:
    color: str  # green|amber|red|purple
    glyph: str
    repo_root: Optional[str]
    branch: Optional[str]
    default_branch: Optional[str]
    clean_tree: Optional[bool]
    has_unstaged: Optional[bool]
    has_uncommitted: Optional[bool]
    stale_uncommitted: Optional[bool]
    stale_age_s: Optional[float]
    tests_passing: Optional[bool]
    last_test_ts: Optional[float]
    ci_failing: Optional[bool]
    ci_summary: Optional[str]
    reasons: List[str]


def _git(args: List[str], cwd: Path, timeout_s: float = 2.5) -> str:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        if p.returncode != 0:
            return ""
        # Important: do NOT .strip() here. Some porcelain outputs rely on leading spaces.
        return (p.stdout or "").rstrip("\r\n")
    except Exception:
        return ""


def _in_git_repo(cwd: Path) -> bool:
    return bool(_git(["rev-parse", "--is-inside-work-tree"], cwd).strip())


def _git_root(cwd: Path) -> Optional[Path]:
    out = _git(["rev-parse", "--show-toplevel"], cwd).strip()
    return Path(out) if out else None


def _git_head_sha(cwd: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd).strip()


def _git_branch(cwd: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()


def _default_branch(cwd: Path) -> str:
    # Prefer local info: refs/remotes/origin/HEAD -> origin/main
    sym = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd).strip()
    if sym and "/" in sym:
        return sym.split("/", 1)[1].strip() or "main"
    return "main"


def _parse_porcelain_paths(line: str) -> List[str]:
    # porcelain v1: XY <path> or XY <orig> -> <new>
    s = (line or "").rstrip("\n")
    if len(s) < 4:
        return []
    rest = s[3:].strip()
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1].strip()
    if rest.startswith('"') and rest.endswith('"'):
        rest = rest[1:-1]
    return [rest] if rest else []


def _git_work_state(cwd: Path) -> Tuple[bool, bool, bool, List[str]]:
    """Return (clean_tree, has_uncommitted, has_unstaged, changed_paths)."""
    out = _git(["status", "--porcelain"], cwd)
    if not out:
        return True, False, False, []

    clean_tree = False
    has_uncommitted = False
    has_unstaged = False
    changed_paths: List[str] = []

    for line in out.splitlines():
        if not line:
            continue
        has_uncommitted = True
        if line.startswith("??"):
            has_unstaged = True
        elif len(line) >= 2 and line[1] != " ":
            has_unstaged = True
        changed_paths.extend(_parse_porcelain_paths(line))

    return clean_tree, has_uncommitted, has_unstaged, changed_paths


def _stale_uncommitted_age_s(repo_root: Path, changed_paths: List[str], *, now: float) -> Optional[float]:
    if not changed_paths:
        return None

    oldest_mtime: Optional[float] = None
    for rel in changed_paths:
        try:
            p = (repo_root / rel).resolve()
        except Exception:
            continue
        try:
            if not p.exists():
                continue
            mt = float(p.stat().st_mtime)
            if oldest_mtime is None or mt < oldest_mtime:
                oldest_mtime = mt
        except Exception:
            continue

    if oldest_mtime is None:
        return None

    return max(0.0, now - oldest_mtime)


_TEST_CMD_RE = re.compile(
    r"(^|\s)(pytest|py\.test)(\s|$)|(^|\s)python(\d+)?\s+-m\s+pytest(\s|$)",
    re.IGNORECASE,
)


def is_test_command(command: str) -> bool:
    return bool(_TEST_CMD_RE.search((command or "").strip()))


def _tests_passing(config: Config, repo_root: str) -> Tuple[Optional[bool], Optional[float], Optional[str]]:
    last = get_last_test_run(config.config_dir, repo_root)
    if not last:
        return None, None, None
    ok = last.exit_code == 0
    return ok, last.ts, last.command


def _ci_failing(
    config: Config,
    cwd: Path,
    repo_root: str,
    sha: str,
    *,
    now: float,
    fetch: bool,
) -> Tuple[Optional[bool], Optional[str]]:
    # Cache for prompts: avoid hitting GitHub repeatedly.
    cached = get_ci_head_status(config.config_dir, repo_root)
    ttl_s = 60.0
    if cached and cached.sha == sha and (now - cached.ts) < ttl_s:
        return bool(cached.failing), cached.summary or None

    if not fetch:
        # Use stale cache if it matches sha, else unknown.
        if cached and cached.sha == sha:
            return bool(cached.failing), cached.summary or None
        return None, None

    # Only try if we have a usable token.
    from cliara.auth import load_token, get_valid_token

    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if not tok:
        token_data = load_token() if get_valid_token() else None
        tok = (token_data.get("github_provider_token") or "").strip() if token_data else ""

    if not tok:
        return None, None

    try:
        ref = resolve_repo(cwd)
    except Exception:
        return None, None

    try:
        client = GitHubClient(tok, ref.api_base, timeout_s=4.0)
        checks = client.list_check_runs(ref.owner, ref.repo, sha)
    except Exception:
        return None, None

    if not checks:
        failing = False
        summary = "no checks"
        record_ci_head_status(config.config_dir, repo_root, sha=sha, failing=failing, summary=summary, ts=now)
        return failing, summary

    failing_conclusions = {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
    }
    total = 0
    failed = 0
    for c in checks:
        try:
            status = str(c.get("status") or "")
            conc = str(c.get("conclusion") or "")
        except Exception:
            continue
        if status != "completed":
            continue
        total += 1
        if conc in failing_conclusions:
            failed += 1

    failing = failed > 0
    summary = f"{failed}/{total} failing" if total else "no completed checks"
    record_ci_head_status(config.config_dir, repo_root, sha=sha, failing=failing, summary=summary, ts=now)
    return failing, summary


def compute_pulse(
    cwd: Optional[Path] = None,
    *,
    config: Optional[Config] = None,
    now: Optional[float] = None,
    fetch_ci: bool = False,
) -> PulseSnapshot:
    """Compute the ambient pulse for *cwd*.

    Set fetch_ci=True to query GitHub (uses cache + token).
    """

    root = (cwd or Path.cwd()).resolve()
    cfg = config or Config()
    t = float(now if now is not None else time.time())

    if not _in_git_repo(root):
        return PulseSnapshot(
            color="amber",
            glyph=GLYPH,
            repo_root=None,
            branch=None,
            default_branch=None,
            clean_tree=None,
            has_unstaged=None,
            has_uncommitted=None,
            stale_uncommitted=None,
            stale_age_s=None,
            tests_passing=None,
            last_test_ts=None,
            ci_failing=None,
            ci_summary=None,
            reasons=["not a git repo"],
        )

    repo_root_p = _git_root(root) or root
    repo_root = str(repo_root_p)

    branch = _git_branch(root) or None
    default_branch = _default_branch(root) or None

    clean_tree, has_uncommitted, has_unstaged, changed_paths = _git_work_state(root)
    stale_age_s = _stale_uncommitted_age_s(repo_root_p, changed_paths, now=t)
    stale_uncommitted = (stale_age_s is not None) and (stale_age_s > 24 * 3600)

    tests_ok, last_test_ts, _last_test_cmd = _tests_passing(cfg, repo_root)

    sha = _git_head_sha(root) or ""
    ci_fail, ci_summary = _ci_failing(cfg, root, repo_root, sha, now=t, fetch=fetch_ci)

    reasons: List[str] = []

    # precedence: red > purple > amber > green
    color = "amber"
    if ci_fail is True:
        color = "red"
        reasons.append("CI failing")
    elif stale_uncommitted:
        color = "purple"
        reasons.append("uncommitted work older than 24h")
    elif has_unstaged:
        color = "amber"
        reasons.append("unstaged changes")
    else:
        on_main = (branch is not None) and (default_branch is not None) and (branch == default_branch)
        # Green = clean tree, on main, and no *explicit* test failure on record.
        # Unknown test state (no test runs recorded yet) is treated as OK so the
        # dot reflects working-tree cleanliness rather than CI/test discipline.
        tests_not_failing = tests_ok is not False
        if clean_tree and on_main and tests_not_failing:
            color = "green"
            if tests_ok is True:
                reasons.append("clean + tests passing + on main")
            else:
                reasons.append("clean + on main")
        else:
            if not clean_tree:
                reasons.append("uncommitted changes")
            if tests_ok is False:
                reasons.append("last test run failed")
            if not on_main:
                reasons.append("not on main")

    return PulseSnapshot(
        color=color,
        glyph=GLYPH,
        repo_root=repo_root,
        branch=branch,
        default_branch=default_branch,
        clean_tree=clean_tree,
        has_unstaged=has_unstaged,
        has_uncommitted=has_uncommitted,
        stale_uncommitted=stale_uncommitted,
        stale_age_s=stale_age_s,
        tests_passing=tests_ok,
        last_test_ts=last_test_ts,
        ci_failing=ci_fail,
        ci_summary=ci_summary,
        reasons=reasons,
    )


def prompt_style_class(color: str) -> str:
    return {
        "green": "class:prompt-pulse-green",
        "amber": "class:prompt-pulse-amber",
        "red": "class:prompt-pulse-red",
        "purple": "class:prompt-pulse-purple",
    }.get(color, "class:prompt-pulse-amber")


def ansi_color_prefix(color: str) -> str:
    # Standard ANSI fg colors; keep minimal and widely supported.
    # green=32, amber/yellow=33, red=31, purple/magenta=35
    code = {
        "green": 32,
        "amber": 33,
        "red": 31,
        "purple": 35,
    }.get(color, 33)
    return f"\033[1;{code}m"


def ansi_color_suffix() -> str:
    return "\033[0m"


class PulseComputer:
    """Small cache wrapper so prompts don't run git/network per keystroke."""

    def __init__(self, config: Config):
        self._config = config
        self._cache: Optional[PulseSnapshot] = None
        self._cache_ts: float = 0.0

    def get(self, *, fetch_ci: bool = False, ttl_s: float = 3.0) -> PulseSnapshot:
        now = time.time()
        if self._cache and (now - self._cache_ts) < max(0.0, ttl_s):
            return self._cache
        snap = compute_pulse(config=self._config, now=now, fetch_ci=fetch_ci)
        self._cache = snap
        self._cache_ts = now
        return snap

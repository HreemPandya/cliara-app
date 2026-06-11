"""
Pre-commit code review (``? review``).

Collects the diff you're about to commit (staged by default, or the working
tree) and hands it to the ``code_review`` agent, which surfaces likely bugs,
missing tests, and undocumented public APIs.

This module holds only the git/diff plumbing so it can be unit-tested in
isolation; the LLM prompt lives in ``cliara/agents/prompts/code_review.md`` and
the shell wiring in ``cliara/shell_app/review_commands.py``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# Default cap on how much diff text we send to the model. Reviews need far more
# context than a commit message, but we still bound it to control token cost.
DEFAULT_MAX_DIFF_CHARS = 12000


@dataclass
class DiffInfo:
    """A snapshot of the changes under review."""

    stat: str = ""               # `git diff --stat` summary
    content: str = ""            # full unified diff
    files: List[str] = field(default_factory=list)
    staged: bool = True          # True = staged (index), False = working tree

    def is_empty(self) -> bool:
        return not self.content.strip() and not self.files


def _git(args: List[str], cwd: Optional[Path], timeout: int = 15) -> str:
    """Run a read-only git command and return stdout (empty string on failure).

    Always decodes as UTF-8 with ``errors="replace"`` — diffs routinely contain
    bytes that the Windows default (cp1252) can't decode, which would otherwise
    crash the subprocess reader thread.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout or ""


def get_repo_root(cwd: Optional[str] = None) -> Optional[Path]:
    """Return the git work-tree root for *cwd*, or None if not a repo."""
    out = _git(["rev-parse", "--show-toplevel"], Path(cwd) if cwd else None, timeout=5).strip()
    if not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
        return Path(out)


def get_diff_info(repo_root: Optional[Path], staged: bool = True) -> DiffInfo:
    """Collect the diff to review.

    *staged* True  → ``git diff --cached`` (what ``git commit`` would record).
    *staged* False → ``git diff`` (unstaged working-tree changes).
    """
    cached = ["--cached"] if staged else []
    stat = _git(["diff", *cached, "--stat"], repo_root).strip()
    content = _git(["diff", *cached], repo_root).strip()
    names = _git(["diff", *cached, "--name-only"], repo_root)
    files = [f for f in (names or "").splitlines() if f.strip()]
    return DiffInfo(stat=stat, content=content, files=files, staged=staged)


def truncate_diff(content: str, max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> Tuple[str, bool]:
    """Return (possibly-truncated diff, was_truncated).

    Truncates on a line boundary so the model never sees a half-line of a hunk.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    clipped = content[:max_chars]
    # Back up to the last newline so we don't cut a line in half.
    nl = clipped.rfind("\n")
    if nl > 0:
        clipped = clipped[:nl]
    return clipped + "\n\n... (diff truncated for review) ...", True

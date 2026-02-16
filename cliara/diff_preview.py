"""
Diff preview for destructive operations.

Intercepts rm, git checkout, git clean, and git reset to show exactly
what will be affected *before* the command runs.  This goes beyond
safety checks (which just warn about danger) — it shows you the
concrete impact so you can make an informed decision.
"""

import glob
import re
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    """Format file size in human-readable form (e.g. 4.1MB)."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def _dir_size(path: Path) -> int:
    """Total size of every file beneath *path*, recursively."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# DiffPreview
# ---------------------------------------------------------------------------

class DiffPreview:
    """
    Intercept destructive commands and show what will be affected.

    Instead of "this is dangerous" (which the SafetyChecker already does),
    this class answers "you're about to delete 8.2MB of logs — here they are".
    """

    # Compiled once at class level
    _RM_RE = re.compile(r"^(rm|del|erase)\b", re.IGNORECASE)
    _GIT_CHECKOUT_RE = re.compile(r"^git\s+checkout\b", re.IGNORECASE)
    _GIT_RESTORE_RE = re.compile(r"^git\s+restore\b", re.IGNORECASE)
    _GIT_CLEAN_RE = re.compile(r"^git\s+clean\b", re.IGNORECASE)
    _GIT_RESET_RE = re.compile(r"^git\s+reset\b", re.IGNORECASE)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def should_preview(self, command: str) -> bool:
        """Return *True* if *command* deserves a diff preview."""
        cmd = command.strip()
        if self._RM_RE.search(cmd):
            return True
        if self._GIT_CHECKOUT_RE.search(cmd):
            return self._is_checkout_restore(cmd)
        if self._GIT_RESTORE_RE.search(cmd):
            return True
        if self._GIT_CLEAN_RE.search(cmd):
            return True
        if self._GIT_RESET_RE.search(cmd):
            return self._is_reset_destructive(cmd)
        return False

    def generate_preview(self, command: str) -> Optional[str]:
        """
        Build the human-readable preview string.

        Returns *None* when the preview cannot be generated (e.g. no
        matching files, not inside a git repo, …).
        """
        cmd = command.strip()
        if self._RM_RE.search(cmd):
            return self._preview_rm(cmd)
        if self._GIT_CHECKOUT_RE.search(cmd):
            return self._preview_git_checkout(cmd)
        if self._GIT_RESTORE_RE.search(cmd):
            return self._preview_git_restore(cmd)
        if self._GIT_CLEAN_RE.search(cmd):
            return self._preview_git_clean(cmd)
        if self._GIT_RESET_RE.search(cmd):
            return self._preview_git_reset(cmd)
        return None

    # ------------------------------------------------------------------ #
    # rm / del                                                            #
    # ------------------------------------------------------------------ #

    def _preview_rm(self, command: str) -> Optional[str]:
        """Preview files that ``rm`` / ``del`` will delete."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        if not tokens:
            return None

        # Separate flags from file arguments
        file_args: List[str] = []
        is_recursive = False
        for tok in tokens[1:]:
            if tok.startswith("-"):
                if "r" in tok or "R" in tok:
                    is_recursive = True
            else:
                file_args.append(tok)

        if not file_args:
            return None

        # Expand globs / literal paths
        matched: List[Tuple[str, int, bool]] = []  # (path, size, is_dir)
        for pattern in file_args:
            expanded = glob.glob(pattern)
            if not expanded:
                p = Path(pattern)
                if p.exists():
                    expanded = [pattern]

            for path_str in expanded:
                p = Path(path_str)
                if p.is_dir():
                    size = _dir_size(p) if is_recursive else 0
                    matched.append((path_str, size, True))
                elif p.is_file():
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    matched.append((path_str, size, False))

        if not matched:
            return None

        total_size = sum(s for _, s, _ in matched)
        file_count = sum(1 for _, _, d in matched if not d)
        dir_count = sum(1 for _, _, d in matched if d)

        lines: List[str] = []
        lines.append(f"  [Preview] Will delete {len(matched)} item(s):")

        MAX_SHOW = 5
        for path_str, size, is_dir in matched[:MAX_SHOW]:
            tag = " (dir)" if is_dir else ""
            lines.append(f"    {path_str}{tag} ({_format_size(size)})")

        if len(matched) > MAX_SHOW:
            remaining = len(matched) - MAX_SHOW
            remaining_size = sum(s for _, s, _ in matched[MAX_SHOW:])
            lines.append(
                f"    ... {remaining} more ({_format_size(remaining_size)})"
            )

        parts: List[str] = []
        if file_count:
            parts.append(f"{file_count} file{'s' if file_count != 1 else ''}")
        if dir_count:
            parts.append(
                f"{dir_count} director{'ies' if dir_count != 1 else 'y'}"
            )
        lines.append(f"  Total: {', '.join(parts)}, {_format_size(total_size)}")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # git checkout (restore working-tree changes)                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_checkout_restore(command: str) -> bool:
        """
        Return *True* when ``git checkout`` restores files (discarding
        working-tree changes) rather than switching branches.
        """
        parts = command.split()
        args = parts[2:] if len(parts) > 2 else []

        if not args:
            return False

        # "git checkout ." or "git checkout -- ."
        if "." in args or "--" in args:
            return True

        # "git checkout HEAD -- <file>"
        if "HEAD" in args:
            return True

        # Check if any argument matches a modified file
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                modified = set(result.stdout.strip().splitlines())
                for arg in args:
                    if arg in modified:
                        return True
        except Exception:
            pass

        return False

    def _preview_git_checkout(self, command: str) -> Optional[str]:
        """Preview changes that ``git checkout`` will discard."""
        parts = command.split()
        args = parts[2:] if len(parts) > 2 else []
        file_args = [a for a in args if a not in (".", "--", "HEAD")]

        if "." in args or not file_args:
            numstat_cmd = ["git", "diff", "--numstat"]
        else:
            numstat_cmd = ["git", "diff", "--numstat", "--"] + file_args

        return self._preview_discard_changes(numstat_cmd, verb="discard")

    # ------------------------------------------------------------------ #
    # git restore                                                         #
    # ------------------------------------------------------------------ #

    def _preview_git_restore(self, command: str) -> Optional[str]:
        """Preview changes that ``git restore`` will discard."""
        parts = command.split()
        args = parts[2:] if len(parts) > 2 else []
        file_args = [a for a in args if not a.startswith("-") and a != "."]

        if "." in args or not file_args:
            numstat_cmd = ["git", "diff", "--numstat"]
        else:
            numstat_cmd = ["git", "diff", "--numstat", "--"] + file_args

        return self._preview_discard_changes(numstat_cmd, verb="discard")

    # ------------------------------------------------------------------ #
    # Shared helper for checkout / restore previews                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _preview_discard_changes(
        numstat_cmd: List[str], *, verb: str = "discard"
    ) -> Optional[str]:
        """
        Run a ``git diff --numstat`` variant and format the result as a
        human-readable preview of changes that will be lost.
        """
        try:
            result = subprocess.run(
                numstat_cmd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            raw_lines = result.stdout.strip().splitlines()
        except Exception:
            return None

        files_info: List[Tuple[str, int, int]] = []
        for line in raw_lines:
            cols = line.split("\t")
            if len(cols) == 3:
                try:
                    added = int(cols[0]) if cols[0] != "-" else 0
                    removed = int(cols[1]) if cols[1] != "-" else 0
                except ValueError:
                    added = removed = 0
                files_info.append((cols[2], added, removed))

        if not files_info:
            return None

        max_name = max(len(f) for f, _, _ in files_info)
        total_added = sum(a for _, a, _ in files_info)
        total_removed = sum(r for _, _, r in files_info)

        lines: List[str] = []
        lines.append(
            f"  [Preview] Will {verb} changes in {len(files_info)} file(s):"
        )

        MAX_SHOW = 8
        for fname, added, removed in files_info[:MAX_SHOW]:
            lines.append(f"    {fname:<{max_name}}  (+{added} -{removed})")

        if len(files_info) > MAX_SHOW:
            lines.append(f"    ... {len(files_info) - MAX_SHOW} more file(s)")

        lines.append(
            f"  Total: +{total_added} -{total_removed} lines "
            f"across {len(files_info)} file(s)"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # git clean                                                           #
    # ------------------------------------------------------------------ #

    def _preview_git_clean(self, command: str) -> Optional[str]:
        """Preview untracked files that ``git clean`` will remove."""
        parts = command.split()
        user_args = parts[2:] if len(parts) > 2 else []

        # Build a dry-run variant of the command
        dry_args = ["git", "clean", "-n"]
        has_d = False
        has_f = False
        for arg in user_args:
            if arg in ("-n", "--dry-run"):
                continue
            if "-d" in arg:
                has_d = True
            if "-f" in arg or "--force" in arg:
                has_f = True
            dry_args.append(arg)

        if not has_f:
            dry_args.insert(3, "-f")
        if not has_d:
            dry_args.append("-d")

        try:
            result = subprocess.run(
                dry_args, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            output = result.stdout.strip()
            if not output:
                return "  [Preview] No untracked files to clean."
            raw_lines = output.splitlines()
        except Exception:
            return None

        files: List[str] = []
        for line in raw_lines:
            if line.startswith("Would remove "):
                files.append(line[len("Would remove "):].strip())

        if not files:
            return "  [Preview] No untracked files to clean."

        items: List[Tuple[str, int, bool]] = []
        total_size = 0
        for fpath in files:
            p = Path(fpath)
            if p.is_dir():
                size = _dir_size(p)
                items.append((fpath, size, True))
            else:
                try:
                    size = p.stat().st_size if p.exists() else 0
                except OSError:
                    size = 0
                items.append((fpath, size, False))
            total_size += items[-1][1]

        lines: List[str] = []
        lines.append(
            f"  [Preview] Will remove {len(items)} untracked item(s):"
        )

        MAX_SHOW = 8
        for fpath, size, is_dir in items[:MAX_SHOW]:
            tag = " (dir)" if is_dir else ""
            lines.append(f"    {fpath}{tag} ({_format_size(size)})")

        if len(items) > MAX_SHOW:
            lines.append(f"    ... {len(items) - MAX_SHOW} more")

        lines.append(f"  Total: {_format_size(total_size)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # git reset                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_reset_destructive(command: str) -> bool:
        """Return *True* when ``git reset`` can lose data."""
        parts = command.split()
        args = parts[2:] if len(parts) > 2 else []

        if "--hard" in args:
            return True
        # --mixed / --soft still re-write history when a target is given
        for arg in args:
            if arg.startswith("-"):
                continue
            # Looks like a ref: HEAD~2, a commit hash, a branch name, etc.
            if arg.startswith("HEAD") or re.match(r"^[a-f0-9]{6,}$", arg):
                return True
        return False

    def _preview_git_reset(self, command: str) -> Optional[str]:
        """Preview what ``git reset`` will do."""
        parts = command.split()
        args = parts[2:] if len(parts) > 2 else []

        is_hard = "--hard" in args
        is_soft = "--soft" in args

        # Find the target ref (first non-flag argument)
        target = None
        for arg in args:
            if not arg.startswith("-"):
                target = arg
                break
        if not target:
            target = "HEAD"

        lines: List[str] = []

        if is_hard:
            lines.append("  [Preview] git reset --hard will:")

            # Commits that will be removed
            if target != "HEAD":
                self._append_commit_log(lines, target)

            # Working-tree changes that will be lost
            self._append_working_changes(lines)

            # Staged changes that will be lost
            self._append_staged_changes(lines)
        else:
            mode = "soft" if is_soft else "mixed"
            lines.append(f"  [Preview] git reset --{mode} will:")

            if target != "HEAD":
                verb = "Uncommit" if is_soft else "Uncommit and unstage"
                self._append_commit_log(lines, target, verb=verb)
                if not is_soft:
                    lines.append(
                        "  Changes will remain in your working directory."
                    )

        # Nothing useful to show
        if len(lines) <= 1:
            return None

        return "\n".join(lines)

    # ── git reset sub-helpers ──────────────────────────────────────────

    @staticmethod
    def _append_commit_log(
        lines: List[str], target: str, *, verb: str = "Discard"
    ):
        """Append a compact commit log between *target* and HEAD."""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", f"{target}..HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                commits = result.stdout.strip().splitlines()
                lines.append(f"  {verb} {len(commits)} commit(s):")
                for c in commits[:5]:
                    lines.append(f"    {c}")
                if len(commits) > 5:
                    lines.append(f"    ... {len(commits) - 5} more")
        except Exception:
            pass

    @staticmethod
    def _append_working_changes(lines: List[str]):
        """Append a summary of unstaged working-tree changes."""
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                rows = result.stdout.strip().splitlines()
                lines.append(
                    f"  Discard working changes in {len(rows)} file(s):"
                )
                for row in rows[:5]:
                    cols = row.split("\t")
                    if len(cols) == 3:
                        lines.append(
                            f"    {cols[2]}  (+{cols[0]} -{cols[1]})"
                        )
                if len(rows) > 5:
                    lines.append(f"    ... {len(rows) - 5} more")
        except Exception:
            pass

    @staticmethod
    def _append_staged_changes(lines: List[str]):
        """Append a summary of staged (cached) changes."""
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--numstat"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                rows = result.stdout.strip().splitlines()
                lines.append(
                    f"  Discard staged changes in {len(rows)} file(s):"
                )
                for row in rows[:5]:
                    cols = row.split("\t")
                    if len(cols) == 3:
                        lines.append(
                            f"    {cols[2]}  (+{cols[0]} -{cols[1]})"
                        )
                if len(rows) > 5:
                    lines.append(f"    ... {len(rows) - 5} more")
        except Exception:
            pass

"""
Ghost Run — Cliara's parallel-universe shadow executor.

Today ``diff_preview`` *predicts* what a destructive command will touch using
static glob heuristics. Ghost Run actually **runs the command** — against a
hardlink-clone of the current directory in a throwaway sandbox — and reports
the real resulting filesystem diff (deleted / created / modified, byte
deltas) before a single real byte changes. The gate stops asking "are you
sure?" and starts showing you the future.

How the universe is forked
--------------------------
Rather than rewriting path arguments token-by-token (fragile for globs, brace
expansion, recursive flags), Ghost Run clones the **entire working
directory** into a sandbox and runs the command there *unchanged*, with the
sandbox as cwd. Relative semantics are therefore byte-for-byte identical to
the real run.

Cloning uses ``os.link`` (hardlinks) per file — near-zero disk cost — and
falls back to a size-capped real copy when hardlinks are unavailable
(cross-volume temp, exotic filesystems). Hardlinks are only safe because the
supported grammar is **deletion-only** (``rm`` / ``del`` / ``erase`` / ``rd``
and ``git clean``): unlinking a hardlinked file never touches the original's
bytes. Commands that could write *in place* through a link are refused by
:func:`assess_shadowability` — honesty over cleverness.

What the ghost refuses (and says so plainly)
--------------------------------------------
* anything outside the deletion grammar,
* compound commands / pipes / redirects (side effects can escape the sandbox),
* network or system-level side effects (``sudo``, ``curl``, ``shutdown``, …),
* absolute or outside-cwd targets (the parallel universe spans cwd only),
* root-like working directories and trees over the file/byte caps.

This module is intentionally free of any UI/console code so it can be unit
tested in isolation. The shell wiring (panels, prompts, personality) lives in
``cliara/shell_app/ghost_commands.py``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cliara.diff_preview import _expand_pattern, _is_root_like_path


# ── Caps (overridable via config through the shell mixin) ────────────────────

DEFAULT_MAX_FILES = 50_000          # files in the cwd tree before we refuse
DEFAULT_MAX_COPY_BYTES = 500 * 1024 * 1024   # bytes physically copied (hardlink fallback)
DEFAULT_TIMEOUT_SECONDS = 120.0

SANDBOX_PREFIX = "cliara-ghost-"
STALE_SANDBOX_MAX_AGE_S = 24 * 3600


# ── Deletion grammar ──────────────────────────────────────────────────────────

_DELETE_CMDS = frozenset({"rm", "del", "erase", "rd", "rmdir"})

# Shell metacharacters that let side effects escape the sandbox (pipes,
# chaining, redirects, substitution). The ghost refuses these outright.
_COMPOUND_RE = re.compile(r"[|;&<>`$]|\&\&|\|\|")

# Commands whose side effects live outside the filesystem — a sandbox can't
# contain them, so the ghost declines honestly instead of lying.
_UNSHADOWABLE_RE = re.compile(
    r"\b(sudo|curl|wget|ssh|scp|rsync\s+.*:|shutdown|reboot|mkfs|dd|"
    r"systemctl|service|npm\s+publish|cargo\s+publish|docker|kubectl|"
    r"terraform|fly|vercel|netlify|heroku|gh)\b",
    re.IGNORECASE,
)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ShadowVerdict:
    """Can this command be ghost-run? If not, *reason* says why (plainly)."""

    eligible: bool
    kind: str = ""          # "delete" | "git_clean"
    reason: str = ""        # human-readable refusal (empty when eligible)


@dataclass
class CloneStats:
    files: int = 0
    dirs: int = 0
    linked: int = 0
    copied: int = 0
    copied_bytes: int = 0
    symlinks_skipped: int = 0
    errors: int = 0


@dataclass
class FileChange:
    path: str               # cwd-relative, forward slashes
    size: int               # bytes (old size for deleted/modified, new for created)


@dataclass
class ShadowDiff:
    deleted: List[FileChange] = field(default_factory=list)
    created: List[FileChange] = field(default_factory=list)
    modified: List[FileChange] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.deleted or self.created or self.modified)

    @property
    def bytes_deleted(self) -> int:
        return sum(c.size for c in self.deleted)


@dataclass
class ShadowResult:
    """Everything the ghost saw in the parallel universe."""

    command: str
    exit_code: int
    diff: ShadowDiff
    stdout: str
    stderr: str
    clone: CloneStats
    elapsed_s: float
    sandbox_kept: bool = False   # sandboxes are destroyed; True only on cleanup failure


# ── Eligibility ───────────────────────────────────────────────────────────────

def _first_token(command: str) -> str:
    parts = (command or "").strip().split()
    return parts[0].lower() if parts else ""


# Windows-style command switches (del /f /q, rd /s) — short, letter-only.
_WIN_SWITCH_RE = re.compile(r"/[a-zA-Z?]{1,3}$")


def _delete_targets(command: str) -> List[str]:
    """Non-flag arguments of an rm-family command (raw, unexpanded).

    Tokenizes in non-POSIX mode on Windows so backslash paths survive
    (POSIX shlex would eat them: ``C:\\temp\\x`` → ``C:tempx``), then strips
    surrounding quotes. ``-x`` flags and short ``/x`` switches are dropped;
    a POSIX absolute path like ``/tmp/x`` is kept as a target (so the
    absolute-path refusal can fire on it).
    """
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        tokens = command.split()
    out: List[str] = []
    for t in tokens[1:]:
        t = t.strip().strip("\"'")
        if not t or t.startswith("-"):
            continue
        if _WIN_SWITCH_RE.fullmatch(t):
            continue
        out.append(t)
    return out


def assess_shadowability(command: str, cwd: Optional[Path] = None) -> ShadowVerdict:
    """Decide whether *command* can be faithfully run in a parallel universe.

    Whitelist-only: the ghost would rather refuse honestly than simulate a
    command whose side effects could differ from (or escape into) reality.
    """
    cmd = (command or "").strip()
    cwd = Path(cwd) if cwd else Path.cwd()

    if not cmd:
        return ShadowVerdict(False, reason="There is no command to ghost-run.")

    if _COMPOUND_RE.search(cmd):
        return ShadowVerdict(
            False,
            reason="Pipes, chaining, and redirects can leak outside the sandbox — "
                   "the ghost only runs single, simple commands.",
        )

    if _UNSHADOWABLE_RE.search(cmd):
        return ShadowVerdict(
            False,
            reason="That command has side effects beyond the filesystem "
                   "(network/system/registry). A parallel universe can't contain "
                   "those, so the ghost won't pretend it can.",
        )

    first = _first_token(cmd)

    # ── git clean ────────────────────────────────────────────────────────
    if first == "git":
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        if sub != "clean":
            return ShadowVerdict(
                False,
                reason="The ghost speaks only deletion magic so far: "
                       "rm / del / erase / rd, and git clean.",
            )
        if not (cwd / ".git").exists():
            return ShadowVerdict(
                False,
                reason="Ghost-running `git clean` needs the repo root as cwd — "
                       "the parallel universe only spans the current directory.",
            )
        return ShadowVerdict(True, kind="git_clean")

    # ── rm / del / erase / rd ────────────────────────────────────────────
    if first in _DELETE_CMDS:
        targets = _delete_targets(cmd)
        if not targets:
            return ShadowVerdict(
                False,
                reason="No file targets found — nothing for the ghost to test.",
            )
        for raw in targets:
            p = Path(raw).expanduser()
            if p.is_absolute() or raw.startswith("~"):
                return ShadowVerdict(
                    False,
                    reason=f"'{raw}' is an absolute path. The parallel universe "
                           "spans the current directory only — cd closer first.",
                )
            if ".." in Path(raw).parts:
                return ShadowVerdict(
                    False,
                    reason=f"'{raw}' climbs out of the current directory — "
                           "the ghost can't follow you out of the universe.",
                )
        return ShadowVerdict(True, kind="delete")

    return ShadowVerdict(
        False,
        reason="The ghost speaks only deletion magic so far: "
               "rm / del / erase / rd, and git clean.",
    )


def expanded_targets_exist(command: str, cwd: Optional[Path] = None) -> bool:
    """True when at least one delete target resolves to a real file/glob hit.

    Lets the UI short-circuit with "the ghost found nothing to delete" without
    forking a universe. git clean is always considered live (git decides).

    Patterns are anchored to *cwd* by prefixing (no ``os.chdir`` — the REPL
    has background threads that read ``Path.cwd()``).
    """
    if _first_token(command) == "git":
        return True
    base = Path(cwd) if cwd else Path.cwd()
    try:
        for raw in _delete_targets(command):
            anchored = str(base / raw)
            if _expand_pattern(anchored):
                return True
        return False
    except (OSError, ValueError):
        return True  # can't tell — let the ghost find out


# ── Universe forking (clone) ─────────────────────────────────────────────────

def count_tree(root: Path, max_files: int) -> Tuple[int, bool]:
    """Count files under *root*; stops early once *max_files* is exceeded."""
    n = 0
    try:
        for _dirpath, _dirnames, filenames in os.walk(str(root), followlinks=False):
            n += len(filenames)
            if n > max_files:
                return n, True
    except OSError:
        pass
    return n, False


def clone_tree(
    src: Path,
    dst: Path,
    max_files: int = DEFAULT_MAX_FILES,
    max_copy_bytes: int = DEFAULT_MAX_COPY_BYTES,
) -> CloneStats:
    """Mirror *src* into *dst* with hardlinks (near-free) or capped copies.

    Hardlinks are safe here because the supported grammar is deletion-only —
    removing a directory entry in the sandbox never alters the original file.
    When hardlinking fails (cross-volume temp), each file falls back to a
    real copy, debited against *max_copy_bytes*.

    Raises ``RuntimeError`` with a human-readable message when a cap is hit.
    """
    stats = CloneStats()
    src = Path(src)
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(str(src), followlinks=False):
        rel = os.path.relpath(dirpath, str(src))
        out_dir = dst if rel == "." else dst / rel
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            stats.dirs += 1
        except OSError:
            stats.errors += 1
            dirnames[:] = []
            continue

        # Don't descend into symlinked dirs — they point at the real universe.
        keep = []
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                stats.symlinks_skipped += 1
            else:
                keep.append(d)
        dirnames[:] = keep

        for name in filenames:
            src_file = os.path.join(dirpath, name)
            dst_file = str(out_dir / name)
            if os.path.islink(src_file):
                stats.symlinks_skipped += 1
                continue
            stats.files += 1
            if stats.files > max_files:
                raise RuntimeError(
                    f"This universe is too big to fork: more than "
                    f"{max_files:,} files under {src}."
                )
            try:
                os.link(src_file, dst_file)
                stats.linked += 1
            except OSError:
                try:
                    size = os.path.getsize(src_file)
                    if stats.copied_bytes + size > max_copy_bytes:
                        raise RuntimeError(
                            "Hardlinks aren't available here and copying would "
                            f"exceed the {max_copy_bytes // (1024 * 1024)} MB cap — "
                            "the fork is too expensive."
                        )
                    shutil.copy2(src_file, dst_file)
                    stats.copied += 1
                    stats.copied_bytes += size
                except RuntimeError:
                    raise
                except OSError:
                    stats.errors += 1
    return stats


# ── Snapshot + diff ───────────────────────────────────────────────────────────

def snapshot_tree(root: Path) -> Dict[str, Tuple[int, int]]:
    """``{relpath: (size, mtime_ns)}`` for every regular file under *root*."""
    manifest: Dict[str, Tuple[int, int]] = {}
    root_s = str(root)
    for dirpath, _dirnames, filenames in os.walk(root_s, followlinks=False):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            if os.path.islink(fp):
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, root_s).replace(os.sep, "/")
            manifest[rel] = (int(st.st_size), int(st.st_mtime_ns))
    return manifest


def diff_manifests(
    before: Dict[str, Tuple[int, int]],
    after: Dict[str, Tuple[int, int]],
) -> ShadowDiff:
    diff = ShadowDiff()
    for rel, (size, mtime) in before.items():
        post = after.get(rel)
        if post is None:
            diff.deleted.append(FileChange(rel, size))
        elif post != (size, mtime):
            diff.modified.append(FileChange(rel, post[0]))
    for rel, (size, _mtime) in after.items():
        if rel not in before:
            diff.created.append(FileChange(rel, size))
    diff.deleted.sort(key=lambda c: (-c.size, c.path))
    diff.created.sort(key=lambda c: (-c.size, c.path))
    diff.modified.sort(key=lambda c: (-c.size, c.path))
    return diff


# ── Sandbox lifecycle ─────────────────────────────────────────────────────────

def sandbox_base() -> Path:
    return Path(tempfile.gettempdir()) / "cliara-shadow"


def purge_stale_sandboxes(base: Optional[Path] = None, max_age_s: float = STALE_SANDBOX_MAX_AGE_S) -> int:
    """Remove leftover universes from crashed sessions. Returns count removed."""
    base = base or sandbox_base()
    removed = 0
    try:
        if not base.is_dir():
            return 0
        now = time.time()
        for child in base.iterdir():
            if not child.name.startswith(SANDBOX_PREFIX):
                continue
            try:
                if (now - child.stat().st_mtime) > max_age_s:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


# ── The main event ────────────────────────────────────────────────────────────

def run_shadow(
    command: str,
    cwd: Optional[Path] = None,
    *,
    shell_path: Optional[str] = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_copy_bytes: int = DEFAULT_MAX_COPY_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
) -> ShadowResult:
    """Fork the universe, run *command* in it, diff, destroy, report.

    Caller is expected to have passed :func:`assess_shadowability` first;
    this function still refuses root-like cwds as a last line of defense.

    Raises ``RuntimeError`` with a friendly message when the fork is
    impossible (caps, root-like cwd, clone failure).
    """
    cwd = Path(cwd) if cwd else Path.cwd()
    if _is_root_like_path(cwd):
        raise RuntimeError(
            "The current directory is a filesystem root — the ghost refuses "
            "to fork an entire universe that large."
        )

    # Pre-flight count so we fail fast (and cheaply) on monster trees.
    n, over = count_tree(cwd, max_files)
    if over:
        raise RuntimeError(
            f"This universe is too big to fork: over {max_files:,} files here. "
            "Raise ghost_run_max_files in config if you really mean it."
        )

    base = sandbox_base()
    base.mkdir(parents=True, exist_ok=True)
    sandbox = base / f"{SANDBOX_PREFIX}{uuid.uuid4().hex[:10]}"

    started = time.time()
    try:
        clone = clone_tree(cwd, sandbox, max_files=max_files, max_copy_bytes=max_copy_bytes)
        before = snapshot_tree(sandbox)

        # Execute exactly like the real shell would, but with cwd = sandbox.
        low_shell = (shell_path or "").lower()
        use_powershell = os.name == "nt" and ("powershell" in low_shell or "pwsh" in low_shell)
        if use_powershell:
            ps_exe = "pwsh" if "pwsh" in low_shell else "powershell"
            proc = subprocess.run(
                [ps_exe, "-NoProfile", "-Command", command],
                cwd=str(sandbox),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        else:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(sandbox),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )

        after = snapshot_tree(sandbox)
        diff = diff_manifests(before, after)
        elapsed = time.time() - started

        return ShadowResult(
            command=command,
            exit_code=int(proc.returncode),
            diff=diff,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            clone=clone,
            elapsed_s=elapsed,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"The ghost got lost in the parallel universe (>{int(timeout_s)}s). "
            "Nothing real was touched."
        )
    finally:
        # The parallel universe is always destroyed. Unlinking hardlinks only
        # removes the sandbox's directory entries — originals are untouched.
        try:
            shutil.rmtree(sandbox, ignore_errors=True)
        except Exception:
            pass


# ── Formatting helpers (pure, shared with the UI layer) ──────────────────────

def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"

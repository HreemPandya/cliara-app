"""Self-maintaining codebase index — the decision core ("Index Sentinel").

The codebase RAG index (see :mod:`cliara.codebase_rag`) is only useful if it
reflects the code as it is *now*. Making the user remember to run
``index rebuild`` after every branch switch or edit is exactly the kind of
manual chore Cliara exists to remove.

This module holds the *pure, side-effect-free* logic that decides **whether**
a reindex is warranted right now. The threading, queueing, and the actual
``index_repository`` call live in :class:`cliara.shell_app.auto_index.AutoIndexMixin`
so this part stays trivially unit-testable without git, embeddings, or a shell.

Best practices baked into the algorithm:

* **Cheap change-detection first.** A reindex (embeddings) is expensive; a
  fingerprint (a couple of fast ``git`` calls) is not. We never embed unless a
  fingerprint actually changed.
* **Debounce / throttle.** Ordinary edits coalesce: at most one reindex per
  ``min_interval_s``. Disruptive *git events* (pull, checkout, merge, …) bypass
  the throttle so the index snaps back to fresh immediately.
* **Exponential backoff on failure.** A missing embedding backend or a flaky
  API must not trigger a reindex storm on every keystroke.
* **Convergence.** The fingerprint recorded on success is the one observed
  *after* indexing, so edits made *during* a pass are caught on the next tick.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


# How often the shell bothers to compute a fingerprint at all (seconds of
# wall-clock between cheap checks, on the post-command hot path). Bypassed when
# a disruptive git command just ran. Kept internal — not a user knob.
DEFAULT_CHECK_INTERVAL_S = 6.0

# Default minimum seconds between actual reindex passes for ordinary edits.
# Overridable via the ``codebase_auto_index_min_interval_s`` config key.
DEFAULT_MIN_INTERVAL_S = 15.0

# Failure backoff bounds.
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 600.0

# Cap on how many working-tree paths we stat for the mtime signal, so a repo
# with tens of thousands of untracked files can't make the check expensive.
_MTIME_SCAN_CAP = 500


# Commands that rewrite the working tree wholesale. Matching one of these lets
# the resulting reindex skip the throttle — the index would otherwise look
# badly stale right after a branch switch or pull. The regex is a *hint* only;
# the fingerprint remains the source of truth for whether anything changed.
_INDEX_DISRUPTING_RE = re.compile(
    r"""^\s*git\s+              # a git invocation ...
        (?:-c\s+\S+\s+)*         # ... optionally with -c overrides ...
        (?P<sub>
            pull | merge | rebase | checkout | switch | reset |
            stash | revert | cherry-pick | clone | clean | am | restore
        )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# `git stash` only disrupts the tree on pop/apply/drop-into-worktree, not on a
# bare `git stash list`. Keep it simple: treat any stash that isn't an obvious
# read-only subcommand as disruptive.
_STASH_READONLY_RE = re.compile(r"^\s*git\s+stash\s+(list|show)\b", re.IGNORECASE)


def is_index_disrupting_command(command: str) -> bool:
    """True if *command* likely rewrote many tracked files (branch switch, pull…).

    Used purely to decide whether a pending reindex may bypass the ordinary
    throttle for snappier freshness — never to decide *that* a reindex happens.
    """
    if not command:
        return False
    if _STASH_READONLY_RE.match(command):
        return False
    return bool(_INDEX_DISRUPTING_RE.match(command))


@dataclass(frozen=True)
class Fingerprint:
    """A cheap snapshot of repo state used to detect 'did anything change?'.

    Combines the committed state (``head``) with the working-tree state
    (``status_hash``, which folds in ``git status`` plus a max-mtime signal so
    repeated edits to an already-modified file are still noticed).
    """

    head: str = ""
    status_hash: str = ""
    ok: bool = False

    def key(self) -> Tuple[str, str]:
        return (self.head, self.status_hash)


def compute_fingerprint(repo_root: Path, *, timeout_s: float = 3.0) -> Fingerprint:
    """Compute a :class:`Fingerprint` for *repo_root* using fast git calls.

    Defensive: any failure yields ``Fingerprint(ok=False)``, which the sentinel
    treats as 'don't touch anything'. Never raises.
    """
    root = Path(repo_root)

    def _git(args: List[str]) -> Optional[str]:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout

    # HEAD may be absent in a freshly-init'd repo with no commits — that's fine,
    # the working-tree status still distinguishes states.
    head = (_git(["rev-parse", "HEAD"]) or "").strip()

    status = _git(["status", "--porcelain", "--untracked-files=all"])
    if status is None:
        # Not a git repo (or git unavailable) — refuse to act.
        return Fingerprint(ok=False)

    # Fold in the most-recent mtime across listed paths so that *re-saving* an
    # already-"modified" file (identical porcelain line) still moves the
    # fingerprint. Capped so huge dirty sets stay cheap.
    max_mtime_ns = 0
    scanned = 0
    for line in status.splitlines():
        # porcelain v1: "XY <path>"  (path may be quoted / contain ' -> ' on rename)
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:  # rename: take the destination
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path:
            continue
        try:
            st = (root / path).stat()
            if st.st_mtime_ns > max_mtime_ns:
                max_mtime_ns = st.st_mtime_ns
        except OSError:
            pass
        scanned += 1
        if scanned >= _MTIME_SCAN_CAP:
            break

    blob = f"{head}\x00{status}\x00{max_mtime_ns}".encode("utf-8", "replace")
    status_hash = hashlib.sha1(blob).hexdigest()
    return Fingerprint(head=head, status_hash=status_hash, ok=True)


@dataclass
class Decision:
    """Outcome of :meth:`IndexSentinel.should_reindex`."""

    do: bool
    reason: str


class IndexSentinel:
    """Stateful decision engine: 'should I reindex right now?'.

    Pure in spirit — it only mutates its own counters and timestamps via the
    explicit ``note_*`` calls. Time is injected (``now`` is a monotonic float)
    so tests are deterministic.
    """

    def __init__(
        self,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_max_s: float = BACKOFF_MAX_S,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_max_s = float(backoff_max_s)

        self._last_fp_key: Optional[Tuple[str, str]] = None
        self._last_attempt: float = float("-inf")
        self._last_success: float = float("-inf")
        self._failures: int = 0

    # -- introspection (for `index auto status`) --------------------------

    @property
    def last_success(self) -> float:
        return self._last_success

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    @property
    def has_baseline(self) -> bool:
        return self._last_fp_key is not None

    def _backoff_window(self) -> float:
        if self._failures <= 0:
            return 0.0
        return min(self.backoff_base_s * (2 ** (self._failures - 1)), self.backoff_max_s)

    # -- the decision ------------------------------------------------------

    def should_reindex(
        self,
        now: float,
        fingerprint: Fingerprint,
        *,
        git_event: bool,
        index_exists: bool,
        bootstrap: bool,
    ) -> Decision:
        """Decide whether to kick off a reindex pass.

        *index_exists* — is there already a non-empty index for this repo?
        *bootstrap*    — may we *create* an index from scratch automatically?
        *git_event*    — did a tree-rewriting git command just run? (bypasses
                          the ordinary edit throttle, never the backoff window)
        """
        if not fingerprint.ok:
            return Decision(False, "no-git")

        # Don't silently embed an entire repo no one asked to index, unless the
        # user opted into bootstrap.
        if not index_exists and not bootstrap:
            return Decision(False, "no-index")

        # Respect failure backoff regardless of how urgent the change seems.
        backoff = self._backoff_window()
        if backoff and (now - self._last_attempt) < backoff:
            return Decision(False, "backoff")

        changed = (fingerprint.key() != self._last_fp_key) or (not index_exists)
        if not changed:
            return Decision(False, "unchanged")

        if git_event:
            return Decision(True, "git-event")
        if (now - self._last_attempt) >= self.min_interval_s:
            return Decision(True, "changed")
        return Decision(False, "throttled")

    # -- state transitions (called by the worker) -------------------------

    def note_attempt(self, now: float) -> None:
        self._last_attempt = now

    def note_success(self, now: float, fingerprint: Fingerprint) -> None:
        # Record the fingerprint observed *after* the pass so edits made during
        # indexing are detected next tick (convergence).
        if fingerprint.ok:
            self._last_fp_key = fingerprint.key()
        self._last_success = now
        self._failures = 0

    def note_failure(self, now: float) -> None:
        self._failures += 1

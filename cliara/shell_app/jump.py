"""Jump (smart cd) directory store + matching.

A lightweight zoxide-style directory ranking:
- Records directories where commands were executed (plus `cd` destinations).
- Persists to `~/.cliara/jump_dirs.json` (via Config.config_dir).
- Ranks candidates by: fuzzy match quality + frequency + recency.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cliara.file_lock import with_file_lock


def _now_ts() -> int:
    return int(time.time())


def _norm_key(p: Path) -> str:
    # normcase makes Windows comparisons case-insensitive.
    return os.path.normcase(os.path.abspath(str(p)))


def _safe_abs_dir(p: Path) -> Optional[Path]:
    try:
        p2 = Path(os.path.abspath(str(p.expanduser())))
    except Exception:
        return None
    try:
        if p2.exists() and p2.is_dir():
            return p2
    except Exception:
        return None
    return None


def _fuzz_score(query: str, candidate_path: str) -> int:
    q = (query or "").strip()
    if not q:
        return 0

    # Prefer rapidfuzz if available (thefuzz dependency); fall back to substring scoring.
    try:
        from rapidfuzz.fuzz import WRatio  # type: ignore

        base = os.path.basename(candidate_path.rstrip("\\/"))
        a = WRatio(q, base)
        b = WRatio(q, candidate_path)
        # Be defensive: RapidFuzz can theoretically return None on odd inputs.
        a = float(a) if a is not None else 0.0
        b = float(b) if b is not None else 0.0
        return int(max(a, b))
    except Exception:
        low_q = q.lower()
        low_p = (candidate_path or "").lower()
        if low_q in low_p:
            # Simple heuristic: closer-to-end matches feel better.
            idx = low_p.rfind(low_q)
            tail_bonus = 20 if idx >= max(0, len(low_p) - 40) else 0
            return min(100, 70 + tail_bonus)
        return 0


_DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    "Lib",
    "Scripts",
}


def _split_query_segments(query: str) -> List[str]:
    """Split a user query into folder-name segments.

    Supports: spaces, '/', '\\' as separators.
    Example: "services/api" -> ["services", "api"].
    """
    q = (query or "").strip()
    if not q:
        return []
    q = q.replace("/", " ").replace("\\", " ")
    return [s for s in (p.strip() for p in q.split()) if s]


def find_best_exact_subdir(
    query: str,
    roots: Iterable[Path],
    max_depth: int = 7,
    max_walk: int = 8000,
) -> Optional[str]:
    """Find a best exact-match directory path for a query.

    This is the primary UX for `jump`: no picker, no prompt.

    Matching rules:
    - Query is split into segments by spaces and path separators.
    - A directory matches when its trailing path components equal the segments,
      case-insensitively on Windows.
      Example: segments ["services", "api"] matches .../services/api.

    Tie-break:
    - Prefer results under current working directory.
    - Prefer shallower depth (closer to root).
    - Prefer shorter absolute path.

    Returns an absolute path string or None.
    """

    segs = _split_query_segments(query)
    if not segs:
        return None

    # Normalize for comparison (Windows: case-insensitive)
    want = [os.path.normcase(s) for s in segs]
    cwd = Path.cwd()
    cwd_norm = os.path.normcase(os.path.abspath(str(cwd)))

    best: Optional[Tuple[Tuple[int, int, int, int], str]] = None
    walked = 0
    seen_roots: set[str] = set()

    for root in roots:
        rr = _safe_abs_dir(root)
        if rr is None:
            continue
        root_norm = os.path.normcase(os.path.abspath(str(rr)))
        if root_norm in seen_roots:
            continue
        seen_roots.add(root_norm)

        for dirpath, dirnames, _filenames in os.walk(str(rr), topdown=True):
            walked += 1
            if walked > max_walk:
                break

            # Depth bound relative to this root.
            try:
                rel = os.path.relpath(dirpath, str(rr))
            except Exception:
                rel = ""
            depth = 0 if rel in (".", "") else rel.count(os.sep) + 1
            if depth > max_depth:
                dirnames[:] = []
                continue

            # Prune heavy folders.
            dirnames[:] = [d for d in dirnames if d not in _DEFAULT_SKIP_DIRS]

            # Exact tail-component match.
            parts = [os.path.normcase(p) for p in Path(dirpath).parts]
            if len(parts) < len(want):
                continue
            if parts[-len(want) :] != want:
                continue

            abs_norm = os.path.normcase(os.path.abspath(dirpath))
            under_cwd = 1 if abs_norm.startswith(cwd_norm + os.sep) or abs_norm == cwd_norm else 0

            # Rank tuple: higher is better.
            # under_cwd first, then shallower depth, then shorter path.
            rank = (
                under_cwd,
                -depth,
                -len(abs_norm),
                0,
            )

            if best is None or rank > best[0]:
                best = (rank, dirpath)

        if walked > max_walk:
            break

    return best[1] if best else None


def search_filesystem_dirs(
    query: str,
    roots: Iterable[Path],
    limit: int = 10,
    max_depth: int = 6,
    max_walk: int = 6000,
) -> List[str]:
    """Search for matching directories under the given roots.

    This is a fallback for when the jump DB has no matches yet.
    It is intentionally bounded (depth + max_walk) and skips common
    large/irrelevant folders.
    """

    q = (query or "").strip()
    if not q:
        return []

    seen: set[str] = set()
    hits: List[Tuple[int, str]] = []
    walked = 0

    for root in roots:
        rr = _safe_abs_dir(root)
        if rr is None:
            continue
        root_key = _norm_key(rr)
        if root_key in seen:
            continue
        seen.add(root_key)

        # os.walk is faster than pathlib.rglob for this use.
        for dirpath, dirnames, _filenames in os.walk(str(rr), topdown=True):
            walked += 1
            if walked > max_walk:
                break

            try:
                rel = os.path.relpath(dirpath, str(rr))
            except Exception:
                rel = ""
            depth = 0 if rel in (".", "") else rel.count(os.sep) + 1
            if depth > max_depth:
                dirnames[:] = []
                continue

            # Prune heavy folders.
            dirnames[:] = [d for d in dirnames if d not in _DEFAULT_SKIP_DIRS]

            score = _fuzz_score(q, dirpath)
            if score <= 0:
                continue

            key = os.path.normcase(os.path.abspath(dirpath))
            if key in seen:
                continue
            seen.add(key)

            hits.append((score, dirpath))

        if walked > max_walk:
            break

    # Best match first, then shorter path for ties.
    hits.sort(key=lambda t: (t[0], -len(t[1])), reverse=True)
    return [p for _s, p in hits[: max(1, int(limit))]]


@dataclass(frozen=True)
class JumpCandidate:
    path: str
    score: float
    visits: int
    last_ts: int
    match: int
    combined: float


class JumpDirectoryStore:
    def __init__(
        self,
        store_path: Path,
        max_entries: int = 750,
        decay: float = 0.985,
    ):
        self._path = store_path
        self._max_entries = max(50, int(max_entries))
        self._decay = float(decay)
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._entries = {}
                return
            try:
                with with_file_lock(self._path):
                    raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw)
                entries = data.get("entries", data) if isinstance(data, dict) else data
                if not isinstance(entries, dict):
                    self._entries = {}
                    return
                # Normalize schema
                out: Dict[str, Dict[str, Any]] = {}
                for k, v in entries.items():
                    if not isinstance(v, dict):
                        continue
                    path = str(v.get("path") or k)
                    score = float(v.get("score") or 0.0)
                    visits = int(v.get("visits") or 0)
                    last_ts = int(v.get("last_ts") or 0)
                    out[str(k)] = {
                        "path": path,
                        "score": score,
                        "visits": visits,
                        "last_ts": last_ts,
                    }
                self._entries = out
            except Exception:
                self._entries = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": self._entries}
        with with_file_lock(self._path):
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    def record_visit(self, directory: Path, weight: float = 1.0, persist: bool = True) -> None:
        d = _safe_abs_dir(directory)
        if d is None:
            return
        key = _norm_key(d)
        ts = _now_ts()

        with self._lock:
            e = self._entries.get(key)
            if e is None:
                e = {"path": str(d), "score": 0.0, "visits": 0, "last_ts": 0}
                self._entries[key] = e

            # Exponential decay + additive weight.
            e["score"] = float(e.get("score", 0.0)) * self._decay + float(weight)
            e["visits"] = int(e.get("visits", 0)) + 1
            e["last_ts"] = ts
            e["path"] = str(d)

            self._evict_if_needed_locked()
            if persist:
                self._save()

    def _evict_if_needed_locked(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        now = _now_ts()

        def _rank(item: Tuple[str, Dict[str, Any]]) -> float:
            v = item[1]
            score = float(v.get("score", 0.0))
            last_ts = int(v.get("last_ts", 0))
            age_days = max(0.0, (now - last_ts) / 86400.0)
            # Lower is worse; evict oldest + lowest score first.
            return score - age_days * 0.25

        items = sorted(self._entries.items(), key=_rank)
        drop = len(items) - self._max_entries
        for k, _ in items[:drop]:
            self._entries.pop(k, None)

    def top(self, limit: int = 10) -> List[JumpCandidate]:
        return self.search("", limit=limit)

    def search(self, query: str, limit: int = 10) -> List[JumpCandidate]:
        q = (query or "").strip()
        now = _now_ts()

        with self._lock:
            rows: List[JumpCandidate] = []
            for v in self._entries.values():
                path = str(v.get("path") or "")
                if not path:
                    continue
                # Skip non-existent dirs.
                try:
                    if not Path(path).exists():
                        continue
                except Exception:
                    continue

                match = _fuzz_score(q, path) if q else 0
                if q and match <= 0:
                    continue

                score = float(v.get("score", 0.0))
                visits = int(v.get("visits", 0))
                last_ts = int(v.get("last_ts", 0))
                recency = 1.0 / (1.0 + max(0.0, (now - last_ts) / 86400.0))  # 1..0
                combined = score + (match / 100.0) + recency * 0.75

                rows.append(
                    JumpCandidate(
                        path=path,
                        score=score,
                        visits=visits,
                        last_ts=last_ts,
                        match=match,
                        combined=combined,
                    )
                )

        rows.sort(key=lambda c: (c.combined, c.match, c.last_ts), reverse=True)
        return rows[: max(1, int(limit))]

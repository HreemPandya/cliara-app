"""Helpers for the `prune branches` built-in command.

Keep this module lightweight so it can be unit-tested without importing the
entire shell orchestrator.
"""

from __future__ import annotations

from typing import List


def parse_selection_spec(spec: str, *, max_index: int) -> List[int]:
    """Parse a user selection like "1-3,5" or "all" into 0-based indexes.

    - Returns an empty list for blank/unknown input.
    - Duplicates are removed; result is sorted.
    """
    s = (spec or "").strip().lower()
    if not s:
        return []

    if s in {"a", "all", "*"}:
        return list(range(max_index))

    out: set[int] = set()
    parts = [p.strip() for p in s.replace(" ", "").split(",") if p.strip()]
    for p in parts:
        if "-" in p:
            lo_s, hi_s = p.split("-", 1)
            if not lo_s.isdigit() or not hi_s.isdigit():
                return []
            lo = int(lo_s)
            hi = int(hi_s)
            if lo < 1 or hi < 1 or lo > hi:
                return []
            for n in range(lo, hi + 1):
                if 1 <= n <= max_index:
                    out.add(n - 1)
        else:
            if not p.isdigit():
                return []
            n = int(p)
            if 1 <= n <= max_index:
                out.add(n - 1)

    return sorted(out)

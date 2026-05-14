"""CLI expansion for the ambient pulse glyph (`cliara pulse`)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from cliara.pulse import PulseSnapshot
from cliara.shell_app.runtime import print_dim, print_info, print_success, print_warning


def _fmt_when(ts: Optional[float]) -> str:
    if not ts:
        return "(never)"
    try:
        dt = datetime.fromtimestamp(float(ts))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "(unknown)"


def _fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "(unknown)"
    s = float(seconds)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.0f}m"
    if s < 86400:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def print_pulse(snapshot: PulseSnapshot) -> None:
    print()
    print_info("  Cliara Pulse")
    print_dim("  -----------")
    print()

    color = snapshot.color
    glyph = snapshot.glyph

    if color == "green":
        print_success(f"  {glyph}  green  — clean tree, tests passing, on main")
    elif color == "amber":
        print_warning(f"  {glyph}  amber  — attention needed")
    elif color == "red":
        print_warning(f"  {glyph}  red    — CI failing")
    elif color == "purple":
        print_warning(f"  {glyph}  purple — uncommitted work older than 24h")
    else:
        print_warning(f"  {glyph}  {color}")

    if snapshot.reasons:
        for r in snapshot.reasons[:6]:
            print_dim(f"    - {r}")

    print()

    if not snapshot.repo_root:
        print_dim("  Repo: (not a git repo)")
        print()
        return

    print_dim(f"  Repo:   {snapshot.repo_root}")
    print_dim(f"  Branch: {snapshot.branch or '(unknown)'} (main: {snapshot.default_branch or '(unknown)'})")

    if snapshot.clean_tree is True:
        print_dim("  Tree:   clean")
    elif snapshot.clean_tree is False:
        print_dim("  Tree:   dirty")
    else:
        print_dim("  Tree:   (unknown)")

    if snapshot.has_unstaged is True:
        print_dim("  Work:   has unstaged changes")
    elif snapshot.has_uncommitted is True:
        print_dim("  Work:   staged/uncommitted")
    else:
        print_dim("  Work:   none")

    if snapshot.stale_uncommitted:
        print_dim(f"  Age:    stale ({_fmt_age(snapshot.stale_age_s)} old)")
    elif snapshot.stale_age_s is not None:
        print_dim(f"  Age:    {_fmt_age(snapshot.stale_age_s)}")

    if snapshot.tests_passing is True:
        print_dim(f"  Tests:  passing (last: {_fmt_when(snapshot.last_test_ts)})")
    elif snapshot.tests_passing is False:
        print_dim(f"  Tests:  failing (last: {_fmt_when(snapshot.last_test_ts)})")
    else:
        print_dim("  Tests:  unknown (run pytest)")

    if snapshot.ci_failing is True:
        print_dim(f"  CI:     failing ({snapshot.ci_summary or 'unknown'})")
    elif snapshot.ci_failing is False:
        print_dim(f"  CI:     ok ({snapshot.ci_summary or 'unknown'})")
    else:
        print_dim("  CI:     unknown (no token or not fetched)")

    print()
    print_dim(f"  Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

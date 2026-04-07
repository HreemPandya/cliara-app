"""
Paste-ready markdown for Copilot/Cursor: last-run context and session handoff.

Keeps formatting stable so IDE chat can rely on section headers.
"""

from __future__ import annotations

import json
import platform
from typing import Any, Dict, List, Optional

from cliara.session_store import CLOSEOUT_KEYS, TaskSession


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate with ellipsis; non-positive max means no limit."""
    if max_chars <= 0 or not text:
        return text or ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 20].rstrip() + "\n… [truncated]"


def format_regression_snippet(
    snapshot: Dict[str, Any], max_chars: int = 2000
) -> str:
    """Serialize a regression-style snapshot for chat (git/deps/env/runtime only)."""
    try:
        raw = json.dumps(snapshot, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(snapshot)
    return truncate_text(raw, max_chars)


def format_last_run_bundle(
    *,
    cwd: str,
    shell: str,
    os_name: str,
    branch: Optional[str],
    last_command: str,
    last_exit_code: int,
    last_stderr: str,
    last_stdout: str,
    session_name: Optional[str],
    session_id: Optional[str],
    max_stderr: int,
    max_stdout: int,
    include_stdout: bool,
    regression_snapshot: Optional[Dict[str, Any]] = None,
    regression_max_chars: int = 2000,
) -> str:
    """Markdown block for the most recent shell run (for Copilot/Cursor)."""
    lines: List[str] = [
        "## Cliara — last run",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| cwd | `{cwd}` |",
        f"| OS | {os_name} |",
        f"| Shell | `{shell}` |",
    ]
    if branch:
        lines.append(f"| Git branch | `{branch}` |")
    if session_name:
        sid = f" (`{session_id}`)" if session_id else ""
        lines.append(f"| Cliara session | **{session_name}**{sid} |")
    lines.extend(
        [
            "",
            "### Command",
            "",
            "```",
            last_command or "(none)",
            "```",
            "",
            f"**Exit code:** `{last_exit_code}`",
            "",
        ]
    )
    err = truncate_text(last_stderr, max_stderr)
    if err:
        lines.extend(["### stderr (captured)", "", "```", err, "```", ""])
    else:
        lines.extend(["### stderr", "", "_(empty or not captured)_", ""])

    if include_stdout and last_stdout.strip():
        out = truncate_text(last_stdout, max_stdout)
        lines.extend(["### stdout (captured)", "", "```", out, "```", ""])

    if regression_snapshot is not None:
        lines.extend(
            [
                "### Environment snapshot (lightweight)",
                "",
                "```json",
                format_regression_snippet(regression_snapshot, regression_max_chars),
                "```",
                "",
            ]
        )

    lines.append(
        "_Paste this block into Copilot or Cursor chat when asking for a fix._"
    )
    return "\n".join(lines).strip() + "\n"


def format_session_for_chat(
    session: TaskSession,
    last_run_bundle: str,
    *,
    max_commands: int = 40,
) -> str:
    """Full session export: intent, history, reflection/closeout, plus last-run bundle."""
    lines: List[str] = [
        "## Cliara — session snapshot (for Copilot/Cursor)",
        "",
        f"**Session:** `{session.name}`  ",
        f"**Session id:** `{session.id}`  ",
    ]
    if session.project_root:
        lines.append(f"**Project root:** `{session.project_root}`  ")
    if session.branch:
        lines.append(f"**Branch:** `{session.branch}`  ")
    lines.extend(["", "### Intent", "", session.intent or "_(none)_", ""])

    if session.commands:
        lines.append("### Command history (oldest first)")
        lines.append("")
        cmds = session.commands[-max_commands:]
        for c in cmds:
            st = "ok" if c.exit_code == 0 else f"exit {c.exit_code}"
            cmd = c.command[:500] + ("…" if len(c.command) > 500 else "")
            lines.append(f"- [{st}] `{c.cwd}` — `{cmd}`")
            if getattr(c, "stderr_preview", None):
                prev = truncate_text(c.stderr_preview or "", 200)
                lines.append(f"  - stderr: {prev}")
            if getattr(c, "stdout_preview", None):
                prev = truncate_text(c.stdout_preview or "", 200)
                lines.append(f"  - stdout: {prev}")
        lines.append("")

    if session.notes:
        lines.append("### Notes")
        lines.append("")
        for n in session.notes[-15:]:
            lines.append(f"- {n.text[:500]}{'…' if len(n.text) > 500 else ''}")
        lines.append("")

    if session.reflection:
        lines.append("### Reflection (session end)")
        lines.append("")
        for entry in session.reflection:
            q = entry.get("question") or ""
            ans = entry.get("answer") or entry.get("selected_label") or ""
            lines.append(f"- **Q:** {q[:400]}")
            if ans:
                lines.append(f"  **A:** {str(ans)[:800]}")
        lines.append("")
    elif session.closeout:
        lines.append("### Closeout")
        lines.append("")
        for k in CLOSEOUT_KEYS:
            v = (session.closeout or {}).get(k)
            if v:
                lines.append(f"- **{k}:** {v}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(last_run_bundle.strip())
    return "\n".join(lines).strip() + "\n"


def default_shell_label(config_shell: Optional[str]) -> str:
    """Human-readable shell for export when config is unset."""
    if config_shell:
        return config_shell
    return platform.system() + " (default)"

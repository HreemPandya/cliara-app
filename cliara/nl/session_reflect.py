"""Session-reflect defaults and validation helpers."""

from typing import Any, Dict, List, Optional


def default_session_reflect_plan() -> List[Dict[str, Any]]:
    """Offline reflection flow when LLM is unavailable or JSON parse fails."""
    return [
        {
            "id": "session_shape",
            "kind": "choice",
            "question": "How would you describe this session for someone who only reads your reflection later?",
            "hint": "Pick the closest fit.",
            "options": [
                "Exploring or learning - no single deliverable yet",
                "Made progress - more work planned for later",
                "Completed a concrete task or milestone",
                "Blocked, interrupted, or mostly troubleshooting",
            ],
        },
        {
            "id": "what_mattered",
            "kind": "long_text",
            "question": "In plain language, what did you accomplish or learn, and why does it matter?",
            "hint": "This is the main story - not a list of commands.",
        },
        {
            "id": "risks_or_deps",
            "kind": "text",
            "question": "Any blockers, dependencies, or risks the next person should know? (one line, or skip)",
            "hint": "Optional.",
        },
        {
            "id": "next_move",
            "kind": "text",
            "question": "What is the single most useful next step when work resumes?",
            "hint": "Be specific if you can.",
        },
    ]


def validate_session_reflect_steps(data: Any) -> Optional[List[Dict[str, Any]]]:
    """Validate LLM JSON for session_reflect; return sanitized steps or None."""
    if not isinstance(data, dict):
        return None
    steps_in = data.get("steps")
    if not isinstance(steps_in, list):
        return None
    out: List[Dict[str, Any]] = []
    for raw in steps_in:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind not in ("choice", "text", "long_text"):
            continue
        q = raw.get("question")
        if not isinstance(q, str) or not q.strip():
            continue
        sid = raw.get("id")
        if not isinstance(sid, str) or not sid.strip():
            sid = "step_%d" % len(out)
        step: Dict[str, Any] = {
            "id": sid.strip()[:80],
            "kind": kind,
            "question": q.strip()[:1200],
        }
        hint = raw.get("hint")
        if isinstance(hint, str) and hint.strip():
            step["hint"] = hint.strip()[:400]
        if kind == "choice":
            opts = raw.get("options")
            if not isinstance(opts, list):
                continue
            clean = [str(o).strip()[:500] for o in opts if str(o).strip()]
            if len(clean) < 2:
                continue
            step["options"] = clean[:6]
        out.append(step)
        if len(out) >= 8:
            break
    if len(out) < 2:
        return None
    return out

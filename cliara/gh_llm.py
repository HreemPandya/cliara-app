"""
LLM helper for ``cliara gh`` — uses the same provider resolution as the rest of Cliara.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from cliara.config import Config
from cliara.nl.service import NLHandler
from cliara.safety import SafetyChecker

_CLOUD_MODEL_PREFIXES = ("gpt-", "claude-", "llama-3.", "gemini-", "mixtral-", "text-")


def _clear_stale_cloud_model_for_ollama(config: Config) -> None:
    stored = config.get("llm_model") or ""
    if any(stored.startswith(p) for p in _CLOUD_MODEL_PREFIXES):
        config.settings["llm_model"] = None
        config.save()


def _extract_json_object(text: str) -> Optional[str]:
    """Find the first JSON object in model output (handles ```json fences)."""
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def init_nl_for_gh(config: Optional[Config] = None) -> NLHandler:
    """Build an NLHandler with LLM from env / token / config (same rules as headless ask)."""
    cfg = config or Config()
    safety = SafetyChecker()
    nl = NLHandler(safety, config=cfg)
    provider = cfg.get_llm_provider()
    api_key = cfg.get_llm_api_key()
    if not provider or not api_key:
        raise RuntimeError(
            "No LLM configured for AI-assisted GitHub commands.\n"
            "  Run: cliara login   (Cliara Cloud)\n"
            "  Or set OPENAI_API_KEY / GROQ_API_KEY / GEMINI_API_KEY, or run Ollama."
        )
    if provider == "ollama":
        _clear_stale_cloud_model_for_ollama(cfg)
        ok = nl.initialize_llm(
            "ollama",
            api_key,
            base_url=cfg.get_ollama_base_url(),
        )
    else:
        ok = nl.initialize_llm(provider, api_key)
    if not ok or not nl.llm_enabled:
        raise RuntimeError("Could not initialize the LLM for GitHub commands.")
    return nl


def gh_llm_complete(user_message: str, *, config: Optional[Config] = None) -> str:
    nl = init_nl_for_gh(config)
    return nl._call_llm("gh_assistant", user_message)  # noqa: SLF001 — internal reuse


def gh_llm_pr_title_body(
    *,
    diff_excerpt: str,
    commit_messages: str,
    base: str,
    head: str,
    config: Optional[Config] = None,
) -> Tuple[str, str]:
    user = (
        f"Branch `{head}` will merge into `{base}`.\n\n"
        "Commit messages (newest last):\n"
        f"{commit_messages or '(none)'}\n\n"
        "Unified diff excerpt:\n"
        f"{diff_excerpt}\n\n"
        'Return a single JSON object with keys "title" and "body" only.\n'
        "- title: under 72 characters, imperative mood, no trailing period.\n"
        "- body: GitHub-flavored Markdown with sections: Summary, Changes, "
        "Test plan, Risks / rollout notes (omit empty sections).\n"
        "No markdown fence around the JSON."
    )
    raw = gh_llm_complete(user, config=config)
    blob = _extract_json_object(raw)
    if not blob:
        raise RuntimeError("Model did not return valid JSON with title/body.")
    data = json.loads(blob)
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    if not title:
        raise RuntimeError("Model returned an empty PR title.")
    return title, body

"""
Agent registry: builds AGENT_REGISTRY from agent modules and prompts.
"""

from pathlib import Path
from typing import Any, Dict

from cliara.agents import nl_to_commands as _nl
from cliara.agents import fix as _fix
from cliara.agents import explain as _explain
from cliara.agents import history_summary as _history_summary
from cliara.agents import history_search as _history_search
from cliara.agents import commit_and_deploy as _commit_deploy
from cliara.agents import copilot_explain as _copilot_explain
from cliara.agents import readme_generator as _readme
from cliara.agents import session_reflect as _session_reflect
from cliara.agents import chat_polish as _chat_polish

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8").strip()


def _build_registry() -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    all_agents = (
        _nl.AGENTS
        | _fix.AGENTS
        | _explain.AGENTS
        | _history_summary.AGENTS
        | _history_search.AGENTS
        |         _commit_deploy.AGENTS
        | _copilot_explain.AGENTS
        | _readme.AGENTS
        | _session_reflect.AGENTS
        | _chat_polish.AGENTS
    )
    for name, cfg in all_agents.items():
        registry[name] = {
            "system": _load_prompt(name),
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }
    return registry


AGENT_REGISTRY: Dict[str, Dict[str, Any]] = _build_registry()

__all__ = ["AGENT_REGISTRY"]

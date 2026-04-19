"""
Agent registry: builds AGENT_REGISTRY from agent modules and prompts.
"""

from pathlib import Path
from typing import Any, Dict

from cliara.agents import nl_to_commands as _nl
from cliara.agents import fix as _fix
from cliara.agents import explain as _explain
from cliara.agents import explain_output as _explain_output
from cliara.agents import history_summary as _history_summary
from cliara.agents import history_search as _history_search
from cliara.agents import commit_and_deploy as _commit_deploy
from cliara.agents import copilot_explain as _copilot_explain
from cliara.agents import readme_generator as _readme
from cliara.agents import session_reflect as _session_reflect
from cliara.agents import chat_polish as _chat_polish
from cliara.agents import gh_assistant as _gh_assistant
from cliara.agents import cliara_qa as _cliara_qa
from cliara.agents import nl_router as _nl_router

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    md_path = _PROMPTS_DIR / f"{name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8").strip()

    raise FileNotFoundError(f"Prompt file not found for agent '{name}'")


def _build_registry() -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    all_agents = (
        _nl.AGENTS
        | _fix.AGENTS
        | _explain.AGENTS
        | _explain_output.AGENTS
        | _history_summary.AGENTS
        | _history_search.AGENTS
        |         _commit_deploy.AGENTS
        | _copilot_explain.AGENTS
        | _readme.AGENTS
        | _session_reflect.AGENTS
        | _chat_polish.AGENTS
        | _gh_assistant.AGENTS
        | _cliara_qa.AGENTS
        | _nl_router.AGENTS
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

"""Constants shared across Cliara NL modules."""

from typing import Dict

from cliara.auth import get_gateway_url

EMBEDDING_MODEL = "text-embedding-3-small"

# Default model used when no per-task or global override is configured.
PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-haiku-20240307",
    "ollama": "gemma4",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-1.5-flash",
    "cliara": "llama-3.3-70b-versatile",  # Gateway picks the best available model
}


def model_id_matches_provider(model: str, provider: str) -> bool:
    """True if *model* looks like a valid id for *provider* (avoids e.g. gemma4 on OpenAI)."""
    if not (model and provider):
        return False
    if provider == "ollama":
        return True
    m = model.strip().lower()
    if provider == "openai":
        return (
            m.startswith("gpt-")
            or m.startswith("o1")
            or m.startswith("o2")
            or m.startswith("o3")
            or m.startswith("o4")
            or m.startswith("chatgpt-")
            or m.startswith("ft:")
            or m.startswith("text-embedding-")
        )
    if provider == "anthropic":
        return m.startswith("claude-")
    if provider == "groq":
        return m.startswith(
            ("llama", "mixtral", "gemma2", "gemma-", "compound", "openai/", "meta-llama/")
        )
    if provider == "gemini":
        return m.startswith("gemini") or m.startswith("models/gemini")
    if provider == "cliara":
        return True
    return True

# Providers that use the OpenAI-compatible client (openai SDK with custom base_url)
OPENAI_COMPAT_PROVIDERS = frozenset({"openai", "ollama", "groq", "gemini", "cliara"})

# Base URLs for OpenAI-compatible cloud providers (not ollama - that's dynamic)
# Cliara URL comes from auth.py (single source of truth; respects CLIARA_GATEWAY_URL env).
PROVIDER_BASE_URLS: Dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "cliara": get_gateway_url(),
}

# Agents whose output is plain text and can be streamed token-by-token to the
# console. JSON-returning agents should not stream raw JSON to the console.
STREAMING_SAFE_AGENTS = frozenset(
    {
        "explain",
        "explain_output",
        "commit_message",
        "copilot_explain",
        "readme",
        "chat_polish",
        "cliara_qa",
    }
)

# Cliara built-ins (and common shortcuts) so NL can treat "what does mc do"
# as a Cliara command-help intent rather than a host-shell lookup.
CLIARA_BUILTIN_COMMANDS = frozenset(
    {
        "exit",
        "quit",
        "q",
        "help",
        "version",
        "status",
        "readme",
        "last",
        "doctor",
        "explain",
        "lint",
        "push",
        "session",
        "deploy",
        "macro",
        "config",
        "theme",
        "themes",
        "setup-ollama",
        "setup-llm",
        "cliara-login",
        "cliara-logout",
        "use",
        # Macro shortcuts
        "m",
        "mc",
        "ml",
        "mr",
        "ma",
        "me",
        "md",
        "ms",
        "mst",
        "msh",
        "msr",
        "mch",
        "mrn",
        "mh",
        # Session shortcuts
        "ss",
        "se",
    }
)

"""NL-to-commands agent: converts natural language to shell commands."""

AGENTS = {
    "nl_to_commands": {
        "temperature": 0.3,
        "max_tokens": 800,  # local models are more verbose; need room for JSON + preamble
    },
    "nl_macro_propose": {
        "temperature": 0.35,
        "max_tokens": 1500,
    },
}

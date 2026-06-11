"""Code review agent: pre-commit review of a git diff (bugs, tests, public APIs)."""

AGENTS = {
    "code_review": {
        "temperature": 0.2,
        "max_tokens": 1500,
    },
}

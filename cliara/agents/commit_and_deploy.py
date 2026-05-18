"""Commit message and deploy agents."""

AGENTS = {
    "commit_message": {
        "temperature": 0.0,  # deterministic: we want one exact answer, not creative variation
        "max_tokens": 120,   # enough for JSON wrapper + the message line
    },
    "deploy": {
        "temperature": 0.2,
        "max_tokens": 500,
    },
}

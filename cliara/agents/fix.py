"""Fix agent: explains command errors and suggests fixes."""

AGENTS = {
    "fix": {
        "temperature": 0.2,
        "max_tokens": 600,  # local models need extra room for JSON explanation fields
    },
}

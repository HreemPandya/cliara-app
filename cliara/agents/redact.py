"""Redaction agent: local-only, preserves structure while masking secrets."""

from typing import Dict, Any


AGENTS: Dict[str, Dict[str, Any]] = {
    "redact": {
        "temperature": 0.0,
        "max_tokens": 512,
    }
}

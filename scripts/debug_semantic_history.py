#!/usr/bin/env python3
"""
Debug script: verify semantic history store path and that adds work.
Run from project root: python scripts/debug_semantic_history.py
"""
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cliara.config import Config
from cliara.semantic_history import SemanticHistoryStore


def main():
    c = Config()
    store_path = c.config_dir / "semantic_history.json"
    print("Config dir:", c.config_dir)
    print("Semantic history path:", store_path)
    print("semantic_history_enabled:", c.get("semantic_history_enabled", True))
    print("semantic_history_summary_on_add:", c.get("semantic_history_summary_on_add", True))
    print()

    store = SemanticHistoryStore(store_path=store_path, max_entries=500)
    store.add("echo debug test", summary="Debug script add", cwd=str(Path.cwd()))
    print("Added test entry. Len:", len(store))
    print("Is empty:", store.is_empty())
    recent = store.get_recent(5)
    print("Recent entries:", len(recent))
    for e in recent:
        print("  -", e.get("command"), "|", e.get("summary"), "|", e.get("timestamp", "")[:19])
    print()
    print("Store file exists:", store_path.exists())
    if store_path.exists():
        print("File size:", store_path.stat().st_size, "bytes")
    print("OK")


if __name__ == "__main__":
    main()

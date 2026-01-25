"""
Demo script showing Natural Language Macros in action.
This creates some example macros and demonstrates features.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.macros import MacroStore


def setup_demo_macros():
    """Create demo macros for testing."""
    store = MacroStore()
    
    print("Setting up demo macros...")
    
    # Simple hello world
    store.add_macro(
        "hello world",
        "Simple hello world test",
        [{"type": "cmd", "value": "echo Hello, World!"}]
    )
    
    # Multi-step macro
    store.add_macro(
        "show info",
        "Display system information",
        [
            {"type": "cmd", "value": "echo === System Information ==="},
            {"type": "cmd", "value": "echo Current Directory: %CD%"},
            {"type": "cmd", "value": "echo Python Version:"},
            {"type": "cmd", "value": "python --version"}
        ]
    )
    
    # Variable macro
    store.add_macro(
        "repeat {count}",
        "Repeat a message",
        [
            {"type": "cmd", "value": "echo Repeating {count} times..."},
            {"type": "cmd", "value": "echo Count is: {count}"}
        ]
    )
    
    # Git macro
    store.add_macro(
        "git status",
        "Show git status and branch",
        [
            {"type": "cmd", "value": "git status -s"},
            {"type": "cmd", "value": "git branch"}
        ]
    )
    
    print(f"\n[OK] Created {store.count()} demo macros!")
    print("\nYou can now run the CLI and try:")
    print("  nl> hello world")
    print("  nl> show info")
    print("  nl> repeat 5")
    print("  nl> macros list")
    print("\nTo start the CLI:")
    print("  python -m app.main")


if __name__ == '__main__':
    setup_demo_macros()

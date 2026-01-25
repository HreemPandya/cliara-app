"""
Cliara - An AI-powered shell that understands natural language and macros.

A shell wrapper that lets you:
- Run normal terminal commands (pass-through)
- Use natural language with ? prefix
- Create and run macros
- Save command history as macros
"""

__version__ = "0.2.0"
__author__ = "Cliara Contributors"

from cliara.shell import CliaraShell
from cliara.macros import MacroManager
from cliara.safety import SafetyChecker

__all__ = ["CliaraShell", "MacroManager", "SafetyChecker"]

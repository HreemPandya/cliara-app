"""
Safety checks for potentially dangerous commands.
"""

import re
from typing import List, Tuple, Dict


# Dangerous command patterns
DANGEROUS_PATTERNS = [
    r'\brm\s+-rf\b',
    r'\brm\s+.*\s+-rf\b',
    r'\bmkfs\b',
    r'\bdd\b.*\bif=',
    r'\bshutdown\b',
    r'\breboot\b',
    r'\bkill\s+-9\b',
    r'\bsudo\b',
    r'\bformat\b',
    r'\bdel\s+/[fs]\b',  # Windows delete with force/recursive
    r'\brd\s+/s\b',      # Windows remove directory
    r'\>\/dev\/',        # Writing to device files
    r'\bchmod\s+777\b',
    r'\bchown\s+.*root',
    r'\bmv\s+.*\s+/dev/null',
]


class SafetyChecker:
    """Checks commands for potentially dangerous operations."""
    
    def __init__(self):
        self.patterns = [re.compile(pattern, re.IGNORECASE) for pattern in DANGEROUS_PATTERNS]
    
    def is_dangerous(self, command: str) -> bool:
        """
        Check if a command contains dangerous patterns.
        
        Args:
            command: Shell command to check
        
        Returns:
            True if command is potentially dangerous
        """
        for pattern in self.patterns:
            if pattern.search(command):
                return True
        return False
    
    def check_steps(self, steps: List[Dict[str, str]]) -> Tuple[bool, List[str]]:
        """
        Check multiple command steps for dangerous operations.
        
        Args:
            steps: List of command steps
        
        Returns:
            Tuple of (is_dangerous, list_of_dangerous_commands)
        """
        dangerous_commands = []
        
        for step in steps:
            if step.get('type') == 'cmd':
                command = step.get('value', '')
                if self.is_dangerous(command):
                    dangerous_commands.append(command)
        
        return len(dangerous_commands) > 0, dangerous_commands
    
    def get_warning_message(self, dangerous_commands: List[str]) -> str:
        """
        Generate a warning message for dangerous commands.
        
        Args:
            dangerous_commands: List of dangerous command strings
        
        Returns:
            Formatted warning message
        """
        msg = "\n[!] WARNING: This macro contains potentially DESTRUCTIVE commands:\n"
        for cmd in dangerous_commands:
            msg += f"   * {cmd}\n"
        msg += "\nThese commands could cause data loss or system instability."
        return msg

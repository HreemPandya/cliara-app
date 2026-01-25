"""
Safety checks for potentially dangerous commands.
Enhanced version for Cliara with better detection and warnings.
"""

import re
from typing import List, Tuple, Dict, Optional
from enum import Enum


class DangerLevel(Enum):
    """Classification of command danger levels."""
    SAFE = "safe"
    CAUTION = "caution"  # Might have side effects
    DANGEROUS = "dangerous"  # Could cause data loss
    CRITICAL = "critical"  # Could destroy system


# Dangerous command patterns with severity levels
DANGER_PATTERNS = {
    DangerLevel.CRITICAL: [
        r'\brm\s+-rf\s+/',  # Delete from root
        r'\bmkfs\b',  # Format filesystem
        r'\bdd\b.*\bif=/dev/',  # Write to device
        r'\>\/dev\/sd',  # Redirect to disk
    ],
    DangerLevel.DANGEROUS: [
        r'\brm\s+-rf\b',
        r'\brm\s+.*\s+-rf\b',
        r'\bshutdown\b',
        r'\breboot\b',
        r'\bkill\s+-9\b',
        r'\bformat\b',
        r'\bdel\s+/[fs]\b',  # Windows delete with force/recursive
        r'\brd\s+/s\b',      # Windows remove directory
        r'\bchmod\s+777\b',
        r'\bchown\s+.*root',
    ],
    DangerLevel.CAUTION: [
        r'\bsudo\b',  # Elevated privileges
        r'\bmv\s+.*\s+/dev/null',
        r'\bnpm\s+install\s+-g',  # Global install
        r'\bpip\s+install.*--force',
        r'\bgit\s+push\s+.*--force',
        r'\bgit\s+reset\s+--hard',
    ]
}


class SafetyChecker:
    """Checks commands for potentially dangerous operations."""
    
    def __init__(self):
        """Initialize safety checker with compiled patterns."""
        self.compiled_patterns = {}
        for level, patterns in DANGER_PATTERNS.items():
            self.compiled_patterns[level] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]
    
    def check_command(self, command: str) -> Tuple[DangerLevel, Optional[str]]:
        """
        Check a single command for danger level.
        
        Args:
            command: Shell command to check
        
        Returns:
            Tuple of (danger_level, matched_pattern)
        """
        # Check in order of severity
        for level in [DangerLevel.CRITICAL, DangerLevel.DANGEROUS, DangerLevel.CAUTION]:
            for pattern in self.compiled_patterns[level]:
                if pattern.search(command):
                    return level, pattern.pattern
        
        return DangerLevel.SAFE, None
    
    def check_commands(self, commands: List[str]) -> Tuple[DangerLevel, List[Tuple[str, str]]]:
        """
        Check multiple commands.
        
        Args:
            commands: List of shell commands
        
        Returns:
            Tuple of (highest_danger_level, list_of_(command, pattern))
        """
        highest_level = DangerLevel.SAFE
        dangerous_commands = []
        
        for cmd in commands:
            level, pattern = self.check_command(cmd)
            
            if level != DangerLevel.SAFE:
                dangerous_commands.append((cmd, pattern or "unknown"))
                
                # Update highest level
                level_order = [DangerLevel.SAFE, DangerLevel.CAUTION, 
                              DangerLevel.DANGEROUS, DangerLevel.CRITICAL]
                if level_order.index(level) > level_order.index(highest_level):
                    highest_level = level
        
        return highest_level, dangerous_commands
    
    def check_steps(self, steps: List[Dict[str, str]]) -> Tuple[bool, List[str]]:
        """
        Check macro steps for dangerous operations (legacy compatibility).
        
        Args:
            steps: List of command steps with 'type' and 'value'
        
        Returns:
            Tuple of (is_dangerous, list_of_dangerous_commands)
        """
        commands = [step['value'] for step in steps if step.get('type') == 'cmd']
        level, dangerous = self.check_commands(commands)
        
        is_dangerous = level in [DangerLevel.DANGEROUS, DangerLevel.CRITICAL]
        dangerous_cmds = [cmd for cmd, _ in dangerous]
        
        return is_dangerous, dangerous_cmds
    
    def get_warning_message(self, commands: List[str], level: Optional[DangerLevel] = None) -> str:
        """
        Generate appropriate warning message.
        
        Args:
            commands: List of dangerous commands
            level: Danger level (auto-detected if None)
        
        Returns:
            Formatted warning message
        """
        if not level:
            level, _ = self.check_commands(commands)
        
        if level == DangerLevel.CRITICAL:
            icon = "[!!!]"
            title = "CRITICAL WARNING"
            desc = "These commands could DESTROY your system or data!"
        elif level == DangerLevel.DANGEROUS:
            icon = "[!!]"
            title = "DANGEROUS"
            desc = "These commands could cause data loss or system instability."
        elif level == DangerLevel.CAUTION:
            icon = "[!]"
            title = "CAUTION"
            desc = "These commands might have unintended side effects."
        else:
            return ""
        
        msg = f"\n{icon} {title}\n"
        msg += f"{desc}\n\n"
        msg += "Commands:\n"
        for cmd in commands:
            msg += f"  * {cmd}\n"
        
        return msg
    
    def get_confirmation_prompt(self, level: DangerLevel) -> str:
        """
        Get appropriate confirmation prompt for danger level.
        
        Args:
            level: Danger level
        
        Returns:
            Confirmation prompt string
        """
        if level == DangerLevel.CRITICAL:
            return "\nType 'I UNDERSTAND' to execute: "
        elif level == DangerLevel.DANGEROUS:
            return "\nType 'RUN' to execute: "
        elif level == DangerLevel.CAUTION:
            return "\nContinue? (y/n): "
        else:
            return "\nRun? (y/n): "
    
    def validate_confirmation(self, response: str, level: DangerLevel) -> bool:
        """
        Validate user confirmation response.
        
        Args:
            response: User's input
            level: Danger level requiring confirmation
        
        Returns:
            True if confirmation is valid
        """
        response = response.strip()
        
        if level == DangerLevel.CRITICAL:
            return response == "I UNDERSTAND"
        elif level == DangerLevel.DANGEROUS:
            return response == "RUN"
        elif level == DangerLevel.CAUTION:
            return response.lower() in ['y', 'yes']
        else:
            return response.lower() in ['y', 'yes']

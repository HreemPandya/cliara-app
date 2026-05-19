"""
Safety checks for potentially dangerous commands.
Enhanced version for Cliara with better detection and warnings.
"""

import re
from typing import List, Tuple, Dict, Optional
from enum import Enum

from cliara import icons


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
        r'\bgit\s+filter-branch\b',            # History rewriting
        r'\bterraform\s+destroy\b',            # Infrastructure destruction
        r'\bfind\s+.*-exec\s+rm\b',            # Mass deletion via find
        r'\bxargs\s+rm\b',                     # Mass deletion via xargs
        r'\bgit\s+clean\s+-fd',                # Remove untracked files + dirs
    ],
    DangerLevel.CAUTION: [
        r'\bsudo\b',  # Elevated privileges
        r'\bmv\s+.*\s+/dev/null',
        r'\bnpm\s+install\s+-g',  # Global install
        r'\bpip\s+install.*--force',
        r'\bgit\s+push\s+.*--force',
        r'\bgit\s+reset\s+--hard',
        r'\bnpm\s+publish\b',                  # Package publishing
        r'\bcargo\s+publish\b',
        r'\bdocker\s+push\b',                  # Image publishing
        r'\bfly\s+deploy\b',                   # Deployment
        r'\bterraform\s+apply\b',              # Infrastructure changes
        r'\bgit\s+rebase\b',                   # History rewriting
        r'\bcat\s+.*\.env\b',                  # Credential exposure
        r'\bprintenv\b.*\|.*\bcurl\b',         # Env piped to network
        r'\bcurl\b.*(-d|--data)\s+@',          # Upload file via curl
        r'\bscp\b',                            # Remote file copy
        r'\bgit\s+push\s+.*--no-verify',       # Skips pre-push hooks
        r'\bgit\s+commit\s+.*--no-verify',     # Skips pre-commit hooks
        r'\bdocker\s+system\s+prune\b',        # Bulk Docker cleanup
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
    
    # Split tokens: pipe, semicolon, &&, ||, command-substitution openers.
    # We intentionally keep this simple (no full shell parser) — false negatives
    # are acceptable as long as we catch the obvious dangerous compositions like
    # `find . -name "*.bak" | xargs rm -rf` and `ls; rm -rf /`.
    _PIPELINE_SPLIT_RE = re.compile(
        r'\|{1,2}|;|&&|`|\$\(|\bdo\b|\bthen\b',
        re.IGNORECASE,
    )

    @staticmethod
    def _split_pipeline_stages(command: str) -> List[str]:
        """Return each pipeline / compound-command stage as a separate string.

        Uses a simple regex split so we evaluate the danger level of every
        component — e.g. the `rm -rf` in `find . | xargs rm -rf` is checked
        even though the leading `find` is safe.
        """
        stages = SafetyChecker._PIPELINE_SPLIT_RE.split(command)
        return [s.strip() for s in stages if s.strip()]

    def check_command(self, command: str) -> Tuple[DangerLevel, Optional[str]]:
        """Check a command (and every pipeline/compound stage within it) for danger.

        Returns the *highest* danger level found across all stages so that
        constructs like ``find . -name '*.bak' | xargs rm -rf`` are correctly
        classified as DANGEROUS rather than SAFE.
        """
        stages = self._split_pipeline_stages(command)
        if not stages:
            stages = [command]

        highest = DangerLevel.SAFE
        matched_pattern: Optional[str] = None
        level_order = [DangerLevel.SAFE, DangerLevel.CAUTION,
                       DangerLevel.DANGEROUS, DangerLevel.CRITICAL]

        for stage in stages:
            for level in [DangerLevel.CRITICAL, DangerLevel.DANGEROUS, DangerLevel.CAUTION]:
                for pattern in self.compiled_patterns[level]:
                    if pattern.search(stage):
                        if level_order.index(level) > level_order.index(highest):
                            highest = level
                            matched_pattern = pattern.pattern
                        break  # one match per level per stage is enough

        return highest, matched_pattern
    
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
            icon = icons.DANGER
            title = "CRITICAL WARNING"
            desc = "These commands could DESTROY your system or data!"
        elif level == DangerLevel.DANGEROUS:
            icon = icons.WARN
            title = "DANGEROUS"
            desc = "These commands could cause data loss or system instability."
        elif level == DangerLevel.CAUTION:
            icon = icons.WARN
            title = "CAUTION"
            desc = "These commands might have unintended side effects."
        else:
            return ""
        
        msg = f"\n[{icon}] {title}\n"
        msg += f"{desc}\n\n"
        msg += "Commands:\n"
        for cmd in commands:
            msg += f"  * {cmd}\n"
        
        return msg
    
    def get_warning_panel_data(
        self, commands: List[str], level: Optional[DangerLevel] = None
    ) -> Optional[Tuple[DangerLevel, str, str, str]]:
        """
        Return (level, title, description, confirmation_prompt) for Rich Panel rendering.
        Returns None if level is SAFE.
        """
        if not level:
            level, _ = self.check_commands(commands)
        if level == DangerLevel.CRITICAL:
            title = "CRITICAL"
            desc = "These commands could DESTROY your system or data!"
            prompt = "Type  I UNDERSTAND  to proceed, or press Enter to cancel."
        elif level == DangerLevel.DANGEROUS:
            title = "DANGEROUS"
            desc = "These commands could cause data loss or system instability."
            prompt = "Type  RUN  to proceed, or press Enter to cancel."
        elif level == DangerLevel.CAUTION:
            title = "CAUTION"
            desc = "These commands might have unintended side effects."
            prompt = "Continue? (y/n) or press Enter to cancel."
        else:
            return None
        return (level, title, desc, prompt)

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

"""
Command parser for macro creation and command interpretation.
Handles the "remember:" syntax and variable substitution.
"""

import re
from typing import Dict, List, Optional, Tuple


class Parser:
    """Parses user input for macro creation and execution."""
    
    # Pattern: remember: "macro name" -> cmd1 ; cmd2 ; cmd3
    REMEMBER_PATTERN = re.compile(
        r'^remember:\s*["\']([^"\']+)["\']\s*->\s*(.+)$',
        re.IGNORECASE
    )
    
    # Alternative pattern: remember "macro name": cmd1 ; cmd2
    REMEMBER_ALT_PATTERN = re.compile(
        r'^remember\s+["\']([^"\']+)["\']:\s*(.+)$',
        re.IGNORECASE
    )
    
    # Variable pattern: {varname}
    VARIABLE_PATTERN = re.compile(r'\{(\w+)\}')
    
    def __init__(self):
        pass
    
    def is_remember_command(self, user_input: str) -> bool:
        """Check if input is a macro creation command."""
        return bool(self.REMEMBER_PATTERN.match(user_input) or 
                   self.REMEMBER_ALT_PATTERN.match(user_input))
    
    def parse_remember(self, user_input: str) -> Optional[Tuple[str, str, List[Dict[str, str]]]]:
        """
        Parse a 'remember' command.
        
        Args:
            user_input: User's input string
        
        Returns:
            Tuple of (macro_name, description, steps) or None if parsing fails
        """
        # Try primary pattern
        match = self.REMEMBER_PATTERN.match(user_input)
        if not match:
            # Try alternative pattern
            match = self.REMEMBER_ALT_PATTERN.match(user_input)
        
        if not match:
            return None
        
        macro_name = match.group(1).strip()
        commands_str = match.group(2).strip()
        
        # Split by semicolon to get individual commands
        command_list = [cmd.strip() for cmd in commands_str.split(';') if cmd.strip()]
        
        # Create steps
        steps = [
            {"type": "cmd", "value": cmd}
            for cmd in command_list
        ]
        
        # Generate description from commands
        description = self._generate_description(command_list)
        
        return macro_name, description, steps
    
    def _generate_description(self, commands: List[str]) -> str:
        """Generate a description from command list."""
        if len(commands) == 1:
            return f"Runs: {commands[0][:60]}..."
        else:
            return f"Runs {len(commands)} commands"
    
    def extract_variables(self, macro_name: str) -> List[str]:
        """
        Extract variable names from a macro name.
        
        Args:
            macro_name: Macro name potentially containing {var} patterns
        
        Returns:
            List of variable names
        """
        return self.VARIABLE_PATTERN.findall(macro_name)
    
    def substitute_variables(self, template: str, values: Dict[str, str]) -> str:
        """
        Substitute variables in a template string.
        
        Args:
            template: String with {var} placeholders
            values: Dictionary of variable_name -> value
        
        Returns:
            String with variables substituted
        """
        result = template
        for var_name, var_value in values.items():
            result = result.replace(f'{{{var_name}}}', var_value)
        return result
    
    def match_macro_with_variables(self, macro_name: str, user_input: str) -> Optional[Dict[str, str]]:
        """
        Match user input against a macro name with variables.
        
        Args:
            macro_name: Macro name template (e.g., "kill port {port}")
            user_input: User's input (e.g., "kill port 3000")
        
        Returns:
            Dictionary of variable values or None if no match
        """
        # Escape regex special characters except {var}
        pattern = re.escape(macro_name)
        
        # Replace escaped variable placeholders with capture groups
        pattern = re.sub(r'\\{(\w+)\\}', r'(?P<\1>\\S+)', pattern)
        
        # Try to match
        regex = re.compile(f'^{pattern}$', re.IGNORECASE)
        match = regex.match(user_input.strip())
        
        if match:
            return match.groupdict()
        return None
    
    def normalize_input(self, user_input: str) -> str:
        """
        Normalize user input for matching.
        
        Args:
            user_input: Raw user input
        
        Returns:
            Normalized string (lowercase, stripped)
        """
        return user_input.strip().lower()
    
    def is_management_command(self, user_input: str) -> Optional[Tuple[str, Optional[str]]]:
        """
        Check if input is a macro management command.
        
        Returns:
            Tuple of (command_type, argument) or None
            command_type: 'list', 'show', 'delete', 'edit'
        """
        normalized = user_input.strip().lower()
        
        # macros list
        if normalized == 'macros list' or normalized == 'list macros':
            return ('list', None)
        
        # macros show <name>
        show_match = re.match(r'^macros\s+show\s+(.+)$', normalized)
        if show_match:
            return ('show', show_match.group(1).strip())
        
        # macros delete <name>
        delete_match = re.match(r'^macros\s+delete\s+(.+)$', normalized)
        if delete_match:
            return ('delete', delete_match.group(1).strip())
        
        # macros edit <name>
        edit_match = re.match(r'^macros\s+edit\s+(.+)$', normalized)
        if edit_match:
            return ('edit', edit_match.group(1).strip())
        
        return None

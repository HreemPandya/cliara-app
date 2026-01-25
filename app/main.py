"""
Natural Language Macros CLI - Main entry point.
Provides an interactive loop for creating and running command macros.
"""

import sys
from typing import Optional
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.macros import MacroStore
from app.parser import Parser
from app.executor import CommandExecutor, ExecutionStatus
from app.safety import SafetyChecker


class NLMacrosCLI:
    """Main CLI application for Natural Language Macros."""
    
    def __init__(self):
        self.store = MacroStore()
        self.parser = Parser()
        self.executor = CommandExecutor(stop_on_error=True)
        self.safety = SafetyChecker()
        self.running = True
    
    def print_banner(self):
        """Print welcome banner."""
        print("\n" + "="*60)
        print("  Natural Language Macros (NLM)")
        print("  Create and run terminal command shortcuts")
        print("="*60)
        print("\nCommands:")
        print("  remember: \"name\" -> cmd1 ; cmd2  - Create a macro")
        print("  <macro name>                     - Run a macro")
        print("  macros list                      - List all macros")
        print("  macros show <name>               - Show macro details")
        print("  macros delete <name>             - Delete a macro")
        print("  help                             - Show this help")
        print("  exit / quit                      - Exit the program")
        print()
    
    def run(self):
        """Main CLI loop."""
        self.print_banner()
        
        while self.running:
            try:
                user_input = input("nl> ").strip()
                
                if not user_input:
                    continue
                
                self.handle_input(user_input)
            
            except KeyboardInterrupt:
                print("\n\nUse 'exit' or 'quit' to leave.")
            except EOFError:
                print("\n\nGoodbye!")
                break
    
    def handle_input(self, user_input: str):
        """
        Handle user input and route to appropriate handler.
        
        Args:
            user_input: Raw user input string
        """
        # Check for exit commands
        if user_input.lower() in ['exit', 'quit', 'q']:
            print("\nGoodbye!")
            self.running = False
            return
        
        # Check for help
        if user_input.lower() in ['help', '?']:
            self.print_banner()
            return
        
        # Check for remember command
        if self.parser.is_remember_command(user_input):
            self.handle_remember(user_input)
            return
        
        # Check for management commands
        mgmt_cmd = self.parser.is_management_command(user_input)
        if mgmt_cmd:
            cmd_type, arg = mgmt_cmd
            self.handle_management(cmd_type, arg)
            return
        
        # Try to execute as macro
        self.handle_macro_execution(user_input)
    
    def handle_remember(self, user_input: str):
        """Handle macro creation."""
        result = self.parser.parse_remember(user_input)
        
        if not result:
            print("[X] Failed to parse macro definition.")
            print("Format: remember: \"name\" -> cmd1 ; cmd2 ; cmd3")
            return
        
        macro_name, description, steps = result
        
        # Check for dangerous commands
        is_dangerous, dangerous_cmds = self.safety.check_steps(steps)
        
        if is_dangerous:
            print(self.safety.get_warning_message(dangerous_cmds))
            confirm = input("\nDo you still want to save this macro? (yes/no): ").strip().lower()
            if confirm not in ['yes', 'y']:
                print("[X] Macro not saved.")
                return
        
        # Save the macro
        self.store.add_macro(macro_name, description, steps)
        print(f"[OK] Macro '{macro_name}' saved!")
        print(f"  Description: {description}")
        print(f"  Steps: {len(steps)}")
    
    def handle_management(self, cmd_type: str, arg: Optional[str]):
        """Handle macro management commands."""
        if cmd_type == 'list':
            self.list_macros()
        elif cmd_type == 'show':
            self.show_macro(arg)
        elif cmd_type == 'delete':
            self.delete_macro(arg)
        elif cmd_type == 'edit':
            self.edit_macro(arg)
    
    def list_macros(self):
        """List all available macros."""
        macros = self.store.list_macros()
        
        if not macros:
            print("No macros defined yet.")
            print('Try: remember: "example" -> echo Hello')
            return
        
        print(f"\n[Macros] Available Macros ({len(macros)}):\n")
        for name, data in macros.items():
            desc = data.get('description', 'No description')
            step_count = len(data.get('steps', []))
            print(f"  * {name}")
            print(f"    {desc} ({step_count} step{'s' if step_count != 1 else ''})")
        print()
    
    def show_macro(self, name: str):
        """Show details of a specific macro."""
        if not name:
            print("[X] Please specify a macro name.")
            return
        
        macro = self.store.get_macro(name)
        
        if not macro:
            # Try fuzzy match
            match = self.store.find_macro_fuzzy(name)
            if match:
                print(f"Did you mean '{match}'?")
                macro = self.store.get_macro(match)
                name = match
            else:
                print(f"[X] Macro '{name}' not found.")
                return
        
        print(f"\n[Macro] {name}")
        print(f"Description: {macro.get('description', 'N/A')}")
        print(f"\nSteps:")
        for i, step in enumerate(macro.get('steps', []), 1):
            print(f"  {i}. {step.get('value', 'N/A')}")
        print()
    
    def delete_macro(self, name: str):
        """Delete a macro."""
        if not name:
            print("[X] Please specify a macro name.")
            return
        
        if not self.store.macro_exists(name):
            print(f"[X] Macro '{name}' not found.")
            return
        
        confirm = input(f"Delete macro '{name}'? (y/n): ").strip().lower()
        if confirm in ['y', 'yes']:
            self.store.delete_macro(name)
            print(f"[OK] Macro '{name}' deleted.")
        else:
            print("Cancelled.")
    
    def edit_macro(self, name: str):
        """Edit a macro (placeholder for future implementation)."""
        print("[!] Edit functionality coming soon!")
        print(f"For now, delete '{name}' and recreate it.")
    
    def handle_macro_execution(self, user_input: str):
        """Handle macro execution."""
        # First, try exact match
        macro = self.store.get_macro(user_input)
        macro_name = user_input
        variables = {}
        
        # If no exact match, try variable substitution
        if not macro:
            macro, macro_name, variables = self._find_macro_with_variables(user_input)
        
        # If still no match, try fuzzy matching
        if not macro:
            match = self.store.find_macro_fuzzy(user_input, threshold=75)
            if match:
                print(f"Did you mean '{match}'? (y/n): ", end='')
                confirm = input().strip().lower()
                if confirm in ['y', 'yes']:
                    macro = self.store.get_macro(match)
                    macro_name = match
        
        if not macro:
            print(f"[X] Unknown command or macro: {user_input}")
            print("Type 'macros list' to see available macros or 'help' for commands.")
            return
        
        # Substitute variables in steps
        steps = macro.get('steps', [])
        if variables:
            steps = self._substitute_variables_in_steps(steps, variables)
        
        # Preview and confirm
        print(self.executor.preview_steps(steps))
        
        # Safety check
        is_dangerous, dangerous_cmds = self.safety.check_steps(steps)
        
        if is_dangerous:
            print(self.safety.get_warning_message(dangerous_cmds))
            confirm = input("\nType 'RUN' to execute anyway: ").strip()
            if confirm != 'RUN':
                print("[X] Execution cancelled.")
                return
        else:
            confirm = input("Run? (y/n): ").strip().lower()
            if confirm not in ['y', 'yes']:
                print("[X] Execution cancelled.")
                return
        
        # Execute
        status, results = self.executor.execute_steps(steps)
        
        if status == ExecutionStatus.SUCCESS:
            print("[OK] Macro completed successfully!")
        elif status == ExecutionStatus.FAILED:
            print("[X] Macro execution failed.")
    
    def _find_macro_with_variables(self, user_input: str):
        """
        Find a macro that matches user input with variable substitution.
        
        Returns:
            Tuple of (macro_dict, macro_name, variables_dict)
        """
        for macro_name in self.store.list_macros().keys():
            variables = self.parser.match_macro_with_variables(macro_name, user_input)
            if variables:
                macro = self.store.get_macro(macro_name)
                return macro, macro_name, variables
        
        return None, None, {}
    
    def _substitute_variables_in_steps(self, steps, variables):
        """Substitute variables in command steps."""
        new_steps = []
        for step in steps:
            new_step = step.copy()
            if step.get('type') == 'cmd':
                new_step['value'] = self.parser.substitute_variables(
                    step['value'], 
                    variables
                )
            new_steps.append(new_step)
        return new_steps


def main():
    """Entry point for the CLI."""
    cli = NLMacrosCLI()
    cli.run()


if __name__ == '__main__':
    main()

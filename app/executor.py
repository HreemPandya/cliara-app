"""
Command executor for running macro steps.
Handles multi-step execution with output capture and error handling.
"""

import subprocess
import sys
from typing import List, Dict, Tuple
from enum import Enum


class ExecutionStatus(Enum):
    """Execution result status."""
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommandExecutor:
    """Executes command steps with output capture."""
    
    def __init__(self, stop_on_error: bool = True):
        """
        Initialize executor.
        
        Args:
            stop_on_error: If True, stop execution on first error
        """
        self.stop_on_error = stop_on_error
    
    def execute_steps(self, steps: List[Dict[str, str]]) -> Tuple[ExecutionStatus, List[Dict]]:
        """
        Execute a list of command steps.
        
        Args:
            steps: List of command steps with 'type' and 'value'
        
        Returns:
            Tuple of (status, results_list)
            results_list contains dicts with: step, stdout, stderr, returncode
        """
        results = []
        
        print("\n" + "="*60)
        print("EXECUTING MACRO")
        print("="*60 + "\n")
        
        for i, step in enumerate(steps, 1):
            if step.get('type') != 'cmd':
                continue
            
            command = step.get('value', '')
            print(f"[{i}/{len(steps)}] Running: {command}")
            print("-" * 60)
            
            result = self._execute_command(command)
            results.append({
                'step': i,
                'command': command,
                'stdout': result['stdout'],
                'stderr': result['stderr'],
                'returncode': result['returncode']
            })
            
            # Print output
            if result['stdout']:
                print(result['stdout'])
            
            if result['stderr']:
                print(f"stderr: {result['stderr']}", file=sys.stderr)
            
            # Check for errors
            if result['returncode'] != 0:
                print(f"\n[X] Command failed with exit code {result['returncode']}")
                
                if self.stop_on_error:
                    print("[!] Stopping execution due to error.\n")
                    return ExecutionStatus.FAILED, results
                else:
                    print("[!] Continuing despite error...\n")
            else:
                print(f"[OK] Command completed successfully\n")
        
        print("="*60)
        print("EXECUTION COMPLETE")
        print("="*60 + "\n")
        
        return ExecutionStatus.SUCCESS, results
    
    def _execute_command(self, command: str) -> Dict[str, any]:
        """
        Execute a single shell command.
        
        Args:
            command: Shell command to execute
        
        Returns:
            Dict with stdout, stderr, and returncode
        """
        try:
            # Determine shell based on platform
            if sys.platform == 'win32':
                shell_exec = True
                # Use PowerShell for better command support on Windows
                # But for simplicity, use cmd.exe default shell behavior
            else:
                shell_exec = True
            
            result = subprocess.run(
                command,
                shell=shell_exec,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            return {
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode
            }
        
        except subprocess.TimeoutExpired:
            return {
                'stdout': '',
                'stderr': 'Command timed out after 5 minutes',
                'returncode': -1
            }
        except Exception as e:
            return {
                'stdout': '',
                'stderr': f'Execution error: {str(e)}',
                'returncode': -1
            }
    
    def preview_steps(self, steps: List[Dict[str, str]]) -> str:
        """
        Generate a preview of steps to be executed.
        
        Args:
            steps: List of command steps
        
        Returns:
            Formatted preview string
        """
        preview = "\nThis macro will run:\n"
        for i, step in enumerate(steps, 1):
            if step.get('type') == 'cmd':
                command = step.get('value', '')
                preview += f"  {i}) {command}\n"
        return preview

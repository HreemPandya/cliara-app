"""
Simple tests for Natural Language Macros.
Run with: python -m pytest tests/ or just python tests/test_basic.py
"""

import sys
import os
from pathlib import Path

# Fix Windows console encoding for Unicode characters
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.parser import Parser
from app.safety import SafetyChecker
from app.macros import MacroStore


def test_parser_remember():
    """Test parsing remember commands."""
    parser = Parser()
    
    # Test primary pattern
    result = parser.parse_remember('remember: "test macro" -> echo hello ; echo world')
    assert result is not None
    name, desc, steps = result
    assert name == "test macro"
    assert len(steps) == 2
    assert steps[0]['value'] == "echo hello"
    assert steps[1]['value'] == "echo world"
    
    print("[OK] Parser remember test passed")


def test_safety_checker():
    """Test safety checks."""
    checker = SafetyChecker()
    
    # Safe commands
    assert not checker.is_dangerous("echo hello")
    assert not checker.is_dangerous("ls -la")
    
    # Dangerous commands
    assert checker.is_dangerous("rm -rf /")
    assert checker.is_dangerous("sudo reboot")
    assert checker.is_dangerous("kill -9 1234")
    
    print("[OK] Safety checker test passed")


def test_variable_extraction():
    """Test variable extraction and substitution."""
    parser = Parser()
    
    # Extract variables
    vars = parser.extract_variables("kill port {port}")
    assert "port" in vars
    
    # Substitute variables
    result = parser.substitute_variables(
        "lsof -ti :{port} | xargs kill -9",
        {"port": "3000"}
    )
    assert result == "lsof -ti :3000 | xargs kill -9"
    
    print("[OK] Variable test passed")


def test_variable_matching():
    """Test matching user input with variable macros."""
    parser = Parser()
    
    result = parser.match_macro_with_variables(
        "kill port {port}",
        "kill port 3000"
    )
    assert result is not None
    assert result['port'] == "3000"
    
    print("[OK] Variable matching test passed")


def test_management_command_detection():
    """Test management command parsing."""
    parser = Parser()
    
    # Test list
    assert parser.is_management_command("macros list") == ('list', None)
    
    # Test show
    result = parser.is_management_command("macros show test")
    assert result[0] == 'show'
    assert result[1] == 'test'
    
    # Test delete
    result = parser.is_management_command("macros delete test")
    assert result[0] == 'delete'
    
    print("[OK] Management command test passed")


if __name__ == '__main__':
    print("Running Natural Language Macros tests...\n")
    
    try:
        test_parser_remember()
        test_safety_checker()
        test_variable_extraction()
        test_variable_matching()
        test_management_command_detection()
        
        print("\n" + "="*50)
        print("[SUCCESS] All tests passed!")
        print("="*50)
    except AssertionError as e:
        print(f"\n[FAIL] Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Error running tests: {e}")
        sys.exit(1)

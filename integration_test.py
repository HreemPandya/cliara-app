"""
Quick test script to verify all components work together.
Run this before committing or releasing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.macros import MacroStore
from app.parser import Parser
from app.executor import CommandExecutor
from app.safety import SafetyChecker


def test_full_workflow():
    """Test the complete workflow from creation to execution."""
    print("="*60)
    print("INTEGRATION TEST: Full Workflow")
    print("="*60)
    
    # Initialize components
    store = MacroStore(data_dir="data_test")
    parser = Parser()
    executor = CommandExecutor()
    safety = SafetyChecker()
    
    # Test 1: Create a macro
    print("\n[Test 1] Creating macro...")
    result = parser.parse_remember('remember: "test echo" -> echo Testing 123')
    assert result is not None
    name, desc, steps = result
    store.add_macro(name, desc, steps)
    assert store.macro_exists("test echo")
    print("[OK] Macro created and stored")
    
    # Test 2: Retrieve macro
    print("\n[Test 2] Retrieving macro...")
    macro = store.get_macro("test echo")
    assert macro is not None
    assert len(macro['steps']) == 1
    print("[OK] Macro retrieved successfully")
    
    # Test 3: Safety check (safe command)
    print("\n[Test 3] Safety check on safe command...")
    is_dangerous, _ = safety.check_steps(steps)
    assert not is_dangerous
    print("[OK] Safe command passed safety check")
    
    # Test 4: Safety check (dangerous command)
    print("\n[Test 4] Safety check on dangerous command...")
    dangerous_steps = [{"type": "cmd", "value": "rm -rf /tmp/test"}]
    is_dangerous, dangerous_cmds = safety.check_steps(dangerous_steps)
    assert is_dangerous
    assert len(dangerous_cmds) > 0
    print("[OK] Dangerous command detected")
    
    # Test 5: Variable substitution
    print("\n[Test 5] Variable substitution...")
    result = parser.parse_remember('remember: "greet {name}" -> echo Hello {name}')
    name, desc, steps = result
    store.add_macro(name, desc, steps)
    
    variables = parser.match_macro_with_variables("greet {name}", "greet World")
    assert variables is not None
    assert variables['name'] == "World"
    
    substituted = parser.substitute_variables("echo Hello {name}", variables)
    assert substituted == "echo Hello World"
    print("[OK] Variable substitution works")
    
    # Test 6: Fuzzy matching
    print("\n[Test 6] Fuzzy matching...")
    match = store.find_macro_fuzzy("test eco", threshold=70)
    assert match == "test echo"
    print("[OK] Fuzzy matching works")
    
    # Test 7: Management commands
    print("\n[Test 7] Management command parsing...")
    assert parser.is_management_command("macros list") == ('list', None)
    assert parser.is_management_command("macros show test")[0] == 'show'
    assert parser.is_management_command("macros delete test")[0] == 'delete'
    print("[OK] Management commands parsed correctly")
    
    # Test 8: Execute simple command
    print("\n[Test 8] Executing simple command...")
    test_steps = [{"type": "cmd", "value": "echo Integration test successful"}]
    status, results = executor.execute_steps(test_steps)
    assert status.value == "success"
    assert len(results) == 1
    print("[OK] Command executed successfully")
    
    # Cleanup
    print("\n[Cleanup] Removing test data...")
    import shutil
    shutil.rmtree("data_test", ignore_errors=True)
    print("[OK] Cleanup complete")
    
    print("\n" + "="*60)
    print("[SUCCESS] All integration tests passed!")
    print("="*60)


if __name__ == '__main__':
    try:
        test_full_workflow()
    except AssertionError as e:
        print(f"\n[FAIL] Integration test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Integration test error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

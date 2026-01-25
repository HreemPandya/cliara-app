"""
Example macros for testing Natural Language Macros CLI.
You can manually add these to data/macros.json or create them via the CLI.
"""

EXAMPLE_MACROS = {
    "hello world": {
        "description": "Simple hello world test",
        "steps": [
            {"type": "cmd", "value": "echo Hello, World!"}
        ]
    },
    
    "show system info": {
        "description": "Display system information",
        "steps": [
            {"type": "cmd", "value": "echo System Information"},
            {"type": "cmd", "value": "echo Current User: %USERNAME%"},
            {"type": "cmd", "value": "echo Current Directory: %CD%"}
        ]
    },
    
    "list files": {
        "description": "List files in current directory",
        "steps": [
            {"type": "cmd", "value": "dir"}
        ]
    },
    
    "create and list": {
        "description": "Create a test file and list it",
        "steps": [
            {"type": "cmd", "value": "echo test content > test.txt"},
            {"type": "cmd", "value": "dir test.txt"},
            {"type": "cmd", "value": "type test.txt"}
        ]
    },
    
    "kill port {port}": {
        "description": "Kill process on specified port",
        "steps": [
            {"type": "cmd", "value": "netstat -ano | findstr :{port}"}
        ]
    },
    
    "git status": {
        "description": "Show git repository status",
        "steps": [
            {"type": "cmd", "value": "git status"},
            {"type": "cmd", "value": "git branch"}
        ]
    }
}

# For manual testing, you can copy the JSON above into data/macros.json

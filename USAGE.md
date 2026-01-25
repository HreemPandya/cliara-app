# Natural Language Macros - Usage Guide

## Installation

1. Clone or download this repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the CLI:
```bash
python -m app.main
```

## Quick Start

### Creating Your First Macro

The syntax for creating macros is:
```
remember: "macro name" -> command1 ; command2 ; command3
```

**Example:**
```
nl> remember: "hello world" -> echo Hello, World!
✓ Macro 'hello world' saved!
```

**Multi-step example:**
```
nl> remember: "show info" -> echo System Info ; python --version ; echo Done
✓ Macro 'show info' saved!
```

### Running Macros

Just type the macro name:
```
nl> hello world

This macro will run:
  1) echo Hello, World!

Run? (y/n): y
```

### Using Variables

Create macros with placeholders using `{variable}` syntax:

```
nl> remember: "greet {name}" -> echo Hello, {name}! ; echo Welcome, {name}!
✓ Macro 'greet {name}' saved!

nl> greet Alice

This macro will run:
  1) echo Hello, Alice!
  2) echo Welcome, Alice!

Run? (y/n): y
```

**Common variable macros:**
```
# Kill process on specific port
remember: "kill port {port}" -> netstat -ano | findstr :{port}

# Git commit with message
remember: "quick commit {msg}" -> git add . ; git commit -m "{msg}"

# Create directory and navigate
remember: "mkcd {dirname}" -> mkdir {dirname} ; cd {dirname}
```

### Managing Macros

#### List All Macros
```
nl> macros list

📋 Available Macros (3):
  • hello world
    Simple hello world test (1 step)
  • show info
    Display system information (4 steps)
  • greet {name}
    Runs 2 commands
```

#### Show Macro Details
```
nl> macros show hello world

📦 Macro: hello world
Description: Simple hello world test

Steps:
  1. echo Hello, World!
```

#### Delete a Macro
```
nl> macros delete hello world
Delete macro 'hello world'? (y/n): y
✓ Macro 'hello world' deleted.
```

## Safety Features

### Dangerous Command Detection

The tool automatically detects potentially dangerous commands and requires extra confirmation:

**Dangerous patterns:**
- `rm -rf`
- `sudo`
- `kill -9`
- `shutdown`, `reboot`
- `mkfs`, `dd`
- `format`, `del /f`

**Example:**
```
nl> remember: "force delete" -> rm -rf temp/

⚠️  WARNING: This macro contains potentially DESTRUCTIVE commands:
   • rm -rf temp/

These commands could cause data loss or system instability.

Do you still want to save this macro? (yes/no): 
```

When running dangerous macros:
```
nl> force delete

This macro will run:
  1) rm -rf temp/

⚠️  WARNING: This macro contains potentially DESTRUCTIVE commands:
   • rm -rf temp/

These commands could cause data loss or system instability.

Type 'RUN' to execute anyway: 
```

### Preview Before Execution

All macros show a preview before execution:
- Lists all commands that will run
- Shows the order of execution
- Waits for confirmation (y/n)
- Safe commands: simple yes/no
- Dangerous commands: type 'RUN' explicitly

## Advanced Features

### Fuzzy Matching

Don't remember the exact macro name? The tool will help:

```
nl> hello wrld
Did you mean 'hello world'? (y/n): y
```

### Multi-Step Execution

Commands run in order and stop on first error (by default):

```
nl> remember: "build project" -> npm install ; npm run build ; npm test

nl> build project
# If npm install fails, build and test won't run
```

### Windows-Specific Examples

```
# List directory contents
remember: "ll" -> dir

# Show network connections
remember: "ports" -> netstat -ano

# Clear terminal
remember: "cls" -> cls

# Show environment variables
remember: "showenv" -> set

# Find process by name
remember: "findproc {name}" -> tasklist | findstr {name}
```

### Git Workflow Examples

```
# Quick status
remember: "gs" -> git status -s

# Pull and update
remember: "update" -> git pull ; git submodule update

# Quick add, commit, push
remember: "save {message}" -> git add . ; git commit -m "{message}" ; git push

# Show recent commits
remember: "recent" -> git log --oneline -10
```

### Development Workflow Examples

```
# Start development server
remember: "dev" -> npm run dev

# Run tests
remember: "test" -> python -m pytest tests/ -v

# Lint and format
remember: "lint" -> black . ; flake8 .

# Build and deploy
remember: "deploy" -> npm run build ; npm run deploy
```

## Tips and Best Practices

### 1. Start Simple
Begin with simple, single-command macros to get familiar:
```
remember: "home" -> cd ~
remember: "proj" -> cd C:\Users\myuser\projects
```

### 2. Use Descriptive Names
Choose names that make sense to you:
```
✓ Good: "reset backend", "start dev server"
✗ Bad: "rb", "sds"
```

### 3. Group Related Commands
Combine commands that you always run together:
```
remember: "morning setup" -> cd C:\work ; git pull ; code .
```

### 4. Test Dangerous Commands First
Before adding `rm` or `kill` commands, run them manually first:
```
# Test individually first!
rm -rf temp/
# If it works as expected, then:
remember: "clean temp" -> rm -rf temp/
```

### 5. Use Variables for Flexibility
Instead of multiple similar macros:
```
✗ Bad:
remember: "kill 3000" -> lsof -ti :3000 | xargs kill -9
remember: "kill 5173" -> lsof -ti :5173 | xargs kill -9

✓ Good:
remember: "kill port {port}" -> lsof -ti :{port} | xargs kill -9
```

### 6. Document Complex Macros
Use clear descriptions in your macro names:
```
remember: "full system backup" -> ...
remember: "database reset and seed" -> ...
```

## Troubleshooting

### Macro Not Found
```
nl> my macro
❌ Unknown command or macro: my macro
Type 'macros list' to see available macros or 'help' for commands.
```

**Solutions:**
- Check spelling: `macros list`
- Try fuzzy match: type close approximation
- Show details: `macros show [partial name]`

### Command Fails During Execution
```
[2/3] Running: npm run build
❌ Command failed with exit code 1
⛔ Stopping execution due to error.
```

**What happens:**
- Execution stops at failed command
- Previous commands remain executed
- Following commands are skipped

**Solutions:**
- Fix the failing command
- Test command individually first
- Check if files/paths exist

### Variables Not Substituting
```
nl> greet john
# Output shows: Hello, {name}!
```

**Cause:** Macro name doesn't match pattern with variable

**Solution:**
- Check macro name format: `macros show greet {name}`
- Ensure exact spacing matches
- Variable names are case-sensitive

## Keyboard Shortcuts

- `Ctrl+C` - Cancel current input (doesn't exit)
- `Ctrl+D` or `EOF` - Exit the CLI
- Type `exit`, `quit`, or `q` - Exit the CLI
- Type `help` or `?` - Show help

## Data Storage

Macros are stored in: `data/macros.json`

You can:
- ✓ Back up this file
- ✓ Share it with others
- ✓ Edit it manually (valid JSON)
- ✓ Version control it

**Example macros.json:**
```json
{
  "hello world": {
    "description": "Simple hello world test",
    "steps": [
      {"type": "cmd", "value": "echo Hello, World!"}
    ]
  }
}
```

## Exit the CLI

Type any of these:
```
nl> exit
nl> quit
nl> q
```

Or press `Ctrl+D`

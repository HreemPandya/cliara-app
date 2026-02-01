# Cliara - Quick Start Guide

## 🚀 Phase 1 is COMPLETE!

You now have a fully functional shell wrapper with macros!

---

## Installation

```bash
# Already installed! If not:
pip install -e .
```

---

## Run Cliara

```bash
# Start Cliara shell (after installation)
cliara

# Alternative (if cliara command not found):
python -m cliara.main
```

---

## First Time Setup

When you first run Cliara, you'll see:

```
============================================================
  Welcome to Cliara!
  Let's get you set up...
============================================================

Detected OS: Windows
Detected shell: C:\Windows\System32\PowerShell\v1.0\powershell.exe

Use these settings? (y/n): y

============================================================
  Quick Start Guide
============================================================

1. Normal commands work as usual:
   cliara ❯ ls -la
   cliara ❯ cd projects

2. Use ? for natural language (Phase 2):
   cliara ❯ ? kill whatever is using port 3000

3. Create and run macros:
   cliara ❯ macro add mycommand
   cliara ❯ macro run mycommand

4. Save your last command as a macro:
   cliara ❯ macro save last as quickfix

Type 'help' anytime for more info!
============================================================
```

---

## Try These Commands

### 1. Normal Commands (Pass-Through)

```bash
cliara:proj ❯ echo "Hello Cliara!"
cliara:proj ❯ dir
cliara:proj ❯ python --version
```

### 2. Create Your First Macro

```bash
cliara:proj ❯ macro add test-macro

Creating macro 'test-macro'
Enter commands (one per line, empty line to finish):
  > echo Step 1
  > echo Step 2
  > echo Done!
  > 
Description (optional): A test macro

[OK] Macro 'test-macro' created with 3 command(s)
```

### 3. Run Your Macro

```bash
cliara:proj ❯ test-macro

[Macro] test-macro
A test macro

Commands:
  1. echo Step 1
  2. echo Step 2
  3. echo Done!

Run? (y/n): y

============================================================
EXECUTING: test-macro
============================================================

[1/3] echo Step 1
------------------------------------------------------------
Step 1

[2/3] echo Step 2
------------------------------------------------------------
Step 2

[3/3] echo Done!
------------------------------------------------------------
Done!

============================================================
[OK] Macro 'test-macro' completed successfully
============================================================
```

### 4. List Macros

```bash
cliara:proj ❯ macro list

[Macros] 1 total

  • test-macro
    A test macro (3 commands)
    Run 1 time
```

### 5. Save Last Command

```bash
cliara:proj ❯ echo "This is a test command"
This is a test command

cliara:proj ❯ macro save last as echo-test

Saving last execution as 'echo-test':
  1. echo "This is a test command"

Save these commands? (y/n): y
Description (optional): Test echo
[OK] Macro 'echo-test' saved!
```

### 6. Try Natural Language (Stub)

```bash
cliara:proj ❯ ? kill process on port 3000

[NL Query] kill process on port 3000

[Phase 2 Feature - Coming Soon!]
This will use LLM to convert your query into commands.

For now, you can:
  • Use normal commands
  • Create macros with 'macro add <name>'
  • Run macros by typing their name
```

### 7. Get Help

```bash
cliara:proj ❯ help

[Cliara Help]

Normal Commands:
  Just type any command - it passes through to your shell
  Examples: ls, cd, git status, npm install

Natural Language (Phase 2 - Coming Soon):
  ? <query>  - Use natural language
  Example: ? kill process on port 3000

Macros:
  macro add <name>    - Create a macro
  macro list          - List all macros
  macro help          - Show macro commands
  <macro-name>        - Run a macro

Other:
  help                - Show this help
  exit                - Quit Cliara
```

---

## Common Workflows

### Development Setup

```bash
macro add dev-setup
  > cd ~/projects/myapp
  > npm install
  > npm run dev
  > 

# Use it
dev-setup
```

### Git Shortcuts

```bash
macro add gs
  > git status -s
  > 

macro add gp
  > git pull
  > 

macro add push-all
  > git add .
  > git commit -m "updates"
  > git push
  > 
```

### Docker Management

```bash
macro add docker-restart
  > docker-compose down
  > docker-compose up -d
  > 

macro add docker-logs
  > docker-compose logs -f
  > 
```

---

## Testing Safety Features

```bash
cliara:proj ❯ macro add dangerous-test

Creating macro 'dangerous-test'
Enter commands (one per line, empty line to finish):
  > echo Testing
  > rm -rf temp/
  > 

[!!] DANGEROUS
These commands could cause data loss or system instability.

Commands:
  * rm -rf temp/

Save anyway? (yes/no): no
[Cancelled]
```

---

## Configuration

Your config is at: `~/.cliara/config.json`

```json
{
  "shell": "C:\\Windows\\System32\\PowerShell\\v1.0\\powershell.exe",
  "os": "Windows",
  "nl_prefix": "?",
  "macro_storage": "~/.cliara/macros.json",
  "history_size": 1000,
  "safety_checks": true,
  "first_run_complete": true
}
```

---

## Exiting Cliara

```bash
cliara:proj ❯ exit
Goodbye!
```

---

## What's Working (Phase 1) ✅

1. ✅ Shell wrapper - wraps your real shell
2. ✅ Pass-through - normal commands work
3. ✅ Macro system - create, list, show, run, delete
4. ✅ Save last - capture last command as macro
5. ✅ Safety checks - detect dangerous commands
6. ✅ Config system - `~/.cliara/config.json`
7. ✅ First-run setup - auto-detect shell
8. ✅ `?` prefix routing (stubbed for Phase 2)

---

## What's Coming (Phase 2) 🚧

1. 🚧 LLM integration (OpenAI/Anthropic)
2. 🚧 NL → commands conversion
3. 🚧 NL macro creation
4. 🚧 Context-aware suggestions
5. 🚧 Rich TUI with syntax highlighting

---

## Troubleshooting

### Cliara command not found

```bash
# Use Python module as fallback:
python -m cliara.main

# Or add Scripts to PATH (Windows)
```

### Config issues

```bash
# Reset config:
rm ~/.cliara/config.json

# Start Cliara again for fresh setup
```

### Macros not saving

```bash
# Check permissions:
ls -la ~/.cliara/

# Manually create if needed:
mkdir -p ~/.cliara
echo "{}" > ~/.cliara/macros.json
```

---

## Next Steps

1. **Create your workflow macros** - automate your daily commands
2. **Try the safety features** - see how dangerous commands are handled
3. **Explore macro management** - list, show, delete, organize
4. **Wait for Phase 2** - LLM integration coming soon!

---

## Feedback

Phase 1 is complete! Try it out and let me know:
- What works well?
- What needs improvement?
- What features do you want in Phase 2?

---

**Enjoy Cliara!** 🎉

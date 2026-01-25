# Cliara - AI-Powered Shell

**An intelligent shell wrapper that understands natural language and macros.**

Cliara wraps your existing shell (bash, zsh, PowerShell, cmd) and adds:
- 🗣️ **Natural language commands** with `?` prefix (Phase 2)
- 📦 **Powerful macro system** for command automation
- 🛡️ **Safety checks** for dangerous operations
- 💾 **Save last command** as macro instantly
- 🚀 **Pass-through mode** - normal commands work as usual

---

## Quick Start

### Installation

```bash
# Install with pip
pip install -e .

# Or with pipx (recommended)
pipx install .

# Start Cliara
cliara
```

### First Run

Cliara will detect your system and shell automatically:

```
============================================================
  Welcome to Cliara!
  Let's get you set up...
============================================================

Detected OS: Windows
Detected shell: C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe

Use these settings? (y/n): y
```

---

## Usage

### Normal Commands (Pass-Through)

Just type commands as usual - they go straight to your shell:

```bash
cliara:proj ❯ ls -la
cliara:proj ❯ cd myproject
cliara:proj ❯ git status
cliara:proj ❯ npm install
```

### Natural Language (Phase 2 - Coming Soon)

Use `?` prefix for natural language:

```bash
cliara:proj ❯ ? kill whatever is using port 3000
cliara:proj ❯ ? clean node_modules and restart dev server
cliara:proj ❯ ? show me the 10 largest files in this directory
```

### Macros

#### Create a Macro

```bash
cliara:proj ❯ macro add reset-backend
Macro name: reset-backend
Enter commands (one per line, empty line to finish):
  > docker-compose down
  > docker-compose up -d
  > npm run dev
  > 
Description (optional): Reset backend services

[OK] Macro 'reset-backend' created with 3 command(s)
```

#### Run a Macro

```bash
# Method 1: Just type the name
cliara:proj ❯ reset-backend

# Method 2: Use macro run
cliara:proj ❯ macro run reset-backend
```

#### Save Last Command as Macro

```bash
cliara:proj ❯ docker-compose logs -f backend
# ... output ...
^C

cliara:proj ❯ macro save last as backend-logs
Saving last execution as 'backend-logs':
  1. docker-compose logs -f backend

Save these commands? (y/n): y
[OK] Macro 'backend-logs' saved!
```

#### List Macros

```bash
cliara:proj ❯ macro list

[Macros] 3 total

  • reset-backend
    Reset backend services (3 commands)
    Run 5 times
  • backend-logs
    No description (1 command)
  • quick-test
    Run tests quickly (2 commands)
    Run 12 times
```

#### Show Macro Details

```bash
cliara:proj ❯ macro show reset-backend

[Macro] reset-backend
Description: Reset backend services
Commands (3):
  1. docker-compose down
  2. docker-compose up -d
  3. npm run dev

Created: 2026-01-24T10:30:00
Run count: 5
Last run: 2026-01-24T15:45:12
```

---

## Safety Features

Cliara checks all commands for dangerous operations:

### Safety Levels

- **Safe** - Normal commands
- **Caution** - Might have side effects (sudo, git push --force)
- **Dangerous** - Could cause data loss (rm -rf, kill -9)
- **Critical** - Could destroy system (rm -rf /, mkfs, dd)

### Confirmation Required

```bash
cliara:proj ❯ macro add cleanup

Enter commands:
  > rm -rf temp/
  > 

[!!] DANGEROUS
These commands could cause data loss or system instability.

Commands:
  * rm -rf temp/

Save anyway? (yes/no): yes
```

When running:

```bash
cliara:proj ❯ cleanup

[Macro] cleanup
Commands:
  1. rm -rf temp/

[!!] DANGEROUS
These commands could cause data loss or system instability.

Commands:
  * rm -rf temp/

Type 'RUN' to execute: RUN
```

---

## Configuration

Configuration is stored in `~/.cliara/config.json`:

```json
{
  "shell": "/bin/bash",
  "os": "Linux",
  "nl_prefix": "?",
  "macro_storage": "~/.cliara/macros.json",
  "history_size": 1000,
  "safety_checks": true,
  "first_run_complete": true
}
```

---

## Command Reference

### Macro Commands

| Command | Description |
|---------|-------------|
| `macro add <name>` | Create a new macro interactively |
| `macro list` | List all macros |
| `macro show <name>` | Show macro details |
| `macro run <name>` | Run a macro (or just type name) |
| `macro delete <name>` | Delete a macro |
| `macro save last as <name>` | Save last commands as macro |
| `macro help` | Show macro help |

### Shell Commands

| Command | Description |
|---------|-------------|
| `<any command>` | Pass through to underlying shell |
| `? <query>` | Natural language query (Phase 2) |
| `help` | Show help |
| `exit` / `quit` | Exit Cliara |

---

## Examples

### Development Workflow

```bash
# Create macro for morning routine
macro add morning
  > cd ~/projects/myapp
  > git pull
  > npm install
  > code .
  > 

# Use it
morning
```

### Git Workflows

```bash
# Quick status
macro add gs
  > git status -s
  > 

# Commit and push
macro add push-changes
  > git add .
  > git commit -m "updates"
  > git push
  > 
```

### Docker Management

```bash
# Restart everything
macro add restart-all
  > docker-compose down
  > docker-compose up -d
  > 

# View logs
macro add logs
  > docker-compose logs -f
  > 
```

---

## Architecture

Cliara is a **shell wrapper**, not a replacement:

1. **Your real shell** (bash/zsh/PowerShell) runs as subprocess
2. **Normal commands** pass straight through
3. **Special commands** (`?`, `macro`) are intercepted
4. **Macros** expand to multiple commands, then execute

This means:
- ✅ All your aliases work
- ✅ Your PATH is respected
- ✅ Environment variables persist
- ✅ cd, export, etc. work normally

---

## Phase 2 (Coming Soon)

- 🤖 **LLM Integration** - OpenAI/Anthropic for NL→commands
- 🎨 **NL Macro Creation** - Describe macros in natural language
- 🔍 **Smart Suggestions** - Context-aware command suggestions
- 📊 **Better TUI** - Rich terminal interface with syntax highlighting

---

## Development

### Project Structure

```
nlp-termimal-proj/
├── cliara/
│   ├── __init__.py
│   ├── main.py          # Entry point
│   ├── shell.py         # Shell wrapper
│   ├── config.py        # Configuration
│   ├── macros.py        # Macro management
│   ├── safety.py        # Safety checks
│   └── nl_handler.py    # NL processing (Phase 2)
├── pyproject.toml       # Package config
├── requirements.txt     # Dependencies
└── README.md
```

### Install for Development

```bash
pip install -e .
```

### Run Tests

```bash
# Phase 1 complete - Phase 2 tests coming
python -m pytest tests/
```

---

## Differences from Old NLM

| Feature | Old NLM | New Cliara |
|---------|---------|------------|
| Installation | Manual run | `pip install` + `cliara` command |
| Shell | Separate tool | Wraps your shell |
| Commands | Pass through | Yes! Normal commands work |
| Macro syntax | `remember: "x" -> y` | `macro add x` (interactive) |
| Save last | No | `macro save last as <name>` |
| NL prefix | No | `?` for natural language |
| First-run | No | Auto-setup on first run |
| Config | data/ folder | `~/.cliara/` |

---

## Migration from Old NLM

Your old macros in `data/macros.json` can be imported:

```python
# Convert old format to new format
python tools/migrate_macros.py
```

---

## Requirements

- Python 3.8+
- Windows, macOS, or Linux
- bash, zsh, PowerShell, or cmd

---

## License

MIT

---

## Version

**v0.2.0** - Phase 1 Complete ✅
- Shell wrapper working
- Macro system complete
- Safety checks enhanced
- Config system implemented
- First-run setup
- "Save last" feature

**Next:** Phase 2 - LLM Integration

---

**Made for productivity** | **Built with Python** | **Powered by AI (Phase 2)**

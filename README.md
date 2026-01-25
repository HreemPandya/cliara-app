# Cliara - AI-Powered Shell

An intelligent shell wrapper that understands natural language and macros.

## Quick Start

```bash
# Install
pip install -e .

# Run
python -m cliara.main
```

## Documentation

- **[Complete Guide](docs/README.md)** - Full documentation
- **[Quick Start](docs/QUICKSTART.md)** - Get started in 5 minutes

## What is Cliara?

Cliara wraps your existing shell and adds:
- 🗣️ Natural language commands with `?` prefix (Phase 2)
- 📦 Powerful macro system
- 🛡️ Safety checks for dangerous operations
- 💾 Save last command as macro instantly
- 🚀 Normal commands work unchanged

## Installation

```bash
pip install -e .
```

## Usage

```bash
# Start Cliara
python -m cliara.main

# Normal commands work
cliara:proj ❯ ls -la
cliara:proj ❯ git status

# Create a macro
cliara:proj ❯ macro add test
  > echo Step 1
  > echo Step 2
  > 

# Run it
cliara:proj ❯ test

# Save last command
cliara:proj ❯ echo "hello"
cliara:proj ❯ macro save last as hello
```

## Features

### Phase 1 ✅ (Complete)
- Shell wrapper with pass-through
- Interactive macro system
- Save last command as macro
- Multi-tier safety checks
- Auto-configuration

### Phase 2 🚧 (Coming)
- LLM integration (OpenAI/Anthropic)
- Natural language → commands
- Context-aware suggestions

## Requirements

- Python 3.8+
- Windows, macOS, or Linux

## Version

**v0.2.0** - Phase 1 Complete

## License

MIT

---

**See [docs/README.md](docs/README.md) for complete documentation.**

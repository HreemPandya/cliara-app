# Cliara - AI-Powered Shell

An intelligent shell wrapper that understands natural language and macros.

## Quick Start

```bash
# Install
pip install -e .

# Setup environment
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# Run
cliara
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
# 1. Clone the repo
git clone https://github.com/yourusername/cliara.git
cd cliara

# 2. Install dependencies
pip install -e .

# 3. Setup environment
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# 4. Run
cliara
```

## Usage

```bash
# Start Cliara
cliara

# Normal commands work
cliara:proj > ls -la
cliara:proj > git status

# Natural language (requires OPENAI_API_KEY)
cliara:proj > ? kill process on port 3000

# Create a macro
cliara:proj > macro add test
  > echo Step 1
  > echo Step 2
  > 

# Run it
cliara:proj > test

# Save last command
cliara:proj > echo "hello"
cliara:proj > macro save last as hello
```

## Features

### Phase 1 ✅ (Complete)
- Shell wrapper with pass-through
- Interactive macro system
- Save last command as macro
- Multi-tier safety checks
- Auto-configuration

### Phase 2 ✅ (Complete)
- LLM integration (OpenAI)
- Natural language → commands
- Context-aware suggestions

### Storage Backends
- **JSON** (default) - Simple file-based storage
- **PostgreSQL** - Scalable database backend for millions of macros
  - See [PostgreSQL Setup Guide](docs/POSTGRES_SETUP.md)

## Requirements

- Python 3.8+
- Windows, macOS, or Linux

### Windows Users
After installation, you may need to add Python Scripts to your PATH:
```
C:\Users\<YourName>\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\LocalCache\local-packages\Python312\Scripts
```
Or restart your computer for PATH changes to take effect.

## Version

**v0.2.0** - Phase 2 Complete
- ✅ Natural language support
- ✅ Windows compatibility fixes
- ✅ PostgreSQL backend support

## License

MIT

---

**See [docs/README.md](docs/README.md) for complete documentation.**

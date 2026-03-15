# Cliara

**An AI-powered shell that understands natural language and macros.**

[![PyPI version](https://badge.fury.io/py/cliara.svg)](https://pypi.org/project/cliara/)
[![Python 3.8+](https://img.shields.io/pypi/pyversions/cliara)](https://pypi.org/project/cliara/)

---

## Quick Start

```bash
# Install (recommended: pipx for automatic PATH setup)
pipx install cliara

# Or with pip
pip install cliara

# Run — no API key needed
cliara
```

On first run, Cliara opens your browser to sign in with GitHub. Once authenticated, you get 150 free AI queries per month — no credit card, no API keys.

```bash
cliara ~/my-project > ? list files in this directory
# → ls -la

cliara ~/my-project > ? kill the process on port 3000
# → Suggests the right command for your OS
```

---

## What is Cliara?

Cliara wraps your existing shell and adds:

| Feature | Description |
|---------|-------------|
| **Natural language** | `? <query>` — describe what you want, get shell commands |
| **Cliara Cloud** | Sign in with GitHub, 150 free queries/month, no API key |
| **Macros** | Create, edit, run reusable command sequences |
| **Semantic history** | `? find when I fixed the login` — search past commands by meaning |
| **Smart push** | `push` — auto-commit message, branch detection, one command |
| **Smart deploy** | `deploy` — auto-detect Vercel, Netlify, Docker, PyPI, and deploy |
| **Safety checks** | Destructive commands show a diff preview before running |
| **Fix failed commands** | `? fix` — AI suggests corrections after a command fails |

All your normal commands work unchanged. Cliara is a thin layer on top of your shell.

---

## Installation

### Option 1: pipx (recommended)

Best for CLI tools — installs in an isolated environment and adds to PATH automatically.

```bash
pip install pipx
pipx ensurepath   # Add pipx bin to PATH (restart terminal if needed)
pipx install cliara
```

### Option 2: pip

```bash
pip install cliara
```

If `cliara` isn't recognized, use:

```bash
python -m cliara.main
```

Or add Python's `Scripts` folder to your PATH.

### Option 3: From source (development)

```bash
git clone https://github.com/HreemPandya/cliara-app.git
cd cliara-app
pip install -e .
```

---

## First Run

1. **Start Cliara:** `cliara`
2. **First time?** A browser opens for GitHub login. Authorize once.
3. **Done.** Your token is saved to `~/.cliara/token.json` and loads automatically on every start.

### Alternative: Bring your own API key

Prefer Groq, Gemini, Ollama, or OpenAI? Run `setup-llm` inside Cliara to configure. Free options include [Groq](https://console.groq.com) and [Google AI Studio](https://aistudio.google.com).

---

## Usage

```bash
# Start the shell
cliara

# Natural language (prefix with ?)
? list files in this directory
? kill process on port 3000
? find when I ran the deploy
? fix                    # Fix the last failed command

# Macros
macro add build          # Create a macro
build                    # Run it
macro save last as test  # Save last command as macro

# Smart push (auto-commit message + branch)
push

# Smart deploy (auto-detect project type)
deploy

# Other
use                      # Show/switch AI provider
theme                    # Change color theme
help                     # Full command reference
```

---

## Requirements

- **Python 3.8+**
- **Windows, macOS, or Linux**

---

## Documentation

- [Complete Guide](docs/README.md) — Full documentation
- [Quick Start](docs/QUICKSTART.md) — Get started in 5 minutes
- [Cliara Cloud Deployment](docs/CLIARA_CLOUD_DEPLOYMENT.md) — Self-host the backend
- [PostgreSQL Setup](docs/POSTGRES_SETUP.md) — Scalable macro storage

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `cliara` not recognized | Use `python -m cliara.main` or install with `pipx install cliara` |
| Connection error | Check network/firewall; try `$env:CLIARA_GATEWAY_URL = "https://cliara-cloud-production.up.railway.app/v1"` |
| Want BYOK instead | Run `setup-llm` inside Cliara for Groq, Gemini, Ollama, OpenAI |

---

## License

MIT

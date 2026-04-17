# Cliara

**An AI-powered shell that understands natural language and macros.**

[![PyPI version](https://badge.fury.io/py/cliara.svg)](https://pypi.org/project/cliara/)
[![Python 3.8+](https://img.shields.io/pypi/pyversions/cliara)](https://pypi.org/project/cliara/)

---

## What is Cliara?

Cliara wraps your existing shell (bash, zsh, PowerShell, cmd) and adds:

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

### Option 3: From source (development)

```bash
git clone https://github.com/HreemPandya/cliara-app.git
cd cliara-app
pip install -e .
```

### Optional Dependencies

To enable PostgreSQL support, install with:

```bash
pip install cliara[postgres]
```

---

## First Run / Setup

1. **Start Cliara:** `cliara`
2. **First time?** A browser opens for GitHub login. Authorize once.
3. **Done.** Your token is saved to `~/.cliara/token.json` and loads automatically on every start.

### Authentication / Cloud Login Flow

- The OAuth login flow opens your browser to sign in with GitHub.
- Once authenticated, your token is stored at `~/.cliara/token.json`.

### Alternative: Bring Your Own API Key

Prefer Groq, Gemini, Ollama, or OpenAI? Run `setup-llm` inside Cliara to configure. Free options include [Groq](https://console.groq.com) and [Google AI Studio](https://aistudio.google.com).

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

### Natural Language Commands

Use `?` prefix for natural language:

```bash
cliara:proj ❯ ? list files in this directory
cliara:proj ❯ ? kill process on port 3000
cliara:proj ❯ ? find when I ran the deploy
cliara:proj ❯ ? fix                    # Fix the last failed command
```

### Macros

Short commands are the default (`macro …` works the same).

```bash
cliara:proj ❯ ma build                 # Create a macro (line-by-line commands)
cliara:proj ❯ mc                       # Create from English (suggested name + steps)
cliara:proj ❯ build                    # Run it — type the macro name
cliara:proj ❯ ms test                  # Save last run as macro named test
cliara:proj ❯ ml                       # List macros  (same as macro list)
```

### Smart Commands

```bash
cliara:proj ❯ push                     # Smart push (auto-commit message + branch)
cliara:proj ❯ deploy                   # Smart deploy (auto-detect project type)
```

### Help

```bash
cliara:proj ❯ help                     # Full command reference
```

---

## Database Setup and Migration

If using PostgreSQL as your backend, follow these steps:

1. **Install PostgreSQL** (see [PostgreSQL Setup Guide](docs/POSTGRES_SETUP.md)).
2. **Create Database and User:**

```bash
# Connect to PostgreSQL
psql postgres

# Create database
CREATE DATABASE cliara;

# Create user
CREATE USER cliara WITH PASSWORD 'your_password_here';

# Grant privileges
GRANT ALL PRIVILEGES ON DATABASE cliara TO cliara;

# Exit
\q
```

3. **Install Python Dependencies:**

```bash
pip install psycopg2-binary
```

4. **Configure Cliara:**

Edit `~/.cliara/config.json`:

```json
{
  "storage_backend": "postgres",
  "postgres": {
    "host": "localhost",
    "port": 5432,
    "database": "cliara",
    "user": "cliara"
  }
}
```

---

## Required Environment Variables

Copy `.env.example` to `.env` and fill in the values for your chosen provider. Only ONE LLM provider should be active at a time.

```plaintext
# ── Option A: OpenAI (cloud, requires API key) ────────────────────────────────
OPENAI_API_KEY=sk-proj-your-key-here

# ── Option B: Anthropic Claude (cloud, requires API key) ─────────────────────
ANTHROPIC_API_KEY=sk-ant-your-key-here

# ── Option C: Ollama (local, free, no key needed) ────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434
```

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
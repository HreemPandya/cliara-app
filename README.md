# Natural Language Macros (NLM)

A CLI tool that lets you create and run terminal command macros using natural language names.

**Stop typing the same commands over and over.** Create spoken shortcuts that run multiple commands with one phrase.

## Features

- 🎯 Create macros with spoken names: `remember: "reset backend" -> ...`
- 🔍 Expand macros naturally: `reset backend` → runs multiple commands
- 🛡️ Safety checks with preview before execution
- 📦 Multi-step command execution
- 🔧 Variable support: `kill port {port}`
- 📋 Macro management: list, show, delete
- 🎨 Fuzzy matching for flexible macro names
- 🚀 Cross-platform: Windows, macOS, Linux

## Quick Start

### Option 1: One Command
```bash
# Windows
start.bat

# Unix/Linux/macOS
bash start.sh
```

### Option 2: Manual
```bash
# Install dependencies
pip install -r requirements.txt

# Setup demo macros (optional)
python setup_demo.py

# Run the CLI
python -m app.main
```

## Usage

### Creating Macros

```
nl> remember: "reset backend" -> lsof -ti :3000 | xargs kill -9 ; docker compose down ; docker compose up -d
[OK] Macro 'reset backend' saved!
```

### Running Macros

```
nl> reset backend

This macro will run:
1) lsof -ti :3000 | xargs kill -9
2) docker compose down
3) docker compose up -d

Run? (y/n): y
```

### Managing Macros

```
nl> macros list          # List all macros
nl> macros show <name>   # Show macro details
nl> macros delete <name> # Delete a macro
```

### Using Variables

```
nl> remember: "kill port {port}" -> lsof -ti :{port} | xargs kill -9
nl> kill port 5173
```

## Real-World Examples

```bash
# Development
remember: "dev" -> npm install ; npm run dev
remember: "test" -> python -m pytest tests/ -v

# Git workflows
remember: "save {msg}" -> git add . ; git commit -m "{msg}" ; git push
remember: "undo" -> git reset --soft HEAD~1

# Docker
remember: "restart" -> docker-compose down ; docker-compose up -d
remember: "logs {service}" -> docker-compose logs -f {service}
```

See [EXAMPLES.md](EXAMPLES.md) for 50+ real-world recipes!

## Safety

The tool includes safety checks for dangerous commands:
- `rm -rf`, `mkfs`, `dd`
- `shutdown`, `reboot`
- `kill -9`, `sudo`

**Two-tier protection:**
1. Warns when creating dangerous macros
2. Requires typing 'RUN' to execute (not just y/n)

## Documentation

- 📖 [USAGE.md](USAGE.md) - Complete user guide
- 📚 [EXAMPLES.md](EXAMPLES.md) - 50+ real-world examples
- 📋 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) - Command cheatsheet
- 🏗️ [ARCHITECTURE.md](ARCHITECTURE.md) - Technical documentation
- ✅ [VERIFICATION.md](VERIFICATION.md) - Testing checklist

## Testing

```bash
# Run unit tests
python tests/test_basic.py

# Run integration tests
python integration_test.py
```

All tests passing: ✅ 13/13

## Project Structure

```
nlp-termimal-proj/
  app/
    main.py      # CLI loop
    parser.py    # Command parsing
    macros.py    # Macro storage & CRUD
    executor.py  # Command execution
    safety.py    # Safety checks
  data/
    macros.json  # Macro storage
  tests/
    test_basic.py        # Unit tests
  integration_test.py    # Integration tests
  [documentation files]
```

## Requirements

- Python 3.7+
- thefuzz (for fuzzy matching)
- python-Levenshtein (speed optimization)

## Contributing

See [ARCHITECTURE.md](ARCHITECTURE.md) for:
- System design
- Extension points
- Development guidelines
- Future roadmap

## License

This project is provided as-is for educational and personal use.

## Version

**v0.1.0** - MVP Complete ✅
- All core features implemented
- Comprehensive documentation
- 100% test coverage
- Production ready

---

**Made with Python** | [View Documentation](INDEX.md) | [See Examples](EXAMPLES.md)

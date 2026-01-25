# Changelog

All notable changes to Natural Language Macros will be documented in this file.

## [0.1.0] - 2026-01-24

### Initial Release - MVP Complete! 🎉

#### Added - Core Features
- Interactive CLI with `nl>` prompt
- Macro creation with `remember: "name" -> cmd1 ; cmd2` syntax
- Macro execution by typing macro name
- Multi-step command execution with semicolon separator
- Sequential execution with stop-on-error
- Command preview before execution
- User confirmation prompts (y/n for safe, 'RUN' for dangerous)
- JSON-based macro storage in `data/macros.json`

#### Added - Safety Features
- Dangerous command pattern detection
  - File deletion: `rm -rf`, `del /f`, `rd /s`
  - System commands: `shutdown`, `reboot`
  - Process killing: `kill -9`
  - Privilege escalation: `sudo`
  - Filesystem operations: `mkfs`, `format`, `dd`
- Two-tier confirmation for dangerous commands
- Warning messages showing matched dangerous patterns
- Preview always shown before execution

#### Added - Quality of Life
- Variable support with `{varname}` syntax
- Variable extraction from macro names
- Variable substitution in commands
- Fuzzy matching with 75% similarity threshold
- Macro management commands:
  - `macros list` - List all macros
  - `macros show <name>` - Show macro details
  - `macros delete <name>` - Delete a macro
  - `macros edit <name>` - Placeholder for future
- Help command with usage information
- Multiple exit options: `exit`, `quit`, `q`, Ctrl+D

#### Added - Documentation
- README.md - Project overview and quick start
- USAGE.md - Comprehensive user guide
- ARCHITECTURE.md - Technical documentation
- EXAMPLES.md - Real-world usage examples and recipes
- TODO.md - Feature checklist and roadmap
- PROJECT_SUMMARY.md - Project completion summary

#### Added - Testing
- Unit tests for all core components
- Integration tests for full workflow
- Test scripts for easy validation
- Demo macro setup script

#### Added - Utilities
- `setup_demo.py` - Create demo macros
- `start.bat` - Windows quick-start script
- `start.sh` - Unix/Linux/macOS quick-start script
- `integration_test.py` - Full workflow testing
- `examples.py` - Example macro definitions

#### Technical Details
- Python 3.7+ support
- Cross-platform compatibility (Windows, macOS, Linux)
- thefuzz library for fuzzy matching
- Subprocess-based command execution
- 5-minute timeout per command
- Output capture (stdout/stderr)
- ASCII-safe output for Windows console compatibility

#### Project Structure
```
nlp-termimal-proj/
├── app/                      # Main application
│   ├── __init__.py
│   ├── main.py              # CLI loop
│   ├── parser.py            # Command parsing
│   ├── macros.py            # Macro storage
│   ├── executor.py          # Command execution
│   └── safety.py            # Safety checks
├── data/
│   └── macros.json          # Macro database
├── tests/
│   └── test_basic.py        # Unit tests
└── [documentation files]
```

## Future Roadmap

### [0.2.0] - Enhanced UX (Planned)
- Rich terminal output with colors
- Progress bars for long-running commands
- Command history with arrow keys
- Tab completion for macro names
- Macro templates for common patterns

### [0.3.0] - Intelligence (Planned)
- LLM integration (OpenAI/Anthropic)
- Natural language to command conversion
- Smart suggestions based on usage
- Context-aware macros

### [0.4.0] - Collaboration (Planned)
- Import/export macros
- Macro marketplace/sharing
- Team macro repositories
- Version control for macros

### [1.0.0] - Production Ready (Planned)
- Comprehensive error handling
- Audit logging
- Configuration file support
- Plugin system
- GUI wrapper option

---

## Version History

- **v0.1.0** (2026-01-24) - Initial MVP release with all core features

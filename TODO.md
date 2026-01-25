# TODO Checklist

## MVP Features ✓
- [x] CLI loop with prompt
- [x] `remember:` syntax parsing
- [x] Save/load macros from JSON
- [x] Multi-step command execution (separated by `;`)
- [x] Preview before execution
- [x] Safety checks for dangerous commands
- [x] Confirmation prompts (normal and dangerous)

## Quality of Life Features ✓
- [x] Variable support: `{varname}` in macro names
- [x] Fuzzy matching for macro names
- [x] Management commands:
  - [x] `macros list` - List all macros
  - [x] `macros show <name>` - Show macro details
  - [x] `macros delete <name>` - Delete a macro
  - [x] `macros edit <name>` - Placeholder for editing

## Future Enhancements
- [ ] LLM integration for natural language → commands
- [ ] OS-specific command variants (Windows/macOS/Linux)
- [ ] Continue-on-error mode
- [ ] Macro export/import
- [ ] Command history
- [ ] Macro chaining (call macros from macros)
- [ ] Environment variable support
- [ ] Conditional execution
- [ ] Retry logic for failed commands
- [ ] Better error messages with suggestions

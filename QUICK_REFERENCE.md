# Natural Language Macros - Quick Reference Card

## 🚀 Quick Start

```bash
pip install -r requirements.txt
python setup_demo.py
python -m app.main
```

## 📝 Basic Syntax

### Create Macro
```
remember: "name" -> cmd1 ; cmd2 ; cmd3
```

### Run Macro
```
name
```

### Create with Variables
```
remember: "name {var}" -> cmd with {var}
```

### Run with Variables
```
name value
```

## 🎮 Commands

| Command | Description |
|---------|-------------|
| `remember: "name" -> cmds` | Create macro |
| `macro_name` | Run macro |
| `macros list` | List all macros |
| `macros show <name>` | Show macro details |
| `macros delete <name>` | Delete macro |
| `help` or `?` | Show help |
| `exit`, `quit`, `q` | Exit CLI |

## 💡 Examples

### Simple
```
remember: "hello" -> echo Hello, World!
```

### Multi-Step
```
remember: "update" -> git pull ; npm install ; npm run build
```

### With Variables
```
remember: "commit {msg}" -> git add . ; git commit -m "{msg}"
commit "Initial commit"
```

### Useful Macros

#### Development
```
remember: "dev" -> npm install ; npm run dev
remember: "test" -> python -m pytest tests/ -v
remember: "lint" -> black . ; flake8 .
```

#### Git
```
remember: "gs" -> git status -s
remember: "save {msg}" -> git add . ; git commit -m "{msg}" ; git push
remember: "undo" -> git reset --soft HEAD~1
```

#### Docker
```
remember: "up" -> docker-compose up -d
remember: "down" -> docker-compose down
remember: "logs {service}" -> docker-compose logs -f {service}
```

#### System (Windows)
```
remember: "ports" -> netstat -ano
remember: "procs" -> tasklist
remember: "disk" -> wmic logicaldisk get size,freespace,caption
```

#### System (Unix/Linux/macOS)
```
remember: "ports" -> lsof -nP -iTCP -sTCP:LISTEN
remember: "disk" -> df -h
remember: "mem" -> free -h
```

## 🔒 Safety

### Dangerous Patterns Detected
- `rm -rf` - File deletion
- `sudo` - Privilege escalation
- `kill -9` - Force kill
- `shutdown`, `reboot` - System control
- `mkfs`, `format`, `dd` - Filesystem ops

### Confirmations
- **Safe commands**: y/n
- **Dangerous commands**: type 'RUN'

## 🎯 Tips

1. **Test First**: Run commands individually before creating macro
2. **Clear Names**: Use descriptive, memorable names
3. **Use Variables**: Make macros flexible with `{var}`
4. **Group Commands**: Combine related steps
5. **Check Safety**: Review dangerous commands carefully

## 🐛 Troubleshooting

| Issue | Solution |
|-------|----------|
| Macro not found | Use `macros list` to check exact name |
| Command fails | Test command in regular terminal first |
| Variable not working | Check spelling and spacing |
| Import error | Run `pip install -r requirements.txt` |

## 📚 Full Documentation

- `README.md` - Project overview
- `USAGE.md` - Complete user guide
- `EXAMPLES.md` - Real-world examples
- `ARCHITECTURE.md` - Technical docs

## 🔗 Workflow

```
1. Create:    remember: "name" -> commands
2. Verify:    macros show name
3. Run:       name
4. Modify:    macros delete name → recreate
```

## ⚡ Power User Tips

### Fuzzy Matching
Type approximate name:
```
nl> helo wrld
Did you mean 'hello world'? (y/n): y
```

### Quick Commands
```
nl> macros list     # or just 'list macros'
nl> gs              # if you created "gs" macro
```

### Chain Operations
```
remember: "deploy" -> npm test ; npm run build ; npm run deploy
```

### Backup Macros
```bash
cp data/macros.json data/macros.backup.json
```

### Share Macros
```bash
# Export
cat data/macros.json > my-macros.json

# Import (merge manually)
code data/macros.json
```

## 🎨 Customization

### Macro Storage
File: `data/macros.json`
- Edit manually (valid JSON)
- Version control friendly
- Human readable

### Adding Features
See `ARCHITECTURE.md` for:
- Extension points
- LLM integration
- OS-specific commands
- Plugin system

---

**Print this card for quick reference!**

Made with ❤️ in Python | Version 0.1.0

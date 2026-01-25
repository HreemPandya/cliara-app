# Natural Language Macros - Examples & Recipes

This file contains real-world examples and recipes for using Natural Language Macros effectively.

## Table of Contents
- [Getting Started Examples](#getting-started-examples)
- [Development Workflows](#development-workflows)
- [Git Workflows](#git-workflows)
- [Docker & Containers](#docker--containers)
- [System Administration](#system-administration)
- [File Operations](#file-operations)
- [Network & Debugging](#network--debugging)
- [Windows-Specific](#windows-specific)
- [Cross-Platform](#cross-platform)

---

## Getting Started Examples

### Simple Hello World
```
nl> remember: "hello" -> echo Hello, Natural Language Macros!
nl> hello
```

### Multi-Step Example
```
nl> remember: "morning" -> echo Good morning! ; echo Starting work... ; echo Ready!
nl> morning
```

### With Variables
```
nl> remember: "say {message}" -> echo You said: {message}
nl> say "Hello World"
```

---

## Development Workflows

### Python Development
```bash
# Setup virtual environment
remember: "setup py" -> python -m venv venv ; pip install -r requirements.txt

# Run tests
remember: "test" -> python -m pytest tests/ -v

# Lint and format
remember: "lint" -> black . ; flake8 . ; mypy .

# Build and test
remember: "build" -> python setup.py build ; python -m pytest

# Install in dev mode
remember: "dev install" -> pip install -e .
```

### Node.js Development
```bash
# Install and start
remember: "start dev" -> npm install ; npm run dev

# Clean install
remember: "fresh install" -> rm -rf node_modules ; npm install

# Build for production
remember: "prod build" -> npm run build ; npm run test

# Lint and fix
remember: "fix" -> npm run lint -- --fix ; npm run format

# Run tests with coverage
remember: "coverage" -> npm test -- --coverage
```

### Full Stack Development
```bash
# Start all services
remember: "start all" -> cd backend ; npm run dev ; cd ../frontend ; npm run dev

# Install everything
remember: "install all" -> cd backend ; npm install ; cd ../frontend ; npm install

# Test everything
remember: "test all" -> cd backend ; npm test ; cd ../frontend ; npm test
```

---

## Git Workflows

### Quick Status
```bash
remember: "gs" -> git status -s
remember: "gl" -> git log --oneline -10
remember: "gb" -> git branch -a
```

### Commit Workflows
```bash
# Quick commit
remember: "commit {msg}" -> git add . ; git commit -m "{msg}"

# Save and push
remember: "save {msg}" -> git add . ; git commit -m "{msg}" ; git push

# Quick fix
remember: "fix {msg}" -> git add . ; git commit -m "fix: {msg}" ; git push
```

### Branch Management
```bash
# Create and checkout
remember: "branch {name}" -> git checkout -b {name}

# Switch branch
remember: "switch {name}" -> git checkout {name}

# Delete branch
remember: "delbranch {name}" -> git branch -d {name}

# Update from main
remember: "update" -> git checkout main ; git pull ; git checkout -
```

### Advanced Git
```bash
# Undo last commit (keep changes)
remember: "undo" -> git reset --soft HEAD~1

# Stash and pull
remember: "refresh" -> git stash ; git pull --rebase ; git stash pop

# Show changed files
remember: "changed" -> git diff --name-only

# Clean up
remember: "git clean" -> git fetch --prune ; git branch --merged | grep -v '*' | xargs git branch -d
```

---

## Docker & Containers

### Basic Docker
```bash
# List containers
remember: "dps" -> docker ps -a

# List images
remember: "dimages" -> docker images

# Clean up
remember: "dclean" -> docker system prune -a --volumes

# Stop all containers
remember: "dstop" -> docker stop $(docker ps -aq)
```

### Docker Compose
```bash
# Start services
remember: "up" -> docker-compose up -d

# Stop services
remember: "down" -> docker-compose down

# Restart services
remember: "restart" -> docker-compose down ; docker-compose up -d

# View logs
remember: "logs {service}" -> docker-compose logs -f {service}

# Rebuild and start
remember: "rebuild" -> docker-compose down ; docker-compose build ; docker-compose up -d
```

### Docker Development
```bash
# Full reset
remember: "docker reset" -> docker-compose down -v ; docker-compose up -d

# Enter container
remember: "shell {container}" -> docker exec -it {container} /bin/bash

# View container logs
remember: "dlogs {container}" -> docker logs -f {container}
```

---

## System Administration

### Windows System Commands
```bash
# Show network info
remember: "netinfo" -> ipconfig /all

# List processes
remember: "procs" -> tasklist

# Show disk space
remember: "disk" -> wmic logicaldisk get size,freespace,caption

# Show running services
remember: "services" -> net start

# System info
remember: "sysinfo" -> systeminfo | findstr /C:"OS" /C:"Memory"
```

### Unix/Linux System Commands
```bash
# Disk usage
remember: "disk" -> df -h

# Memory usage
remember: "mem" -> free -h

# Top processes
remember: "top10" -> ps aux --sort=-%mem | head -11

# System info
remember: "sysinfo" -> uname -a ; lsb_release -a

# Network info
remember: "netinfo" -> ip addr ; ip route
```

### Process Management
```bash
# Kill port (Windows)
remember: "kill port {port}" -> netstat -ano | findstr :{port}

# Kill port (Unix/Linux/macOS)
remember: "kill port {port}" -> lsof -ti :{port} | xargs kill -9

# Find process
remember: "findproc {name}" -> ps aux | grep {name}
```

---

## File Operations

### Basic File Operations
```bash
# Create directory and enter
remember: "mkcd {dir}" -> mkdir {dir} ; cd {dir}

# Backup file
remember: "backup {file}" -> cp {file} {file}.backup

# Find files
remember: "find {name}" -> find . -name "*{name}*"

# Count files
remember: "count" -> find . -type f | wc -l
```

### Cleanup Operations
```bash
# Clean Python cache
remember: "clean py" -> find . -type d -name __pycache__ -exec rm -rf {} +

# Clean node modules
remember: "clean node" -> find . -name node_modules -type d -exec rm -rf {} +

# Clean build artifacts
remember: "clean build" -> rm -rf build/ dist/ *.egg-info/

# Clean temp files
remember: "clean temp" -> rm -rf *.tmp *.log *.cache
```

---

## Network & Debugging

### Network Diagnostics
```bash
# Check connection
remember: "ping google" -> ping -n 4 google.com

# Show open ports
remember: "ports" -> netstat -ano

# Test port
remember: "testport {port}" -> telnet localhost {port}

# DNS lookup
remember: "dns {domain}" -> nslookup {domain}
```

### Application Debugging
```bash
# Check API health
remember: "health" -> curl http://localhost:3000/health

# Test endpoint
remember: "api {endpoint}" -> curl -X GET http://localhost:3000/api/{endpoint}

# View app logs
remember: "applogs" -> tail -f logs/app.log

# Check service status
remember: "status {service}" -> systemctl status {service}
```

---

## Windows-Specific

### Environment & Path
```bash
# Show PATH
remember: "path" -> echo %PATH%

# Show environment
remember: "env" -> set

# Set temp variable
remember: "setvar {name} {value}" -> set {name}={value}
```

### File System
```bash
# List directory
remember: "ll" -> dir /a

# Tree view
remember: "tree" -> tree /F /A

# Find file
remember: "where {file}" -> where /r . {file}

# File info
remember: "fileinfo {file}" -> dir {file} ; type {file}
```

### System Management
```bash
# Flush DNS
remember: "flushdns" -> ipconfig /flushdns

# Renew IP
remember: "renewip" -> ipconfig /release ; ipconfig /renew

# Show installed programs
remember: "programs" -> wmic product get name,version

# Event viewer errors
remember: "errors" -> wevtutil qe System /c:10 /rd:true /f:text /q:"*[System[(Level=2)]]"
```

---

## Cross-Platform

### Python Projects
```bash
# Create new project
remember: "new py {name}" -> mkdir {name} ; cd {name} ; python -m venv venv ; touch README.md

# Activate venv (Windows)
remember: "activate" -> .\\venv\\Scripts\\activate

# Activate venv (Unix)
remember: "activate" -> source venv/bin/activate

# Freeze requirements
remember: "freeze" -> pip freeze > requirements.txt
```

### Git Repositories
```bash
# Clone and setup
remember: "clone {url}" -> git clone {url} ; cd $(basename {url} .git)

# Init new repo
remember: "init repo {name}" -> mkdir {name} ; cd {name} ; git init ; touch README.md

# Quick push
remember: "push" -> git add . ; git commit -m "Update" ; git push
```

### Web Development
```bash
# Start local server (Python)
remember: "serve" -> python -m http.server 8000

# Start local server (Node)
remember: "serve" -> npx http-server -p 8000

# Open in browser
remember: "open localhost" -> start http://localhost:3000

# Check what's on port
remember: "port {port}" -> lsof -ti :{port}
```

---

## Advanced Examples

### Conditional Workflows
```bash
# Test then deploy
remember: "deploy" -> npm test ; npm run build ; npm run deploy

# Backup before update
remember: "safe update" -> git stash ; git pull ; git stash pop
```

### Database Operations
```bash
# Backup database
remember: "db backup" -> pg_dump mydb > backup.sql

# Restore database
remember: "db restore" -> psql mydb < backup.sql

# Reset database
remember: "db reset" -> dropdb mydb ; createdb mydb ; psql mydb < schema.sql
```

### Build & Deploy
```bash
# Full build pipeline
remember: "build all" -> npm run lint ; npm run test ; npm run build

# Deploy to staging
remember: "deploy stage" -> npm run build ; scp -r build/ user@staging:/var/www

# Deploy to production
remember: "deploy prod" -> npm run test ; npm run build ; npm run deploy:prod
```

---

## Tips for Creating Good Macros

### 1. Use Clear, Memorable Names
```
✓ Good: "start dev", "run tests", "deploy staging"
✗ Bad: "sd", "rt", "ds"
```

### 2. Group Related Commands
```
✓ Good: "morning setup" -> cd ~/projects ; git pull ; npm install
✗ Bad: Separate macros for each step
```

### 3. Use Variables for Flexibility
```
✓ Good: "commit {msg}" -> git add . ; git commit -m "{msg}"
✗ Bad: Multiple macros for different commit messages
```

### 4. Add Safety Checks
```
✓ Good: Test dangerous commands individually first
✗ Bad: Creating "rm -rf /" macros
```

### 5. Document Complex Macros
```
✓ Good: Use descriptive names that explain what the macro does
✗ Bad: Cryptic names that you'll forget
```

---

## Common Patterns

### The "Reset" Pattern
```bash
remember: "reset backend" -> kill port 3000 ; npm install ; npm run dev
remember: "reset db" -> docker-compose down ; docker-compose up -d db
```

### The "Status" Pattern
```bash
remember: "status" -> git status ; docker ps ; npm run lint
remember: "check" -> ping google.com ; curl localhost:3000/health
```

### The "Clean" Pattern
```bash
remember: "clean" -> rm -rf node_modules dist coverage
remember: "clean all" -> docker system prune -a ; npm cache clean --force
```

### The "Update" Pattern
```bash
remember: "update" -> git pull ; npm install ; npm run migrate
remember: "update deps" -> npm update ; pip install -U -r requirements.txt
```

---

## Troubleshooting Examples

### Macro Not Working
```
nl> macros show "problem macro"
# Check the commands are correct

nl> macros delete "problem macro"
# Delete and recreate if needed
```

### Command Fails
```
# Test command individually first
nl> remember: "test cmd" -> echo Testing
nl> test cmd
# If it works, add more steps
```

### Variable Issues
```
# Make sure variable names match
nl> remember: "greet {name}" -> echo Hello {name}
nl> greet World  # Correct spacing
```

---

## Next Steps

1. Start with simple macros
2. Test each command individually
3. Combine into multi-step macros
4. Use variables for flexibility
5. Share your best macros!

For more information, see:
- `USAGE.md` - Complete usage guide
- `README.md` - Quick start
- `ARCHITECTURE.md` - Technical details

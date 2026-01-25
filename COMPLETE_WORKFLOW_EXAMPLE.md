# Complete Workflow Example - Natural Language Macros

This document shows a **complete end-to-end workflow** of using Natural Language Macros from scratch.

---

## 🚀 Part 1: Installation & Setup

### Step 1: Install Dependencies
```bash
C:\Users\hreem\nlp-termimal-proj> pip install -r requirements.txt

Installing collected packages: thefuzz, python-Levenshtein
Successfully installed thefuzz-0.20.0 python-Levenshtein-0.21.0
```

### Step 2: Start the CLI
```bash
C:\Users\hreem\nlp-termimal-proj> python -m app.main

============================================================
  Natural Language Macros (NLM)
  Create and run terminal command shortcuts
============================================================

Commands:
  remember: "name" -> cmd1 ; cmd2  - Create a macro
  <macro name>                     - Run a macro
  macros list                      - List all macros
  macros show <name>               - Show macro details
  macros delete <name>             - Delete a macro
  help                             - Show this help
  exit / quit                      - Exit the program

nl>
```

---

## 💡 Part 2: Creating Your First Macro

### Example 1: Simple Single Command
```bash
nl> remember: "hello" -> echo Hello, Natural Language Macros!

[OK] Macro 'hello' saved!
  Description: Runs: echo Hello, Natural Language Macros!...
  Steps: 1
```

### Let's Run It!
```bash
nl> hello

This macro will run:
  1) echo Hello, Natural Language Macros!

Run? (y/n): y

============================================================
EXECUTING MACRO
============================================================

[1/1] Running: echo Hello, Natural Language Macros!
------------------------------------------------------------
Hello, Natural Language Macros!

[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

---

## 📦 Part 3: Multi-Step Macros

### Example 2: Creating a Multi-Step Macro
```bash
nl> remember: "show env" -> echo === Environment === ; echo User: %USERNAME% ; echo Directory: %CD% ; python --version

[OK] Macro 'show env' saved!
  Description: Runs 4 commands
  Steps: 4
```

### Run the Multi-Step Macro
```bash
nl> show env

This macro will run:
  1) echo === Environment ===
  2) echo User: %USERNAME%
  3) echo Directory: %CD%
  4) python --version

Run? (y/n): y

============================================================
EXECUTING MACRO
============================================================

[1/4] Running: echo === Environment ===
------------------------------------------------------------
=== Environment ===

[OK] Command completed successfully

[2/4] Running: echo User: %USERNAME%
------------------------------------------------------------
User: hreem

[OK] Command completed successfully

[3/4] Running: echo Directory: %CD%
------------------------------------------------------------
Directory: C:\Users\hreem\nlp-termimal-proj

[OK] Command completed successfully

[4/4] Running: python --version
------------------------------------------------------------
Python 3.12.0

[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

---

## 🔧 Part 4: Macros with Variables

### Example 3: Variable Macro
```bash
nl> remember: "greet {name}" -> echo Hello, {name}! ; echo Welcome to Natural Language Macros, {name}!

[OK] Macro 'greet {name}' saved!
  Description: Runs 2 commands
  Steps: 2
```

### Use the Variable Macro
```bash
nl> greet Alice

This macro will run:
  1) echo Hello, Alice!
  2) echo Welcome to Natural Language Macros, Alice!

Run? (y/n): y

============================================================
EXECUTING MACRO
============================================================

[1/2] Running: echo Hello, Alice!
------------------------------------------------------------
Hello, Alice!

[OK] Command completed successfully

[2/2] Running: echo Welcome to Natural Language Macros, Alice!
------------------------------------------------------------
Welcome to Natural Language Macros, Alice!

[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

### Try with a Different Value
```bash
nl> greet Bob

This macro will run:
  1) echo Hello, Bob!
  2) echo Welcome to Natural Language Macros, Bob!

Run? (y/n): y
...
Hello, Bob!
Welcome to Natural Language Macros, Bob!
```

---

## 📋 Part 5: Managing Macros

### List All Macros
```bash
nl> macros list

[Macros] Available Macros (3):
  * hello
    Runs: echo Hello, Natural Language Macros!... (1 step)
  * show env
    Runs 4 commands (4 steps)
  * greet {name}
    Runs 2 commands (2 steps)
```

### Show Macro Details
```bash
nl> macros show "greet {name}"

[Macro] greet {name}
Description: Runs 2 commands

Steps:
  1. echo Hello, {name}!
  2. echo Welcome to Natural Language Macros, {name}!
```

---

## 🎯 Part 6: Real-World Examples

### Example 4: Development Workflow
```bash
nl> remember: "check project" -> echo Checking project... ; dir ; git status -s ; echo Done!

[OK] Macro 'check project' saved!
  Description: Runs 4 commands
  Steps: 4

nl> check project

This macro will run:
  1) echo Checking project...
  2) dir
  3) git status -s
  4) echo Done!

Run? (y/n): y

============================================================
EXECUTING MACRO
============================================================

[1/4] Running: echo Checking project...
------------------------------------------------------------
Checking project...
[OK] Command completed successfully

[2/4] Running: dir
------------------------------------------------------------
 Volume in drive C has no label.
 Directory of C:\Users\hreem\nlp-termimal-proj

01/24/2026  10:30 AM    <DIR>          app
01/24/2026  10:30 AM    <DIR>          data
01/24/2026  10:30 AM    <DIR>          tests
01/24/2026  10:30 AM             5,432 README.md
               1 File(s)          5,432 bytes
[OK] Command completed successfully

[3/4] Running: git status -s
------------------------------------------------------------
 M data/macros.json
[OK] Command completed successfully

[4/4] Running: echo Done!
------------------------------------------------------------
Done!
[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

### Example 5: File Operations with Variables
```bash
nl> remember: "find {filename}" -> echo Searching for {filename}... ; dir /s /b *{filename}*

[OK] Macro 'find {filename}' saved!
  Description: Runs 2 commands
  Steps: 2

nl> find README

This macro will run:
  1) echo Searching for README...
  2) dir /s /b *README*

Run? (y/n): y
...
Searching for README...
C:\Users\hreem\nlp-termimal-proj\README.md
```

---

## 🎨 Part 7: Fuzzy Matching in Action

### Typo in Macro Name
```bash
nl> helo
Did you mean 'hello'? (y/n): y

This macro will run:
  1) echo Hello, Natural Language Macros!

Run? (y/n): y
...
Hello, Natural Language Macros!
```

### Another Fuzzy Match
```bash
nl> show envirnment
Did you mean 'show env'? (y/n): y
...
```

---

## 🛡️ Part 8: Safety Features

### Example 6: Creating a Dangerous Macro
```bash
nl> remember: "cleanup temp" -> echo Cleaning... ; rm -rf temp/ ; echo Done

[!] WARNING: This macro contains potentially DESTRUCTIVE commands:
   * rm -rf temp/

These commands could cause data loss or system instability.

Do you still want to save this macro? (yes/no): yes

[OK] Macro 'cleanup temp' saved!
  Description: Runs 3 commands
  Steps: 3
```

### Running a Dangerous Macro
```bash
nl> cleanup temp

This macro will run:
  1) echo Cleaning...
  2) rm -rf temp/
  3) echo Done

[!] WARNING: This macro contains potentially DESTRUCTIVE commands:
   * rm -rf temp/

These commands could cause data loss or system instability.

Type 'RUN' to execute anyway: RUN

============================================================
EXECUTING MACRO
============================================================

[1/3] Running: echo Cleaning...
------------------------------------------------------------
Cleaning...
[OK] Command completed successfully

[2/3] Running: rm -rf temp/
------------------------------------------------------------
[OK] Command completed successfully

[3/3] Running: echo Done
------------------------------------------------------------
Done
[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

---

## 🗑️ Part 9: Deleting Macros

### Delete a Macro
```bash
nl> macros delete hello

Delete macro 'hello'? (y/n): y
[OK] Macro 'hello' deleted.
```

### Verify It's Gone
```bash
nl> macros list

[Macros] Available Macros (5):
  * show env
    Runs 4 commands (4 steps)
  * greet {name}
    Runs 2 commands (2 steps)
  * check project
    Runs 4 commands (4 steps)
  * find {filename}
    Runs 2 commands (2 steps)
  * cleanup temp
    Runs 3 commands (3 steps)
```

---

## 💼 Part 10: Practical Use Cases

### Git Workflow Macros
```bash
# Quick status
nl> remember: "gs" -> git status -s
[OK] Macro 'gs' saved!

# Commit with message
nl> remember: "commit {msg}" -> git add . ; git commit -m "{msg}"
[OK] Macro 'commit {msg}' saved!

# Push changes
nl> remember: "push" -> git push origin main
[OK] Macro 'push {msg}' saved!

# Use them!
nl> gs
...
 M data/macros.json

nl> commit "Added new macros"
...
[main abc1234] Added new macros
 1 file changed, 10 insertions(+)

nl> push
...
Pushing to origin...
```

### Development Workflow
```bash
# Start development server
nl> remember: "dev" -> echo Starting dev server... ; npm run dev

# Run tests
nl> remember: "test" -> echo Running tests... ; python -m pytest tests/ -v

# Lint code
nl> remember: "lint" -> echo Linting... ; black . ; flake8 .

# Full build
nl> remember: "build" -> npm run lint ; npm run test ; npm run build
```

### Docker Macros
```bash
# Start containers
nl> remember: "up" -> docker-compose up -d

# Stop containers
nl> remember: "down" -> docker-compose down

# Restart everything
nl> remember: "restart" -> docker-compose down ; docker-compose up -d

# View logs
nl> remember: "logs {service}" -> docker-compose logs -f {service}

nl> logs backend
...
```

---

## 📊 Part 11: Complete Session Example

### A Full Day's Workflow
```bash
# Morning - Start work
nl> remember: "morning" -> cd C:\Users\hreem\projects ; git pull ; code .
nl> morning
...

# Check what's running
nl> remember: "ports" -> netstat -ano | findstr LISTENING
nl> ports
...

# Start services
nl> remember: "start all" -> docker-compose up -d ; npm run dev
nl> start all
...

# Make changes, then commit
nl> gs
 M src/app.js
 M README.md

nl> commit "Updated app logic"
...

nl> push
...

# Afternoon - Testing
nl> test
...
All tests passed!

# Build for production
nl> remember: "prod" -> npm run build ; npm run deploy
nl> prod
...

# End of day - Clean up
nl> remember: "shutdown" -> docker-compose down ; echo Done for the day!
nl> shutdown
...
Done for the day!

nl> exit

Goodbye!
```

---

## 🎯 Part 12: Advanced Patterns

### Chain of Macros
```bash
# Setup
nl> remember: "setup" -> npm install ; pip install -r requirements.txt

# Test everything
nl> remember: "test all" -> pytest ; npm test

# Deploy pipeline
nl> remember: "deploy" -> npm run lint ; npm run test ; npm run build ; npm run deploy
```

### Variables with Multiple Uses
```bash
nl> remember: "docker logs {service}" -> echo === Logs for {service} === ; docker-compose logs -f {service}

nl> remember: "create dir {name}" -> mkdir {name} ; cd {name} ; echo Created {name} directory

nl> remember: "backup {file}" -> echo Backing up {file}... ; cp {file} {file}.backup ; echo Backup created!
```

---

## 🎓 Part 13: Tips from This Workflow

### What We Learned:
1. ✅ **Start Simple** - Begin with single commands
2. ✅ **Build Up** - Add multi-step macros as needed
3. ✅ **Use Variables** - Make macros flexible
4. ✅ **Name Clearly** - Use memorable, descriptive names
5. ✅ **Test First** - Run commands individually before macro-izing
6. ✅ **Be Safe** - Review dangerous commands carefully
7. ✅ **Manage Actively** - List, show, and delete as needed

### Common Patterns:
- **Status checks**: `gs`, `ports`, `check project`
- **Development**: `dev`, `test`, `build`
- **Git workflows**: `commit {msg}`, `push`, `undo`
- **Docker**: `up`, `down`, `restart`, `logs {service}`
- **Utilities**: `find {file}`, `backup {file}`, `cleanup`

---

## 🚀 Part 14: Your Turn!

Now create macros for your workflow:

```bash
nl> macros list
# See what you have

nl> remember: "your macro" -> your commands
# Create something useful for you

nl> your macro
# Run it and save time!
```

---

## 📝 Summary of Complete Workflow

### What We Did:
1. ✅ Installed dependencies
2. ✅ Started the CLI
3. ✅ Created simple macros
4. ✅ Created multi-step macros
5. ✅ Used variables
6. ✅ Listed and managed macros
7. ✅ Tried fuzzy matching
8. ✅ Tested safety features
9. ✅ Deleted macros
10. ✅ Built real-world workflows
11. ✅ Created development macros
12. ✅ Used advanced patterns

### Time Saved:
- **Before**: Type 5-10 commands repeatedly
- **After**: Type 1 macro name
- **Result**: 10x faster workflow! ⚡

---

## 🎉 You're Now a Macro Master!

You've seen:
- ✅ Complete installation
- ✅ All feature types
- ✅ Real-world examples
- ✅ Best practices
- ✅ Safety in action
- ✅ Full workflow integration

**Start building your own macro library and supercharge your command line!** 🚀

---

**Need more examples?** See `EXAMPLES.md` for 50+ recipes!  
**Need help?** See `USAGE.md` for the complete guide!

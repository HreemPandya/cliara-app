# 🎯 QUICK START - 5 Minute Guide

**Get up and running in 5 minutes!**

---

## ⚡ Method 1: Fastest (30 seconds)

```bash
# Run this ONE command:
start.bat
```

That's it! The tool will:
1. Check dependencies
2. Install if needed
3. Load demo macros
4. Start the CLI

**Then try:**
```
nl> macros list
nl> hello world
```

---

## 🎮 Method 2: Interactive Demo (2 minutes)

```bash
# Run the guided demo:
demo.bat
```

This will walk you through:
- Installation
- Demo setup
- Basic usage

---

## 📚 Method 3: Learn by Doing (5 minutes)

### Step 1: Start (10 seconds)
```bash
python -m app.main
```

### Step 2: See what's available (5 seconds)
```
nl> macros list
```

### Step 3: Try one (10 seconds)
```
nl> hello world
y
```

### Step 4: Create your own (30 seconds)
```
nl> remember: "test" -> echo My first macro!
nl> test
y
```

### Step 5: Try variables (30 seconds)
```
nl> remember: "greet {name}" -> echo Hello, {name}!
nl> greet Alice
y
```

### Step 6: Create something useful (3 minutes)
```
nl> remember: "morning" -> echo Good morning! ; echo %date% %time%
nl> morning
y
```

**Done!** You're now using Natural Language Macros! 🎉

---

## 🎯 Your First 5 Macros

Copy-paste these to get started:

```bash
# 1. Quick directory listing
nl> remember: "ll" -> dir /w

# 2. Show current location
nl> remember: "whereami" -> echo %CD%

# 3. Git status (if you use git)
nl> remember: "gs" -> git status -s

# 4. Find files
nl> remember: "find {name}" -> dir /s /b *{name}*

# 5. Show time
nl> remember: "now" -> echo %date% %time%
```

**Test them:**
```
nl> ll
nl> whereami
nl> find README
nl> now
```

---

## 💡 What to Do Next

### Beginner (Today)
- Create 3-5 macros for commands you use daily
- Try using variables in at least one macro
- Run `macros list` to see your collection

### Intermediate (This Week)
- Read `EXAMPLES.md` for inspiration
- Create workflow macros (morning routine, deployment, etc.)
- Share your best macros with teammates

### Advanced (This Month)
- Read `ARCHITECTURE.md` to understand the system
- Create complex multi-step workflows
- Organize macros by project/context

---

## 🚀 Real-World Quick Start

Based on what you do:

### If You're a Python Developer:
```bash
remember: "test" -> python -m pytest tests/ -v
remember: "run" -> python main.py
remember: "install" -> pip install -r requirements.txt
```

### If You're a Node Developer:
```bash
remember: "dev" -> npm run dev
remember: "build" -> npm run build
remember: "install" -> npm install
```

### If You Use Git:
```bash
remember: "gs" -> git status -s
remember: "commit {msg}" -> git add . ; git commit -m "{msg}"
remember: "push" -> git push
```

### If You Use Docker:
```bash
remember: "up" -> docker-compose up -d
remember: "down" -> docker-compose down
remember: "logs {service}" -> docker-compose logs -f {service}
```

---

## 📖 Documentation Roadmap

**Read these in order:**

1. **This file** (5 min) - You are here! ✓
2. **QUICK_REFERENCE.md** (5 min) - Command cheatsheet
3. **LIVE_DEMO_GUIDE.md** (15 min) - Step-by-step walkthrough
4. **EXAMPLES.md** (30 min) - 50+ real-world recipes
5. **USAGE.md** (1 hour) - Complete user guide

---

## ⚡ TL;DR - The Absolute Fastest Start

```bash
# 1. Start it
python -m app.main

# 2. Create a macro
nl> remember: "hi" -> echo Hello!

# 3. Run it
nl> hi

# 4. Done! Start creating your own!
```

**Time: 1 minute** ⏱️

---

## 🎊 You're Ready!

You now know enough to:
- ✅ Start the tool
- ✅ Create macros
- ✅ Run macros
- ✅ Be more productive!

**Go save some time!** 🚀

---

**Need help?** Run `help` in the CLI or see **USAGE.md**  
**Want examples?** See **EXAMPLES.md** or **COMPLETE_WORKFLOW_EXAMPLE.md**  
**Want deep dive?** See **LIVE_DEMO_GUIDE.md**

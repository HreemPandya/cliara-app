# 🎬 LIVE DEMO - Step by Step Guide

Follow these steps to see Natural Language Macros in action!

---

## 🚀 **STEP 1: Launch the Tool**

### Option A: One-Click Start
```bash
# Windows
start.bat

# Or run the demo
demo.bat
```

### Option B: Manual Start
```bash
python -m app.main
```

**You Should See:**
```
============================================================
  Natural Language Macros (NLM)
  Create and run terminal command shortcuts
============================================================

Commands:
  remember: "name" -> cmd1 ; cmd2  - Create a macro
  <macro name>                     - Run a macro
  macros list                      - List all macros
  ...

nl> 
```

---

## 📋 **STEP 2: List Existing Macros**

**Type this:**
```
macros list
```

**You'll See:**
```
[Macros] Available Macros (5):
  * hello world
    Simple hello world test (1 step)
  * show info
    Display system information (4 steps)
  * repeat {count}
    Repeat a message (2 steps)
  * git status
    Show git status and branch (2 steps)
  * _example
    Example macro - delete this entry (1 step)
```

✅ **Success!** You can see all available macros.

---

## 👋 **STEP 3: Run Your First Macro**

**Type this:**
```
hello world
```

**You'll See:**
```
This macro will run:
  1) echo Hello, World!

Run? (y/n): 
```

**Type:** `y` and press Enter

**Output:**
```
============================================================
EXECUTING MACRO
============================================================

[1/1] Running: echo Hello, World!
------------------------------------------------------------
Hello, World!

[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

✅ **Success!** You just ran your first macro!

---

## 🔧 **STEP 4: Try a Variable Macro**

**Type this:**
```
repeat 10
```

**You'll See:**
```
This macro will run:
  1) echo Repeating 10 times...
  2) echo Count is: 10

Run? (y/n): 
```

**Type:** `y`

**Output:**
```
Repeating 10 times...
Count is: 10
```

✅ **Amazing!** The `{count}` variable was replaced with `10`.

**Try different values:**
```
repeat 3
repeat 100
repeat anything
```

---

## ✨ **STEP 5: Create Your Own Macro**

**Type this:**
```
remember: "test" -> echo This is my first macro!
```

**You'll See:**
```
[OK] Macro 'test' saved!
  Description: Runs: echo This is my first macro!...
  Steps: 1
```

**Now run it:**
```
test
```

**Output:**
```
This is my first macro!
```

✅ **You're a macro creator now!** 🎉

---

## 🎯 **STEP 6: Create a Multi-Step Macro**

**Type this:**
```
remember: "check" -> echo Step 1 ; echo Step 2 ; echo Step 3
```

**You'll See:**
```
[OK] Macro 'check' saved!
  Description: Runs 3 commands
  Steps: 3
```

**Run it:**
```
check
```

**You'll See:**
```
This macro will run:
  1) echo Step 1
  2) echo Step 2
  3) echo Step 3

Run? (y/n): y

============================================================
EXECUTING MACRO
============================================================

[1/3] Running: echo Step 1
------------------------------------------------------------
Step 1
[OK] Command completed successfully

[2/3] Running: echo Step 2
------------------------------------------------------------
Step 2
[OK] Command completed successfully

[3/3] Running: echo Step 3
------------------------------------------------------------
Step 3
[OK] Command completed successfully

============================================================
EXECUTION COMPLETE
============================================================

[OK] Macro completed successfully!
```

✅ **Perfect!** Multi-step macros work!

---

## 🎨 **STEP 7: Create a Variable Macro**

**Type this:**
```
remember: "say {message}" -> echo You said: {message}
```

**Try it:**
```
say Hello
say "Good morning"
say Testing123
```

**Each time you'll see:**
```
You said: [your message]
```

✅ **Variables make macros super flexible!**

---

## 📊 **STEP 8: View Macro Details**

**Type this:**
```
macros show "say {message}"
```

**You'll See:**
```
[Macro] say {message}
Description: Runs: echo You said: {message}...

Steps:
  1. echo You said: {message}
```

✅ **You can inspect any macro!**

---

## 🧪 **STEP 9: Test Fuzzy Matching**

**Type this (intentional typo):**
```
helo world
```

**You'll See:**
```
Did you mean 'hello world'? (y/n): 
```

**Type:** `y`

**It runs the correct macro!**

✅ **Fuzzy matching saves you from typos!**

---

## 🗑️ **STEP 10: Delete a Macro**

**Type this:**
```
macros delete test
```

**You'll See:**
```
Delete macro 'test'? (y/n): 
```

**Type:** `y`

**Output:**
```
[OK] Macro 'test' deleted.
```

**Verify it's gone:**
```
macros list
```

✅ **Macro management is easy!**

---

## 🌟 **STEP 11: Real-World Example**

Let's create a useful development macro:

**Type this:**
```
remember: "morning setup" -> echo Good morning! ; echo Checking project... ; dir ; echo Ready to code!
```

**Run it:**
```
morning setup
```

**You'll See:**
```
Good morning!
Checking project...
[directory listing]
Ready to code!
```

✅ **Now you have a morning routine macro!**

---

## 🎯 **STEP 12: Create Your Workflow**

Based on what you do regularly, create macros:

### For Git Users:
```
remember: "gs" -> git status -s
remember: "gp" -> git pull
remember: "commit {msg}" -> git add . ; git commit -m "{msg}"
```

### For Python Developers:
```
remember: "test" -> python -m pytest tests/
remember: "run" -> python main.py
remember: "lint" -> black . ; flake8 .
```

### For Node Developers:
```
remember: "dev" -> npm run dev
remember: "build" -> npm run build
remember: "deploy" -> npm run build ; npm run deploy
```

### For Docker Users:
```
remember: "up" -> docker-compose up -d
remember: "down" -> docker-compose down
remember: "restart" -> docker-compose down ; docker-compose up -d
```

---

## 🎓 **STEP 13: Practice Session**

Try creating these macros and running them:

1. **Info macro:**
   ```
   remember: "info" -> echo User: %USERNAME% ; echo Dir: %CD%
   info
   ```

2. **File finder:**
   ```
   remember: "find {name}" -> dir /s *{name}*
   find README
   ```

3. **Quick list:**
   ```
   remember: "ll" -> dir /w
   ll
   ```

4. **Timestamp:**
   ```
   remember: "now" -> echo %date% %time%
   now
   ```

---

## 🏆 **STEP 14: Challenge Yourself**

Create a macro that:
1. Shows a welcome message
2. Displays the current directory
3. Lists files
4. Shows a completion message

**Solution:**
```
remember: "startup" -> echo Welcome back! ; echo Current location: %CD% ; dir /b ; echo All set!
startup
```

---

## 🎉 **STEP 15: Explore More**

### Try These Commands:
```
macros list          # See all your macros
macros show [name]   # View details
help                 # Show help
```

### Learn More:
- **EXAMPLES.md** - 50+ real-world recipes
- **USAGE.md** - Complete user guide
- **QUICK_REFERENCE.md** - Command cheatsheet

---

## 🚪 **STEP 16: Exit**

**Type:**
```
exit
```

**You'll See:**
```
Goodbye!
```

✅ **Demo Complete!** 🎊

---

## 📝 **What You've Learned**

In this demo, you:
- ✅ Listed existing macros
- ✅ Ran simple macros
- ✅ Used variable macros
- ✅ Created your own macros
- ✅ Created multi-step macros
- ✅ Created variable macros
- ✅ Viewed macro details
- ✅ Tested fuzzy matching
- ✅ Deleted macros
- ✅ Created real-world macros

---

## 🚀 **Next Steps**

### 1. Create Your Daily Workflow
Think about commands you type repeatedly and make macros for them!

### 2. Explore Advanced Features
Check out **EXAMPLES.md** for inspiration.

### 3. Build Your Library
Over time, you'll build a personal library of time-saving macros.

### 4. Share Your Macros
Export your `data/macros.json` and share with teammates!

---

## 💡 **Pro Tips**

1. **Name macros clearly** - You'll remember them better
2. **Start simple** - Add complexity as needed
3. **Use variables** - Makes macros reusable
4. **Test commands first** - Run them manually before macro-izing
5. **Organize by workflow** - Group related macros

---

## 🎯 **Common First Macros**

Most people create these first:

```bash
# Navigation
remember: "proj" -> cd C:\Users\hreem\projects
remember: "home" -> cd ~

# Quick checks
remember: "ports" -> netstat -ano
remember: "procs" -> tasklist

# Git shortcuts
remember: "gs" -> git status -s
remember: "gl" -> git log --oneline -10

# Project commands
remember: "test" -> [your test command]
remember: "run" -> [your run command]
remember: "build" -> [your build command]
```

---

## 🎊 **Congratulations!**

You've completed the live demo! You now know how to:
- Create macros
- Run macros
- Use variables
- Manage your macro library
- Build real workflows

**Start automating your command line today!** ⚡

---

**Questions?** See **USAGE.md** for the complete guide!  
**Want more examples?** See **EXAMPLES.md** for 50+ recipes!  
**Ready to dive deep?** See **ARCHITECTURE.md** for technical details!

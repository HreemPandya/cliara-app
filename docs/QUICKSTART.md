# cliara quick start

Hands-on guide. No pitch here. Only run flow.

## 1) install

```bash
pip install cliara
```

If command not found:

```bash
python -m cliara.main
```

## 2) start shell

```bash
cliara
```

First run asks shell/OS confirm. Say yes if detected value good.

## 3) set AI mode (pick one)

### Option A: Cliara Cloud

```text
cliara ~/proj ❯ cliara login
```

Browser opens. GitHub OAuth flow. Token saved in `~/.cliara/token.json`.

### Option B: BYOK provider

```text
cliara ~/proj ❯ setup-llm
```

Then check status:

```text
cliara ~/proj ❯ status
```

## 4) understand prompt fast

```text
✓ cliara ~/proj ❯
X 1 cliara [my-session] ~/proj ❯
```

- `✓` = last shell command success.
- `X 1` = last shell command failed with exit code 1.
- `[my-session]` = task session active.

## 5) first commands to run

```text
cliara ~/proj ❯ help
cliara ~/proj ❯ doctor
cliara ~/proj ❯ ? kill whatever using port 3000
cliara ~/proj ❯ explain git rebase -i HEAD~3
cliara ~/proj ❯ lint find . -name "*.log" -delete
```

## 6) macro walkthrough (real flow)

Create macro from English:

```text
cliara ~/proj ❯ mc build app then run tests then open coverage
```

Create named macro manually:

```text
cliara ~/proj ❯ ma release-check
```

Add parameterized macro:

```text
cliara ~/proj ❯ ma deploy-prod --params env,tag
```

Run macro with inline values:

```text
cliara ~/proj ❯ deploy-prod env=prod tag=v1.2
```

Save last run as macro:

```text
cliara ~/proj ❯ ms quick-fix
```

List/search/show:

```text
cliara ~/proj ❯ ml
cliara ~/proj ❯ msr deploy
cliara ~/proj ❯ msh deploy-prod
```

Macro help:

```text
cliara ~/proj ❯ mh
```

## 7) push and deploy walkthrough

Smart push:

```text
cliara ~/proj ❯ push
```

Smart deploy:

```text
cliara ~/proj ❯ deploy
```

Inspect saved deploy config/history:

```text
cliara ~/proj ❯ deploy config
cliara ~/proj ❯ deploy history
```

Reset deploy detection:

```text
cliara ~/proj ❯ deploy reset
```

## 8) session walkthrough (task memory)

Start task session:

```text
cliara ~/proj ❯ ss auth-bug -- fix callback loop in prod
```

Add note while working:

```text
cliara ~/proj ❯ session note repro only on main branch
```

Resume later:

```text
cliara ~/proj ❯ session resume auth-bug
```

End with reflection prompts:

```text
cliara ~/proj ❯ se --reflect
```

Export session for chat tools:

```text
cliara ~/proj ❯ session snapshot --chat auth-bug
```

## 9) safety behavior

Cliara checks risky commands. Dangerous command asks stronger confirm.
Diff preview shows targets for destructive patterns when possible.

If AI-pasted command detected, Copilot Gate reviews before run.

## 10) config you will use most

Config file:

`~/.cliara/config.json`

Useful toggles:

- `theme`
- `nl_prefix`
- `safety_checks`
- `diff_preview`
- `copilot_gate`
- `semantic_history_enabled`

Change theme live:

```text
cliara ~/proj ❯ theme
cliara ~/proj ❯ theme dracula
```

Set config value live:

```text
cliara ~/proj ❯ config set semantic_history_enabled false
```

## 11) non-interactive CLI mode

Run one command through Cliara gate and exit:

```bash
cliara -c "rm -rf dist"
```

Get command suggestions only (no execute):

```bash
cliara ask list git branches
cliara nl undo last commit
```

## 12) quick troubleshoot

`cliara` not found:

```bash
python -m cliara.main
```

LLM features unavailable:

```text
cliara ~/proj ❯ status
cliara ~/proj ❯ cliara login
cliara ~/proj ❯ setup-llm
```

General health check:

```text
cliara ~/proj ❯ doctor
```

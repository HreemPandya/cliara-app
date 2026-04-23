# cliara

<p align="center">
	<strong>AI shell assistant for real terminal work.</strong><br/>
	Natural language, macros, smart git/deploy helpers, and safer execution flow.
</p>

<p align="center">
	<a href="https://pypi.org/project/cliara/"><img src="https://badge.fury.io/py/cliara.svg" alt="PyPI version"></a>
	<a href="https://pypi.org/project/cliara/"><img src="https://img.shields.io/pypi/pyversions/cliara" alt="Python 3.8+"></a>
	<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

<p align="center">
	<a href="#why-cliara">Why</a> •
	<a href="#before--after">Before/After</a> •
	<a href="#install">Install</a> •
	<a href="#quick-start-60-seconds">Quick Start</a> •
	<a href="#core-workflows">Workflows</a> •
	<a href="#safety-by-default">Safety</a> •
	<a href="#docs">Docs</a>
</p>

---

Cliara wraps your existing shell (bash, zsh, PowerShell, cmd).

- Normal commands still run as-is.
- `?` turns intent into shell commands.
- Macros turn repeated command chains into reusable commands.
- `push`, `deploy`, and `? fix` speed up common dev loops.

No new terminal to learn. You keep your shell habits and get an AI layer on top.

## Why Cliara

Most AI terminal tools are great in demos and weak in daily workflows.
Cliara is built for repetitive, error-prone, real development loops.

| Daily pain | Cliara flow | What backs it up |
|---|---|---|
| Too many command lookups | Ask with `?` in plain English | Natural-language command mode |
| Repeating the same multi-step tasks | Save and re-run as macros | Macro aliases (`ma`, `mc`, `ms`, `ml`) |
| Failed command context switching | Run `? fix` in-place | Error-aware fix flow |
| Messy release command chains | Use `push` and `deploy` helpers | Built-in smart commands |
| Risky destructive commands | Confirmation and previews | Safety checks + diff preview |

## Before / After

| Task | Plain shell approach | Cliara approach |
|---|---|---|
| Free port 3000 | Find process, inspect, then kill | `? kill whatever is using port 3000` |
| Recover from failed command | Copy error, search docs/issues, retry | `? fix` |
| Re-run release routine | Find old notes or shell history | Save macro once, run by name |
| Push + deploy flow | Multiple manual git/platform steps | `push` then `deploy` |

The point is not replacing shell skills. The point is reducing repetitive glue work.

## Install

```bash
pip install cliara
```

## Run

```bash
cliara
```

First launch opens GitHub sign-in once for cloud features. After that, run `cliara` normally.

## Quick Start (60 seconds)

```text
cliara ~/projects/myapp ❯ ? kill whatever is using port 3000
✓ cliara ~/projects/myapp ❯ ma deploy-prod
✓ cliara [deploy-prod] ~/projects/myapp ❯ push
✓ cliara [deploy-prod] ~/projects/myapp ❯ deploy
X 1 cliara [deploy-prod] ~/projects/myapp ❯ ? fix
cliara [deploy-prod] ~/projects/myapp ❯ help
```

## Core Workflows

### 1) Natural language to commands

```text
cliara ~/myapp ❯ ? show largest files in this folder
cliara ~/myapp ❯ ? find when I last changed docker config
cliara ~/myapp ❯ ? explain this command output
```

### 2) Macros for repeated routines

```text
cliara ~/myapp ❯ ma release-check
cliara ~/myapp ❯ mc
cliara ~/myapp ❯ release-check
cliara ~/myapp ❯ ml
```

### 3) Built-in helpers for shipping

```text
cliara ~/myapp ❯ push
cliara ~/myapp ❯ deploy
cliara ~/myapp ❯ ? fix
```

## Safety By Default

Cliara is designed to help you move fast without blindly executing dangerous commands.

- Risky commands trigger stronger confirmation flow.
- Potentially destructive operations can show a diff preview before execution.
- You can still inspect and control what gets run.

## Who It Fits

- Developers who live in terminal all day
- Teams with repeated setup/release/debug routines
- People who want shell speed without command memorization overhead

## Docs

- [Quick Start](docs/QUICKSTART.md)
- [Full Docs](docs/README.md)
- [Architecture](docs/ARCHITECTURE.md)

## Troubleshooting

If `cliara` is not recognized:

```bash
python -m cliara.main
```

## License

MIT
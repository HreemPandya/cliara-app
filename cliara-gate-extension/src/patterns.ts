/**
 * Pattern-based command explanation templates and danger classification.
 * Ported from the Python reference in copilot_gate.py and safety.py.
 */

// ---------------------------------------------------------------------------
// Danger levels
// ---------------------------------------------------------------------------

export enum DangerLevel {
  SAFE = "safe",
  CAUTION = "caution",
  DANGEROUS = "dangerous",
  CRITICAL = "critical",
}

export const DANGER_LEVEL_ORDER: DangerLevel[] = [
  DangerLevel.SAFE,
  DangerLevel.CAUTION,
  DangerLevel.DANGEROUS,
  DangerLevel.CRITICAL,
];

export function dangerIndex(level: DangerLevel): number {
  return DANGER_LEVEL_ORDER.indexOf(level);
}

// ---------------------------------------------------------------------------
// Danger patterns  (regex → severity)
// ---------------------------------------------------------------------------

export const DANGER_PATTERNS: Record<DangerLevel, RegExp[]> = {
  [DangerLevel.CRITICAL]: [
    /\brm\s+-rf\s+\//i,
    /\bmkfs\b/i,
    /\bdd\b.*\bif=\/dev\//i,
    />\s*\/dev\/sd/i,
  ],
  [DangerLevel.DANGEROUS]: [
    /\brm\s+-rf\b/i,
    /\brm\s+.*\s+-rf\b/i,
    /\bshutdown\b/i,
    /\breboot\b/i,
    /\bkill\s+-9\b/i,
    /\bformat\b/i,
    /\bdel\s+\/[fs]\b/i,
    /\brd\s+\/s\b/i,
    /\bchmod\s+777\b/i,
    /\bchown\s+.*root/i,
    /\bgit\s+filter-branch\b/i,
    /\bterraform\s+destroy\b/i,
    /\bfind\s+.*-exec\s+rm\b/i,
    /\bxargs\s+rm\b/i,
    /\bgit\s+clean\s+-fd/i,
  ],
  [DangerLevel.CAUTION]: [
    /\bsudo\b/i,
    /\bmv\s+.*\s+\/dev\/null/i,
    /\bnpm\s+install\s+-g/i,
    /\bpip\s+install.*--force/i,
    /\bgit\s+push\s+.*--force/i,
    /\bgit\s+reset\s+--hard/i,
    /\bnpm\s+publish\b/i,
    /\bcargo\s+publish\b/i,
    /\bdocker\s+push\b/i,
    /\bfly\s+deploy\b/i,
    /\bterraform\s+apply\b/i,
    /\bgit\s+rebase\b/i,
    /\bcat\s+.*\.env\b/i,
    /\bprintenv\b.*\|.*\bcurl\b/i,
    /\bcurl\b.*(-d|--data)\s+@/i,
    /\bscp\b/i,
    /\bgit\s+push\s+.*--no-verify/i,
    /\bgit\s+commit\s+.*--no-verify/i,
    /\bdocker\s+system\s+prune\b/i,
  ],
  [DangerLevel.SAFE]: [],
};

// ---------------------------------------------------------------------------
// Explanation templates  (regex → template string, {0} = first capture group)
// ---------------------------------------------------------------------------

export interface ExplanationPattern {
  regex: RegExp;
  template: string;
}

export const EXPLANATION_PATTERNS: ExplanationPattern[] = [
  // ── git ──
  { regex: /^git\s+status$/i, template: "Shows working tree status" },
  { regex: /^git\s+status\s/i, template: "Shows working tree status" },
  { regex: /^git\s+diff$/i, template: "Shows unstaged changes" },
  { regex: /^git\s+diff\s+--cached/i, template: "Shows staged changes" },
  { regex: /^git\s+diff\s+--staged/i, template: "Shows staged changes" },
  { regex: /^git\s+diff\s/i, template: "Shows differences between revisions" },
  { regex: /^git\s+log\b/i, template: "Shows commit history" },
  { regex: /^git\s+show\b/i, template: "Shows commit details" },
  { regex: /^git\s+branch\b/i, template: "Lists or manages branches" },
  { regex: /^git\s+checkout\s+-b\s/i, template: "Creates and switches to a new branch" },
  { regex: /^git\s+checkout\b/i, template: "Switches branches or restores files" },
  { regex: /^git\s+switch\b/i, template: "Switches branches" },
  { regex: /^git\s+add\s+\.\s*$/i, template: "Stages all changes" },
  { regex: /^git\s+add\b/i, template: "Stages files for commit" },
  { regex: /^git\s+commit\b/i, template: "Creates a new commit" },
  { regex: /^git\s+push\s+.*--force/i, template: "Force-pushes, rewriting remote history" },
  { regex: /^git\s+push\s+.*-f\b/i, template: "Force-pushes, rewriting remote history" },
  { regex: /^git\s+push\b/i, template: "Pushes commits to remote" },
  { regex: /^git\s+pull\s+--rebase/i, template: "Pulls and rebases local commits" },
  { regex: /^git\s+pull\b/i, template: "Pulls and merges remote changes" },
  { regex: /^git\s+fetch\b/i, template: "Downloads remote refs without merging" },
  { regex: /^git\s+merge\b/i, template: "Merges another branch into current" },
  { regex: /^git\s+rebase\b/i, template: "Replays commits onto another base" },
  { regex: /^git\s+reset\s+--hard/i, template: "Hard-resets HEAD, discarding all changes" },
  { regex: /^git\s+reset\b/i, template: "Resets HEAD to a previous state" },
  { regex: /^git\s+stash\s+pop/i, template: "Applies and removes top stash entry" },
  { regex: /^git\s+stash\s+drop/i, template: "Deletes a stash entry" },
  { regex: /^git\s+stash\b/i, template: "Stashes working directory changes" },
  { regex: /^git\s+clean\s+-fd/i, template: "Removes untracked files and directories" },
  { regex: /^git\s+clean\b/i, template: "Removes untracked files" },
  { regex: /^git\s+clone\b/i, template: "Clones a repository" },
  { regex: /^git\s+remote\b/i, template: "Manages remote repositories" },
  { regex: /^git\s+tag\b/i, template: "Manages tags" },
  { regex: /^git\s+cherry-pick\b/i, template: "Applies a commit from another branch" },
  { regex: /^git\s+revert\b/i, template: "Reverts a commit by creating a new one" },
  { regex: /^git\s+filter-branch\b/i, template: "Rewrites entire branch history" },
  { regex: /^git\s+restore\b/i, template: "Restores working tree files" },
  { regex: /^git\s+init\b/i, template: "Initialises a new git repository" },

  // ── file operations ──
  { regex: /^rm\s+-rf\s+(\S+)/i, template: "Recursively deletes {0}" },
  { regex: /^rm\s+-r\s+(\S+)/i, template: "Recursively deletes {0}" },
  { regex: /^rm\s+(\S+)/i, template: "Deletes {0}" },
  { regex: /^del\s+/i, template: "Deletes files (Windows)" },
  { regex: /^rd\s+\/s/i, template: "Removes directory tree (Windows)" },
  { regex: /^rmdir\s+/i, template: "Removes directory" },
  { regex: /^mkdir\s+/i, template: "Creates directory" },
  { regex: /^touch\s+/i, template: "Creates or updates file timestamp" },
  { regex: /^cp\s+-r/i, template: "Recursively copies files" },
  { regex: /^cp\s+/i, template: "Copies files" },
  { regex: /^mv\s+/i, template: "Moves or renames files" },
  { regex: /^chmod\s+777\b/i, template: "Sets full permissions (world-writable)" },
  { regex: /^chmod\s+/i, template: "Changes file permissions" },
  { regex: /^chown\s+/i, template: "Changes file ownership" },
  { regex: /^ln\s+-s/i, template: "Creates a symbolic link" },
  { regex: /^ln\s+/i, template: "Creates a hard link" },

  // ── reading / searching ──
  { regex: /^cat\s+/i, template: "Displays file contents" },
  { regex: /^less\s+/i, template: "Pages through file contents" },
  { regex: /^head\s+/i, template: "Shows first lines of a file" },
  { regex: /^tail\s+-f/i, template: "Follows file output in real-time" },
  { regex: /^tail\s+/i, template: "Shows last lines of a file" },
  { regex: /^grep\s+/i, template: "Searches text by pattern" },
  { regex: /^rg\s+/i, template: "Searches text by pattern (ripgrep)" },
  { regex: /^find\s+/i, template: "Finds files by criteria" },
  { regex: /^fd\s+/i, template: "Finds files by name (fd)" },
  { regex: /^wc\s+/i, template: "Counts lines, words, or characters" },
  { regex: /^ls\b/i, template: "Lists directory contents" },
  { regex: /^dir\b/i, template: "Lists directory contents (Windows)" },
  { regex: /^pwd$/i, template: "Prints current directory" },
  { regex: /^tree\b/i, template: "Displays directory tree" },

  // ── package managers ──
  { regex: /^npm\s+install\s+-g\b/i, template: "Installs npm package globally" },
  { regex: /^npm\s+install\b/i, template: "Installs npm dependencies" },
  { regex: /^npm\s+run\s+(\S+)/i, template: "Runs npm script '{0}'" },
  { regex: /^npm\s+publish\b/i, template: "Publishes package to npm registry" },
  { regex: /^npm\s+test\b/i, template: "Runs project tests" },
  { regex: /^npm\s+start\b/i, template: "Starts the application" },
  { regex: /^npm\s+ci\b/i, template: "Clean-installs dependencies from lockfile" },
  { regex: /^npx\s+/i, template: "Runs an npm package binary" },
  { regex: /^yarn\s+add\b/i, template: "Adds a yarn dependency" },
  { regex: /^yarn\s+install\b/i, template: "Installs yarn dependencies" },
  { regex: /^yarn\b/i, template: "Runs a yarn command" },
  { regex: /^pnpm\s+/i, template: "Runs a pnpm command" },
  { regex: /^pip\s+install\s+/i, template: "Installs Python packages" },
  { regex: /^pip\s+uninstall\s+/i, template: "Uninstalls Python packages" },
  { regex: /^pip\s+freeze\b/i, template: "Lists installed Python packages" },
  { regex: /^pip\s+/i, template: "Runs pip package manager" },
  { regex: /^pipx\s+install\s+/i, template: "Installs a Python CLI tool in isolation" },
  { regex: /^poetry\s+/i, template: "Runs Poetry dependency manager" },
  { regex: /^cargo\s+build\b/i, template: "Builds a Rust project" },
  { regex: /^cargo\s+run\b/i, template: "Builds and runs a Rust project" },
  { regex: /^cargo\s+test\b/i, template: "Runs Rust tests" },
  { regex: /^cargo\s+publish\b/i, template: "Publishes a Rust crate" },
  { regex: /^cargo\s+/i, template: "Runs a Cargo command" },
  { regex: /^go\s+build\b/i, template: "Compiles Go packages" },
  { regex: /^go\s+run\b/i, template: "Compiles and runs Go program" },
  { regex: /^go\s+test\b/i, template: "Runs Go tests" },
  { regex: /^go\s+/i, template: "Runs a Go command" },

  // ── docker / containers ──
  { regex: /^docker\s+compose\s+up\b/i, template: "Starts containers via Compose" },
  { regex: /^docker\s+compose\s+down\b/i, template: "Stops and removes containers" },
  { regex: /^docker\s+compose\s+build\b/i, template: "Builds Compose service images" },
  { regex: /^docker\s+compose\s+/i, template: "Runs Docker Compose command" },
  { regex: /^docker\s+build\b/i, template: "Builds a Docker image" },
  { regex: /^docker\s+run\b/i, template: "Runs a container" },
  { regex: /^docker\s+push\b/i, template: "Pushes image to registry" },
  { regex: /^docker\s+pull\b/i, template: "Pulls image from registry" },
  { regex: /^docker\s+stop\b/i, template: "Stops running container(s)" },
  { regex: /^docker\s+rm\b/i, template: "Removes container(s)" },
  { regex: /^docker\s+rmi\b/i, template: "Removes image(s)" },
  { regex: /^docker\s+system\s+prune/i, template: "Removes unused Docker data" },
  { regex: /^docker\s+exec\b/i, template: "Executes command in a running container" },
  { regex: /^docker\s+ps\b/i, template: "Lists running containers" },
  { regex: /^docker\s+images\b/i, template: "Lists Docker images" },
  { regex: /^docker\s+/i, template: "Runs a Docker command" },

  // ── kubernetes ──
  { regex: /^kubectl\s+apply\b/i, template: "Applies Kubernetes manifests" },
  { regex: /^kubectl\s+delete\b/i, template: "Deletes Kubernetes resources" },
  { regex: /^kubectl\s+get\b/i, template: "Lists Kubernetes resources" },
  { regex: /^kubectl\s+/i, template: "Runs a kubectl command" },

  // ── system / admin ──
  { regex: /^sudo\s+(.+)/i, template: "Runs with elevated privileges: {0}" },
  { regex: /^kill\s+-9\s+/i, template: "Force-kills a process" },
  { regex: /^kill\s+/i, template: "Sends signal to a process" },
  { regex: /^pkill\s+/i, template: "Kills processes by name" },
  { regex: /^shutdown\b/i, template: "Shuts down the system" },
  { regex: /^reboot\b/i, template: "Reboots the system" },
  { regex: /^systemctl\s+/i, template: "Manages systemd services" },
  { regex: /^service\s+/i, template: "Manages system services" },

  // ── network ──
  { regex: /^curl\s+/i, template: "Makes an HTTP request" },
  { regex: /^wget\s+/i, template: "Downloads a file from the web" },
  { regex: /^ssh\s+/i, template: "Opens an SSH connection" },
  { regex: /^scp\s+/i, template: "Copies files over SSH" },
  { regex: /^rsync\s+/i, template: "Syncs files between locations" },
  { regex: /^ping\s+/i, template: "Pings a host" },
  { regex: /^nslookup\s+/i, template: "Queries DNS records" },
  { regex: /^dig\s+/i, template: "Queries DNS records" },

  // ── deploy / publish ──
  { regex: /^fly\s+deploy\b/i, template: "Deploys to Fly.io" },
  { regex: /^fly\s+/i, template: "Runs a Fly.io command" },
  { regex: /^vercel\s+/i, template: "Runs a Vercel command" },
  { regex: /^netlify\s+deploy\b/i, template: "Deploys to Netlify" },
  { regex: /^netlify\s+/i, template: "Runs a Netlify command" },
  { regex: /^heroku\s+/i, template: "Runs a Heroku command" },
  { regex: /^railway\s+/i, template: "Runs a Railway command" },
  { regex: /^serverless\s+deploy\b/i, template: "Deploys via Serverless Framework" },
  { regex: /^terraform\s+apply\b/i, template: "Applies Terraform changes" },
  { regex: /^terraform\s+destroy\b/i, template: "Destroys Terraform-managed infrastructure" },
  { regex: /^terraform\s+/i, template: "Runs a Terraform command" },

  // ── python ──
  { regex: /^python3?\s+-m\s+pytest\b/i, template: "Runs Python tests" },
  { regex: /^python3?\s+-m\s+(\S+)/i, template: "Runs Python module {0}" },
  { regex: /^python3?\s+(\S+\.py)/i, template: "Runs Python script {0}" },
  { regex: /^python3?\b/i, template: "Starts Python" },
  { regex: /^pytest\b/i, template: "Runs Python tests" },

  // ── misc ──
  { regex: /^echo\s+/i, template: "Prints text to stdout" },
  { regex: /^export\s+/i, template: "Sets an environment variable" },
  { regex: /^set\s+/i, template: "Sets a shell variable" },
  { regex: /^env\b/i, template: "Prints environment variables" },
  { regex: /^printenv\b/i, template: "Prints environment variables" },
  { regex: /^source\s+/i, template: "Sources a shell script" },
  { regex: /^\.\s+/i, template: "Sources a shell script" },
  { regex: /^make\s+(\S+)/i, template: "Runs make target '{0}'" },
  { regex: /^make$/i, template: "Runs the default make target" },
  { regex: /^cmake\s+/i, template: "Configures a CMake build" },
  { regex: /^xargs\s+/i, template: "Runs a command for each input line" },
  { regex: /^crontab\s+/i, template: "Edits scheduled tasks" },
  { regex: /^dd\s+/i, template: "Low-level block copy (disk utility)" },
  { regex: /^mkfs\b/i, template: "Formats a filesystem" },

  // ── Windows PowerShell ──
  { regex: /^Get-ChildItem\b/i, template: "Lists directory contents (PowerShell)" },
  { regex: /^Remove-Item\b/i, template: "Deletes files (PowerShell)" },
  { regex: /^Set-Location\b/i, template: "Changes directory (PowerShell)" },
  { regex: /^Invoke-WebRequest\b/i, template: "Makes an HTTP request (PowerShell)" },
  { regex: /^Start-Process\b/i, template: "Starts a process (PowerShell)" },
  { regex: /^Stop-Process\b/i, template: "Stops a process (PowerShell)" },
];

// ---------------------------------------------------------------------------
// Irreversibility indicators
// ---------------------------------------------------------------------------

export const IRREVERSIBLE_PATTERNS: RegExp[] = [
  /\brm\b/i,
  /\bdel\b/i,
  /\berase\b/i,
  /\brd\s+\/s/i,
  /\brmdir\b/i,
  /\bgit\s+push\s+.*--force/i,
  /\bgit\s+push\s+.*-f\b/i,
  /\bgit\s+reset\s+--hard/i,
  /\bgit\s+clean\b/i,
  /\bgit\s+filter-branch\b/i,
  /\bnpm\s+publish\b/i,
  /\bcargo\s+publish\b/i,
  /\bdocker\s+push\b/i,
  /\bfly\s+deploy\b/i,
  /\bvercel\s+--prod\b/i,
  /\bnetlify\s+deploy\s+--prod\b/i,
  /\bterraform\s+destroy\b/i,
  /\bheroku\s+apps:destroy\b/i,
  /\bmkfs\b/i,
  /\bdd\b/i,
];

// ---------------------------------------------------------------------------
// Global-scope (system-wide blast radius) patterns
// ---------------------------------------------------------------------------

export const GLOBAL_SCOPE_PATTERNS: RegExp[] = [
  /\bnpm\s+install\s+-g\b/i,
  /\bpip\s+install\b(?!.*--user)/i,
  /\bsudo\b/i,
  /\bsystemctl\b/i,
  /\bservice\b/i,
  /\bshutdown\b/i,
  /\breboot\b/i,
  /\bmkfs\b/i,
  /\bdd\b/i,
  /\bchmod\s+777\s+\//i,
];

// ---------------------------------------------------------------------------
// Protected branch names
// ---------------------------------------------------------------------------

export const PROTECTED_BRANCHES = new Set([
  "main", "master", "production", "prod", "release",
]);

// ---------------------------------------------------------------------------
// Interactive programs that should bypass gating
// ---------------------------------------------------------------------------

export const INTERACTIVE_PROGRAMS = new Set([
  "vim", "nvim", "vi", "nano", "emacs",
  "ssh", "telnet",
  "python", "python3", "node", "irb", "ghci", "lua", "erl",
  "less", "more", "top", "htop", "btop",
  "tmux", "screen",
  "psql", "mysql", "sqlite3", "mongosh",
  "ftp", "sftp",
]);

/**
 * Pseudoterminal implementation for the Cliara Gate terminal profile.
 * Buffers keystrokes, gates commands through the risk engine,
 * shows VS Code approval UI, then spawns the real command and
 * pipes output back to the terminal.
 */

import * as vscode from "vscode";
import { spawn, ChildProcess } from "child_process";
import * as path from "path";
import { RiskEngine, RiskAssessment } from "./riskEngine";
import { DangerLevel, INTERACTIVE_PROGRAMS } from "./patterns";

const IS_WINDOWS = process.platform === "win32";

export class CliaraTerminal implements vscode.Pseudoterminal {
  private writeEmitter = new vscode.EventEmitter<string>();
  private closeEmitter = new vscode.EventEmitter<number | void>();

  onDidWrite: vscode.Event<string> = this.writeEmitter.event;
  onDidClose: vscode.Event<number | void> = this.closeEmitter.event;

  private buffer = "";
  private cwd: string;
  private riskEngine: RiskEngine;
  private activeProcess: ChildProcess | null = null;
  private isRunning = false;
  private dimensions: { columns: number; rows: number } = { columns: 80, rows: 24 };

  constructor(startCwd?: string) {
    this.cwd = startCwd ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
    this.riskEngine = new RiskEngine(this.cwd);
  }

  // ── lifecycle ─────────────────────────────────────────────────────

  open(initialDimensions: vscode.TerminalDimensions | undefined): void {
    if (initialDimensions) {
      this.dimensions = initialDimensions;
    }
    this.writeLine("\x1b[1;36mCliara Gate\x1b[0m — command interception active");
    this.writeLine("Every command is explained and risky ones require approval.\r\n");
    this.showPrompt();
  }

  close(): void {
    this.killActive();
  }

  setDimensions(dimensions: vscode.TerminalDimensions): void {
    this.dimensions = dimensions;
  }

  // ── input handling ────────────────────────────────────────────────

  handleInput(data: string): void {
    // If a child process is running, forward raw input to it
    if (this.isRunning && this.activeProcess?.stdin?.writable) {
      this.activeProcess.stdin.write(data);
      return;
    }

    for (const ch of data) {
      switch (ch) {
        case "\r": // Enter
          this.writeEmitter.fire("\r\n");
          this.handleCommand(this.buffer.trim());
          this.buffer = "";
          break;

        case "\x7f": // Backspace
          if (this.buffer.length > 0) {
            this.buffer = this.buffer.slice(0, -1);
            this.writeEmitter.fire("\b \b");
          }
          break;

        case "\x03": // Ctrl+C
          if (this.isRunning) {
            this.killActive();
          } else {
            this.buffer = "";
            this.writeEmitter.fire("^C\r\n");
            this.showPrompt();
          }
          break;

        default:
          this.buffer += ch;
          this.writeEmitter.fire(ch);
          break;
      }
    }
  }

  // ── command processing ────────────────────────────────────────────

  private async handleCommand(command: string): Promise<void> {
    if (!command) {
      this.showPrompt();
      return;
    }

    // Handle `cd` specially so the working directory persists
    if (/^cd\s+/.test(command) || command === "cd") {
      this.handleCd(command);
      return;
    }

    // Handle `clear` / `cls`
    if (/^(clear|cls)$/i.test(command)) {
      this.writeEmitter.fire("\x1b[2J\x1b[H");
      this.showPrompt();
      return;
    }

    // Detect interactive programs — pass through without gating
    const baseBin = command.split(/\s+/)[0];
    if (this.isInteractive(baseBin, command)) {
      this.spawnCommand(command);
      return;
    }

    // Risk assessment
    const assessment = this.riskEngine.assess(command);
    const approved = await this.gateCommand(command, assessment);

    if (approved) {
      this.spawnCommand(command);
    } else {
      this.writeLine("\x1b[90mCancelled.\x1b[0m");
      this.showPrompt();
    }
  }

  // ── gating UI ─────────────────────────────────────────────────────

  private async gateCommand(command: string, ra: RiskAssessment): Promise<boolean> {
    const detailParts: string[] = [];
    if (ra.blastRadius !== "local") {
      detailParts.push(`Scope: ${ra.blastRadius}`);
    }
    detailParts.push(...ra.riskFactors);
    detailParts.push(...ra.contextWarnings);

    const detail = detailParts.length > 0 ? ` [${detailParts.join(" | ")}]` : "";
    const message = `${ra.explanation}${detail}`;

    switch (ra.dangerLevel) {
      case DangerLevel.SAFE:
        this.showStatusBarExplanation(ra.explanation);
        return true;

      case DangerLevel.CAUTION: {
        const pick = await vscode.window.showInformationMessage(
          `⚡ ${message}`,
          { detail: `Command: ${command}` },
          "Run",
          "Cancel",
        );
        return pick === "Run";
      }

      case DangerLevel.DANGEROUS: {
        const pick = await vscode.window.showWarningMessage(
          `⚠️ ${message}`,
          { detail: `Command: ${command}` },
          "Run",
          "Cancel",
        );
        return pick === "Run";
      }

      case DangerLevel.CRITICAL: {
        const pick = await vscode.window.showWarningMessage(
          `🛑 ${message}`,
          { modal: true, detail: `Command: ${command}\n\nThis action is potentially destructive and irreversible.` },
          "Run",
        );
        return pick === "Run";
      }
    }
  }

  private showStatusBarExplanation(explanation: string): void {
    const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 0);
    item.text = `$(shield) ${explanation}`;
    item.color = new vscode.ThemeColor("statusBar.foreground");
    item.show();
    setTimeout(() => item.dispose(), 3000);
  }

  // ── spawning ──────────────────────────────────────────────────────

  private spawnCommand(command: string): void {
    this.isRunning = true;

    const shell = IS_WINDOWS ? "powershell.exe" : "sh";
    const args = IS_WINDOWS ? ["-NoProfile", "-Command", command] : ["-c", command];

    const child = spawn(shell, args, {
      cwd: this.cwd,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.activeProcess = child;

    child.stdout?.on("data", (data: Buffer) => {
      this.writeRaw(data.toString("utf-8"));
    });

    child.stderr?.on("data", (data: Buffer) => {
      this.writeRaw(data.toString("utf-8"));
    });

    child.on("close", (code) => {
      this.isRunning = false;
      this.activeProcess = null;
      if (code !== null && code !== 0) {
        this.writeLine(`\x1b[90mProcess exited with code ${code}\x1b[0m`);
      }
      this.showPrompt();
    });

    child.on("error", (err) => {
      this.isRunning = false;
      this.activeProcess = null;
      this.writeLine(`\x1b[31mError: ${err.message}\x1b[0m`);
      this.showPrompt();
    });
  }

  // ── cd handling ───────────────────────────────────────────────────

  private handleCd(command: string): void {
    const target = command.replace(/^cd\s*/, "").trim() || (IS_WINDOWS ? process.env.USERPROFILE! : process.env.HOME!);
    try {
      const resolved = path.resolve(this.cwd, target);
      // Verify it exists by trying to read it
      const fs = require("fs");
      if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
        this.writeLine(`\x1b[31mcd: no such directory: ${target}\x1b[0m`);
        this.showPrompt();
        return;
      }
      this.cwd = resolved;
      this.riskEngine.setCwd(this.cwd);
    } catch {
      this.writeLine(`\x1b[31mcd: ${target}: No such file or directory\x1b[0m`);
    }
    this.showPrompt();
  }

  // ── interactive detection ─────────────────────────────────────────

  private isInteractive(bin: string, command: string): boolean {
    const base = path.basename(bin).replace(/\.exe$/i, "");
    if (INTERACTIVE_PROGRAMS.has(base)) {
      // python/node with a file argument are scripts, not interactive
      if ((base === "python" || base === "python3" || base === "node") && command.split(/\s+/).length > 1) {
        return false;
      }
      return true;
    }
    return false;
  }

  // ── output helpers ────────────────────────────────────────────────

  private showPrompt(): void {
    const short = this.shortenPath(this.cwd);
    this.writeEmitter.fire(`\x1b[1;35mcliara\x1b[0m:\x1b[1;34m${short}\x1b[0m > `);
  }

  private writeLine(text: string): void {
    this.writeEmitter.fire(text + "\r\n");
  }

  /** Write raw process output, normalising bare \n to \r\n for the terminal. */
  private writeRaw(text: string): void {
    const normalised = text.replace(/\r?\n/g, "\r\n");
    this.writeEmitter.fire(normalised);
  }

  private killActive(): void {
    if (this.activeProcess) {
      this.activeProcess.kill();
      this.activeProcess = null;
      this.isRunning = false;
      this.writeEmitter.fire("^C\r\n");
      this.showPrompt();
    }
  }

  private shortenPath(p: string): string {
    const home = IS_WINDOWS ? process.env.USERPROFILE : process.env.HOME;
    if (home && p.startsWith(home)) {
      return "~" + p.slice(home.length).replace(/\\/g, "/");
    }
    return p.replace(/\\/g, "/");
  }
}

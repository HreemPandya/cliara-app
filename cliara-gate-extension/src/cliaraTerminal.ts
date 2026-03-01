/**
 * Pseudoterminal implementation for the Cliara Gate terminal profile.
 * Buffers keystrokes, gates commands through the risk engine,
 * renders warnings inline in the terminal with y/n approval,
 * then spawns the real command and pipes output back.
 */

import * as vscode from "vscode";
import { spawn, ChildProcess } from "child_process";
import * as path from "path";
import * as fs from "fs";
import { RiskEngine, RiskAssessment } from "./riskEngine";
import { DangerLevel, INTERACTIVE_PROGRAMS } from "./patterns";

const IS_WINDOWS = process.platform === "win32";

const enum InputMode {
  COMMAND,
  CONFIRM,
}

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

  private inputMode: InputMode = InputMode.COMMAND;
  private pendingCommand = "";
  private confirmBuffer = "";

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
    if (this.isRunning && this.activeProcess?.stdin?.writable) {
      this.activeProcess.stdin.write(data);
      return;
    }

    if (this.inputMode === InputMode.CONFIRM) {
      this.handleConfirmInput(data);
      return;
    }

    for (const ch of data) {
      switch (ch) {
        case "\r":
          this.writeEmitter.fire("\r\n");
          this.handleCommand(this.buffer.trim());
          this.buffer = "";
          break;

        case "\x7f":
          if (this.buffer.length > 0) {
            this.buffer = this.buffer.slice(0, -1);
            this.writeEmitter.fire("\b \b");
          }
          break;

        case "\x03":
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

  // ── y/n confirmation input ────────────────────────────────────────

  private handleConfirmInput(data: string): void {
    for (const ch of data) {
      if (ch === "\x03") {
        this.writeEmitter.fire("^C\r\n");
        this.writeLine("\x1b[90mCancelled.\x1b[0m");
        this.inputMode = InputMode.COMMAND;
        this.pendingCommand = "";
        this.confirmBuffer = "";
        this.showPrompt();
        return;
      }

      if (ch === "\r") {
        this.writeEmitter.fire("\r\n");
        const answer = this.confirmBuffer.trim().toLowerCase();
        const cmd = this.pendingCommand;
        this.inputMode = InputMode.COMMAND;
        this.pendingCommand = "";
        this.confirmBuffer = "";

        if (answer === "y" || answer === "yes") {
          this.spawnCommand(cmd);
        } else {
          this.writeLine("\x1b[90mCancelled.\x1b[0m");
          this.showPrompt();
        }
        return;
      }

      if (ch === "\x7f") {
        if (this.confirmBuffer.length > 0) {
          this.confirmBuffer = this.confirmBuffer.slice(0, -1);
          this.writeEmitter.fire("\b \b");
        }
        continue;
      }

      this.confirmBuffer += ch;
      this.writeEmitter.fire(ch);
    }
  }

  // ── command processing ────────────────────────────────────────────

  private handleCommand(command: string): void {
    if (!command) {
      this.showPrompt();
      return;
    }

    if (/^cd\s+/.test(command) || command === "cd") {
      this.handleCd(command);
      return;
    }

    if (/^(clear|cls)$/i.test(command)) {
      this.writeEmitter.fire("\x1b[2J\x1b[H");
      this.showPrompt();
      return;
    }

    const baseBin = command.split(/\s+/)[0];
    if (this.isInteractive(baseBin, command)) {
      this.spawnCommand(command);
      return;
    }

    const ra = this.riskEngine.assess(command);
    this.gateCommand(command, ra);
  }

  // ── inline terminal gating ────────────────────────────────────────

  private gateCommand(command: string, ra: RiskAssessment): void {
    switch (ra.dangerLevel) {
      case DangerLevel.SAFE:
        this.renderSafe(ra);
        this.spawnCommand(command);
        break;

      case DangerLevel.CAUTION:
        this.renderCaution(ra);
        this.promptConfirm(command);
        break;

      case DangerLevel.DANGEROUS:
        this.renderDangerous(ra);
        this.promptConfirm(command);
        break;

      case DangerLevel.CRITICAL:
        this.renderCritical(ra);
        this.promptConfirm(command);
        break;
    }
  }

  // ── rendering tiers ───────────────────────────────────────────────

  private renderSafe(ra: RiskAssessment): void {
    this.writeLine(`  \x1b[90m↳ ${ra.explanation}\x1b[0m`);
  }

  private renderCaution(ra: RiskAssessment): void {
    this.writeLine(`  \x1b[33m⚡ CAUTION:\x1b[0m ${ra.explanation}`);
    this.renderDetails(ra, "33");
  }

  private renderDangerous(ra: RiskAssessment): void {
    this.writeLine(`  \x1b[31m⚠  DANGEROUS:\x1b[0m ${ra.explanation}`);
    this.renderDetails(ra, "31");
  }

  private renderCritical(ra: RiskAssessment): void {
    this.writeLine(`  \x1b[1;31m🛑 CRITICAL:\x1b[0m ${ra.explanation}`);
    this.renderDetails(ra, "1;31");
    this.writeLine(`  \x1b[1;31mThis action is potentially destructive and irreversible.\x1b[0m`);
  }

  private renderDetails(ra: RiskAssessment, colorCode: string): void {
    if (ra.blastRadius !== "local") {
      this.writeLine(`  \x1b[${colorCode}m│\x1b[0m Scope: ${ra.blastRadius}`);
    }
    for (const factor of ra.riskFactors) {
      this.writeLine(`  \x1b[${colorCode}m│\x1b[0m ${factor}`);
    }
    for (const warning of ra.contextWarnings) {
      this.writeLine(`  \x1b[${colorCode}m│\x1b[0m ${warning}`);
    }
  }

  private promptConfirm(command: string): void {
    this.inputMode = InputMode.CONFIRM;
    this.pendingCommand = command;
    this.confirmBuffer = "";
    this.writeEmitter.fire("  Proceed? (\x1b[1my\x1b[0m/\x1b[1mn\x1b[0m): ");
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

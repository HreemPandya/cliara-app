/**
 * Context-aware risk assessment engine.
 * Analyses commands for danger level, blast radius, reversibility,
 * and augments with live git repo context probes.
 */

import { execSync } from "child_process";
import {
  DangerLevel,
  DANGER_LEVEL_ORDER,
  DANGER_PATTERNS,
  EXPLANATION_PATTERNS,
  GLOBAL_SCOPE_PATTERNS,
  IRREVERSIBLE_PATTERNS,
  PROTECTED_BRANCHES,
  dangerIndex,
} from "./patterns";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RiskAssessment {
  dangerLevel: DangerLevel;
  explanation: string;
  riskFactors: string[];
  blastRadius: string;
  reversible: boolean;
  contextWarnings: string[];
}

interface RepoContext {
  branch: string;
  isDirty: boolean;
  unpushed: number;
  hasRemote: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escalate(current: DangerLevel, candidate: DangerLevel): DangerLevel {
  return dangerIndex(candidate) > dangerIndex(current) ? candidate : current;
}

function explainByPattern(command: string): string | null {
  const cmd = command.trim();
  for (const { regex, template } of EXPLANATION_PATTERNS) {
    const m = regex.exec(cmd);
    if (m) {
      try {
        return template.replace(/\{(\d+)\}/g, (_, idx) => m[parseInt(idx) + 1] ?? "");
      } catch {
        return template;
      }
    }
  }
  return null;
}

function genericExplanation(command: string): string {
  const base = command.trim().split(/\s+/)[0] || "unknown";
  return `Runs '${base}'`;
}

// ---------------------------------------------------------------------------
// RiskEngine
// ---------------------------------------------------------------------------

export class RiskEngine {
  private cwd: string;

  constructor(cwd?: string) {
    this.cwd = cwd ?? process.cwd();
  }

  setCwd(dir: string): void {
    this.cwd = dir;
  }

  // ── public ────────────────────────────────────────────────────────

  assess(command: string): RiskAssessment {
    const subCommands = this.splitCompound(command);

    let highestLevel = DangerLevel.SAFE;

    for (const sub of subCommands) {
      const lvl = this.checkDanger(sub);
      highestLevel = escalate(highestLevel, lvl);
    }

    const explanation = this.buildExplanation(subCommands);
    const reversible = this.checkReversible(command);
    const blastRadius = this.estimateBlastRadius(command);
    const riskFactors = this.collectRiskFactors(command);
    const contextWarnings: string[] = [];

    try {
      const ctx = this.gatherRepoContext();
      if (ctx) {
        const [warnings, amplified] = this.applyContextAmplifiers(command, highestLevel, ctx);
        contextWarnings.push(...warnings);
        highestLevel = escalate(highestLevel, amplified);
      }
    } catch {
      // not in a git repo – skip context probes
    }

    if (!reversible) {
      riskFactors.push("Irreversible");
    }

    return {
      dangerLevel: highestLevel,
      explanation,
      riskFactors,
      blastRadius,
      reversible,
      contextWarnings,
    };
  }

  // ── danger classification ─────────────────────────────────────────

  private checkDanger(command: string): DangerLevel {
    for (const level of [DangerLevel.CRITICAL, DangerLevel.DANGEROUS, DangerLevel.CAUTION]) {
      for (const pattern of DANGER_PATTERNS[level]) {
        if (pattern.test(command)) {
          return level;
        }
      }
    }
    return DangerLevel.SAFE;
  }

  // ── compound command splitting ────────────────────────────────────

  private splitCompound(command: string): string[] {
    return command
      .split(/\s*(?:&&|\|\|?|;)\s*/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  // ── explanation ───────────────────────────────────────────────────

  private buildExplanation(subCommands: string[]): string {
    if (subCommands.length === 1) {
      return explainByPattern(subCommands[0]) ?? genericExplanation(subCommands[0]);
    }
    return subCommands
      .map((sub) => explainByPattern(sub) ?? genericExplanation(sub))
      .join(" ; ");
  }

  // ── reversibility ─────────────────────────────────────────────────

  private checkReversible(command: string): boolean {
    return !IRREVERSIBLE_PATTERNS.some((p) => p.test(command));
  }

  // ── blast radius ──────────────────────────────────────────────────

  private estimateBlastRadius(command: string): string {
    for (const pat of GLOBAL_SCOPE_PATTERNS) {
      if (pat.test(command)) {
        return "system-wide";
      }
    }

    const tokens = command.trim().split(/\s+/);
    if (tokens.length >= 2) {
      const target = tokens[tokens.length - 1];
      if (["/", "~", "C:\\", "C:/"].includes(target)) {
        return "system-wide";
      }
      if (target.startsWith("/") || target.startsWith("~")) {
        return "home directory";
      }
      if (target === ".") {
        return "entire repo";
      }
    }

    if (/^(rm|del|erase)\b/i.test(command)) {
      const fileTargets = tokens.slice(1).filter((t) => !t.startsWith("-"));
      if (fileTargets.length > 0) {
        return `${fileTargets.length} file${fileTargets.length !== 1 ? "s" : ""}`;
      }
    }

    return "local";
  }

  // ── risk factors ──────────────────────────────────────────────────

  private collectRiskFactors(command: string): string[] {
    const factors: string[] = [];
    const cmd = command.toLowerCase();

    if (cmd.includes("--force") || / -f /.test(cmd) || cmd.endsWith(" -f")) {
      factors.push("Uses --force flag");
    }
    if (cmd.includes("--no-verify")) {
      factors.push("Skips verification hooks");
    }
    if (cmd.includes("--skip-hooks")) {
      factors.push("Skips hooks");
    }
    if (/(\.env\b|\bcredentials\b|\bsecrets?\b|\.pem\b|\.key\b)/i.test(command)) {
      factors.push("Touches sensitive files");
    }
    if (/\b(curl|wget)\b.*\b(POST|PUT|DELETE)\b/i.test(command)) {
      factors.push("Mutating HTTP request");
    }
    if (/\bcurl\b.*(-d|--data)\b/i.test(command)) {
      factors.push("Sends data via HTTP");
    }
    if (/\bnpm\s+publish\b/i.test(command)) {
      factors.push("Publishes to npm registry");
    }
    if (/\bdocker\s+push\b/i.test(command)) {
      factors.push("Pushes image to registry");
    }
    if (/\bterraform\s+(apply|destroy)\b/i.test(command)) {
      factors.push("Modifies cloud infrastructure");
    }

    return factors;
  }

  // ── repo context ──────────────────────────────────────────────────

  private git(args: string): string {
    try {
      const result = execSync(`git ${args}`, {
        cwd: this.cwd,
        timeout: 3000,
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
      });
      return result.trim();
    } catch {
      return "";
    }
  }

  private gatherRepoContext(): RepoContext | null {
    const branch = this.git("rev-parse --abbrev-ref HEAD");
    if (!branch) {
      return null;
    }

    const dirty = this.git("status --porcelain").length > 0;
    let unpushed = 0;
    try {
      unpushed = parseInt(this.git("rev-list @{u}..HEAD --count") || "0", 10);
    } catch {
      unpushed = 0;
    }
    const hasRemote = this.git("remote").length > 0;

    return { branch, isDirty: dirty, unpushed, hasRemote };
  }

  private applyContextAmplifiers(
    command: string,
    baseLevel: DangerLevel,
    ctx: RepoContext,
  ): [string[], DangerLevel] {
    const warnings: string[] = [];
    let level = baseLevel;

    const { branch, unpushed, isDirty } = ctx;

    if (PROTECTED_BRANCHES.has(branch)) {
      const isPush = /\bgit\s+push\b/i.test(command);
      const isForce = /--force|-f\b/i.test(command);
      const isRebase = /\bgit\s+rebase\b/i.test(command);
      const isDeploy = /\b(deploy|publish|terraform\s+apply)\b/i.test(command);

      if (isPush && isForce) {
        warnings.push(`Force-pushing to protected branch '${branch}'`);
        level = escalate(level, DangerLevel.CRITICAL);
      } else if (isPush) {
        warnings.push(`Pushing to protected branch '${branch}'`);
      } else if (isRebase) {
        warnings.push(`Rebasing on protected branch '${branch}'`);
        level = escalate(level, DangerLevel.DANGEROUS);
      } else if (isDeploy) {
        warnings.push(`Deploying from protected branch '${branch}'`);
      }
    }

    if (unpushed && /\bgit\s+reset\b/i.test(command)) {
      warnings.push(`${unpushed} unpushed commit(s) may be lost`);
      level = escalate(level, DangerLevel.DANGEROUS);
    }

    if (isDirty && /\bgit\s+(checkout|switch|reset|clean)\b/i.test(command)) {
      warnings.push("Uncommitted changes in working tree");
    }

    return [warnings, level];
  }
}

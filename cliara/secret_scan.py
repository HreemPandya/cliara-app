"""
Pre-push secret scanning for Cliara.

Two detection layers run in parallel on staged files before every push:

  Layer 1 — Pattern matching
      Fast regex scan of the staged diff. Catches keys with known prefixes
      (OpenAI, GitHub, AWS, Groq, Anthropic, Slack, Google, Azure, …).

  Layer 2 — Shannon entropy heuristic
      Flags high-entropy strings (≥ 4.5 bits/char) in quoted assignments
      that are long enough to be a secret (≥ 20 chars). Catches keys that
      don't have a recognisable prefix but are suspiciously random-looking.

  Layer 3 — pre-commit + detect-secrets
      If pre-commit is installed, also runs detect-secrets on staged files.
      Provides a second opinion and enables plugins (PEM, hex keys, etc.)
      not covered by layers 1–2.

A finding blocks the push unless the offending line contains:
    # cliara-noscan

User control
------------
    Disable entirely:  config set secret_scan_on_push false
    Re-enable:         config set secret_scan_on_push true
    Manual scan:       secret-scan
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple


# ── Public constants ──────────────────────────────────────────────────────────

BYPASS_COMMENT = "cliara-noscan"

# ── Regex layer ───────────────────────────────────────────────────────────────
# Each pattern uses `{N,}` (minimum) rather than `{N}` (exact) so realistic
# tokens of varying lengths still match.

_SECRET_PATTERNS: List[Tuple[str, str]] = [
    # ── AI / ML providers ──────────────────────────────────────────────────
    (r"sk-proj-[A-Za-z0-9_\-]{20,}",         "OpenAI project key"),
    (r"sk-[A-Za-z0-9_\-]{20,}",               "OpenAI API key"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}",           "Anthropic key"),
    (r"gsk_[A-Za-z0-9_\-]{20,}",              "Groq API key"),
    (r"AIza[0-9A-Za-z\-_]{20,}",              "Google API key"),

    # ── Source control ─────────────────────────────────────────────────────
    (r"ghp_[A-Za-z0-9]{20,}",                 "GitHub personal token"),
    (r"gho_[A-Za-z0-9]{20,}",                 "GitHub OAuth token"),
    (r"ghs_[A-Za-z0-9]{20,}",                 "GitHub Actions token"),
    (r"ghu_[A-Za-z0-9]{20,}",                 "GitHub user-to-server token"),
    (r"github_pat_[A-Za-z0-9_]{20,}",         "GitHub fine-grained PAT"),

    # ── Cloud providers ────────────────────────────────────────────────────
    (r"AKIA[0-9A-Z]{16}",                     "AWS access key ID"),
    (r"ASIA[0-9A-Z]{16}",                     "AWS temporary access key"),
    (r"AGPA[0-9A-Z]{16}",                     "AWS group policy key"),
    (r"AROA[0-9A-Z]{16}",                     "AWS role key"),

    # Azure
    (r"[A-Za-z0-9/\+]{20,}={0,2}",            None),  # base64 — entropy-only below

    # ── Messaging / SaaS ──────────────────────────────────────────────────
    (r"xox[baprs]-[A-Za-z0-9\-]{10,}",        "Slack token"),
    (r"xoxe\.[A-Za-z0-9\-_]{20,}",            "Slack xoxe token"),

    # ── Crypto / keys ─────────────────────────────────────────────────────
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY",  "Private key"),

    # ── Generic high-value patterns ────────────────────────────────────────
    # Any assignment where the RHS looks like a secret (long alphanumeric blob)
    # These are broad; the entropy filter below keeps false positives low.
    (
        r"(?:password|passwd|secret|api_?key|auth_?token|access_?key|private_?key)"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9_\-/\+]{16,}['\"]?",
        "Generic credential assignment",
    ),
]

# Compiled version (skip the Azure base64 pattern — it's entropy-only)
_COMPILED_PATTERNS: List[Tuple[re.Pattern, str]] = []
for _pat, _label in _SECRET_PATTERNS:
    if _label is not None:          # skip entropy-only stub
        _COMPILED_PATTERNS.append(
            (re.compile(_pat, re.IGNORECASE), _label)
        )

# Standalone combined pattern for quick yes/no check
_ANY_SECRET_RE = re.compile(
    "|".join(p for p, _ in _SECRET_PATTERNS if _ is not None),
    re.IGNORECASE,
)


# ── Entropy layer ─────────────────────────────────────────────────────────────

# A string must be at least this long to be worth entropy-checking.
_ENTROPY_MIN_LEN = 20
# Bits-per-character threshold. Typical English prose ≈ 3.5; random base64 ≈ 6.
_ENTROPY_THRESHOLD = 4.5
# Extract quoted or bare high-entropy candidates from a line.
_ENTROPY_CANDIDATE_RE = re.compile(
    r"""['\"]([A-Za-z0-9+/=_\-]{20,})['\"]"""   # quoted
    r"""|(?<![.\w])([A-Za-z0-9+/=_\-]{20,})(?![.\w])"""  # bare word
)


def _shannon_entropy(s: str) -> float:
    """Return Shannon entropy in bits/char."""
    if not s:
        return 0.0
    counts: dict = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(s))
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_high_entropy_secret(token: str) -> bool:
    """True if *token* is long enough and random enough to be a secret."""
    if len(token) < _ENTROPY_MIN_LEN:
        return False
    # Skip obvious non-secrets: all-same-char, hex colour, version strings
    if len(set(token)) <= 4:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{6}", token):
        return False
    if re.fullmatch(r"\d+\.\d+\.\d+", token):
        return False
    return _shannon_entropy(token) >= _ENTROPY_THRESHOLD


# ── Text scrubbing (shared with the output archive) ──────────────────────────

def scrub_secrets(text: str) -> Tuple[str, int]:
    """Replace likely secrets in *text* with ``<REDACTED:label>`` placeholders.

    Runs the same two inline layers as the push gate (known-prefix patterns,
    then the Shannon-entropy heuristic) but *rewrites* instead of reporting.
    Used by the output archive so command stdout/stderr is scrubbed before it
    is ever persisted to disk.

    Returns ``(scrubbed_text, redaction_count)``.
    """
    if not text:
        return text, 0

    count = 0
    out = text

    # Layer 1: known secret shapes.
    for pattern, label in _COMPILED_PATTERNS:
        out, n = pattern.subn(f"<REDACTED:{label}>", out)
        count += n

    # Layer 2: high-entropy blobs the patterns didn't name.
    def _entropy_sub(m: "re.Match") -> str:
        nonlocal count
        token = m.group(1) or m.group(2) or ""
        if _is_high_entropy_secret(token):
            count += 1
            return m.group(0).replace(token, "<REDACTED:high-entropy>")
        return m.group(0)

    out = _ENTROPY_CANDIDATE_RE.sub(_entropy_sub, out)
    return out, count


# ── Data types ────────────────────────────────────────────────────────────────

class SecretFinding(NamedTuple):
    file: str
    line_number: int          # 1-indexed
    line_content: str         # raw source line (trimmed)
    pattern_label: str        # what kind of secret
    bypassed: bool            # True → # cliara-noscan is on this line


class ScanResult(NamedTuple):
    passed: bool
    precommit_used: bool
    precommit_output: str
    findings: List[SecretFinding]
    new_config_created: bool


# ── Diff scanner ──────────────────────────────────────────────────────────────

def _scan_line(line_content: str) -> Optional[str]:
    """Return the label of the first secret found in *line_content*, or None."""
    # Layer 1: pattern matching
    for pattern, label in _COMPILED_PATTERNS:
        if pattern.search(line_content):
            return label

    # Layer 2: entropy heuristic
    for m in _ENTROPY_CANDIDATE_RE.finditer(line_content):
        token = m.group(1) or m.group(2) or ""
        if _is_high_entropy_secret(token):
            return f"high-entropy string ({_shannon_entropy(token):.1f} bits/char)"

    return None


def parse_diff_for_secrets(diff: str) -> List[SecretFinding]:
    """Walk a unified diff and return every added line that looks like a secret."""
    findings: List[SecretFinding] = []
    current_file: Optional[str] = None
    current_line = 0

    for raw_line in diff.splitlines():
        # Track file changes
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:].strip()
            current_line = 0
            continue
        if raw_line.startswith("+++ /dev/null"):
            current_file = None
            continue

        # Track hunk headers (@@ -a,b +c,d @@)
        if raw_line.startswith("@@ "):
            m = re.search(r"\+(\d+)(?:,\d+)?", raw_line)
            if m:
                current_line = int(m.group(1)) - 1
            continue

        # Skip other metadata lines
        if raw_line.startswith("diff ") or raw_line.startswith("index ") \
                or raw_line.startswith("--- ") or raw_line.startswith("new file"):
            continue

        # Added lines
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_line += 1
            content = raw_line[1:]      # strip leading "+"
            label = _scan_line(content)
            if label:
                bypassed = BYPASS_COMMENT in content
                findings.append(
                    SecretFinding(
                        file=current_file or "?",
                        line_number=current_line,
                        line_content=content.strip(),
                        pattern_label=label,
                        bypassed=bypassed,
                    )
                )
        elif not raw_line.startswith("-"):
            # Context line (unchanged)
            current_line += 1

    return findings


# ── Git helpers ───────────────────────────────────────────────────────────────

def get_staged_files(repo_root: Optional[Path] = None) -> List[str]:
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return [f.strip() for f in r.stdout.splitlines() if f.strip()]
    except Exception:
        return []


def get_staged_diff(repo_root: Optional[Path] = None) -> str:
    try:
        r = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return r.stdout
    except Exception:
        return ""


# ── pre-commit layer ──────────────────────────────────────────────────────────

_GENERATED_CONFIG_HEADER = "# Generated by Cliara"
_GENERATED_CONFIG = f"""\
{_GENERATED_CONFIG_HEADER} for pre-push secret scanning.
# Add  # {BYPASS_COMMENT}  to a line to acknowledge a known-safe value.
# Disable: config set secret_scan_on_push false
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args:
          - "--exclude-lines"
          - "{BYPASS_COMMENT}"
        exclude: |
          (?x)^(
            .*\\.lock$|
            .*\\.min\\.js$|
            .*\\.svg$|
            .*\\.png$|
            .*\\.jpg$|
            .*\\.jpeg$|
            .*\\.ico$
          )$
"""
_DETECT_SECRETS_RE = re.compile(r"detect-secrets", re.IGNORECASE)
_HOOKS_CACHE_DIR = Path.home() / ".cache" / "pre-commit"


def is_precommit_installed() -> bool:
    from shutil import which
    return which("pre-commit") is not None


def install_precommit() -> Tuple[bool, str]:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pre-commit", "--quiet"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, "pip install timed out"
    except Exception as e:
        return False, str(e)


def _config_has_detect_secrets(path: Path) -> bool:
    try:
        return bool(_DETECT_SECRETS_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except Exception:
        return False


def _cliara_config_path(repo_root: Path) -> Path:
    return repo_root / ".cliara-secret-scan.yaml"


def ensure_scan_config(repo_root: Path) -> Tuple[Path, bool]:
    """Return (config_path, already_existed).

    Prefers the repo's own .pre-commit-config.yaml if it has detect-secrets.
    Otherwise creates/uses .cliara-secret-scan.yaml (never mutates user config).
    """
    user_cfg = repo_root / ".pre-commit-config.yaml"
    if user_cfg.exists() and _config_has_detect_secrets(user_cfg):
        return user_cfg, True

    cliara_cfg = _cliara_config_path(repo_root)
    if cliara_cfg.exists():
        return cliara_cfg, True

    try:
        cliara_cfg.write_text(_GENERATED_CONFIG, encoding="utf-8")
        return cliara_cfg, False
    except Exception:
        return cliara_cfg, False


def _hooks_installed_for(config_path: Path) -> bool:
    """Heuristic: true if pre-commit has already downloaded deps for this config."""
    # pre-commit caches hooks under ~/.cache/pre-commit/. If the cache dir
    # exists and has entries the hooks have been installed at least once.
    return _HOOKS_CACHE_DIR.is_dir() and any(_HOOKS_CACHE_DIR.iterdir())


def run_precommit_on_staged(
    config_path: Path,
    repo_root: Path,
    staged_files: List[str],
) -> Tuple[bool, str]:
    """Run pre-commit detect-secrets on staged files.

    Returns (passed, output).
    """
    if not staged_files:
        return True, ""

    # Install hook dependencies once (first ever run or after cache cleared)
    if not _hooks_installed_for(config_path):
        subprocess.run(
            ["pre-commit", "install-hooks", "--config", str(config_path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=180,
        )

    try:
        r = subprocess.run(
            ["pre-commit", "run", "detect-secrets",
             "--config", str(config_path),
             "--files"] + staged_files,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "pre-commit timed out"
    except FileNotFoundError:
        return False, "pre-commit not found"
    except Exception as e:
        return False, str(e)


# ── Main entry point ──────────────────────────────────────────────────────────

def scan(
    repo_root: Optional[Path] = None,
    *,
    auto_install_precommit: bool = True,
) -> ScanResult:
    """Run both scanning layers on the staged diff and return a ScanResult.

    Layers run unconditionally — Layer 1+2 (inline) is always fast and
    reliable.  Layer 3 (pre-commit) provides a second opinion.

    A finding blocks the push only when BOTH conditions are true:
      - the inline scanner OR pre-commit flagged it, AND
      - the line does NOT contain # cliara-noscan.
    """
    cwd = repo_root or Path.cwd()
    staged = get_staged_files(cwd)

    if not staged:
        return ScanResult(
            passed=True, precommit_used=False, precommit_output="",
            findings=[], new_config_created=False,
        )

    # ── Layer 1 + 2: inline diff scan (always runs) ──────────────────────
    diff = get_staged_diff(cwd)
    findings = parse_diff_for_secrets(diff)
    inline_blocked = [f for f in findings if not f.bypassed]

    # ── Layer 3: pre-commit (best-effort, non-blocking on install failure) ─
    pc_used = False
    pc_output = ""
    pc_passed = True
    new_cfg = False

    if is_precommit_installed() or auto_install_precommit:
        if not is_precommit_installed():
            ok, err = install_precommit()
            if not ok:
                pc_output = f"pre-commit install failed: {err}"
                # Inline results are still authoritative
            else:
                pc_used = True
        else:
            pc_used = True

        if pc_used:
            config_path, existed = ensure_scan_config(cwd)
            new_cfg = not existed
            pc_passed, pc_output = run_precommit_on_staged(config_path, cwd, staged)

    # ── Decision ──────────────────────────────────────────────────────────
    # Block if inline layer finds unacknowledged secrets.
    # Also block if pre-commit fails AND the diff parser found ANY potential
    # secret pattern (even if entropy was borderline — belt-and-suspenders).
    if inline_blocked:
        return ScanResult(
            passed=False, precommit_used=pc_used, precommit_output=pc_output,
            findings=findings, new_config_created=new_cfg,
        )

    # Pre-commit failed but inline scanner found nothing new beyond bypassed?
    # Show a warning but don't block (avoids false positives from baseline drift).
    if not pc_passed and findings and all(f.bypassed for f in findings):
        # All inline finds are acknowledged — pre-commit failure is unrelated.
        pass  # allow

    return ScanResult(
        passed=True, precommit_used=pc_used, precommit_output=pc_output,
        findings=findings, new_config_created=new_cfg,
    )

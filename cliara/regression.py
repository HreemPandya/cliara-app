"""
Automatic regression detection for Cliara.

Captures lightweight success snapshots per workflow; on failure compares
against last success (git, deps, env, runtime) and ranks likely causes.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _run(cmd: List[str], cwd: Path, timeout: int = 5) -> Tuple[bool, str]:
    """Run command, return (success, stdout)."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception:
        return False, ""


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def _detect_deps_kind(cwd: Path) -> Optional[str]:
    if (cwd / "package.json").exists():
        return "npm"
    if (cwd / "pyproject.toml").exists() or (cwd / "requirements.txt").exists():
        return "python"
    if (cwd / "Cargo.toml").exists():
        return "cargo"
    return None


def _deps_hash(cwd: Path, kind: Optional[str]) -> Optional[str]:
    if kind == "python":
        ok, out = _run([sys.executable, "-m", "pip", "freeze"], cwd, timeout=8)
        return _hash(out) if ok and out else None
    if kind == "npm":
        lock = cwd / "package-lock.json"
        if lock.exists():
            try:
                return _hash(lock.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
        ok, out = _run(["npm", "ls", "--all"], cwd, timeout=5)
        return _hash(out) if ok and out else None
    if kind == "cargo":
        lock = cwd / "Cargo.lock"
        if lock.exists():
            try:
                return _hash(lock.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    return None


def capture_snapshot(cwd: Path) -> Dict[str, Any]:
    """
    Build a lightweight snapshot of git, deps, env, and runtime for *cwd*.
    """
    snapshot: Dict[str, Any] = {}

    # Git
    ok_sha, sha = _run(["git", "rev-parse", "HEAD"], cwd)
    ok_branch, branch = _run(["git", "branch", "--show-current"], cwd)
    ok_stat, short_stat = _run(["git", "diff", "--shortstat", "HEAD"], cwd)
    if ok_sha and sha:
        snapshot["git"] = {
            "sha": sha[:12],
            "branch": branch if ok_branch and branch else None,
            "short_stat": short_stat if ok_stat and short_stat else None,
        }

    # Deps
    kind = _detect_deps_kind(cwd)
    deps_hash_val = _deps_hash(cwd, kind)
    if kind or deps_hash_val is not None:
        snapshot["deps"] = {"kind": kind, "hash": deps_hash_val}

    # Env (minimal)
    env: Dict[str, str] = {}
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        env["VIRTUAL_ENV"] = venv
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if conda:
        env["CONDA_DEFAULT_ENV"] = conda
    path_val = os.environ.get("PATH", "")
    env["PATH_hash"] = _hash(path_val) if path_val else ""
    if env:
        snapshot["env"] = env

    # Runtime
    runtime: Dict[str, str] = {}
    runtime["python"] = sys.version.split()[0]
    if (cwd / "package.json").exists():
        ok_node, node_v = _run(["node", "-v"], cwd, timeout=2)
        if ok_node and node_v:
            runtime["node"] = node_v.strip()
    if runtime:
        snapshot["runtime"] = runtime

    return snapshot


def save_success_snapshot(
    workflow_key: str, snapshot: Dict[str, Any], store_path: Path
) -> None:
    """Persist last-success snapshot for workflow_key. Overwrites previous."""
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **snapshot}
    data: Dict[str, Dict[str, Any]] = {}
    if store_path.exists():
        try:
            data = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[workflow_key] = payload
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_last_success(workflow_key: str, store_path: Path) -> Optional[Dict[str, Any]]:
    """Return last success snapshot for workflow_key, or None."""
    if not store_path.exists():
        return None
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
        return data.get(workflow_key)
    except Exception:
        return None


def gather_current_snapshot(cwd: Path) -> Dict[str, Any]:
    """Same as capture_snapshot: current state for comparison."""
    return capture_snapshot(cwd)


def diff_snapshots(
    last: Dict[str, Any], current: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Compare last success vs current. Returns dict with git_changed, deps_changed,
    env_changed, runtime_changed and evidence fields.
    """
    result: Dict[str, Any] = {
        "git_changed": False,
        "sha_before": None,
        "sha_after": None,
        "short_stat": None,
        "deps_changed": False,
        "env_changed": False,
        "env_vars": [],
        "runtime_changed": False,
        "runtime_which": [],
    }

    # Git
    last_git = last.get("git") or {}
    cur_git = current.get("git") or {}
    last_sha = last_git.get("sha")
    cur_sha = cur_git.get("sha")
    if last_sha and cur_sha and last_sha != cur_sha:
        result["git_changed"] = True
        result["sha_before"] = last_sha
        result["sha_after"] = cur_sha
        result["short_stat"] = cur_git.get("short_stat")
    elif (last_git.get("short_stat") or "") != (cur_git.get("short_stat") or ""):
        result["git_changed"] = True
        result["short_stat"] = cur_git.get("short_stat")

    # Deps
    last_deps = last.get("deps") or {}
    cur_deps = current.get("deps") or {}
    if (last_deps.get("hash") or "") != (cur_deps.get("hash") or ""):
        result["deps_changed"] = True

    # Env
    last_env = last.get("env") or {}
    cur_env = current.get("env") or {}
    for k in set(last_env) | set(cur_env):
        if (last_env.get(k) or "") != (cur_env.get(k) or ""):
            result["env_changed"] = True
            result["env_vars"].append(k)

    # Runtime
    last_rt = last.get("runtime") or {}
    cur_rt = current.get("runtime") or {}
    for k in set(last_rt) | set(cur_rt):
        if (last_rt.get(k) or "") != (cur_rt.get(k) or ""):
            result["runtime_changed"] = True
            result["runtime_which"].append(k)

    return result


def rank_causes(
    diff_result: Dict[str, Any],
    last_snapshot: Dict[str, Any],
    current_snapshot: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """
    Ordered list of (cause_label, evidence) for display.
    """
    causes: List[Tuple[str, str]] = []

    if diff_result.get("git_changed"):
        stat = diff_result.get("short_stat") or ""
        if diff_result.get("sha_before") and diff_result.get("sha_after"):
            evidence = f"HEAD changed ({diff_result['sha_before']} → {diff_result['sha_after']})"
            if stat:
                evidence += f" · {stat}"
        else:
            evidence = stat or "working tree or HEAD changed"
        causes.append(("git", evidence))

    if diff_result.get("deps_changed"):
        causes.append(("deps", "lockfile or dependency set changed"))

    if diff_result.get("env_changed"):
        vars_list = diff_result.get("env_vars") or []
        causes.append(("env", ", ".join(vars_list) + " differs"))

    if diff_result.get("runtime_changed"):
        which = diff_result.get("runtime_which") or []
        causes.append(("runtime", ", ".join(which) + " version changed"))

    return causes


def format_minimal_report(ranked_causes: List[Tuple[str, str]]) -> str:
    """Single line for automatic print after failure (displayed inside a titled panel)."""
    if not ranked_causes:
        return ""
    labels = [c[0] for c in ranked_causes]
    return ", ".join(labels) + " · ? why"


def format_expanded_report(
    ranked_causes: List[Tuple[str, str]],
    last_snapshot: Dict[str, Any],
    current_snapshot: Dict[str, Any],
) -> str:
    """Multi-line report for ? why."""
    lines = ["Regression (vs last success):"]
    for label, evidence in ranked_causes:
        lines.append(f"  {label:<6} {evidence}")
    return "\n".join(lines)

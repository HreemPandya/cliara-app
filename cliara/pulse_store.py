"""Pulse persistent cache.

Stores small ambient-state facts that are expensive to recompute every prompt:
- last local test run result (py.test/pytest)
- last fetched CI status for the current HEAD commit

No notifications are emitted from this module.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cliara.file_lock import with_file_lock


@dataclass(frozen=True)
class LastTestRun:
    ts: float
    exit_code: int
    command: str
    cwd: str
    branch: str


@dataclass(frozen=True)
class CiHeadStatus:
    ts: float
    sha: str
    failing: bool
    summary: str


def _pulse_path(config_dir: Path) -> Path:
    return Path(config_dir).expanduser() / "pulse.json"


def _load(config_dir: Path) -> Dict[str, Any]:
    path = _pulse_path(config_dir)
    if not path.exists():
        return {}
    try:
        with with_file_lock(path):
            raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(config_dir: Path, data: Dict[str, Any]) -> None:
    path = _pulse_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with with_file_lock(path):
            path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def get_last_test_run(config_dir: Path, repo_root: str) -> Optional[LastTestRun]:
    data = _load(config_dir)
    tests = data.get("tests")
    if not isinstance(tests, dict):
        return None
    entry = tests.get(repo_root)
    if not isinstance(entry, dict):
        return None
    try:
        return LastTestRun(
            ts=float(entry.get("ts") or 0.0),
            exit_code=int(entry.get("exit_code") or 0),
            command=str(entry.get("command") or ""),
            cwd=str(entry.get("cwd") or ""),
            branch=str(entry.get("branch") or ""),
        )
    except Exception:
        return None


def record_last_test_run(
    config_dir: Path,
    repo_root: str,
    *,
    exit_code: int,
    command: str,
    cwd: str,
    branch: str,
    ts: Optional[float] = None,
) -> None:
    now = float(ts if ts is not None else time.time())
    data = _load(config_dir)
    if not isinstance(data.get("tests"), dict):
        data["tests"] = {}
    data["tests"][repo_root] = {
        "ts": now,
        "exit_code": int(exit_code),
        "command": str(command or ""),
        "cwd": str(cwd or ""),
        "branch": str(branch or ""),
    }
    _save(config_dir, data)


def get_ci_head_status(config_dir: Path, repo_root: str) -> Optional[CiHeadStatus]:
    data = _load(config_dir)
    ci = data.get("ci")
    if not isinstance(ci, dict):
        return None
    entry = ci.get(repo_root)
    if not isinstance(entry, dict):
        return None
    try:
        return CiHeadStatus(
            ts=float(entry.get("ts") or 0.0),
            sha=str(entry.get("sha") or ""),
            failing=bool(entry.get("failing")),
            summary=str(entry.get("summary") or ""),
        )
    except Exception:
        return None


def record_ci_head_status(
    config_dir: Path,
    repo_root: str,
    *,
    sha: str,
    failing: bool,
    summary: str,
    ts: Optional[float] = None,
) -> None:
    now = float(ts if ts is not None else time.time())
    data = _load(config_dir)
    if not isinstance(data.get("ci"), dict):
        data["ci"] = {}
    data["ci"][repo_root] = {
        "ts": now,
        "sha": str(sha or ""),
        "failing": bool(failing),
        "summary": str(summary or ""),
    }
    _save(config_dir, data)

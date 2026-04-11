"""
Persistent task sessions for Cliara.

Stores named task sessions in ~/.cliara/sessions.json so users can
start a session, work across terminal closes, and resume with a
structured summary and suggested next step. Sessions are keyed by
(name, project_root) so the same session name can exist in different projects.
"""

import json
import subprocess
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cliara.file_lock import with_file_lock

# Keys for optional structured closeout (session end --reflect)
CLOSEOUT_KEYS = ("blocked", "decided", "next")


def _normalize_closeout(raw: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Return None if empty; otherwise only known keys with non-empty stripped values."""
    if not raw:
        return None
    out: Dict[str, str] = {}
    for k in CLOSEOUT_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[k] = s
    return out if out else None


def _normalize_closeout_prompts(raw: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Question text shown at reflect time; same keys, stored for session show."""
    if not raw:
        return None
    out: Dict[str, str] = {}
    for k in CLOSEOUT_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[k] = s[:800]
    return out if out else None


def _normalize_reflection_log(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """Sanitize reflection entries from session end --reflect (session_reflect skill)."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        eid = item.get("id")
        kind = item.get("kind")
        q = item.get("question")
        ans = item.get("answer")
        if not isinstance(eid, str) or not isinstance(kind, str):
            continue
        if not isinstance(q, str) or not q.strip():
            continue
        entry: Dict[str, Any] = {
            "id": eid.strip()[:80],
            "kind": kind,
            "question": q.strip()[:1200],
            "answer": str(ans).strip()[:8000] if ans is not None else "",
        }
        if isinstance(item.get("hint"), str) and item["hint"].strip():
            entry["hint"] = item["hint"].strip()[:400]
        if isinstance(item.get("options"), list):
            entry["options"] = [str(o)[:500] for o in item["options"]][:8]
        if item.get("selected_index") is not None:
            try:
                entry["selected_index"] = int(item["selected_index"])
            except (TypeError, ValueError):
                pass
        if isinstance(item.get("selected_label"), str) and item["selected_label"].strip():
            entry["selected_label"] = item["selected_label"].strip()[:500]
        out.append(entry)
    return out if out else None


@dataclass
class CommandEntry:
    """A single command executed during a session."""

    command: str
    cwd: str
    exit_code: int
    timestamp: str  # ISO
    id: str = ""  # UUID for graph parent/child links
    parent_id: Optional[str] = None  # id of command this is a follow-up of (e.g. fix after failure)
    # Optional truncated captures when session_persist_output is enabled
    stderr_preview: Optional[str] = None
    stdout_preview: Optional[str] = None

    def to_dict(self) -> dict:
        d: Dict[str, Any] = {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timestamp": self.timestamp,
            "id": self.id,
            "parent_id": self.parent_id,
        }
        if self.stderr_preview:
            d["stderr_preview"] = self.stderr_preview
        if self.stdout_preview:
            d["stdout_preview"] = self.stdout_preview
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "CommandEntry":
        return cls(
            command=data.get("command", ""),
            cwd=data.get("cwd", ""),
            exit_code=data.get("exit_code", 0),
            timestamp=data.get("timestamp", ""),
            id=data.get("id") or str(uuid.uuid4()),
            parent_id=data.get("parent_id"),
            stderr_preview=(
                str(data["stderr_preview"]).strip()[:16000]
                if data.get("stderr_preview")
                else None
            ),
            stdout_preview=(
                str(data["stdout_preview"]).strip()[:16000]
                if data.get("stdout_preview")
                else None
            ),
        )


@dataclass
class NoteEntry:
    """A user-added note during a session."""

    text: str
    timestamp: str  # ISO

    def to_dict(self) -> dict:
        return {"text": self.text, "timestamp": self.timestamp}

    @classmethod
    def from_dict(cls, data: dict) -> "NoteEntry":
        return cls(
            text=data.get("text", ""),
            timestamp=data.get("timestamp", ""),
        )


@dataclass
class TaskSession:
    """
    A named task session: intent, commands, cwds, project context, notes.
    """

    id: str  # UUID
    name: str
    intent: str
    created: str  # ISO
    updated: str  # ISO
    ended_at: Optional[str] = None  # ISO when session was ended
    cwds: List[str] = field(default_factory=list)
    branch: Optional[str] = None
    project_root: Optional[str] = None
    commands: List[CommandEntry] = field(default_factory=list)
    notes: List[NoteEntry] = field(default_factory=list)
    end_note: Optional[str] = None
    # Optional structured closeout from `session end --reflect` (blocked / decided / next)
    closeout: Optional[Dict[str, str]] = None
    # Questions asked at reflect (LLM-tailored or defaults); for display in session show
    closeout_prompts: Optional[Dict[str, str]] = None
    # session_reflect skill: list of {id, kind, question, answer, ...}
    reflection: Optional[List[Dict[str, Any]]] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "intent": self.intent,
            "created": self.created,
            "updated": self.updated,
            "ended_at": self.ended_at,
            "cwds": list(self.cwds),
            "branch": self.branch,
            "project_root": self.project_root,
            "commands": [c.to_dict() for c in self.commands],
            "notes": [n.to_dict() for n in self.notes],
            "end_note": self.end_note,
            "closeout": dict(self.closeout) if self.closeout else None,
            "closeout_prompts": dict(self.closeout_prompts)
            if self.closeout_prompts
            else None,
            "reflection": list(self.reflection) if self.reflection else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSession":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            intent=data.get("intent", ""),
            created=data.get("created", ""),
            updated=data.get("updated", ""),
            ended_at=data.get("ended_at"),
            cwds=list(data.get("cwds", [])),
            branch=data.get("branch"),
            project_root=data.get("project_root"),
            commands=[CommandEntry.from_dict(c) for c in data.get("commands", [])],
            notes=[NoteEntry.from_dict(n) for n in data.get("notes", [])],
            end_note=data.get("end_note"),
            closeout=(
                _normalize_closeout({k: raw_co.get(k) for k in CLOSEOUT_KEYS})
                if isinstance((raw_co := data.get("closeout")), dict)
                else None
            ),
            closeout_prompts=(
                _normalize_closeout_prompts(
                    {k: raw_p.get(k) for k in CLOSEOUT_KEYS}
                )
                if isinstance((raw_p := data.get("closeout_prompts")), dict)
                else None
            ),
            reflection=_normalize_reflection_log(data.get("reflection")),
        )

    @property
    def is_ended(self) -> bool:
        return self.ended_at is not None

    def last_active(self) -> str:
        """Return the updated timestamp (last activity)."""
        return self.updated


def _get_project_root(cwd: Path) -> Optional[str]:
    """Return git root for cwd, or None if not in a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return str(Path(r.stdout.strip()).resolve())
    except Exception:
        pass
    return None


def _get_branch(cwd: Path) -> Optional[str]:
    """Return current git branch for cwd, or None."""
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _session_key(name: str, project_root: Optional[str]) -> str:
    """Unique key for a session: name + project root (or 'global' if no repo)."""
    root = (project_root or "").strip() or "global"
    return f"{name}::{root}"


class SessionStore:
    """
    Read/write ~/.cliara/sessions.json.
    Maps session_key -> TaskSession dict.
    """

    def __init__(self, store_path: Optional[Path] = None):
        self._path = store_path or (Path.home() / ".cliara" / "sessions.json")
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}
        else:
            self._data = {}

    def _reload_unlocked(self):
        """Refresh ``_data`` from disk (caller must hold the store lock)."""
        self._load()

    def _save_unlocked(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _save(self):
        with with_file_lock(self._path):
            self._reload_unlocked()
            self._save_unlocked()

    def _mutate(self, fn: Callable[[], None]) -> None:
        """Reload latest file state, apply *fn* mutating ``_data``, then save."""
        with with_file_lock(self._path):
            self._reload_unlocked()
            fn()
            self._save_unlocked()

    def get_by_key(self, name: str, project_root: Optional[str]) -> Optional[TaskSession]:
        """Return the session for this name and project root, or None."""
        key = _session_key(name, project_root)
        entry = self._data.get(key)
        if entry is None:
            return None
        return TaskSession.from_dict(entry)

    def get_by_id(self, session_id: str) -> Optional[TaskSession]:
        """Return a session by its id."""
        for entry in self._data.values():
            if entry.get("id") == session_id:
                return TaskSession.from_dict(entry)
        return None

    def create(
        self,
        name: str,
        intent: str = "",
        project_root: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> TaskSession:
        """Create a new session. Replaces any existing session with same key."""
        now = datetime.now(timezone.utc).isoformat()
        key = _session_key(name, project_root)
        session = TaskSession(
            id=str(uuid.uuid4()),
            name=name,
            intent=intent,
            created=now,
            updated=now,
            ended_at=None,
            cwds=[],
            branch=branch,
            project_root=project_root,
            commands=[],
            notes=[],
            end_note=None,
            closeout=None,
            closeout_prompts=None,
            reflection=None,
        )

        def _apply():
            self._data[key] = session.to_dict()

        self._mutate(_apply)
        return session

    def update(self, session: TaskSession):
        """Persist session changes."""
        key = _session_key(session.name, session.project_root)

        def _apply():
            self._data[key] = session.to_dict()

        self._mutate(_apply)

    def add_command(
        self,
        session_id: str,
        command: str,
        cwd: str,
        exit_code: int,
        branch: Optional[str] = None,
        project_root: Optional[str] = None,
        parent_id: Optional[str] = None,
        stderr_preview: Optional[str] = None,
        stdout_preview: Optional[str] = None,
    ) -> Optional[str]:
        """Append a command to the session and update cwds/branch/updated.
        Returns the new command's id, or None if session not found."""
        holder: List[Optional[str]] = [None]

        def _apply():
            session = self.get_by_id(session_id)
            if session is None:
                return
            now = datetime.now(timezone.utc).isoformat()
            entry_id = str(uuid.uuid4())
            entry = CommandEntry(
                command=command,
                cwd=cwd,
                exit_code=exit_code,
                timestamp=now,
                id=entry_id,
                parent_id=parent_id,
                stderr_preview=stderr_preview,
                stdout_preview=stdout_preview,
            )
            session.commands.append(entry)
            if cwd and cwd not in session.cwds:
                session.cwds.append(cwd)
            if branch is not None:
                session.branch = branch
            if project_root is not None:
                session.project_root = project_root
            session.updated = now
            key = _session_key(session.name, session.project_root)
            self._data[key] = session.to_dict()
            holder[0] = entry_id

        self._mutate(_apply)
        return holder[0]

    def get_last_command_id(self, session_id: str) -> Optional[str]:
        """Return the id of the last command in the session, or None."""
        session = self.get_by_id(session_id)
        if session is None or not session.commands:
            return None
        last = session.commands[-1]
        return last.id if last.id else None

    def add_note(self, session_id: str, text: str):
        """Append a note to the session."""

        def _apply():
            session = self.get_by_id(session_id)
            if session is None:
                return
            now = datetime.now(timezone.utc).isoformat()
            session.notes.append(NoteEntry(text=text, timestamp=now))
            session.updated = now
            key = _session_key(session.name, session.project_root)
            self._data[key] = session.to_dict()

        self._mutate(_apply)

    def end_session(
        self,
        session_id: str,
        end_note: Optional[str] = None,
        closeout: Optional[Dict[str, str]] = None,
        closeout_prompts: Optional[Dict[str, str]] = None,
        reflection: Optional[List[Dict[str, Any]]] = None,
    ):
        """Mark session ended. Use reflection= for session_reflect; else legacy closeout fields."""

        def _apply():
            session = self.get_by_id(session_id)
            if session is None:
                return
            session.ended_at = datetime.now(timezone.utc).isoformat()
            session.updated = session.ended_at
            if end_note is not None:
                session.end_note = end_note
            if reflection is not None:
                session.reflection = _normalize_reflection_log(reflection)
                session.closeout = None
                session.closeout_prompts = None
            else:
                session.reflection = None
                session.closeout = _normalize_closeout(closeout)
                session.closeout_prompts = _normalize_closeout_prompts(closeout_prompts)
            key = _session_key(session.name, session.project_root)
            self._data[key] = session.to_dict()

        self._mutate(_apply)

    def list_all(self) -> List[TaskSession]:
        """Return all sessions, sorted by updated descending."""
        sessions = [TaskSession.from_dict(v) for v in self._data.values()]
        sessions.sort(key=lambda s: s.updated, reverse=True)
        return sessions

    def list_by_project(self, project_root: Optional[str]) -> List[TaskSession]:
        """Return sessions for this project (same project_root), sorted by updated."""
        root = (project_root or "").strip() or "global"
        result = [
            s
            for s in self.list_all()
            if ((s.project_root or "").strip() or "global") == root
        ]
        result.sort(key=lambda s: s.updated, reverse=True)
        return result

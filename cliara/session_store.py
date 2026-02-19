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
from typing import Optional, List, Dict, Any


@dataclass
class CommandEntry:
    """A single command executed during a session."""

    command: str
    cwd: str
    exit_code: int
    timestamp: str  # ISO

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CommandEntry":
        return cls(
            command=data.get("command", ""),
            cwd=data.get("cwd", ""),
            exit_code=data.get("exit_code", 0),
            timestamp=data.get("timestamp", ""),
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

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

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
        )
        self._data[key] = session.to_dict()
        self._save()
        return session

    def update(self, session: TaskSession):
        """Persist session changes."""
        key = _session_key(session.name, session.project_root)
        self._data[key] = session.to_dict()
        self._save()

    def add_command(
        self,
        session_id: str,
        command: str,
        cwd: str,
        exit_code: int,
        branch: Optional[str] = None,
        project_root: Optional[str] = None,
    ):
        """Append a command to the session and update cwds/branch/updated."""
        session = self.get_by_id(session_id)
        if session is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        session.commands.append(
            CommandEntry(command=command, cwd=cwd, exit_code=exit_code, timestamp=now)
        )
        if cwd and cwd not in session.cwds:
            session.cwds.append(cwd)
        if branch is not None:
            session.branch = branch
        if project_root is not None:
            session.project_root = project_root
        session.updated = now
        self.update(session)

    def add_note(self, session_id: str, text: str):
        """Append a note to the session."""
        session = self.get_by_id(session_id)
        if session is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        session.notes.append(NoteEntry(text=text, timestamp=now))
        session.updated = now
        self.update(session)

    def end_session(self, session_id: str, end_note: Optional[str] = None):
        """Mark session as ended with optional note."""
        session = self.get_by_id(session_id)
        if session is None:
            return
        session.ended_at = datetime.now(timezone.utc).isoformat()
        session.updated = session.ended_at
        if end_note is not None:
            session.end_note = end_note
        self.update(session)

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

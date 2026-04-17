"""
Semantic history store for Cliara.

Persists command history enriched with short summaries (and optionally embeddings)
to ~/.cliara/semantic_history.json so users can search by intent via ? find / ? when did I ...
"""

import json
import threading

from cliara.file_lock import with_file_lock
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any


# Dedupe window: same command within this many seconds is considered duplicate
_DEDUPE_WINDOW_SECONDS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SemanticHistoryStore:
    """
    Load/save semantic history (command + summary + optional embedding).
    Evicts oldest when over max_entries. Optional dedupe by (command, cwd, timestamp window).
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        max_entries: int = 500,
    ):
        self._path = store_path or (Path.home() / ".cliara" / "semantic_history.json")
        self._max_entries = max(1, max_entries)
        self._entries: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """Load from disk. Treat missing or malformed file as empty."""
        with self._lock:
            if not self._path.exists():
                self._entries = []
                return
            try:
                with with_file_lock(self._path):
                    raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw)
                entries = data.get("entries", data) if isinstance(data, dict) else data
                if not isinstance(entries, list):
                    self._entries = []
                    return
                self._entries = []
                for e in entries:
                    if isinstance(e, dict) and e.get("command") is not None:
                        self._entries.append(self._normalize_entry(e))
                # Keep most recent by timestamp
                self._entries.sort(key=lambda x: x.get("timestamp", ""), reverse=False)
                self._entries = self._entries[-self._max_entries:]
            except Exception:
                self._entries = []

    def _normalize_entry(self, e: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            "command": str(e.get("command", "")),
            "summary": str(e.get("summary", "")),
            "timestamp": str(e.get("timestamp", _now_iso())),
        }
        if "cwd" in e and e["cwd"] is not None:
            out["cwd"] = str(e["cwd"])
        if "exit_code" in e and e["exit_code"] is not None:
            out["exit_code"] = int(e["exit_code"])
        if "embedding" in e and e["embedding"] is not None:
            out["embedding"] = list(e["embedding"])
        return out

    def _save(self) -> None:
        """Persist to disk. Caller must hold _lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": self._entries}
        with with_file_lock(self._path):
            # Compact JSON: faster writes and smaller files than indent=2.
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    def flush(self) -> None:
        """Write the current in-memory entries to disk (e.g. before process exit)."""
        with self._lock:
            self._save()

    def add(
        self,
        command: str,
        summary: str = "",
        cwd: Optional[str] = None,
        exit_code: Optional[int] = None,
        embedding: Optional[List[float]] = None,
        timestamp: Optional[str] = None,
        dedupe: bool = True,
        persist: bool = True,
    ) -> None:
        """
        Add an entry. Evicts oldest if over max_entries.
        If dedupe is True, skip if same (command, cwd) was added within the last minute.
        If persist is False, only update in-memory state (caller should persist later, or call flush()).
        """
        ts = timestamp or _now_iso()
        entry = {
            "command": command,
            "summary": summary,
            "timestamp": ts,
            "cwd": cwd,
            "exit_code": exit_code,
            "embedding": embedding,
        }
        entry = self._normalize_entry(entry)

        with self._lock:
            if dedupe and self._entries:
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    ts_dt = None
                for i, existing in enumerate(reversed(self._entries)):
                    if existing.get("command") != command:
                        continue
                    if existing.get("cwd") != (cwd or ""):
                        continue
                    try:
                        existing_ts = datetime.fromisoformat(
                            existing.get("timestamp", "").replace("Z", "+00:00")
                        )
                        delta = (ts_dt - existing_ts).total_seconds() if ts_dt else 0
                        if 0 <= delta <= _DEDUPE_WINDOW_SECONDS:
                            # Update existing with new summary/embedding
                            self._entries[-(i + 1)] = entry
                            if persist:
                                self._save()
                            return
                    except Exception:
                        pass

            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                self._entries.sort(key=lambda x: x.get("timestamp", ""), reverse=False)
                self._entries = self._entries[-self._max_entries :]
            if persist:
                self._save()

    def update_summary_for_command(
        self,
        command: str,
        summary: str,
        cwd: Optional[str] = None,
        *,
        embedding: Optional[List[float]] = None,
    ) -> bool:
        """
        Update the summary of the most recent entry matching (command, cwd).
        Returns True if an entry was updated.

        *embedding*: if a list, store it (e.g. embedding of ``command`` + summary,
        matching the semantic-history worker). If ``None``, any existing
        embedding is removed so vector search falls back until a new vector
        exists.
        """
        if not summary:
            return False
        with self._lock:
            for i in range(len(self._entries) - 1, -1, -1):
                e = self._entries[i]
                if e.get("command") != command:
                    continue
                if (cwd or "") != (e.get("cwd") or ""):
                    continue
                new_e = {**e, "summary": summary}
                if embedding is not None:
                    new_e["embedding"] = embedding
                else:
                    new_e.pop("embedding", None)
                self._entries[i] = self._normalize_entry(new_e)
                self._save()
                return True
        return False

    def get_recent(self, n: int = 100) -> List[Dict[str, Any]]:
        """Return the n most recent entries (newest last). For LLM prompt."""
        with self._lock:
            copy = [dict(e) for e in self._entries]
        copy.sort(key=lambda x: x.get("timestamp", ""), reverse=False)
        return copy[-n:]

    def get_all(self) -> List[Dict[str, Any]]:
        """Return all entries (newest last). For embedding search."""
        with self._lock:
            copy = [dict(e) for e in self._entries]
        copy.sort(key=lambda x: x.get("timestamp", ""), reverse=False)
        return copy

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._entries) == 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

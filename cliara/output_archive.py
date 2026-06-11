"""
Output Time-Machine: embedded archive of what commands *printed*.

Semantic history remembers what you ran; this module remembers what it said.
Each executed command's stdout/stderr is digested (head + tail), scrubbed for
secrets (see :func:`cliara.secret_scan.scrub_secrets`), compressed, and stored
in a per-project SQLite database under ``~/.cliara/output_archive/``. That
unlocks queries like *"what was the exact error when the staging deploy failed
last Thursday"* — the digests are surfaced both by the ``outputs`` command and
as extra grounding inside ``? find`` / ``? when did I ...`` RAG answers.

Design notes
------------
* **Store**: same proven pattern as :mod:`cliara.codebase_rag` — one SQLite DB
  per project root, embeddings as raw float32 BLOBs, batched NumPy cosine.
* **Privacy first**: scrubbing runs *before* persist, the feature is opt-in
  (``output_archive_enabled`` defaults to ``false``), and a plaintext
  ``search_text`` snippet (also scrubbed) is kept only for keyword search.
* **Lazy embeddings**: nothing is embedded on the write path. ``outputs
  search`` embeds at most a small candidate pool the keyword prefilter already
  matched, so Ollama users don't pay per-command embedding cost.
* **Bounded disk**: per-entry digest cap (default 64 KB) and per-project DB
  cap (default 50 MB) with oldest-first eviction.

This module is intentionally free of any UI/console code so it can be unit
tested in isolation. The shell wiring lives in
``cliara/shell_app/output_archive_commands.py``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


# Schema version — bump when the table layout changes so stale DBs are rebuilt.
SCHEMA_VERSION = 1

# Per-entry digest cap (characters of scrubbed text stored, head + tail).
DEFAULT_MAX_DIGEST_CHARS = 64 * 1024

# Per-project database cap (bytes of stored blobs) before oldest-first eviction.
DEFAULT_MAX_DB_BYTES = 50 * 1024 * 1024

# Plaintext snippet length kept alongside the compressed digest for keyword search.
SEARCH_TEXT_CHARS = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_epoch(ts: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp (Z-suffixed ok) into a Unix epoch, or None."""
    s = (ts or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Compression (zstandard when available, stdlib zlib fallback)
# ---------------------------------------------------------------------------

def _zstd_module():
    try:
        import zstandard  # type: ignore

        return zstandard
    except ImportError:
        return None


def compress_text(text: str) -> Tuple[bytes, str]:
    """Compress *text*; returns ``(blob, method)`` where method is zstd|zlib."""
    raw = (text or "").encode("utf-8", errors="replace")
    zstd = _zstd_module()
    if zstd is not None:
        try:
            return zstd.ZstdCompressor(level=6).compress(raw), "zstd"
        except Exception:
            pass
    return zlib.compress(raw, 6), "zlib"


def decompress_text(blob: bytes, method: str) -> str:
    """Inverse of :func:`compress_text`. Returns '' when the blob is unreadable."""
    if not blob:
        return ""
    try:
        if method == "zstd":
            zstd = _zstd_module()
            if zstd is None:
                return ""
            raw = zstd.ZstdDecompressor().decompress(bytes(blob))
        else:
            raw = zlib.decompress(bytes(blob))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Digesting streams
# ---------------------------------------------------------------------------

def _head_tail(text: str, max_chars: int) -> str:
    """Truncate *text* to ~max_chars keeping 60% head + 40% tail on line bounds."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_budget = (max_chars * 3) // 5
    tail_budget = max_chars - head_budget

    head = text[:head_budget]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]

    tail = text[-tail_budget:]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]

    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... ({omitted} chars omitted) ...\n{tail}"


def digest_streams(
    stdout: str,
    stderr: str,
    max_chars: int = DEFAULT_MAX_DIGEST_CHARS,
) -> str:
    """Combine stdout/stderr into one labeled digest, head+tail truncated.

    stderr comes first: for forensic queries ("what was the exact error")
    it is almost always the part that matters, so it must survive truncation.
    """
    parts: List[str] = []
    err = (stderr or "").strip()
    out = (stdout or "").strip()
    if err:
        parts.append("[stderr]\n" + err)
    if out:
        parts.append("[stdout]\n" + out)
    if not parts:
        return ""
    text = "\n\n".join(parts)
    return _head_tail(text, max_chars)


# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

def archive_db_path(config_dir: Path, project_root: str) -> Path:
    """Return the SQLite path for *project_root*'s output archive.

    Mirrors :func:`cliara.codebase_rag.repo_db_path`: name + path-hash so two
    projects with the same basename don't collide. Non-repo commands use the
    sentinel root ``"global"``.
    """
    base = Path(config_dir) / "output_archive"
    root = (project_root or "global").strip() or "global"
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:16]
    name = Path(root).name or "global"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:32]
    return base / f"{safe}-{digest}.db"


# ---------------------------------------------------------------------------
# Keyword scoring (mirrors the semantic-history token approach)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOP_TOKENS = frozenset({
    "when", "what", "where", "why", "how", "did", "do", "does", "was", "were",
    "i", "me", "my", "we", "you", "the", "a", "an", "to", "of", "in", "on",
    "for", "with", "and", "or", "is", "it", "that", "this",
    "last", "recent", "recently", "latest", "most", "exact", "error", "output",
})


def query_tokens(query: str) -> List[str]:
    """Lowercase content tokens from *query*, stop-words removed, order kept."""
    raw = [t for t in _TOKEN_RE.findall((query or "").lower()) if len(t) > 1]
    seen: set = set()
    out: List[str] = []
    for t in raw:
        if t in _STOP_TOKENS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def keyword_score(tokens: Sequence[str], command: str, search_text: str) -> float:
    """Token-overlap score in [0, 1] (command hits weigh more than output hits)."""
    if not tokens:
        return 0.0
    cmd = (command or "").lower()
    body = (search_text or "").lower()
    score = 0.0
    for t in tokens:
        if t in cmd:
            score += 1.0
        if t in body:
            score += 0.6
    denom = max(1.0, float(min(len(tokens), 3)) * 1.3)
    return max(0.0, min(1.0, score / denom))


# ---------------------------------------------------------------------------
# Vector (de)serialization — same convention as codebase_rag
# ---------------------------------------------------------------------------

def _pack_vector(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass
class ArchiveEntry:
    """One archived command run (digest decompressed on demand, not here)."""

    id: int
    ts: float
    timestamp: str
    command: str
    cwd: str
    exit_code: Optional[int]
    git_branch: str
    session_name: str
    elapsed_s: Optional[float]
    digest_chars: int
    redactions: int
    search_text: str
    score: float = 0.0  # filled by search paths


class OutputArchiveStore:
    """SQLite-backed archive of scrubbed command-output digests for one project."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The store is written from the REPL main thread and read by search;
        # check_same_thread=False keeps it safe if a future caller moves
        # archiving onto the existing background-worker pattern.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    # -- lifecycle ---------------------------------------------------------

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                timestamp    TEXT NOT NULL,
                command      TEXT NOT NULL,
                cwd          TEXT NOT NULL DEFAULT '',
                exit_code    INTEGER,
                git_branch   TEXT NOT NULL DEFAULT '',
                session_name TEXT NOT NULL DEFAULT '',
                elapsed_s    REAL,
                digest_blob  BLOB NOT NULL,
                digest_chars INTEGER NOT NULL,
                compression  TEXT NOT NULL,
                search_text  TEXT NOT NULL DEFAULT '',
                redactions   INTEGER NOT NULL DEFAULT 0,
                embedding    BLOB
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_ts ON entries(ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_command ON entries(command)")
        self._conn.commit()

        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif existing != str(SCHEMA_VERSION):
            self.clear()
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    # -- write path ------------------------------------------------------------

    def add_entry(
        self,
        command: str,
        digest_text: str,
        *,
        ts: Optional[float] = None,
        cwd: str = "",
        exit_code: Optional[int] = None,
        git_branch: str = "",
        session_name: str = "",
        elapsed_s: Optional[float] = None,
        redactions: int = 0,
    ) -> Optional[int]:
        """Persist one scrubbed digest. Returns the new row id (None if empty)."""
        text = (digest_text or "").strip()
        if not text or not (command or "").strip():
            return None
        epoch = float(ts) if ts is not None else time.time()
        blob, method = compress_text(text)
        search_text = text[:SEARCH_TEXT_CHARS].lower()
        cur = self._conn.execute(
            "INSERT INTO entries("
            "ts, timestamp, command, cwd, exit_code, git_branch, session_name,"
            "elapsed_s, digest_blob, digest_chars, compression, search_text, redactions"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                epoch,
                datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
                command.strip(),
                cwd or "",
                exit_code,
                git_branch or "",
                session_name or "",
                elapsed_s,
                blob,
                len(text),
                method,
                search_text,
                int(redactions),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # -- read path ---------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> ArchiveEntry:
        return ArchiveEntry(
            id=int(row["id"]),
            ts=float(row["ts"]),
            timestamp=str(row["timestamp"]),
            command=str(row["command"]),
            cwd=str(row["cwd"]),
            exit_code=row["exit_code"],
            git_branch=str(row["git_branch"]),
            session_name=str(row["session_name"]),
            elapsed_s=row["elapsed_s"],
            digest_chars=int(row["digest_chars"]),
            redactions=int(row["redactions"]),
            search_text=str(row["search_text"]),
        )

    _ENTRY_COLS = (
        "id, ts, timestamp, command, cwd, exit_code, git_branch, session_name, "
        "elapsed_s, digest_chars, redactions, search_text"
    )

    def get_digest(self, entry_id: int) -> str:
        """Decompress and return the stored digest for *entry_id* ('' if missing)."""
        row = self._conn.execute(
            "SELECT digest_blob, compression FROM entries WHERE id = ?",
            (int(entry_id),),
        ).fetchone()
        if row is None:
            return ""
        return decompress_text(row["digest_blob"], str(row["compression"]))

    def recent_entries(self, limit: int = 1000) -> List[ArchiveEntry]:
        rows = self._conn.execute(
            f"SELECT {self._ENTRY_COLS} FROM entries ORDER BY ts DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def search_keyword(
        self,
        query: str,
        pool: int = 32,
        scan_limit: int = 1000,
    ) -> List[ArchiveEntry]:
        """Rank recent entries by token overlap on command + scrubbed output."""
        tokens = query_tokens(query)
        if not tokens:
            return []
        scored: List[ArchiveEntry] = []
        for e in self.recent_entries(limit=scan_limit):
            s = keyword_score(tokens, e.command, e.search_text)
            if s > 0:
                e.score = s
                scored.append(e)
        scored.sort(key=lambda e: (-e.score, -e.ts))
        return scored[: max(1, int(pool))]

    def find_output_for(
        self,
        command: str,
        timestamp: str,
        tolerance_s: float = 240.0,
    ) -> Optional[str]:
        """Digest for the archive entry matching (*command*, *timestamp*±tolerance).

        Used to enrich semantic-history RAG sources: history entries carry the
        command text and an ISO timestamp; the nearest archived run wins.
        """
        cmd = (command or "").strip()
        epoch = iso_to_epoch(timestamp)
        if not cmd or epoch is None:
            return None
        row = self._conn.execute(
            "SELECT id, ts FROM entries "
            "WHERE command = ? AND ABS(ts - ?) <= ? "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (cmd, epoch, float(tolerance_s), epoch),
        ).fetchone()
        if row is None:
            return None
        digest = self.get_digest(int(row["id"]))
        return digest or None

    # -- embeddings (lazy, search-time only) ---------------------------------

    def missing_embedding_ids(self, ids: Sequence[int]) -> List[int]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT id FROM entries WHERE id IN ({marks}) AND embedding IS NULL",
            [int(i) for i in ids],
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def set_embedding(self, entry_id: int, vec: Sequence[float]) -> None:
        if not vec:
            return
        self._conn.execute(
            "UPDATE entries SET embedding = ? WHERE id = ?",
            (_pack_vector(vec), int(entry_id)),
        )
        self._conn.commit()

    def embedding_scores(
        self,
        ids: Sequence[int],
        query_embedding: Sequence[float],
    ) -> Dict[int, float]:
        """Cosine similarity per id for rows in *ids* that have embeddings."""
        if not ids:
            return {}
        q = np.asarray(query_embedding, dtype=np.float32)
        if q.ndim != 1 or q.size == 0:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT id, embedding FROM entries "
            f"WHERE id IN ({marks}) AND embedding IS NOT NULL",
            [int(i) for i in ids],
        ).fetchall()
        kept_ids: List[int] = []
        vectors: List[np.ndarray] = []
        for r in rows:
            v = _unpack_vector(r["embedding"])
            if v.shape[0] != q.shape[0]:
                continue  # embedding model changed — skip, don't crash
            kept_ids.append(int(r["id"]))
            vectors.append(v)
        if not vectors:
            return {}
        M = np.stack(vectors, axis=0)
        q_norm = q / (np.linalg.norm(q) + 1e-12)
        M_norm = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-12)
        scores = M_norm @ q_norm
        return {i: float(s) for i, s in zip(kept_ids, scores)}

    # -- maintenance ---------------------------------------------------------

    def stored_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(digest_blob)) + SUM(LENGTH(COALESCE(embedding, x''))), 0) AS b "
            "FROM entries"
        ).fetchone()
        return int(row["b"] or 0)

    def prune_to_cap(self, max_bytes: int = DEFAULT_MAX_DB_BYTES) -> int:
        """Evict oldest entries until stored blob bytes fit *max_bytes*.

        Returns the number of rows deleted. Runs VACUUM after a real eviction
        so the file actually shrinks.
        """
        if max_bytes <= 0:
            return 0
        deleted = 0
        while self.stored_bytes() > max_bytes:
            # Small batches so we stop as soon as we're under the cap instead
            # of overshooting and evicting recent entries unnecessarily.
            rows = self._conn.execute(
                "SELECT id FROM entries ORDER BY ts ASC LIMIT 10"
            ).fetchall()
            if not rows:
                break
            ids = [int(r["id"]) for r in rows]
            marks = ",".join("?" for _ in ids)
            self._conn.execute(f"DELETE FROM entries WHERE id IN ({marks})", ids)
            self._conn.commit()
            deleted += len(ids)
        if deleted:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.Error:
                pass
        return deleted

    # -- stats ----------------------------------------------------------------

    def stats(self) -> Dict[str, object]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(redactions), 0) AS r, "
            "MIN(ts) AS oldest, MAX(ts) AS newest FROM entries"
        ).fetchone()
        emb = self._conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE embedding IS NOT NULL"
        ).fetchone()
        return {
            "entries": int(row["n"] or 0),
            "redactions": int(row["r"] or 0),
            "embedded": int(emb["n"] or 0),
            "stored_bytes": self.stored_bytes(),
            "oldest_ts": row["oldest"],
            "newest_ts": row["newest"],
            "db_path": str(self.db_path),
        }

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()
        return int(row["n"] or 0) == 0

    def clear(self) -> None:
        self._conn.execute("DELETE FROM entries")
        self._conn.commit()
        try:
            self._conn.execute("VACUUM")
        except sqlite3.Error:
            pass


def embedding_text_for_entry(entry: ArchiveEntry) -> str:
    """Text embedded for one archive entry: the command plus an output snippet.

    Capped well under embedding-model context limits; the search_text snippet
    is already scrubbed and lowercased.
    """
    return f"$ {entry.command}\n{entry.search_text[:1200]}"

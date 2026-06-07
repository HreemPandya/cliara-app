"""
RAG over your codebase.

Indexes every git-tracked file into a local SQLite vector store and answers
questions like ``? how does auth work`` with ``path:line`` citations.

Design notes
------------
* **Store**: one SQLite database per repository under
  ``~/.cliara/codebase_index/<repo-hash>.db``. Embeddings are stored as raw
  float32 BLOBs; similarity search loads them into a single NumPy matrix and
  does a batched cosine — the same approach Cliara already uses for semantic
  history (no heavy native deps like sqlite-vss/chroma, which are painful to
  install on Windows). For codebases of a few thousand chunks this is fast.
* **Source of truth**: ``git ls-files`` — only tracked files are indexed, so
  build artifacts, ``node_modules``, secrets in ``.env`` (gitignored), etc. are
  excluded automatically.
* **Chunking**: line windows with overlap so every hit maps back to a concrete
  ``path:start-end`` range for citation.
* **Incremental**: each file's content hash is stored; reindexing only
  re-embeds files that changed and drops files that were deleted/untracked.

This module is intentionally free of any UI/console code so it can be unit
tested in isolation. The shell wiring lives in
``cliara/shell_app/codebase_commands.py``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np


# Schema version — bump when the table layout changes so stale DBs are rebuilt.
SCHEMA_VERSION = 1

# Files larger than this are skipped (likely data/minified/vendored).
DEFAULT_MAX_FILE_BYTES = 256 * 1024

# Chunking defaults (lines).
DEFAULT_CHUNK_LINES = 40
DEFAULT_CHUNK_OVERLAP = 10

# Extensions we never try to index (binary / non-source). git ls-files can list
# these when they're tracked (images, fonts, archives, ...).
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".webm",
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".class", ".jar", ".wasm",
    ".db", ".sqlite", ".sqlite3", ".lock", ".pdf",
    ".parquet", ".npy", ".npz", ".pkl", ".pickle",
})


@dataclass
class Chunk:
    """A contiguous slice of a file, addressable as ``path:start-end``."""

    path: str          # repo-relative, forward-slash normalized
    start_line: int    # 1-based, inclusive
    end_line: int      # 1-based, inclusive
    content: str

    def citation(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.path}:{self.start_line}"
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass
class SearchHit:
    """A retrieved chunk plus its cosine similarity to the query."""

    path: str
    start_line: int
    end_line: int
    content: str
    score: float

    def citation(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.path}:{self.start_line}"
        return f"{self.path}:{self.start_line}-{self.end_line}"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_repo_root(cwd: Optional[str] = None) -> Optional[Path]:
    """Return the git work-tree root for *cwd*, or None if not a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
        return Path(out)


def list_tracked_files(repo_root: Path) -> List[str]:
    """Return repo-relative paths of tracked files (``git ls-files``).

    Paths use forward slashes (git's native form) so citations are stable
    across platforms.
    """
    try:
        r = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    raw = r.stdout or ""
    # -z gives NUL-separated entries (handles spaces/newlines in names).
    return [p for p in raw.split("\0") if p]


def repo_db_path(repo_root: Path, base_dir: Optional[Path] = None) -> Path:
    """Return the SQLite path for *repo_root*'s index.

    Uses a hash of the absolute path so two repos with the same basename don't
    collide, and the user's filesystem layout doesn't leak into the filename.
    """
    base = base_dir or (Path.home() / ".cliara" / "codebase_index")
    digest = hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()[:16]
    name = repo_root.name or "repo"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:32]
    return base / f"{safe}-{digest}.db"


# ---------------------------------------------------------------------------
# File reading + chunking
# ---------------------------------------------------------------------------

def _looks_binary_sample(sample: bytes) -> bool:
    """Heuristic: NUL byte or a high ratio of non-text bytes => binary."""
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    # Count bytes that aren't typical text (tab/newline/cr + printable range).
    text_bytes = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D, 0x0C, 0x08}
    nontext = sum(1 for b in sample if b not in text_bytes)
    return (nontext / len(sample)) > 0.30


def read_text_file(
    abs_path: Path,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Optional[str]:
    """Read *abs_path* as UTF-8 text, or return None if binary/too large/unreadable."""
    try:
        size = abs_path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    if abs_path.suffix.lower() in _BINARY_SUFFIXES:
        return None
    try:
        data = abs_path.read_bytes()
    except OSError:
        return None
    if _looks_binary_sample(data[:1024]):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except UnicodeDecodeError:
            return None


def content_hash(text: str) -> str:
    """Stable content fingerprint used for incremental reindexing."""
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def chunk_text(
    path: str,
    text: str,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Chunk]:
    """Split *text* into overlapping line-window chunks.

    Blank/whitespace-only chunks are dropped. Line numbers are 1-based and
    inclusive so a hit cites the exact range a reader can open.
    """
    if not text.strip():
        return []
    chunk_lines = max(1, int(chunk_lines))
    overlap = max(0, min(int(overlap), chunk_lines - 1))
    step = chunk_lines - overlap

    lines = text.splitlines()
    n = len(lines)
    chunks: List[Chunk] = []
    start = 0
    while start < n:
        end = min(start + chunk_lines, n)
        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append(
                Chunk(
                    path=path,
                    start_line=start + 1,
                    end_line=end,
                    content=body,
                )
            )
        if end >= n:
            break
        start += step
    return chunks


def embedding_text_for_chunk(chunk: Chunk) -> str:
    """The text we actually embed: a path header + the code.

    Prefixing the path helps retrieval connect natural-language file/module
    references ("the auth module") to the right chunk.
    """
    return f"# {chunk.path} (lines {chunk.start_line}-{chunk.end_line})\n{chunk.content}"


# ---------------------------------------------------------------------------
# Vector (de)serialization
# ---------------------------------------------------------------------------

def _pack_vector(vec: Sequence[float]) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def _unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class CodebaseRAGStore:
    """SQLite-backed vector store for one repository's tracked files."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
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
            CREATE TABLE IF NOT EXISTS chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT NOT NULL,
                start_line  INTEGER NOT NULL,
                end_line    INTEGER NOT NULL,
                content     TEXT NOT NULL,
                embedding   BLOB NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path          TEXT PRIMARY KEY,
                content_hash  TEXT NOT NULL,
                chunk_count   INTEGER NOT NULL,
                indexed_at    REAL NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
        self._conn.commit()

        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif existing != str(SCHEMA_VERSION):
            # Layout changed under us — wipe rows but keep the file.
            self.clear()
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # -- meta --------------------------------------------------------------

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

    # -- file/chunk maintenance -------------------------------------------

    def get_indexed_files(self) -> Dict[str, str]:
        """Return ``{path: content_hash}`` for every indexed file."""
        rows = self._conn.execute("SELECT path, content_hash FROM files").fetchall()
        return {r["path"]: r["content_hash"] for r in rows}

    def remove_file(self, path: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    def replace_file(
        self,
        path: str,
        file_hash: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
    ) -> None:
        """Replace all chunks for *path* with new (chunk, embedding) pairs.

        ``chunks`` and ``embeddings`` must be the same length and aligned.
        """
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        cur = self._conn.cursor()
        cur.execute("DELETE FROM chunks WHERE path = ?", (path,))
        cur.executemany(
            "INSERT INTO chunks(path, start_line, end_line, content, embedding) "
            "VALUES(?, ?, ?, ?, ?)",
            [
                (
                    c.path,
                    c.start_line,
                    c.end_line,
                    c.content,
                    _pack_vector(emb),
                )
                for c, emb in zip(chunks, embeddings)
            ],
        )
        cur.execute(
            "INSERT INTO files(path, content_hash, chunk_count, indexed_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "content_hash = excluded.content_hash, "
            "chunk_count = excluded.chunk_count, "
            "indexed_at = excluded.indexed_at",
            (path, file_hash, len(chunks), time.time()),
        )
        self._conn.commit()

    def clear(self) -> None:
        """Drop all indexed content (keeps schema + schema_version)."""
        self._conn.execute("DELETE FROM chunks")
        self._conn.execute("DELETE FROM files")
        self._conn.commit()

    # -- stats -------------------------------------------------------------

    def stats(self) -> Dict[str, object]:
        files = self._conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
        chunks = self._conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
        return {
            "files": int(files),
            "chunks": int(chunks),
            "embed_model": self.get_meta("embed_model") or "",
            "embed_dim": self.get_meta("embed_dim") or "",
            "indexed_at": self.get_meta("indexed_at") or "",
            "repo_root": self.get_meta("repo_root") or "",
            "db_path": str(self.db_path),
        }

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()
        return int(row["c"]) == 0

    # -- search ------------------------------------------------------------

    def search(
        self,
        query_embedding: Sequence[float],
        top_k: int = 8,
        min_score: float = 0.0,
    ) -> List[SearchHit]:
        """Return the *top_k* chunks most similar to *query_embedding*.

        Cosine similarity over all stored vectors (batched in NumPy). Hits
        below *min_score* are dropped.
        """
        q = np.asarray(query_embedding, dtype=np.float32)
        if q.ndim != 1 or q.size == 0:
            return []

        rows = self._conn.execute(
            "SELECT path, start_line, end_line, content, embedding FROM chunks"
        ).fetchall()
        if not rows:
            return []

        vectors: List[np.ndarray] = []
        keep: List[sqlite3.Row] = []
        for r in rows:
            v = _unpack_vector(r["embedding"])
            if v.shape[0] != q.shape[0]:
                # Dimension mismatch (model changed) — skip rather than crash.
                continue
            vectors.append(v)
            keep.append(r)
        if not vectors:
            return []

        M = np.stack(vectors, axis=0)
        q_norm = q / (np.linalg.norm(q) + 1e-12)
        row_norms = np.linalg.norm(M, axis=1, keepdims=True)
        M_norm = M / (row_norms + 1e-12)
        scores = M_norm @ q_norm

        top_k = max(1, int(top_k))
        order = np.argsort(-scores)
        hits: List[SearchHit] = []
        for rank in range(min(top_k, order.size)):
            i = int(order[rank])
            score = float(scores[i])
            if score < min_score:
                break
            r = keep[i]
            hits.append(
                SearchHit(
                    path=r["path"],
                    start_line=int(r["start_line"]),
                    end_line=int(r["end_line"]),
                    content=r["content"],
                    score=score,
                )
            )
        return hits


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

@dataclass
class IndexResult:
    """Summary of an index/reindex run."""

    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    chunks_indexed: int = 0
    embed_failures: int = 0
    aborted_reason: Optional[str] = None


# An embed function takes a list of texts and returns a list of vectors
# (or None for an individual failure). Mirrors NLHandler.get_embeddings_batch.
EmbedBatchFn = Callable[[List[str]], List[Optional[List[float]]]]

# Progress callback: (phase, current, total, detail).
ProgressFn = Callable[[str, int, int, str], None]


def index_repository(
    store: CodebaseRAGStore,
    repo_root: Path,
    embed_batch: EmbedBatchFn,
    embed_model: str,
    *,
    force: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    embed_batch_size: int = 64,
    progress: Optional[ProgressFn] = None,
) -> IndexResult:
    """Build or refresh the index for *repo_root*.

    Incremental by default: only files whose content hash changed are
    re-embedded, and files that are no longer tracked are dropped. Pass
    ``force=True`` to re-embed everything (e.g. after switching embed models).
    """
    result = IndexResult()

    # If the embedding model changed, a partial/incremental update would mix
    # incompatible vector spaces — force a full rebuild.
    prev_model = store.get_meta("embed_model")
    if prev_model and prev_model != embed_model:
        force = True
    if force:
        store.clear()

    tracked = list_tracked_files(repo_root)
    if not tracked:
        result.aborted_reason = "no tracked files (is this a git repo with commits?)"
        return result

    tracked_set = set(tracked)
    indexed = {} if force else store.get_indexed_files()

    # Drop files that are no longer tracked.
    for old_path in list(indexed.keys()):
        if old_path not in tracked_set:
            store.remove_file(old_path)
            result.files_removed += 1
            indexed.pop(old_path, None)

    def _emit(phase: str, cur: int, total: int, detail: str = "") -> None:
        if progress is not None:
            try:
                progress(phase, cur, total, detail)
            except Exception:
                pass

    # Decide which files need (re)indexing.
    pending: List[Tuple[str, str, List[Chunk]]] = []  # (path, hash, chunks)
    total = len(tracked)
    for i, rel in enumerate(tracked, 1):
        _emit("scan", i, total, rel)
        abs_path = repo_root / rel
        text = read_text_file(abs_path, max_bytes=max_file_bytes)
        if text is None:
            # Unreadable/binary/too-large: ensure any stale rows are gone.
            if rel in indexed:
                store.remove_file(rel)
            result.files_skipped += 1
            continue
        h = content_hash(text)
        if not force and indexed.get(rel) == h:
            result.files_unchanged += 1
            continue
        chunks = chunk_text(rel, text, chunk_lines=chunk_lines, overlap=chunk_overlap)
        if not chunks:
            if rel in indexed:
                store.remove_file(rel)
            result.files_skipped += 1
            continue
        pending.append((rel, h, chunks))

    # Embed + store, batching embedding calls across files for efficiency.
    # We accumulate chunks into a flat buffer, flush when it reaches the batch
    # size, then write completed files to the store.
    buffer_texts: List[str] = []
    buffer_index: List[Tuple[int, int]] = []  # (file_idx_in_pending, chunk_idx)
    file_embeddings: Dict[int, List[Optional[List[float]]]] = {
        fi: [None] * len(chunks) for fi, (_, _, chunks) in enumerate(pending)
    }
    done_files = 0

    def _flush() -> None:
        nonlocal buffer_texts, buffer_index
        if not buffer_texts:
            return
        vectors = embed_batch(buffer_texts)
        for (fi, ci), vec in zip(buffer_index, vectors):
            file_embeddings[fi][ci] = vec
        buffer_texts = []
        buffer_index = []

    for fi, (_, _, chunks) in enumerate(pending):
        for ci, ch in enumerate(chunks):
            buffer_texts.append(embedding_text_for_chunk(ch))
            buffer_index.append((fi, ci))
            if len(buffer_texts) >= max(1, int(embed_batch_size)):
                _flush()
        _emit("embed", fi + 1, len(pending), pending[fi][0])
    _flush()

    # Persist each fully-embedded file. Skip chunks whose embedding failed so a
    # transient API error doesn't poison the whole file; if *all* embeddings for
    # a file failed, leave the file unindexed (counts as a failure).
    for fi, (rel, h, chunks) in enumerate(pending):
        embs = file_embeddings[fi]
        good_chunks: List[Chunk] = []
        good_embs: List[List[float]] = []
        for ch, emb in zip(chunks, embs):
            if emb is None:
                result.embed_failures += 1
                continue
            good_chunks.append(ch)
            good_embs.append(list(emb))
        if not good_chunks:
            continue
        store.replace_file(rel, h, good_chunks, good_embs)
        result.files_indexed += 1
        result.chunks_indexed += len(good_chunks)
        done_files += 1

    # Record run metadata (use the dimension of the first stored vector).
    store.set_meta("embed_model", embed_model)
    store.set_meta("repo_root", str(repo_root))
    store.set_meta("indexed_at", _now_iso())
    if pending:
        for embs in file_embeddings.values():
            dim = next((len(e) for e in embs if e), None)
            if dim:
                store.set_meta("embed_dim", str(dim))
                break

    return result


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

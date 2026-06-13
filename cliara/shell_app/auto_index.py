"""Self-maintaining codebase index — the shell side ("Index Sentinel").

Keeps the codebase RAG index fresh **automatically** so the user never has to
run ``index rebuild`` by hand. After each command, a cheap fingerprint check
decides whether the working tree drifted from what was indexed; if so, a
background daemon re-runs the (already incremental) ``index_repository`` pass.

The actual *decision* logic lives in :mod:`cliara.auto_indexer` (pure, tested
in isolation). This mixin owns the threading, the per-command hook, and the
``index auto`` control surface. Mixed into
:class:`cliara.shell_app.orchestrator.CliaraShell`, alongside
:class:`cliara.shell_app.codebase_commands.CodebaseCommandMixin` (whose
``_get_codebase_store`` / ``has_codebase_index`` / ``_cb_cfg_int`` it reuses).
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Optional

from cliara.shell_app.runtime import (
    _cliara_console,
    _ui_accent_style,
    print_dim,
    print_info,
    print_success,
    print_warning,
)


class AutoIndexMixin:
    """Background freshness daemon for the codebase index."""

    # Lazily-initialised so the thread only spins up inside a real git repo with
    # auto-indexing enabled — never for one-shot / non-repo sessions.
    _auto_index_started: bool = False
    _auto_index_sentinel = None
    _auto_index_queue: "Optional[queue.Queue]" = None
    _auto_index_thread: "Optional[threading.Thread]" = None
    _auto_index_last_check: float = float("-inf")  # monotonic; throttles cheap checks
    _auto_index_busy: bool = False
    _auto_index_last_result = None  # last IndexResult, for `index auto status`

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _auto_index_enabled(self) -> bool:
        return bool(self.config.get("codebase_auto_index", True)) and bool(
            self.config.get("codebase_rag_enabled", True)
        )

    def _auto_index_bootstrap(self) -> bool:
        return bool(self.config.get("codebase_auto_index_bootstrap", False))

    # ------------------------------------------------------------------
    # The per-command hot path (called from the execution engine's finally)
    # ------------------------------------------------------------------

    def _auto_index_tick(self, command: str = "") -> None:
        """Cheap, silent, called after every command. Never raises out."""
        from cliara import auto_indexer

        try:
            if not self._auto_index_enabled():
                return

            # Cheap repo gate: reuse the 15 s-cached git context so we don't
            # shell out on every keystroke. No repo → nothing to maintain.
            gctx = self._get_quick_git_context()
            if not gctx.get("git_repo"):
                return

            git_event = auto_indexer.is_index_disrupting_command(command)
            now = time.monotonic()
            if not git_event and (now - self._auto_index_last_check) < auto_indexer.DEFAULT_CHECK_INTERVAL_S:
                return
            self._auto_index_last_check = now

            self._ensure_auto_index_worker()
            self._auto_index_consider(git_event)
        except Exception:
            # Freshness maintenance must never disrupt the prompt.
            pass

    def _ensure_auto_index_worker(self) -> None:
        from cliara import auto_indexer

        if self._auto_index_started:
            return
        self._auto_index_started = True
        min_interval = self._cb_cfg_float(
            "codebase_auto_index_min_interval_s", auto_indexer.DEFAULT_MIN_INTERVAL_S
        )
        self._auto_index_sentinel = auto_indexer.IndexSentinel(min_interval_s=min_interval)
        self._auto_index_queue = queue.Queue(maxsize=1)
        self._auto_index_thread = threading.Thread(
            target=self._auto_index_worker, daemon=True, name="cliara-index-sentinel"
        )
        self._auto_index_thread.start()

    def _auto_index_consider(self, git_event: bool) -> None:
        """Compute the fingerprint and enqueue a pass if the sentinel says so."""
        from cliara import auto_indexer, codebase_rag

        repo_root = codebase_rag.get_repo_root(str(Path.cwd()))
        if repo_root is None:
            return

        fp = auto_indexer.compute_fingerprint(repo_root)
        index_exists = False
        try:
            index_exists = self.has_codebase_index()
        except Exception:
            index_exists = False

        decision = self._auto_index_sentinel.should_reindex(
            time.monotonic(),
            fp,
            git_event=git_event,
            index_exists=index_exists,
            bootstrap=self._auto_index_bootstrap(),
        )
        if not decision.do:
            return

        # Only commit to a pass if we can actually embed — otherwise we'd churn
        # and rack up failures. (Checked here, off the per-tick fast path.)
        try:
            if not self.nl_handler.supports_embedding_api():
                return
        except Exception:
            return

        self._auto_index_enqueue(str(repo_root))

    def _auto_index_enqueue(self, repo_root: str) -> None:
        q = self._auto_index_queue
        if q is None:
            return
        try:
            q.put_nowait(repo_root)
        except queue.Full:
            # A pass is already pending; it'll pick up the latest state.
            pass

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _auto_index_worker(self) -> None:
        from cliara import auto_indexer, codebase_rag

        q = self._auto_index_queue
        if q is None:
            return
        while True:
            token = q.get()
            try:
                if token is None:
                    break
                self._auto_index_run_pass(Path(token))
            except Exception:
                # Self-healing: a failed pass just feeds the backoff window.
                try:
                    self._auto_index_sentinel.note_failure(time.monotonic())
                except Exception:
                    pass
            finally:
                try:
                    q.task_done()
                except Exception:
                    pass

    def _auto_index_run_pass(self, repo_root: Path) -> None:
        """One incremental reindex. Opens its OWN sqlite handle (worker thread).

        ``CodebaseRAGStore`` uses a thread-bound sqlite connection, so the
        cached main-thread store can't be reused here — we open and close a
        short-lived connection scoped to this pass.
        """
        from cliara import auto_indexer, codebase_rag

        self._auto_index_busy = True
        self._auto_index_sentinel.note_attempt(time.monotonic())
        store = None
        try:
            embed_model = self.nl_handler.embedding_model_id()
            if not embed_model:
                self._auto_index_sentinel.note_failure(time.monotonic())
                return

            db_path = codebase_rag.repo_db_path(repo_root)
            store = codebase_rag.CodebaseRAGStore(db_path)

            max_kb = self._cb_cfg_int("codebase_rag_max_file_kb", 256)
            chunk_lines = self._cb_cfg_int("codebase_rag_chunk_lines", 40)
            overlap = self._cb_cfg_int("codebase_rag_chunk_overlap", 10)
            batch_size = self._cb_cfg_int("codebase_rag_embed_batch", 64)

            result = codebase_rag.index_repository(
                store,
                repo_root,
                embed_batch=self.nl_handler.get_embeddings_batch,
                embed_model=embed_model,
                force=False,  # always incremental — the cheap, fast path
                max_file_bytes=max_kb * 1024,
                chunk_lines=chunk_lines,
                chunk_overlap=overlap,
                embed_batch_size=batch_size,
            )

            if result.aborted_reason:
                self._auto_index_sentinel.note_failure(time.monotonic())
                return

            self._auto_index_last_result = result
            # Record the fingerprint observed *after* indexing so edits made
            # mid-pass are caught on the next tick (convergence).
            post_fp = auto_indexer.compute_fingerprint(repo_root)
            self._auto_index_sentinel.note_success(time.monotonic(), post_fp)

            # Drop the main-thread cached store so the next question re-opens
            # and sees freshly-written rows.
            self._invalidate_codebase_store_cache(repo_root)

            if self.config.get("codebase_auto_index_notify", False):
                touched = result.files_indexed + result.files_removed
                if touched:
                    print_dim(
                        f"  [index] auto-updated: {result.files_indexed} changed, "
                        f"{result.files_removed} removed"
                    )
        finally:
            self._auto_index_busy = False
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    def _invalidate_codebase_store_cache(self, repo_root: Path) -> None:
        """Flag the main-thread store cache stale so the next question reopens.

        We must NOT touch the cached sqlite connection here: it was opened on
        the main thread and sqlite3 forbids cross-thread handle use. Instead we
        set a flag that ``CodebaseCommandMixin._get_codebase_store`` honours on
        the main thread (where closing/reopening is safe).
        """
        self._codebase_store_dirty_root = str(repo_root)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown_auto_index(self) -> None:
        """Signal the worker to stop and wait briefly. Best-effort."""
        q = self._auto_index_queue
        if q is not None:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        t = self._auto_index_thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # `index auto [status|on|off]` control surface
    # ------------------------------------------------------------------

    def handle_index_auto(self, args: str = "") -> None:
        sub = (args or "").strip().lower()
        if sub in ("", "status", "info"):
            self._auto_index_status()
            return
        if sub in ("on", "enable", "true"):
            self.config.set("codebase_auto_index", True)
            self.config.save()
            print_success("  [index] auto-maintenance enabled — the index now keeps itself fresh.")
            return
        if sub in ("off", "disable", "false"):
            self.config.set("codebase_auto_index", False)
            self.config.save()
            print_warning("  [index] auto-maintenance disabled — run `index` manually to refresh.")
            return
        if sub in ("bootstrap", "bootstrap on", "bootstrap-on"):
            self.config.set("codebase_auto_index", True)
            self.config.set("codebase_auto_index_bootstrap", True)
            self.config.save()
            print_success("  [index] auto-bootstrap enabled — Cliara will build the index on its own.")
            return
        print_warning(f"  [index] unknown auto subcommand: {sub}")
        print_dim("  Usage: index auto [status | on | off | bootstrap]")

    def _auto_index_status(self) -> None:
        from rich.panel import Panel
        from rich.text import Text

        accent = _ui_accent_style()
        enabled = self._auto_index_enabled()
        bootstrap = self._auto_index_bootstrap()

        lines = [
            f"Auto-maintenance: {'on' if enabled else 'off'}",
            f"Auto-bootstrap:   {'on' if bootstrap else 'off'} (build index from scratch)",
        ]
        sentinel = self._auto_index_sentinel
        if sentinel is not None:
            if sentinel.last_success > float("-inf"):
                ago = max(0.0, time.monotonic() - sentinel.last_success)
                lines.append(f"Last auto-update: {self._auto_index_fmt_ago(ago)} ago")
            else:
                lines.append("Last auto-update: not yet this session")
            if sentinel.consecutive_failures:
                lines.append(f"Recent failures:  {sentinel.consecutive_failures} (backing off)")
            if self._auto_index_busy:
                lines.append("Status:           updating now…")
        else:
            lines.append("Status:           idle (no changes seen yet this session)")

        res = self._auto_index_last_result
        if res is not None:
            lines.append(
                f"Last pass:        {res.files_indexed} changed · "
                f"{res.files_removed} removed · {res.files_unchanged} unchanged"
            )

        _cliara_console().print(
            Panel(
                "\n".join(lines),
                title=Text("Index Sentinel", style=accent),
                subtitle=Text("keeps the codebase index fresh automatically", style="dim"),
                border_style=accent,
                padding=(0, 1),
            )
        )
        if not enabled:
            print_dim("  Enable with:  index auto on")
        elif not bootstrap and not self.has_codebase_index():
            print_dim("  No index yet. Run `index` once, or `index auto bootstrap` to build it automatically.")

    @staticmethod
    def _auto_index_fmt_ago(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"

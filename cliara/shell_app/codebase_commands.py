"""Codebase RAG commands for Cliara.

Implements ``index`` (build/refresh/status/clear) and the question-answering
side (``ask <q>`` / ``? how does auth work``) on top of
:mod:`cliara.codebase_rag`.

Mixed into :class:`cliara.shell_app.orchestrator.CliaraShell`.
"""

from pathlib import Path
from typing import List, Optional

from cliara.shell_app.runtime import (
    _cliara_console,
    _ui_accent_style,
    print_dim,
    print_error,
    print_info,
    print_success,
    print_warning,
    thinking_status,
)


class CodebaseCommandMixin:
    """``index`` + codebase Q&A (RAG over git-tracked files)."""

    # Lazily-created store for the current repo (keyed by repo root).
    _codebase_store = None
    _codebase_store_root: Optional[str] = None
    # Set by the Index Sentinel's worker thread after an auto-reindex so the
    # next main-thread access drops the stale cached connection and reopens.
    _codebase_store_dirty_root: Optional[str] = None

    # -- config helpers ----------------------------------------------------

    def _cb_cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cb_cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    # -- store access ------------------------------------------------------

    def _get_codebase_store(self, create: bool = True):
        """Return (store, repo_root) for the current directory, or (None, None).

        Caches the open store per repo root so repeated questions don't reopen
        the SQLite file.
        """
        from cliara import codebase_rag

        repo_root = codebase_rag.get_repo_root(str(Path.cwd()))
        if repo_root is None:
            return None, None

        root_key = str(repo_root)

        # The auto-index worker may have rewritten this repo's DB on another
        # thread. If so, drop our cached (now-stale) connection here on the
        # main thread, where closing it is safe.
        if self._codebase_store_dirty_root == root_key:
            self._codebase_store_dirty_root = None
            if self._codebase_store is not None and self._codebase_store_root == root_key:
                try:
                    self._codebase_store.close()
                except Exception:
                    pass
                self._codebase_store = None
                self._codebase_store_root = None

        if (
            self._codebase_store is not None
            and self._codebase_store_root == root_key
        ):
            return self._codebase_store, repo_root

        # Switched repos — close the old handle.
        if self._codebase_store is not None:
            try:
                self._codebase_store.close()
            except Exception:
                pass
            self._codebase_store = None
            self._codebase_store_root = None

        db_path = codebase_rag.repo_db_path(repo_root)
        if not create and not db_path.exists():
            return None, repo_root

        try:
            store = codebase_rag.CodebaseRAGStore(db_path)
        except Exception as e:
            print_error(f"[Error] Could not open codebase index: {e}")
            return None, repo_root
        self._codebase_store = store
        self._codebase_store_root = root_key
        return store, repo_root

    def has_codebase_index(self) -> bool:
        """True when a non-empty codebase index exists for the current repo."""
        if not self.config.get("codebase_rag_enabled", True):
            return False
        store, _ = self._get_codebase_store(create=False)
        if store is None:
            return False
        try:
            return not store.is_empty()
        except Exception:
            return False

    # -- index command -----------------------------------------------------

    def handle_codebase_index(self, args: str = "") -> None:
        """``index [refresh|rebuild|status|clear|auto]`` — manage the codebase index."""
        sub = (args or "").strip().lower()

        # Self-maintenance control surface (handled by AutoIndexMixin).
        if sub == "auto" or sub.startswith("auto "):
            rest = (args or "").strip()[4:].strip()
            self.handle_index_auto(rest)
            return
        if sub in ("status", "info", "stats"):
            self._codebase_index_status()
            return
        if sub in ("clear", "reset", "delete"):
            self._codebase_index_clear()
            return

        force = sub in ("rebuild", "force", "full", "--force")
        if sub and sub not in ("refresh", "update", "build", "rebuild", "force", "full", "--force"):
            print_error(f"[Error] Unknown index subcommand: {sub}")
            print_dim("Usage: index [refresh | rebuild | status | clear | auto]")
            return
        self._codebase_index_build(force=force)

    def _codebase_index_build(self, force: bool = False) -> None:
        from cliara import codebase_rag

        if not self.config.get("codebase_rag_enabled", True):
            print_warning("[Codebase RAG is disabled — set codebase_rag_enabled true to use it.]")
            return

        repo_root = codebase_rag.get_repo_root(str(Path.cwd()))
        if repo_root is None:
            print_error("[Error] Not inside a git repository — codebase indexing needs git-tracked files.")
            return

        if not self.nl_handler.supports_embedding_api():
            print_error("[Error] No embedding backend available.")
            print_dim("  Set OPENAI_API_KEY, or run Ollama with an embedding model (e.g. `ollama pull nomic-embed-text`).")
            print_dim("  Then run: setup-llm")
            return

        embed_model = self.nl_handler.embedding_model_id() or "unknown"

        store, _ = self._get_codebase_store(create=True)
        if store is None:
            return

        max_kb = self._cb_cfg_int("codebase_rag_max_file_kb", 256)
        chunk_lines = self._cb_cfg_int("codebase_rag_chunk_lines", 40)
        overlap = self._cb_cfg_int("codebase_rag_chunk_overlap", 10)
        batch_size = self._cb_cfg_int("codebase_rag_embed_batch", 64)

        print_info(f"\n[Indexing codebase] {repo_root}")
        print_dim(f"  embeddings: {embed_model}  •  {'full rebuild' if force else 'incremental'}")

        label = "scanning files"
        with thinking_status(label) as status:
            def _progress(phase: str, cur: int, total: int, detail: str) -> None:
                if phase == "scan":
                    status.update(f"[dim]scanning {cur}/{total}[/dim]")
                elif phase == "embed":
                    status.update(f"[dim]embedding {cur}/{total} files[/dim]")

            result = codebase_rag.index_repository(
                store,
                repo_root,
                embed_batch=self.nl_handler.get_embeddings_batch,
                embed_model=embed_model,
                force=force,
                max_file_bytes=max_kb * 1024,
                chunk_lines=chunk_lines,
                chunk_overlap=overlap,
                embed_batch_size=batch_size,
                progress=_progress,
            )

        if result.aborted_reason:
            print_error(f"[Error] Indexing aborted: {result.aborted_reason}")
            return

        print_success(
            f"[{self._ok_icon()}] Indexed {result.files_indexed} file(s), "
            f"{result.chunks_indexed} chunk(s)."
        )
        details: List[str] = []
        if result.files_unchanged:
            details.append(f"{result.files_unchanged} unchanged")
        if result.files_skipped:
            details.append(f"{result.files_skipped} skipped (binary/large)")
        if result.files_removed:
            details.append(f"{result.files_removed} removed")
        if result.embed_failures:
            details.append(f"{result.embed_failures} chunk embed failures")
        if details:
            print_dim("  " + ", ".join(details))
        print_dim('  Ask away:  ? how does <X> work   (or:  ask <question>)')

    def _codebase_index_status(self) -> None:
        store, repo_root = self._get_codebase_store(create=False)
        if store is None:
            if repo_root is None:
                print_dim("Not inside a git repository — nothing to index.")
            else:
                print_dim("No codebase index yet. Run `index` to build one.")
            return
        stats = store.stats()
        from rich.panel import Panel
        from rich.text import Text

        accent = _ui_accent_style()
        auto_on = False
        try:
            auto_on = self._auto_index_enabled()
        except Exception:
            auto_on = False
        lines = [
            f"Repo:       {stats.get('repo_root', '')}",
            f"Files:      {stats.get('files', 0)}",
            f"Chunks:     {stats.get('chunks', 0)}",
            f"Embeddings: {stats.get('embed_model', '')} (dim {stats.get('embed_dim', '?')})",
            f"Indexed:    {stats.get('indexed_at', '') or 'never'}",
            f"Auto:       {'on — self-maintaining' if auto_on else 'off (index auto on)'}",
            f"Store:      {stats.get('db_path', '')}",
        ]
        _cliara_console().print(
            Panel(
                "\n".join(lines),
                title=Text("Codebase Index", style=accent),
                border_style=accent,
                padding=(0, 1),
            )
        )
        if int(stats.get("chunks", 0) or 0) == 0:
            print_dim("Index is empty. Run `index` to build it.")

    def _codebase_index_clear(self) -> None:
        store, repo_root = self._get_codebase_store(create=False)
        if store is None:
            print_dim("No codebase index to clear.")
            return
        try:
            store.clear()
            print_success(f"[{self._ok_icon()}] Codebase index cleared.")
        except Exception as e:
            print_error(f"[Error] Could not clear index: {e}")

    # -- question answering ------------------------------------------------

    def handle_codebase_question(self, question: str) -> None:
        """Answer *question* using RAG over the codebase, with file:line citations.

        Used both by the explicit ``ask`` command and by ``?`` routing when a
        codebase-style question is detected and an index exists.
        """
        from cliara import codebase_rag

        q = (question or "").strip()
        if not q:
            print_error("[Error] Ask a question, e.g.  ask how does auth work")
            return

        if not self.config.get("codebase_rag_enabled", True):
            print_warning("[Codebase RAG is disabled — set codebase_rag_enabled true to use it.]")
            return

        store, repo_root = self._get_codebase_store(create=False)
        if store is None:
            if repo_root is None:
                print_error("[Error] Not inside a git repository.")
            else:
                print_warning("[No codebase index yet — run `index` first.]")
            return
        if store.is_empty():
            print_warning("[Codebase index is empty — run `index` to build it.]")
            return

        if not self.nl_handler.supports_embedding_api():
            print_error("[Error] No embedding backend available to search the index. Run setup-llm.")
            return
        if not self.nl_handler.llm_enabled:
            print_error("[Error] LLM not configured. Run setup-llm to answer codebase questions.")
            return

        top_k = max(1, min(self._cb_cfg_int("codebase_rag_top_k", 8), 50))
        min_score = max(0.0, min(self._cb_cfg_float("codebase_rag_min_score", 0.15), 1.0))

        ql = q if len(q) <= 48 else (q[:45] + "...")
        with thinking_status(ql) as status:
            query_emb = self.nl_handler.get_embedding(q)
            if not query_emb:
                hits = []
            else:
                status.update("[dim]searching index[/dim]")
                hits = store.search(query_emb, top_k=top_k, min_score=min_score)

        if not hits:
            print_warning("[No relevant code found in the index for that question.]")
            print_dim("  Try rephrasing, or `index rebuild` if the code changed a lot.")
            return

        snippets = [
            {"citation": h.citation(), "content": h.content}
            for h in hits
        ]

        stream_cb = None
        with thinking_status(ql) as status:
            answer_chars = 0
            if self.config.get("stream_llm", True):
                def _answer_stream_cb(chunk: str) -> None:
                    nonlocal answer_chars
                    answer_chars += len(chunk or "")
                    if answer_chars and answer_chars % 120 == 0:
                        status.update(f"[dim]{answer_chars} chars[/dim]")

                stream_cb = _answer_stream_cb

            answer = self.nl_handler.answer_codebase_query(q, snippets, stream_callback=stream_cb)

        self._render_codebase_answer(q, answer, hits)

    def _render_codebase_answer(self, question: str, answer: str, hits) -> None:
        from rich.panel import Panel
        from rich.markdown import Markdown
        from rich.table import Table
        from rich.text import Text
        from rich import box

        accent = _ui_accent_style()
        body = (answer or "").strip()
        if body:
            _cliara_console().print(
                Panel(
                    Markdown(body),
                    title=Text("Answer", style=accent),
                    subtitle=Text(f"? {question}", style="dim"),
                    border_style=accent,
                    padding=(0, 1),
                )
            )
        else:
            print_error("[Error] No answer content returned from the LLM.")

        # Sources table — the file:line citations backing the answer.
        tbl = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style=f"bold {accent}",
            padding=(0, 1),
        )
        tbl.add_column("#", style="dim", justify="right", no_wrap=True)
        tbl.add_column("Location", style="bold white", overflow="fold")
        tbl.add_column("Score", style="dim", justify="right", no_wrap=True)
        for i, h in enumerate(hits, 1):
            tbl.add_row(str(i), h.citation(), f"{h.score:.2f}")
        print_dim("\nSources")
        _cliara_console().print(tbl)

    # -- misc --------------------------------------------------------------

    @staticmethod
    def _ok_icon() -> str:
        from cliara import icons

        return icons.OK

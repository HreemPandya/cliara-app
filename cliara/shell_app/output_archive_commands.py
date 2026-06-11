"""Output Time-Machine commands for Cliara (``outputs ...``).

Persists secret-scrubbed digests of every command's stdout/stderr into a
per-project SQLite archive (see :mod:`cliara.output_archive`) and lets the
user search them by keyword + meaning. ``? find`` / ``? when did I ...``
answers automatically quote archived output for the history entries they
retrieve.

Opt-in: ``outputs on`` (or ``config set output_archive_enabled true``).

Mixed into :class:`cliara.shell_app.orchestrator.CliaraShell`.
"""

from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from cliara.session_store import _get_project_root
from cliara.shell_app.runtime import (
    _cliara_console,
    _ui_accent_style,
    print_dim,
    print_error,
    print_info,
    print_success,
    print_warning,
    safe_input,
    thinking_status,
)


# How many keyword candidates may be lazily embedded per search (cost bound,
# mainly for Ollama where each embedding call is a local inference).
_MAX_LAZY_EMBEDS_PER_SEARCH = 40

# Digest characters quoted inside a ? find RAG answer (cloud / local backend).
_RAG_DIGEST_CHARS_CLOUD = 900
_RAG_DIGEST_CHARS_LOCAL = 400


class OutputArchiveCommandMixin:
    """``outputs`` — archive, search, and surface what commands printed."""

    _output_archive_store = None
    _output_archive_root: Optional[str] = None
    _output_archive_adds: int = 0

    # -- config helpers ------------------------------------------------------

    def _output_archive_enabled(self) -> bool:
        return bool(self.config.get("output_archive_enabled", False))

    def _oa_cfg_int(self, key: str, default: int) -> int:
        try:
            v = int(self.config.get(key, default))
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    # -- store access ----------------------------------------------------------

    def _get_output_archive_store(self, create: bool = True):
        """Return the archive store for the current project (or None).

        Cached per project root so consecutive commands in one repo reuse the
        SQLite handle; switching repos closes the old handle.
        """
        from cliara import output_archive

        root = _get_project_root(Path.cwd()) or "global"
        if (
            self._output_archive_store is not None
            and self._output_archive_root == root
        ):
            return self._output_archive_store

        self._close_output_archive_store()

        db_path = output_archive.archive_db_path(self.config.config_dir, root)
        if not create and not db_path.exists():
            return None
        try:
            store = output_archive.OutputArchiveStore(db_path)
        except Exception as e:
            print_error(f"[Error] Could not open output archive: {e}")
            return None
        self._output_archive_store = store
        self._output_archive_root = root
        return store

    def _close_output_archive_store(self) -> None:
        if self._output_archive_store is not None:
            try:
                self._output_archive_store.close()
            except Exception:
                pass
            self._output_archive_store = None
            self._output_archive_root = None

    # -- capture hook (called from execute_shell_command's finally block) -----

    def _archive_last_output(self, command: str) -> None:
        """Scrub + digest + persist the last run's output. Best-effort, silent."""
        if not self._output_archive_enabled():
            return
        stdout = getattr(self, "last_stdout", "") or ""
        stderr = getattr(self, "last_stderr", "") or ""
        if not (stdout.strip() or stderr.strip()):
            return

        from cliara import output_archive
        from cliara.secret_scan import scrub_secrets

        max_chars = self._oa_cfg_int("output_archive_max_entry_kb", 64) * 1024
        digest = output_archive.digest_streams(stdout, stderr, max_chars=max_chars)
        if not digest:
            return

        # Scrub BEFORE persist — output is far more secret-dense than commands.
        digest, n_redacted = scrub_secrets(digest)
        cmd_scrubbed, n_cmd = scrub_secrets((command or "").strip())

        store = self._get_output_archive_store(create=True)
        if store is None:
            return

        git_ctx = self._get_quick_git_context()
        session_name = self.current_session.name if self.current_session else ""
        store.add_entry(
            cmd_scrubbed,
            digest,
            cwd=str(Path.cwd()),
            exit_code=getattr(self, "last_exit_code", None),
            git_branch=git_ctx.get("git_branch", "") or "",
            session_name=session_name,
            elapsed_s=getattr(self, "_last_command_elapsed", None),
            redactions=n_redacted + n_cmd,
        )

        # Enforce the disk cap occasionally, not on every command.
        self._output_archive_adds += 1
        if self._output_archive_adds % 20 == 0:
            max_bytes = self._oa_cfg_int("output_archive_max_db_mb", 50) * 1024 * 1024
            try:
                store.prune_to_cap(max_bytes)
            except Exception:
                pass

    # -- history-RAG enrichment ------------------------------------------------

    def _build_output_archive_lookup(self) -> Optional[Callable[[Dict], Optional[str]]]:
        """Closure for NLHandler.search_history_rag(output_lookup=...).

        Given one semantic-history entry dict, returns the archived output
        digest for that run (capped for prompt budget), or None.
        """
        if not self._output_archive_enabled():
            return None
        store = self._get_output_archive_store(create=False)
        if store is None or store.is_empty():
            return None

        # Local models get a much tighter budget (4K ctx, 2-entry retrieval —
        # mirrors the is_local branch inside search_history_rag).
        is_local = self.nl_handler.provider == "ollama"
        cap = _RAG_DIGEST_CHARS_LOCAL if is_local else _RAG_DIGEST_CHARS_CLOUD

        def _lookup(entry: Dict) -> Optional[str]:
            cmd = (entry.get("command") or "").strip()
            ts = (entry.get("timestamp") or "").strip()
            if not cmd or not ts:
                return None
            digest = store.find_output_for(cmd, ts)
            if not digest:
                return None
            if len(digest) > cap:
                digest = digest[:cap].rstrip() + "\n... (output truncated)"
            return digest

        return _lookup

    # -- outputs command ---------------------------------------------------------

    def handle_output_archive(self, args: str = "") -> None:
        """``outputs [status | search <q> | on | off | clear]``."""
        raw = (args or "").strip()
        sub = raw.split(maxsplit=1)[0].lower() if raw else "status"
        rest = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""

        if sub in ("help", "-h", "--help"):
            self._print_outputs_usage()
            return
        if sub in ("on", "enable"):
            self.config.settings["output_archive_enabled"] = True
            self.config.save()
            print_success("  Output archive enabled — command output is now digested,")
            print_dim("  secret-scrubbed, and stored locally per project.")
            print_dim("  Disable anytime: outputs off")
            return
        if sub in ("off", "disable"):
            self.config.settings["output_archive_enabled"] = False
            self.config.save()
            print_info("  Output archive disabled. Existing archives are kept;")
            print_dim("  delete this project's with: outputs clear")
            return
        if sub in ("clear", "reset", "delete"):
            self._outputs_clear()
            return
        if sub in ("status", "stats", "info", ""):
            self._outputs_status()
            return
        if sub in ("search", "find"):
            if not rest:
                print_error("[Error] Usage: outputs search <what you remember about the output>")
                print_dim("Example: outputs search numpy version pip resolved")
                return
            self._outputs_search(rest)
            return

        # Bare `outputs <words>` is treated as a search for convenience.
        self._outputs_search(raw)

    # -- subcommand impls ---------------------------------------------------

    @staticmethod
    def _print_outputs_usage() -> None:
        print_dim("Usage: outputs [status | search <query> | on | off | clear]")
        print_dim("  outputs                 Archive status for this project")
        print_dim("  outputs search <q>      Search archived stdout/stderr by meaning")
        print_dim("  outputs on / off        Enable or disable archiving (off by default)")
        print_dim("  outputs clear           Delete this project's archived output")
        print_dim("Archived digests are secret-scrubbed before they touch disk and")
        print_dim("are quoted automatically in `? find` / `? when did I ...` answers.")

    def _outputs_status(self) -> None:
        from rich.panel import Panel
        from rich.text import Text

        enabled = self._output_archive_enabled()
        store = self._get_output_archive_store(create=False)

        accent = _ui_accent_style()
        lines: List[str] = [f"Enabled:    {'yes' if enabled else 'no  (outputs on to enable)'}"]
        if store is None or store.is_empty():
            lines.append("Entries:    0")
            lines.append("Store:      (nothing archived for this project yet)")
        else:
            s = store.stats()
            size_mb = (s.get("stored_bytes") or 0) / (1024 * 1024)
            cap_mb = self._oa_cfg_int("output_archive_max_db_mb", 50)
            lines.append(f"Entries:    {s.get('entries', 0)}  ({s.get('embedded', 0)} embedded)")
            lines.append(f"Size:       {size_mb:.1f} MB / {cap_mb} MB cap")
            lines.append(f"Redactions: {s.get('redactions', 0)} secrets scrubbed before storage")
            lines.append(f"Range:      {self._oa_fmt_ts(s.get('oldest_ts'))} → {self._oa_fmt_ts(s.get('newest_ts'))}")
            lines.append(f"Store:      {s.get('db_path', '')}")
        _cliara_console().print(
            Panel(
                "\n".join(lines),
                title=Text("Output Archive", style=accent),
                border_style=accent,
                padding=(0, 1),
            )
        )
        if not enabled:
            print_dim("  Opt-in feature: stores head+tail digests of command output locally.")
            print_dim("  Secrets are scrubbed with the same engine as the push gate.")

    def _outputs_clear(self) -> None:
        store = self._get_output_archive_store(create=False)
        if store is None or store.is_empty():
            print_dim("  No archived output for this project.")
            return
        n = store.stats().get("entries", 0)
        confirm = (safe_input(f"  Delete {n} archived output digest(s) for this project? (y/n): ") or "").lower()
        if confirm not in ("y", "yes"):
            print_warning("  [Cancelled]")
            return
        try:
            store.clear()
            print_success("  Output archive cleared for this project.")
        except Exception as e:
            print_error(f"[Error] Could not clear archive: {e}")

    def _outputs_search(self, query: str) -> None:
        from cliara import output_archive

        store = self._get_output_archive_store(create=False)
        if store is None or store.is_empty():
            print_dim("  No archived output for this project yet.")
            if not self._output_archive_enabled():
                print_dim("  The archive is off — enable with: outputs on")
            return

        q = query.strip()
        with thinking_status(q if len(q) <= 48 else q[:45] + "...") as status:
            results = store.search_keyword(q, pool=32)

            # Lazy embedding blend: only the keyword candidates get embedded,
            # and only when an embedding backend exists. Nothing is embedded
            # on the write path, so this is the entire embedding cost.
            if results and self.nl_handler.supports_embedding_api():
                ids = [e.id for e in results]
                missing = store.missing_embedding_ids(ids)[:_MAX_LAZY_EMBEDS_PER_SEARCH]
                if missing:
                    status.update(f"[dim]embedding {len(missing)} entries[/dim]")
                    by_id = {e.id: e for e in results}
                    texts = [
                        output_archive.embedding_text_for_entry(by_id[i])
                        for i in missing
                    ]
                    vecs = self.nl_handler.get_embeddings_batch(texts)
                    for i, vec in zip(missing, vecs):
                        if vec:
                            store.set_embedding(i, vec)
                qv = self.nl_handler.get_embedding(q)
                if qv:
                    status.update("[dim]ranking[/dim]")
                    emb_scores = store.embedding_scores(ids, qv)
                    for e in results:
                        emb = max(0.0, min(1.0, emb_scores.get(e.id, 0.0)))
                        e.score = 0.6 * emb + 0.4 * e.score
                    results.sort(key=lambda e: (-e.score, -e.ts))

        results = results[:10]
        if not results:
            print_dim(f"  No archived output matched '{q}'.")
            print_dim("  Only commands run while the archive was enabled are searchable.")
            return

        self._render_outputs_results(q, results)
        self._outputs_view_prompt(store, results)

    def _render_outputs_results(self, query: str, results: List) -> None:
        from rich import box
        from rich.table import Table
        from rich.text import Text

        accent = _ui_accent_style()
        print()
        print_info(f"  {len(results)} archived run(s) for: {query}\n")
        tbl = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style=f"bold {accent}",
            padding=(0, 1),
        )
        tbl.add_column("#", style="dim", justify="right", no_wrap=True)
        tbl.add_column("When", style="dim", no_wrap=True)
        tbl.add_column("Command", style="bold white", overflow="fold")
        tbl.add_column("Output snippet", overflow="fold")
        tbl.add_column("", justify="right", no_wrap=True)

        for i, e in enumerate(results, 1):
            snippet = (e.search_text or "").replace("\n", " ")
            snippet = (snippet[:70] + "…") if len(snippet) > 70 else snippet
            if e.exit_code is None:
                status = Text("", style="dim")
            elif int(e.exit_code) == 0:
                status = Text("✓", style="green")
            else:
                status = Text(f"✗ {e.exit_code}", style="red")
            tbl.add_row(
                str(i),
                self._oa_fmt_ts(e.ts),
                e.command,
                Text(snippet, style="dim"),
                status,
            )
        _cliara_console().print(tbl)

    def _outputs_view_prompt(self, store, results: List) -> None:
        from rich.panel import Panel
        from rich.text import Text

        choice = (safe_input(f"\nView full output? (1-{len(results)} / Enter to skip): ") or "").strip()
        if not choice or not choice.isdigit():
            return
        idx = int(choice)
        if not (1 <= idx <= len(results)):
            return
        e = results[idx - 1]
        digest = store.get_digest(e.id)
        if not digest:
            print_warning("  [Could not read the stored digest.]")
            return
        accent = _ui_accent_style()
        subtitle = f"{self._oa_fmt_ts(e.ts)} • exit {e.exit_code if e.exit_code is not None else '?'}"
        if e.redactions:
            subtitle += f" • {e.redactions} secret(s) scrubbed"
        _cliara_console().print(
            Panel(
                digest,
                title=Text(f"$ {e.command}", style=accent),
                subtitle=Text(subtitle, style="dim"),
                border_style=accent,
                padding=(0, 1),
            )
        )

    # -- misc ------------------------------------------------------------------

    @staticmethod
    def _oa_fmt_ts(ts) -> str:
        if ts is None:
            return "?"
        try:
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                dt = datetime.fromtimestamp(float(ts))
            return dt.astimezone().strftime("%b %d, %H:%M")
        except Exception:
            return str(ts)[:16]

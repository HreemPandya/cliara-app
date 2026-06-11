"""Pre-commit code review command for Cliara (``? review`` / ``review``).

Collects the diff you're about to commit and runs it through the
``code_review`` agent, which surfaces likely bugs, missing tests, and
undocumented public APIs.

Mixed into :class:`cliara.shell_app.orchestrator.CliaraShell`.
"""

from pathlib import Path

from cliara.shell_app.runtime import (
    _cliara_console,
    _ui_accent_style,
    print_dim,
    print_error,
    print_warning,
    thinking_status,
)


class ReviewCommandMixin:
    """``review`` / ``? review`` — AI review of staged (or unstaged) changes."""

    def handle_code_review(self, args: str = "") -> None:
        """Review the diff about to be committed.

        Usage:
          review                 review staged changes (default)
          review unstaged        review working-tree (unstaged) changes
          review --all           review staged + unstaged together
        """
        from cliara import code_review

        arg = (args or "").strip().lower()
        if arg in ("-h", "--help", "help"):
            self._print_review_usage()
            return

        # Decide scope.
        review_unstaged = arg in ("unstaged", "--unstaged", "working", "wip")
        review_all = arg in ("all", "--all", "everything")
        if arg and not (review_unstaged or review_all or arg in ("staged", "--staged", "cached")):
            print_error(f"[Error] Unknown review option: {arg}")
            self._print_review_usage()
            return

        repo_root = code_review.get_repo_root(str(Path.cwd()))
        if repo_root is None:
            print_error("[Error] Not inside a git repository — nothing to review.")
            return

        if not self.nl_handler.llm_enabled:
            print_warning("[LLM not configured — run 'setup-llm' to enable `? review`.]")
            return

        # Gather the requested diff(s). For --all we concatenate staged then
        # unstaged so the model sees the complete picture.
        if review_all:
            staged = code_review.get_diff_info(repo_root, staged=True)
            unstaged = code_review.get_diff_info(repo_root, staged=False)
            info = self._merge_diffs(staged, unstaged)
            scope_label = "staged + unstaged"
            is_staged_scope = True
        else:
            want_staged = not review_unstaged
            info = code_review.get_diff_info(repo_root, staged=want_staged)
            scope_label = "staged" if want_staged else "unstaged"
            is_staged_scope = want_staged

        if info.is_empty():
            if is_staged_scope and not review_all:
                print_warning("[No staged changes to review.]")
                print_dim("  Stage files with `git add <path>`, or review the working tree: review unstaged")
            else:
                print_warning(f"[No {scope_label} changes to review.]")
            return

        max_chars = self._review_max_diff_chars()
        diff_text, truncated = code_review.truncate_diff(info.content, max_chars)

        branch = self._current_branch_safe(repo_root)

        n_files = len(info.files)
        header = f"reviewing {n_files} file{'s' if n_files != 1 else ''} ({scope_label})"

        stream_cb = None
        with thinking_status(header) as status:
            reviewed_chars = 0
            if self.config.get("stream_llm", True):
                def _review_stream_cb(chunk: str) -> None:
                    nonlocal reviewed_chars
                    reviewed_chars += len(chunk or "")
                    if reviewed_chars and reviewed_chars % 200 == 0:
                        status.update(f"[dim]{reviewed_chars} chars[/dim]")

                stream_cb = _review_stream_cb

            review = self.nl_handler.review_changes(
                info.stat,
                diff_text,
                info.files,
                staged=is_staged_scope,
                branch=branch,
                truncated=truncated,
                stream_callback=stream_cb,
            )

        self._render_review(review, info, scope_label, truncated)

    # -- helpers -----------------------------------------------------------

    def _render_review(self, review: str, info, scope_label: str, truncated: bool) -> None:
        from rich.panel import Panel
        from rich.markdown import Markdown
        from rich.text import Text

        accent = _ui_accent_style()
        body = (review or "").strip()
        if not body:
            print_error("[Error] No review content returned from the LLM.")
            return

        n_files = len(info.files)
        subtitle = f"{n_files} file{'s' if n_files != 1 else ''} • {scope_label}"
        if truncated:
            subtitle += " • diff truncated"

        _cliara_console().print(
            Panel(
                Markdown(body),
                title=Text("Code Review", style=accent),
                subtitle=Text(subtitle, style="dim"),
                border_style=accent,
                padding=(0, 1),
            )
        )
        print_dim("  Review only — nothing was committed. Run `push` when you're ready.")

    @staticmethod
    def _merge_diffs(staged, unstaged):
        from cliara.code_review import DiffInfo

        files = list(dict.fromkeys([*staged.files, *unstaged.files]))  # de-dupe, keep order
        stat_parts = []
        if staged.stat:
            stat_parts.append("# staged\n" + staged.stat)
        if unstaged.stat:
            stat_parts.append("# unstaged\n" + unstaged.stat)
        content_parts = []
        if staged.content:
            content_parts.append("# ===== STAGED CHANGES =====\n" + staged.content)
        if unstaged.content:
            content_parts.append("# ===== UNSTAGED CHANGES =====\n" + unstaged.content)
        return DiffInfo(
            stat="\n\n".join(stat_parts),
            content="\n\n".join(content_parts),
            files=files,
            staged=True,
        )

    def _review_max_diff_chars(self) -> int:
        from cliara.code_review import DEFAULT_MAX_DIFF_CHARS

        try:
            v = int(self.config.get("code_review_max_diff_chars", DEFAULT_MAX_DIFF_CHARS))
            return v if v > 0 else DEFAULT_MAX_DIFF_CHARS
        except (TypeError, ValueError):
            return DEFAULT_MAX_DIFF_CHARS

    @staticmethod
    def _current_branch_safe(repo_root) -> str:
        from cliara import code_review

        return code_review._git(["branch", "--show-current"], repo_root, timeout=3).strip()

    @staticmethod
    def _print_review_usage() -> None:
        print_dim("Usage: review [staged | unstaged | all]")
        print_dim("  review            Review staged changes (default)")
        print_dim("  review unstaged   Review unstaged working-tree changes")
        print_dim("  review all        Review staged + unstaged together")
        print_dim("Or:    ? review     Same as `review`")

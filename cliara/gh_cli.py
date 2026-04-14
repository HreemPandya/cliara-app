"""
``cliara gh`` subcommands — GitHub REST on device + Cliara LLM for prose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from cliara.auth import get_github_provider_token
from cliara.gh_api import (
    GitHubClient,
    git_current_branch,
    git_diff_range,
    git_log_since,
    git_recent_commits_messages,
    git_user_email,
    resolve_repo,
)
from cliara.gh_llm import gh_llm_complete, gh_llm_pr_title_body


def _parse_pr_number(raw: str) -> int:
    s = raw.strip().upper().replace("PR#", " ").replace("PR", " ").replace("#", " ").strip()
    m = re.search(r"(\d+)", s)
    if not m:
        raise ValueError(f"Could not parse PR number from: {raw!r}")
    return int(m.group(1))


def _require_github_token() -> str:
    tok = get_github_provider_token()
    if not tok:
        print(
            "cliara gh: no GitHub API token.\n"
            "  Run: cliara login   (GitHub is authorized together with Cliara Cloud)\n"
            "  Or set environment variable GITHUB_TOKEN.\n"
            "  If you logged in before this feature, run cliara login again so GitHub scopes are granted.",
            file=sys.stderr,
        )
        sys.exit(2)
    return tok


def _issue_lines_for_llm(issues: List[Dict[str, Any]], limit: int) -> str:
    lines: List[str] = []
    for it in issues[:limit]:
        if "pull_request" in it:
            continue
        num = it.get("number")
        title = it.get("title", "")
        labels = [l.get("name", "") for l in (it.get("labels") or []) if isinstance(l, dict)]
        body = (it.get("body") or "")[:1200]
        lines.append(f"#{num} [{', '.join(labels)}] {title}\n{body}\n---")
    return "\n".join(lines) if lines else "(no open issues)"


def cmd_pr(ns: argparse.Namespace, config_dir: Optional[str]) -> None:
    from cliara.config import Config

    cwd = Path.cwd()
    tok = _require_github_token()
    ref = resolve_repo(cwd)
    client = GitHubClient(tok, ref.api_base)
    repo_meta = client.get_repo(ref.owner, ref.repo)
    default_branch = str(repo_meta.get("default_branch") or "main")
    base = ns.base or default_branch
    head_branch = ns.head or git_current_branch(cwd)

    try:
        diff = git_diff_range(cwd, base, head_branch)
    except RuntimeError as e:
        print(f"cliara gh pr: {e}", file=sys.stderr)
        sys.exit(1)

    commits = git_recent_commits_messages(cwd, base, head_branch)
    cfg = Config(config_dir=config_dir) if config_dir else Config()

    if ns.no_ai:
        title = (ns.title or "").strip()
        body = (ns.body or "").strip()
        if not title:
            print("cliara gh pr: --no-ai requires --title (and optional --body).", file=sys.stderr)
            sys.exit(2)
    else:
        try:
            title, body = gh_llm_pr_title_body(
                diff_excerpt=diff,
                commit_messages=commits,
                base=base,
                head=head_branch,
                config=cfg,
            )
        except RuntimeError as e:
            print(f"cliara gh pr: {e}", file=sys.stderr)
            sys.exit(1)

    if ns.dry_run:
        print(title)
        print()
        print(body)
        return

    try:
        pr = client.create_pull(
            ref.owner,
            ref.repo,
            title=title,
            body=body,
            head=head_branch,
            base=base,
            draft=ns.draft,
        )
    except RuntimeError as e:
        print(f"cliara gh pr: {e}", file=sys.stderr)
        sys.exit(1)
    html_url = pr.get("html_url", "")
    print(html_url or json.dumps(pr, indent=2))


def cmd_issue(ns: argparse.Namespace, config_dir: Optional[str]) -> None:
    from cliara.config import Config

    cwd = Path.cwd()
    tok = _require_github_token()
    ref = resolve_repo(cwd)
    client = GitHubClient(tok, ref.api_base)
    title = " ".join(ns.title).strip()
    body = (ns.body or "").strip()
    cfg = Config(config_dir=config_dir) if config_dir else Config()

    if ns.ai_body and not body:
        try:
            user_msg = (
                f"Draft a GitHub issue body in Markdown for this title:\n{title}\n\n"
                "Include: Summary, Steps to reproduce (numbered), Expected vs actual, "
                "Environment (OS/browser/version if relevant), Possible severity.\n"
                "Output only the body text, no title line, no markdown fence."
            )
            body = gh_llm_complete(user_msg, config=cfg).strip()
        except RuntimeError as e:
            print(f"cliara gh issue: {e}", file=sys.stderr)
            sys.exit(1)

    if not body:
        body = "_No description provided._"

    if ns.dry_run:
        print(title)
        print()
        print(body)
        return

    try:
        issue = client.create_issue(
            ref.owner,
            ref.repo,
            title=title,
            body=body,
            labels=ns.labels or None,
        )
    except RuntimeError as e:
        print(f"cliara gh issue: {e}", file=sys.stderr)
        sys.exit(1)
    print(issue.get("html_url", "") or json.dumps(issue, indent=2))


def cmd_review(ns: argparse.Namespace, config_dir: Optional[str]) -> None:
    from cliara.config import Config

    cwd = Path.cwd()
    tok = _require_github_token()
    ref = resolve_repo(cwd)
    client = GitHubClient(tok, ref.api_base)
    cfg = Config(config_dir=config_dir) if config_dir else Config()

    number = ns.number
    if number is None:
        pulls = client.list_pulls(ref.owner, ref.repo, state="open", per_page=10)
        if not pulls:
            print("cliara gh review: no open pull requests in this repo.", file=sys.stderr)
            sys.exit(1)
        pr0 = pulls[0]
        number = int(pr0["number"])
        print(f"Using newest updated open PR #{number}: {pr0.get('title', '')}\n")

    try:
        pr = client.get_pull(ref.owner, ref.repo, number)
        files = client.list_pull_files(ref.owner, ref.repo, number)
    except RuntimeError as e:
        print(f"cliara gh review: {e}", file=sys.stderr)
        sys.exit(1)

    patches: List[str] = []
    for f in files[:40]:
        filename = f.get("filename", "")
        status = f.get("status", "")
        patch = f.get("patch") or ""
        cap = 8000
        if len(patch) > cap:
            patch = patch[:cap] + "\n[…patch truncated…]\n"
        patches.append(f"### {filename} ({status})\n```diff\n{patch}\n```\n")

    ctx = (
        f"PR #{number}: {pr.get('title', '')}\n"
        f"{pr.get('body') or ''}\n\n"
        "Files changed (unified diffs may be partial):\n"
        + "\n".join(patches)
    )

    if ns.dry_run:
        print(ctx[:12000])
        return

    user_msg = (
        ctx[:100_000]
        + "\n\nGive a concise code review:\n"
        "- Summary (2–4 bullets)\n"
        "- Issues by severity: Blocker / Major / Minor / Nit\n"
        "- Suggested tests or checks\n"
        "- Verdict line: APPROVE | COMMENT | REQUEST CHANGES (pick one)\n"
    )
    try:
        text = gh_llm_complete(user_msg, config=cfg)
    except RuntimeError as e:
        print(f"cliara gh review: {e}", file=sys.stderr)
        sys.exit(1)
    print(text)


def cmd_standup(ns: argparse.Namespace, config_dir: Optional[str]) -> None:
    from cliara.config import Config

    cwd = Path.cwd()
    tok = _require_github_token()
    ref = resolve_repo(cwd)
    client = GitHubClient(tok, ref.api_base)
    cfg = Config(config_dir=config_dir) if config_dir else Config()

    user = client.get_user()
    login = user.get("login") or ""
    email = git_user_email(cwd)
    author_filter = email if email else None
    log_text = git_log_since(cwd, ns.hours, author_filter)

    since_day = (datetime.now(timezone.utc) - timedelta(hours=max(1, ns.hours))).strftime("%Y-%m-%d")
    pr_q = f"is:pr repo:{ref.owner}/{ref.repo} involves:{login} updated:>={since_day}"
    iss_q = f"is:issue repo:{ref.owner}/{ref.repo} involves:{login} updated:>={since_day}"
    pr_block = ""
    iss_block = ""
    try:
        pr_search = client.search_issues(pr_q, per_page=15)
        for it in pr_search.get("items", [])[:15]:
            pr_block += f"- PR#{it.get('number')}: {it.get('title')}\n"
    except RuntimeError:
        pr_block = "(could not search PRs)\n"
    try:
        is_search = client.search_issues(iss_q, per_page=15)
        for it in is_search.get("items", [])[:15]:
            if "pull_request" not in it:
                iss_block += f"- Issue#{it.get('number')}: {it.get('title')}\n"
    except RuntimeError:
        iss_block = "(could not search issues)\n"

    bundle = (
        f"Git user.email filter: {author_filter or '(none)'}\n\n"
        f"Local commits (~{ns.hours}h, matched loosely by git author):\n{log_text or '(none)'}\n\n"
        f"GitHub @{login} PR activity (search, same window):\n{pr_block or '(none)'}\n"
        f"GitHub issue activity:\n{iss_block or '(none)'}\n"
    )

    user_msg = (
        bundle
        + "\nWrite a standup in plain English: Yesterday / Today / Blockers.\n"
        "Keep it under 12 short bullets total. If data is thin, say so honestly."
    )
    try:
        print(gh_llm_complete(user_msg, config=cfg))
    except RuntimeError as e:
        print(f"cliara gh standup: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_explain(ns: argparse.Namespace, config_dir: Optional[str]) -> None:
    from cliara.config import Config

    cwd = Path.cwd()
    tok = _require_github_token()
    ref = resolve_repo(cwd)
    client = GitHubClient(tok, ref.api_base)
    cfg = Config(config_dir=config_dir) if config_dir else Config()

    try:
        n = _parse_pr_number(ns.pr_ref)
    except ValueError as e:
        print(f"cliara gh explain: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        pr = client.get_pull(ref.owner, ref.repo, n)
        files = client.list_pull_files(ref.owner, ref.repo, n)
    except RuntimeError as e:
        print(f"cliara gh explain: {e}", file=sys.stderr)
        sys.exit(1)

    names = [f.get("filename", "") for f in files[:80]]
    user_msg = (
        f"PR #{n}: {pr.get('title', '')}\n\n"
        f"Description from author:\n{pr.get('body') or '(none)'}\n\n"
        f"Files touched ({len(names)}): {', '.join(names)}\n\n"
        "Explain in plain English what this PR does, who it affects, and any risks. "
        "Under 15 sentences."
    )
    try:
        print(gh_llm_complete(user_msg, config=cfg))
    except RuntimeError as e:
        print(f"cliara gh explain: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_triage(ns: argparse.Namespace, config_dir: Optional[str]) -> None:
    from cliara.config import Config

    cwd = Path.cwd()
    tok = _require_github_token()
    ref = resolve_repo(cwd)
    client = GitHubClient(tok, ref.api_base)
    cfg = Config(config_dir=config_dir) if config_dir else Config()

    try:
        issues = client.list_issues(ref.owner, ref.repo, state="open", per_page=40)
    except RuntimeError as e:
        print(f"cliara gh triage: {e}", file=sys.stderr)
        sys.exit(1)

    block = _issue_lines_for_llm(issues, ns.limit)
    print("Open issues (preview):\n")
    print(block[:8000])
    print("\n---\nSuggested order (AI):\n")

    user_msg = (
        "Here are open GitHub issues for this repo:\n\n"
        f"{block}\n\n"
        "List them in recommended tackle order (most urgent first). "
        "For each line: #number — one-line reason (severity: high/med/low). "
        "Then one paragraph: what to pick up first and why. "
        "If the list is empty, say so."
    )
    try:
        print(gh_llm_complete(user_msg, config=cfg))
    except RuntimeError as e:
        print(f"cliara gh triage: {e}", file=sys.stderr)
        sys.exit(1)


def register_gh_subparser(subparsers: Any) -> None:
    gh = subparsers.add_parser(
        "gh",
        help="GitHub: PRs, issues, reviews, standup (uses GitHub token from cliara login or GITHUB_TOKEN)",
    )
    gh_sub = gh.add_subparsers(dest="gh_command", required=True)

    pr_p = gh_sub.add_parser("pr", help="Open a PR with AI-generated title and body")
    pr_p.add_argument("--base", default=None, help="Base branch (default: repo default branch)")
    pr_p.add_argument("--head", default=None, help="Head branch (default: current branch)")
    pr_p.add_argument("--draft", action="store_true", help="Open as draft PR")
    pr_p.add_argument("--dry-run", action="store_true", help="Print title/body only, do not create")
    pr_p.add_argument("--no-ai", action="store_true", help="Use --title/--body instead of AI")
    pr_p.add_argument("--title", default=None, help="With --no-ai: PR title")
    pr_p.add_argument("--body", default=None, help="With --no-ai: PR body (markdown)")

    is_p = gh_sub.add_parser("issue", help="Create an issue (optional AI body from title)")
    is_p.add_argument("title", nargs="+", help="Issue title")
    is_p.add_argument("--body", default="", help="Issue body (markdown)")
    is_p.add_argument("--label", action="append", dest="labels", default=[], metavar="NAME")
    is_p.add_argument(
        "--ai-body",
        action="store_true",
        help="When --body is omitted, draft the body from the title using the LLM",
    )
    is_p.add_argument("--dry-run", action="store_true")

    rv_p = gh_sub.add_parser("review", help="AI review of an open pull request")
    rv_p.add_argument("--number", type=int, default=None, help="PR number (default: latest open PR)")
    rv_p.add_argument("--dry-run", action="store_true", help="Print fetched context only")

    st_p = gh_sub.add_parser("standup", help="Summarize recent git + GitHub activity")
    st_p.add_argument("--hours", type=int, default=24, help="Look-back window (default: 24)")

    ex_p = gh_sub.add_parser("explain", help="Explain a pull request in plain English")
    ex_p.add_argument("pr_ref", help='PR number, e.g. 234 or PR#234')

    tr_p = gh_sub.add_parser("triage", help="Open issues + suggested priority order")
    tr_p.add_argument("--limit", type=int, default=25, help="Max issues to send to the model")


def run_gh(args: argparse.Namespace) -> None:
    cmd = args.gh_command
    config_dir = getattr(args, "config_dir", None)
    if cmd == "pr":
        cmd_pr(args, config_dir)
    elif cmd == "issue":
        cmd_issue(args, config_dir)
    elif cmd == "review":
        cmd_review(args, config_dir)
    elif cmd == "standup":
        cmd_standup(args, config_dir)
    elif cmd == "explain":
        cmd_explain(args, config_dir)
    elif cmd == "triage":
        cmd_triage(args, config_dir)
    else:
        print(f"cliara gh: unknown subcommand {cmd!r}", file=sys.stderr)
        sys.exit(2)

"""
GitHub REST API client + repository resolution (Option A: token on device).

Uses httpx. Token from ``get_github_provider_token()`` or ``GITHUB_TOKEN``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx


@dataclass
class RepoRef:
    owner: str
    repo: str
    web_host: str  # e.g. github.com
    api_base: str  # e.g. https://api.github.com


def _run_git(args: List[str], cwd: Path) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "git failed").strip())
    return (p.stdout or "").strip()


def parse_github_remote(url: str) -> Tuple[str, str, str]:
    """
    Parse owner, repo, web hostname from a git remote URL.

    Supports https://github.com/o/r(.git), git@github.com:o/r.git,
    and GitHub Enterprise https://host/o/r.git / git@host:o/r.git.
    """
    u = url.strip()
    if u.startswith("git@"):
        # git@github.com:owner/repo.git
        m = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", u)
        if not m:
            raise RuntimeError(f"Unrecognized git SSH remote: {url}")
        host, owner, repo = m.group(1), m.group(2), m.group(3).rstrip("/")
        return owner, repo, host
    parsed = urlparse(u)
    host = parsed.netloc or "github.com"
    path = (parsed.path or "").strip("/")
    if not path:
        raise RuntimeError(f"Unrecognized git HTTPS remote: {url}")
    parts = path.split("/")
    if len(parts) < 2:
        raise RuntimeError(f"Unrecognized git HTTPS remote (need owner/repo): {url}")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo, host


def resolve_repo(cwd: Optional[Path] = None) -> RepoRef:
    """Resolve origin remote into owner/repo and API base URL."""
    root = cwd or Path.cwd()
    url = _run_git(["remote", "get-url", "origin"], root)
    owner, repo, host = parse_github_remote(url)
    if host == "www.github.com":
        host = "github.com"
    if host == "github.com":
        api_base = "https://api.github.com"
        web = "github.com"
    else:
        api_base = f"https://{host}/api/v3"
        web = host
    return RepoRef(owner=owner, repo=repo, web_host=web, api_base=api_base)


def git_current_branch(cwd: Optional[Path] = None) -> str:
    root = cwd or Path.cwd()
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)


def git_user_email(cwd: Optional[Path] = None) -> str:
    root = cwd or Path.cwd()
    try:
        return _run_git(["config", "user.email"], root)
    except RuntimeError:
        return ""


def git_log_since(cwd: Path, since_hours: int, author_pattern: Optional[str]) -> str:
    """Return short git log text since N hours ago, optionally filtered by --author."""
    root = cwd
    hours = max(1, since_hours)
    args = ["log"]
    if author_pattern:
        args.append(f"--author={author_pattern}")
    args.extend(
        [
            f"--since={hours} hours ago",
            "--pretty=format:- %h %s (%cr)",
            "-n",
            "50",
        ]
    )
    try:
        return _run_git(args, root)
    except RuntimeError:
        return ""


def git_diff_range(cwd: Path, base: str, head: str, max_bytes: int = 120_000) -> str:
    """Unified diff between two refs (truncated if huge)."""
    p = subprocess.run(
        ["git", "diff", f"{base}...{head}"],
        cwd=str(cwd),
        capture_output=True,
        timeout=60,
    )
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or "git diff failed")
    raw = (p.stdout or b"").decode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        return raw[:max_bytes] + "\n\n[diff truncated for size — use smaller changes or GitHub compare view]\n"
    return raw


def git_recent_commits_messages(cwd: Path, base: str, head: str, limit: int = 30) -> str:
    """One line per commit on head not in base."""
    spec = f"{base}..{head}"
    try:
        out = _run_git(["log", spec, f"-n{limit}", "--pretty=format:%s"], cwd)
        return out
    except RuntimeError:
        return ""


class GitHubClient:
    def __init__(self, token: str, api_base: str):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._api_base}{path}"
        with httpx.Client(timeout=60.0) as client:
            r = client.request(method, url, headers=self._headers, params=params, json=json_body)
        if r.status_code == 401:
            raise RuntimeError(
                "GitHub returned 401 Unauthorized. Run `cliara login` again (GitHub scopes), "
                "or set GITHUB_TOKEN with repo access."
            )
        if r.status_code == 403:
            msg = r.text[:500] if r.text else ""
            raise RuntimeError(
                "GitHub returned 403 Forbidden (rate limit or missing scope?). "
                f"Details: {msg}"
            )
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:800]
            raise RuntimeError(f"GitHub API {r.status_code}: {detail}")
        if not r.content:
            return None
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            return r.json()
        return r.text

    def get_user(self) -> Dict[str, Any]:
        return self._request("GET", "/user")

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}")

    def list_pulls(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        per_page: int = 20,
    ) -> List[Dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"},
        )

    def get_pull(self, owner: str, repo: str, number: int) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def compare(self, owner: str, repo: str, basehead: str) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}/compare/{basehead}")

    def create_pull(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        }
        return self._request("POST", f"/repos/{owner}/{repo}/pulls", json_body=payload)

    def list_issues(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        per_page: int = 40,
    ) -> List[Dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page},
        )

    def create_issue(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self._request("POST", f"/repos/{owner}/{repo}/issues", json_body=payload)

    def search_issues(self, q: str, per_page: int = 20) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/search/issues",
            params={"q": q, "per_page": per_page},
        )

    def list_pull_files(self, owner: str, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}/files", params={"per_page": 100})

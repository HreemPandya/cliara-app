"""
Cliara Cloud authentication — client side.

Implements the OAuth 2.0 PKCE flow against Supabase so that users can
log in with GitHub in a browser and have their JWT stored locally.

Usage (from shell.py):
    from cliara.auth import login, logout, get_valid_token

Flow:
    1. Generate PKCE code_verifier + code_challenge
    2. Open browser → Supabase GitHub OAuth URL (with redirect to localhost)
    3. Tiny local HTTP server captures the ?code= callback
    4. Exchange code + code_verifier for access_token + refresh_token
    5. Write ~/.cliara/token.json (Supabase session + GitHub provider_token when returned)
    6. On future startups, load token, refresh if expired — transparently
"""

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — update these after deploying the backend and creating the
# Supabase project.  They can also be overridden with env vars for dev.
# ---------------------------------------------------------------------------

_SUPABASE_URL: str = os.getenv(
    "CLIARA_SUPABASE_URL",
    "https://rzkfebrsfvhyfmfcfhin.supabase.co",  # replaced at publish time
)
_SUPABASE_ANON_KEY: str = os.getenv(
    "CLIARA_SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ6a2ZlYnJzZnZoeWZtZmNmaGluIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM1MjM1ODEsImV4cCI6MjA4OTA5OTU4MX0.6sf8uYZK5Ca8ZDtdy46JL0_-vqixL7IOfyBA60lf-Fk",  # pragma: allowlist secret — Supabase anon key is public, safe to embed
)
_CLIARA_GATEWAY_URL: str = os.getenv(
    "CLIARA_GATEWAY_URL",
    "https://cliara-cloud-production.up.railway.app/v1",
)

_TOKEN_FILE = Path.home() / ".cliara" / "token.json"
_CALLBACK_TIMEOUT = 120   # seconds to wait for the browser callback
_TOKEN_REFRESH_BUFFER = 60  # refresh token when < 60 seconds until expiry

# GitHub OAuth scopes passed through Supabase → GitHub (space-separated, URL-encoded in authorize URL).
# Required for Issues/PRs and user identity in `cliara gh` commands.
_GITHUB_PROVIDER_SCOPES = os.getenv(
    "CLIARA_GITHUB_SCOPES",
    "repo read:user read:org",
)


def get_gateway_url() -> str:
    """Return the Cliara Cloud gateway URL (single source of truth for API calls)."""
    return _CLIARA_GATEWAY_URL


# ---------------------------------------------------------------------------
# PKCE helpers (stdlib only — no extra dependencies)
# ---------------------------------------------------------------------------

def _generate_pkce_pair() -> "tuple[str, str]":
    """Return (code_verifier, code_challenge) using S256 method."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _find_free_port() -> int:
    """Bind to port 0 and let the OS pick a free ephemeral port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Local callback HTTP server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Minimal HTTP handler that captures the OAuth ?code= parameter and
    shows a success page in the browser, then signals the waiting thread.
    """

    auth_code: Optional[str] = None
    error: Optional[str] = None
    _event: threading.Event

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            _CallbackHandler.error = params["error"][0]
            body = _html_page("Login failed", f"<p>Error: {_CallbackHandler.error}</p><p>You can close this tab.</p>")
        elif "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            body = _html_page(
                "Logged in to Cliara",
                "<p>You're logged in! You can close this tab and return to your terminal.</p>",
            )
        else:
            _CallbackHandler.error = "No code in callback"
            body = _html_page("Unexpected response", "<p>No auth code received. Please try again.</p>")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _CallbackHandler._event.set()

    def log_message(self, *_):  # suppress default request logging
        pass


def _html_page(title: str, body_html: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           display: flex; justify-content: center; align-items: center;
           min-height: 100vh; margin: 0; background: #0f0f0f; color: #e0e0e0; }}
    .card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 12px;
             padding: 2rem 2.5rem; max-width: 420px; text-align: center; }}
    h1 {{ font-size: 1.4rem; color: #a78bfa; margin-top: 0; }}
    p {{ color: #aaa; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    {body_html}
  </div>
</body>
</html>"""
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# Supabase REST helpers (stdlib only)
# ---------------------------------------------------------------------------

def _supabase_post(path: str, payload: dict) -> dict:
    """POST to a Supabase Auth endpoint and return the parsed JSON response."""
    url = f"{_SUPABASE_URL}/auth/v1{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "apikey": _SUPABASE_ANON_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except Exception:
            detail = {"raw": body}
        raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc


def _exchange_pkce_code(auth_code: str, code_verifier: str) -> dict:
    """Exchange the auth code + verifier for a full session (tokens)."""
    return _supabase_post(
        "/token?grant_type=pkce",
        {"auth_code": auth_code, "code_verifier": code_verifier},
    )


def _refresh_session(refresh_token: str) -> dict:
    """Use a stored refresh token to get a new access token."""
    return _supabase_post(
        "/token?grant_type=refresh_token",
        {"refresh_token": refresh_token},
    )


# ---------------------------------------------------------------------------
# Token file management
# ---------------------------------------------------------------------------

def _token_dir() -> Path:
    d = Path.home() / ".cliara"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_token(session: dict) -> None:
    """Persist session data to ~/.cliara/token.json."""
    token_path = _token_dir() / "token.json"
    prior: dict = {}
    if token_path.exists():
        try:
            prior = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}

    # Supabase may return provider tokens at the top level of the session dict.
    # On refresh, provider_token is often omitted — keep the previous GitHub token.
    s_pt = session.get("provider_token")
    s_prt = session.get("provider_refresh_token")
    gh_token = s_pt if s_pt else prior.get("github_provider_token", "")
    gh_refresh = s_prt if s_prt else prior.get("github_provider_refresh_token", "")

    token_data = {
        "access_token": session.get("access_token", ""),
        "refresh_token": session.get("refresh_token", ""),
        # Supabase returns expires_in (seconds); compute absolute timestamp
        "expires_at": time.time() + int(session.get("expires_in", 3600)),
        "user_id": session.get("user", {}).get("id", ""),
        "email": session.get("user", {}).get("email", ""),
        "github_provider_token": gh_token,
        "github_provider_refresh_token": gh_refresh,
    }
    token_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    # Restrict permissions on Unix (Windows ignores chmod, which is fine)
    try:
        token_path.chmod(0o600)
    except Exception:
        pass


def load_token() -> Optional[dict]:
    """
    Read ~/.cliara/token.json.
    Returns the parsed dict or None if the file doesn't exist or is corrupt.
    """
    token_path = _token_dir() / "token.json"
    if not token_path.exists():
        return None
    try:
        return json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_github_provider_token() -> Optional[str]:
    """
    Return a GitHub OAuth access token for REST API calls (Option A: stored locally).

    Resolution order:
      1. GITHUB_TOKEN environment variable (for CI or advanced users)
      2. github_provider_token from ~/.cliara/token.json (from Cliara login + scopes)

    Returns None if not configured. Does not validate or refresh the GitHub token;
    on 401 from GitHub, users should run ``cliara login`` again.
    """
    env_tok = os.getenv("GITHUB_TOKEN", "").strip()
    if env_tok:
        return env_tok
    data = load_token()
    if not data:
        return None
    tok = (data.get("github_provider_token") or "").strip()
    return tok or None


def get_valid_token() -> Optional[str]:
    """
    Return a non-expired access token, refreshing it automatically if needed.
    Returns None if:
      - No token file exists (user has never logged in)
      - Token is expired AND refresh fails (user must re-login)
    This function is called at startup and must not raise exceptions.
    """
    token_data = load_token()
    if token_data is None:
        return None

    access_token = token_data.get("access_token", "")
    expires_at = token_data.get("expires_at", 0)
    refresh_token_val = token_data.get("refresh_token", "")

    # Still valid with buffer
    if time.time() < expires_at - _TOKEN_REFRESH_BUFFER:
        return access_token

    # Expired — attempt silent refresh
    if not refresh_token_val:
        return None

    try:
        session = _refresh_session(refresh_token_val)
        _write_token(session)
        return session.get("access_token", "")
    except Exception:
        # Refresh failed (network error, revoked token, etc.) — user must re-login
        return None


def logout() -> None:
    """Delete the stored token. The next startup will have no provider configured."""
    token_path = _token_dir() / "token.json"
    if token_path.exists():
        token_path.unlink()


# ---------------------------------------------------------------------------
# Main login flow
# ---------------------------------------------------------------------------

def login() -> "tuple[str, str]":
    """
    Run the full PKCE OAuth flow.

    Opens a browser to Supabase GitHub OAuth, waits for the callback on a
    local port, exchanges the code for tokens, saves them, and returns the
    access token.

    Raises RuntimeError with a human-readable message on failure.
    """
    if not _SUPABASE_ANON_KEY or _SUPABASE_URL == "https://placeholder.supabase.co":
        raise RuntimeError(
            "Cliara Cloud is not configured yet.\n"
            "Visit https://cliara.dev/signup when the service launches, "
            "or set CLIARA_SUPABASE_URL and CLIARA_SUPABASE_ANON_KEY for dev mode."
        )

    code_verifier, code_challenge = _generate_pkce_pair()
    port = _find_free_port()
    redirect_uri = f"http://localhost:{port}/callback"

    # GitHub expects space-separated scopes; allow comma-separated env for convenience.
    _scope_str = " ".join(_GITHUB_PROVIDER_SCOPES.replace(",", " ").split())
    scope_q = urllib.parse.quote(_scope_str, safe="")
    oauth_url = (
        f"{_SUPABASE_URL}/auth/v1/authorize"
        f"?provider=github"
        f"&redirect_to={urllib.parse.quote(redirect_uri, safe='')}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scopes={scope_q}"
    )

    # Shared event to signal when the callback has been received
    done_event = threading.Event()
    _CallbackHandler._event = done_event
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.style import Style
    from rich.text import Text

    from cliara.console import get_console

    _c = get_console()
    _c.print()
    _c.print(
        Panel(
            Group(
                Text.from_markup(
                    "[bold white]Opening your browser[/] for [bold]GitHub[/] sign-in…\n"
                ),
                Text(""),
                Text(
                    "If nothing opens, copy this URL into a browser:",
                    style="dim",
                ),
                Text(""),
                Text(
                    oauth_url,
                    style=Style(color="cyan", dim=True, link=oauth_url),
                ),
            ),
            title=Text.from_markup("[bold cyan]Cliara Cloud[/]"),
            border_style="cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    _c.print()

    try:
        webbrowser.open(oauth_url)
    except Exception:
        pass  # Browser open may fail in headless environments; URL is already printed

    # Wait for the callback (with timeout)
    received = done_event.wait(timeout=_CALLBACK_TIMEOUT)
    server.shutdown()

    if not received:
        raise RuntimeError(
            f"Login timed out after {_CALLBACK_TIMEOUT}s waiting for the browser callback.\n"
            "Make sure the browser opened and you completed the GitHub login."
        )

    if _CallbackHandler.error:
        raise RuntimeError(f"OAuth error from Supabase: {_CallbackHandler.error}")

    auth_code = _CallbackHandler.auth_code
    if not auth_code:
        raise RuntimeError("No auth code received from Supabase callback.")

    # Exchange code → tokens
    try:
        session = _exchange_pkce_code(auth_code, code_verifier)
    except RuntimeError as exc:
        raise RuntimeError(f"Token exchange failed: {exc}") from exc

    access_token = session.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "Supabase did not return an access token. "
            f"Response keys: {list(session.keys())}"
        )

    _write_token(session)

    email = session.get("user", {}).get("email", "")
    return access_token, email

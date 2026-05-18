"""
Natural Language handler for Cliara (Phase 2).
Converts natural language queries to shell commands using LLM.
"""

import contextlib
import json
import os
import platform
import re
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
import math

import numpy as np
from shutil import which
from typing import List, Tuple, Optional, Dict, Any, Callable, Literal

from cliara.safety import SafetyChecker, DangerLevel
from cliara.shell_app.runtime import print_dim
from cliara.agents import AGENT_REGISTRY
from cliara.nl.constants import (
    CLIARA_BUILTIN_COMMANDS,
    EMBEDDING_MODEL,
    OPENAI_COMPAT_PROVIDERS,
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULT_MODELS,
    STREAMING_SAFE_AGENTS,
    model_id_matches_provider,
)
from cliara.nl.session_reflect import (
    default_session_reflect_plan,
    validate_session_reflect_steps,
)

# Backward-compatible aliases expected by existing imports/tests.
_CLIARA_BUILTIN_COMMANDS = CLIARA_BUILTIN_COMMANDS
_OPENAI_COMPAT_PROVIDERS = OPENAI_COMPAT_PROVIDERS
_PROVIDER_BASE_URLS = PROVIDER_BASE_URLS
_PROVIDER_DEFAULT_MODELS = PROVIDER_DEFAULT_MODELS
_STREAMING_SAFE_AGENTS = STREAMING_SAFE_AGENTS


def _default_session_reflect_plan() -> List[Dict[str, Any]]:
    """Backward-compatible wrapper for session_reflect default plan."""
    return default_session_reflect_plan()


def _validate_session_reflect_steps(data: Any) -> Optional[List[Dict[str, Any]]]:
    """Backward-compatible wrapper for session_reflect step validation."""
    return validate_session_reflect_steps(data)


def _openai_compat_text_from_content(content: Any) -> str:
    """Turn ``message.content`` or a stream ``delta`` into a plain string.

    Some OpenAI-compatible APIs (e.g. Gemini) return a list of part dicts; reading
    only ``.content`` as *str* yields ``""`` and looks like a failed model call.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                t = block.get("text")
                if t is None and isinstance(block.get("content"), str):
                    t = block["content"]
                if t:
                    out.append(t)
            else:
                t = getattr(block, "text", None) or str(block) if block is not None else None
                if t:
                    out.append(t)
        return "".join(out)
    return str(content) if content else ""


# Markdown code fences: first non-empty line is often only ```, which
# ``.strip("`\`\"'")`` can erase entirely (false "empty" commit line).
_MD_FENCE_LINE = re.compile(r"^`{3,}[a-zA-Z0-9_.-]*\s*$")
# Conventional first line: optional bullet, type(scope)?: rest
# MULTILINE: second-pass search finds a commit line not at the start of the blob.
# \s* before ":" allows "chore : x"; \s* after ":" allows "chore:go" (no space).
_CC_LINE = re.compile(
    r"^\s*[-*]?\s*"
    r"(revert|feat|fix|refactor|docs|style|test|chore|perf|ci|build)"
    r"(?:\([^)]+\))?\s*:\s*\S.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _first_usable_commit_line(raw: str) -> str:
    """Return one conventional commit line, or '' if the model output is unusable."""
    s0 = (raw or "").strip()
    if s0:
        # Some models send reasoning in XML-like blocks; strip so the line scan sees the line.
        s0 = re.sub(
            r"<" + "thinking" + r">[\s\S]*?</" + "thinking" + r">\s*",
            "",
            s0,
            flags=re.IGNORECASE,
        )
        s0 = re.sub(
            r"<" + "redacted" + r"_thinking>[\s\S]*?" + r"</" + "redacted" + r"_thinking>\s*",
            "",
            s0,
            flags=re.IGNORECASE,
        )
        s0 = s0.strip()
    if not s0:
        return ""
    raw = s0
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s or _MD_FENCE_LINE.match(s):
            continue
        s = s.strip("'\"")  # do not use strip(\"'`\") — lone ``` lines break that
        if s.startswith("`") and s.endswith("`") and s.count("`") == 2 and len(s) > 2:
            s = s[1:-1].strip()  # inline `chore: ...`
        m = _CC_LINE.match(s)
        if m:
            t = m.group(0).strip()
            t = re.sub(r"^[-*]\s+", "", t, count=1)
            return t
    for m in _CC_LINE.finditer(raw or ""):
        t = m.group(0).strip()
        t = re.sub(r"^[-*]\s+", "", t, count=1)
        return t
    return ""


# ---------------------------------------------------------------------------
# Commit message — type-inference keyword table
# ---------------------------------------------------------------------------
# When a model returns a plain description without a CC type prefix, we infer
# the best-matching type by scanning the description for domain keywords.
# Checked in priority order: specific types before the catch-all "chore".
_CC_TYPE_KEYWORDS: List[Tuple[str, frozenset]] = [
    ("fix",      frozenset({"fix", "fixes", "bug", "error", "issue", "crash", "broken",
                             "resolve", "correct", "patch", "handle", "prevent", "revert"})),
    ("feat",     frozenset({"add", "adds", "implement", "create", "introduce", "support",
                             "enable", "new", "include", "feature", "allow", "expose"})),
    ("perf",     frozenset({"performance", "speed", "faster", "slower", "latency", "memory",
                             "cache", "efficient", "lazy", "throttle", "debounce", "optimiz"})),
    ("refactor", frozenset({"refactor", "restructure", "reorganize", "simplify", "clean",
                             "improve", "reduce", "move", "rename", "extract", "split",
                             "merge", "dedup", "consolidate"})),
    ("docs",     frozenset({"doc", "docs", "documentation", "readme", "comment", "comments",
                             "changelog", "license", "typo", "clarif", "example"})),
    ("test",     frozenset({"test", "tests", "spec", "specs", "coverage", "unit",
                             "integration", "mock", "fixture", "assert"})),
    ("ci",       frozenset({"ci", "cd", "workflow", "pipeline", "action", "travis",
                             "circleci", "jenkins", "github action"})),
    ("build",    frozenset({"build", "package", "dependency", "dependencies",
                             "requirements", "version", "bump", "release", "publish",
                             "install", "wheel", "lockfile"})),
    ("chore",    frozenset({"update", "upgrade", "downgrade", "remove", "delete",
                             "cleanup", "misc", "config", "setup", "format", "lint"})),
]


def _infer_cc_type(description: str) -> str:
    """Return the best-matching Conventional Commits type for a plain description."""
    desc = description.lower()
    for cc_type, keywords in _CC_TYPE_KEYWORDS:
        if any(kw in desc for kw in keywords):
            return cc_type
    return "chore"


# Lines that are clearly model preamble and should be skipped when extracting
# a commit description from plain-text responses.
_COMMIT_PREAMBLE_RE = re.compile(
    r"^(here\b|this\s+(is|would|would be)\b|the\s+commit\b|i\s+(would|suggest|recommend|think)\b"
    r"|please\b|note[:\s]|output[:\s]|result[:\s]|sure[,!]|of\s+course\b|based\s+on\b"
    r"|looking\s+at\b|commit\s+message[:\s]|conventional\s+commit)",
    re.IGNORECASE,
)


class NLHandler:
    """Handles natural language to command conversion using LLM."""

    # --- Ollama reachability probe cache (class-level, shared across instances) ---
    # Probing Ollama on every initialize_llm() call adds 1.5–3 s when Ollama is
    # not running. A short TTL cache avoids repeated network waits in one session.
    _ollama_probe_cache: Dict[str, Tuple[float, bool]] = {}
    _OLLAMA_PROBE_TTL: float = 30.0   # seconds before re-probing the same URL
    _OLLAMA_PROBE_TIMEOUT: float = 1.5 # per-connection timeout (localhost responds <5 ms)

    # --- Embedding query cache (instance-level) ---
    _EMBEDDING_CACHE_TTL: float = 300.0  # 5-minute TTL for query vectors
    _EMBEDDING_CACHE_MAX: int = 256      # avoid unbounded memory growth

    def __init__(self, safety_checker: SafetyChecker, config=None):
        """
        Initialize NL handler.

        Args:
            safety_checker: Safety checker instance
            config: Optional Config instance for model/provider settings
        """
        self.safety = safety_checker
        self.config = config
        self.llm_enabled = False
        self.llm_client = None
        self.provider = None

        # Optional local backend (Ollama) for transparent routing/redaction.
        self._local_llm_client: Optional[Any] = None
        self._local_enabled: bool = False
        self._force_local_next: bool = False
        self._last_backend_used: Optional[Literal["local", "cloud"]] = None
        # Lazy OpenAI client for embeddings when chat uses another provider (e.g. Groq).
        self._openai_embedding_client: Optional[Any] = None
        # Small TTL cache to avoid re-scanning the same directory tree on every NL query.
        self._dir_listing_cache: Dict[str, Tuple[float, str]] = {}
        # Read-only git snapshot is cheap; short TTL avoids duplicate work in one session burst.
        self._git_snapshot_cache: Dict[str, Tuple[float, str]] = {}
        # One-time banner for the first cloud call in this interactive session.
        self._cloud_redaction_preview_shown: bool = False
        # Per-instance embedding cache: avoids re-fetching the same query vector
        # within a session (e.g. repeated "? find ..." with the same wording).
        self._embedding_cache: Dict[str, Tuple[float, List[float]]] = {}

    def _request_timeout_seconds(self) -> float:
        """Return the HTTP timeout for cloud LLM calls.

        A hard timeout prevents the REPL from hanging indefinitely when the
        network/provider is unreachable.
        """
        try:
            if self.config is None:
                return 60.0
            raw = self.config.get("llm_timeout_seconds", 60)
            v = float(raw)
            return v if v > 0 else 60.0
        except Exception:
            return 60.0

    # ------------------------------------------------------------------
    # Ollama reachability probe (cached)
    # ------------------------------------------------------------------

    @classmethod
    def _probe_ollama_url(cls, url: str, timeout: Optional[float] = None) -> bool:
        """Return True if Ollama is reachable at *url*.

        Results are cached for _OLLAMA_PROBE_TTL seconds so that multiple
        callers within one startup sequence (initialize_llm + initialize_local_ollama,
        or 'use ollama' → initialize_llm) only hit the network once.

        Args:
            timeout: Override the default _OLLAMA_PROBE_TIMEOUT (e.g. pass a
                     shorter value for opportunistic background probes).
        """
        base = url.rstrip("/")
        now = time.monotonic()
        cached = cls._ollama_probe_cache.get(base)
        if cached is not None and (now - cached[0]) <= cls._OLLAMA_PROBE_TTL:
            return cached[1]
        probe_t = timeout if timeout is not None else cls._OLLAMA_PROBE_TIMEOUT
        try:
            urllib.request.urlopen(base, timeout=probe_t)
            cls._ollama_probe_cache[base] = (now, True)
            return True
        except Exception:
            cls._ollama_probe_cache[base] = (now, False)
            return False

    @classmethod
    def invalidate_ollama_probe_cache(cls) -> None:
        """Force the next probe to hit the network (e.g. after user starts Ollama)."""
        cls._ollama_probe_cache.clear()

    # ------------------------------------------------------------------
    # Local-backend context manager
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _use_local_backend(self):
        """Temporarily route one LLM call through the local Ollama backend.

        Restores original provider/client/enabled on exit, even if an
        exception is raised. This is the only place that mutates these
        attributes during a call; extracting it here avoids the identical
        try/finally blocks that previously appeared three times in _call_llm_stream.
        """
        orig = (self.provider, self.llm_client, self.llm_enabled)
        try:
            self.provider = "ollama"
            self.llm_client = self._local_llm_client
            self.llm_enabled = True
            yield
        finally:
            self.provider, self.llm_client, self.llm_enabled = orig

    # ------------------------------------------------------------------
    # Cloud redaction UX
    # ------------------------------------------------------------------

    _HIGH_ENTROPY_BLOB_RE = re.compile(
        r"\b(?:[A-Za-z0-9_\-]{60,}|[A-Za-z0-9+/]{60,}={0,2})\b"
    )

    @staticmethod
    def _shannon_entropy(s: str) -> float:
        """Return Shannon entropy (bits/char)."""
        if not s:
            return 0.0
        counts: Dict[str, int] = {}
        for ch in s:
            counts[ch] = counts.get(ch, 0) + 1
        n = float(len(s))
        ent = 0.0
        for c in counts.values():
            p = c / n
            ent -= p * math.log2(p)
        return ent

    @staticmethod
    def _looks_like_git_hash(token: str) -> bool:
        t = (token or "").strip()
        if len(t) not in (40, 64):
            return False
        return bool(re.fullmatch(r"[0-9a-fA-F]{%d}" % len(t), t))

    def _unknown_high_entropy_blobs(self, text: str) -> List[str]:
        """Return suspicious high-entropy blobs that are not already covered by known secret patterns."""
        out: List[str] = []
        s = (text or "")
        for m in self._HIGH_ENTROPY_BLOB_RE.finditer(s):
            blob = (m.group(0) or "").strip()
            if not blob:
                continue
            if self._looks_like_git_hash(blob):
                continue
            # If it's already a known secret shape, our normal redaction should catch it.
            if self._LIKELY_SECRET.search(blob):
                continue
            # Entropy threshold: reduce false positives for structured ids.
            if self._shannon_entropy(blob) < 4.0:
                continue
            out.append(blob)
        return out

    def _redact_for_cloud_with_report(self, user_message: str) -> Tuple[str, int, bool]:
        """Redact likely secrets before sending to cloud.

        Returns (routed_message, redaction_count, fail_closed).
        fail_closed indicates we detected a suspicious high-entropy blob that was not redacted.
        """
        if not user_message:
            return user_message, 0, False

        original_intent = self._extract_user_intent_text(user_message)
        original_redacted_tokens = original_intent.count("<REDACTED>")

        # Prefer local model for redaction when available; otherwise fall back to regex.
        routed = user_message
        changed = False

        for prefix in ("User's request:", "User question:", "User query:"):
            idx = routed.find(prefix)
            if idx < 0:
                continue
            line_start = idx
            line_end = routed.find("\n", idx)
            if line_end < 0:
                line_end = len(routed)
            line = routed[line_start:line_end]
            before = prefix
            after = line[len(prefix) :]
            raw = after.strip()
            if not raw:
                continue

            if self._LIKELY_SECRET.search(raw):
                redacted = self._redact_text_local(raw) or self._redact_text_regex(raw)
                spacer = after[: len(after) - len(after.lstrip(" "))]
                new_line = f"{before}{spacer}{redacted}"
                routed = routed[:line_start] + new_line + routed[line_end:]
                changed = True
            break

        if not changed:
            # Fallback: redact the extracted intent line if it looks secret-y.
            if original_intent and self._LIKELY_SECRET.search(original_intent):
                redacted_intent = self._redact_text_local(original_intent) or self._redact_text_regex(original_intent)
                # Replace only the first occurrence to keep the change bounded.
                routed = user_message.replace(original_intent, redacted_intent, 1)

        redacted_intent_now = self._extract_user_intent_text(routed)
        redaction_count = max(
            0,
            redacted_intent_now.count("<REDACTED>") - original_redacted_tokens,
        )

        # Fail closed if we see high-entropy blobs that remain unredacted.
        suspicious = self._unknown_high_entropy_blobs(original_intent)
        fail_closed = any(blob in redacted_intent_now for blob in suspicious)

        return routed, redaction_count, fail_closed

    # ------------------------------------------------------------------
    # Provider initialisation
    # ------------------------------------------------------------------

    def _client_for_embeddings(self):
        """
        Return an OpenAI-compatible client that can call ``embeddings.create``.

        Uses the primary client when provider is *openai* or *ollama*; otherwise
        tries ``OPENAI_API_KEY`` so vector search can work without a chat LLM.
        """
        if self.llm_client is not None and self.provider in ("openai", "ollama"):
            return self.llm_client
        if self._openai_embedding_client is not None:
            return self._openai_embedding_client
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            return None
        try:
            from openai import OpenAI

            self._openai_embedding_client = OpenAI(api_key=key)
            return self._openai_embedding_client
        except Exception:
            return None

    def supports_embedding_api(self) -> bool:
        """True if embedding vectors can be fetched (OpenAI or Ollama, including API-key-only)."""
        return self._client_for_embeddings() is not None

    def initialize_llm(
        self,
        provider: str,
        api_key: str,
        base_url: Optional[str] = None,
        *,
        skip_probe: bool = False,
    ) -> bool:
        """
        Initialize LLM client.

        Args:
            provider:    "openai" | "anthropic" | "ollama"
            api_key:     API key (use any non-empty string for ollama)
            base_url:    Base URL override (required for ollama, optional for openai-compatible)
            skip_probe:  When True, skip the Ollama reachability probe (caller already verified).
        """
        if not api_key:
            return False

        try:
            if provider in _OPENAI_COMPAT_PROVIDERS:
                from openai import OpenAI
                kwargs: Dict[str, Any] = {
                    "api_key": api_key,
                    # Avoid indefinite hangs on bad network / blocked provider.
                    "timeout": self._request_timeout_seconds(),
                    # Keep retries low; retries + long timeouts can feel like a hang.
                    "max_retries": 2,
                }
                if provider == "ollama":
                    url = base_url or "http://localhost:11434"
                    kwargs["base_url"] = url.rstrip("/") + "/v1"
                    # Probe Ollama before marking as ready — fail fast with a clear
                    # message rather than hanging on the first query.
                    # Skip when the caller has already verified reachability (avoids
                    # double-probing on 'use ollama' or auto-detect paths).
                    if not skip_probe and not self.__class__._probe_ollama_url(url):
                        print(
                            f"[Warning] Ollama is not reachable at {url}. "
                            "Start Ollama and restart cliara to enable local LLM."
                        )
                        return False
                elif provider in _PROVIDER_BASE_URLS:
                    kwargs["base_url"] = _PROVIDER_BASE_URLS[provider]
                elif base_url:
                    kwargs["base_url"] = base_url
                self.llm_client = OpenAI(**kwargs)
                self.provider = provider
                self.llm_enabled = True
                return True

            elif provider == "anthropic":
                from anthropic import Anthropic
                self.llm_client = Anthropic(api_key=api_key)
                self.provider = "anthropic"
                self.llm_enabled = True
                return True

            else:
                print(f"[Error] Unknown LLM provider: {provider}")
                return False

        except ImportError:
            pkg = "anthropic" if provider == "anthropic" else "openai"
            print(f"[Error] {pkg} package not installed. Run: pip install {pkg}")
            return False

        except Exception as e:
            print(f"[Error] Failed to initialize LLM: {e}")
            return False

    def initialize_local_ollama(
        self,
        base_url: Optional[str] = None,
        *,
        skip_probe: bool = False,
    ) -> bool:
        """Initialize a *secondary* local Ollama backend without changing the cloud provider.

        This enables the transparent local/cloud router. Safe to call even when
        cloud is unconfigured.

        Args:
            skip_probe: When True, skip the reachability probe (caller already verified).
        """
        url = (base_url or (self.config.get_ollama_base_url() if self.config is not None else None) or "http://localhost:11434").strip()
        if not url:
            return False
        try:
            from openai import OpenAI

            kwargs: Dict[str, Any] = {
                "api_key": "ollama",  # pragma: allowlist secret
                "base_url": url.rstrip("/") + "/v1",
                "timeout": self._request_timeout_seconds(),
                "max_retries": 1,
            }

            # Probe Ollama before enabling. Uses the class-level cached probe so
            # calling both initialize_llm() and initialize_local_ollama() for the
            # same URL in one startup does not result in two network round-trips.
            # Use a short 0.5 s timeout: this is an opportunistic secondary-backend
            # probe (localhost only); if Ollama is running it responds in <5 ms.
            if not skip_probe and not self.__class__._probe_ollama_url(url, timeout=0.5):
                return False

            self._local_llm_client = OpenAI(**kwargs)
            self._local_enabled = True
            # Treat overall LLM as enabled if either backend is available.
            self.llm_enabled = bool(self.llm_enabled or self._local_enabled)
            return True
        except Exception:
            return False

    def force_local_next_request(self) -> None:
        """Force the next LLM call to use the local backend (one-shot)."""
        self._force_local_next = True

    def _select_backend(
        self,
        agent_type: str,
        user_message: str,
    ) -> Literal["local", "cloud"]:
        """Choose local vs cloud for a single LLM call.

        Policy:
        - If only one backend exists, use it.
        - If forced, use local (one-shot).
        - Trivial/classifier/redaction tasks prefer local.
        - Hard reasoning (long/complex prompts) prefers cloud.
        """
        cloud_ok = bool(self.llm_client is not None and self.provider)
        local_ok = bool(self._local_enabled and self._local_llm_client is not None)

        if local_ok and not cloud_ok:
            return "local"
        if cloud_ok and not local_ok:
            return "cloud"
        if not cloud_ok and not local_ok:
            return "cloud"

        if self._force_local_next and local_ok:
            self._force_local_next = False
            return "local"

        if agent_type in {"nl_router", "redact"}:
            return "local"

        # Heuristic complexity score
        q = self._extract_user_intent_text(user_message)
        prompt_len = len(user_message or "")
        q_len = len(q)

        # Very large prompts are usually grounding-heavy; prefer cloud.
        if prompt_len >= 3500:
            return "cloud"

        # Lightweight tasks: short prompt + short request -> local.
        if q_len <= 140 and prompt_len <= 1800:
            return "local"

        hard_keywords = (
            "design",
            "architecture",
            "refactor",
            "root cause",
            "why is",
            "why does",
            "debug",
            "optimize",
            "tradeoff",
            "pros and cons",
            "compare",
            "implement",
            "migration",
            "performance",
            "security",
            "concurrency",
            "deadlock",
            "race condition",
        )
        q_low = (q or "").lower()
        if any(k in q_low for k in hard_keywords):
            return "cloud"

        # Multi-line or step-heavy tends to require more reasoning.
        if (user_message or "").count("\n") >= 18:
            return "cloud"
        if re.search(r"\b(step|steps|plan|workflow|multi[- ]step)\b", q_low):
            return "cloud"

        # Default: cloud when uncertain.
        return "cloud"

    @staticmethod
    def _extract_user_intent_text(user_message: str) -> str:
        """Try to extract the short user intent line from the full prompt."""
        text = (user_message or "").strip()
        if not text:
            return ""
        # Common prompt prefixes in this file.
        for prefix in ("User's request:", "User question:", "User query:"):
            m = re.search(re.escape(prefix) + r"\s*(.*)", text)
            if m:
                return (m.group(1) or "").strip()
        # Fallback: first line
        return (text.splitlines()[0] if text else "").strip()

    _LIKELY_SECRET = re.compile(
        r"(sk-[A-Za-z0-9]{20,}"
        r"|ghp_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|AIza[0-9A-Za-z\-_]{20,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"|\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*[^\s]{8,})",
        flags=re.IGNORECASE,
    )

    def _maybe_redact_request_line_for_cloud(self, user_message: str) -> str:
        """Redact likely secrets in the *user intent line* before sending to cloud."""
        if not user_message:
            return user_message

        # Only redact the query text after known prefixes (keeps output bounded).
        for prefix in ("User's request:", "User question:", "User query:"):
            idx = user_message.find(prefix)
            if idx < 0:
                continue
            # Extract the rest of that line.
            line_start = idx
            line_end = user_message.find("\n", idx)
            if line_end < 0:
                line_end = len(user_message)
            line = user_message[line_start:line_end]
            # Split at prefix.
            before = prefix
            after = line[len(prefix):]
            raw = after.strip()
            if not raw:
                continue
            if not self._LIKELY_SECRET.search(raw):
                continue

            redacted = self._redact_text_local(raw)
            if not redacted:
                redacted = self._redact_text_regex(raw)

            # Preserve original spacing after prefix when possible.
            spacer = after[: len(after) - len(after.lstrip(" "))]
            new_line = f"{before}{spacer}{redacted}"
            return user_message[:line_start] + new_line + user_message[line_end:]

        return user_message

    def _redact_text_regex(self, text: str) -> str:
        """Conservative regex redaction fallback."""
        if not text:
            return text

        def _sub(m: re.Match) -> str:
            g = m.group(0)
            # Preserve label when present.
            if re.search(r"(?i)\b(api[_-]?key|token|secret|password)\b", g):
                # Replace only the value portion.
                parts = re.split(r"([:=])", g, maxsplit=1)
                if len(parts) >= 3:
                    return parts[0] + parts[1] + " <REDACTED>"
            return "<REDACTED>"

        return self._LIKELY_SECRET.sub(_sub, text)

    def _redact_text_local(self, text: str) -> str:
        """Use local model to redact *text* when available."""
        if not (self._local_enabled and self._local_llm_client is not None):
            return ""
        try:
            with self._use_local_backend():
                return (self._call_llm_stream_impl("redact", text, stream_callback=None) or "").strip()
        except Exception:
            return ""

    def predict_next_backend_for_user_input(self, user_input: str, nl_prefix: str = "?") -> Literal["local", "cloud"]:
        """Best-effort preview used by the prompt glyph (no LLM calls)."""
        cloud_ok = bool(self.llm_client is not None and self.provider)
        local_ok = bool(self._local_enabled and self._local_llm_client is not None)
        if local_ok and not cloud_ok:
            return "local"
        if cloud_ok and not local_ok:
            return "cloud"
        if not (local_ok or cloud_ok):
            return "cloud"
        if self._force_local_next and local_ok:
            return "local"

        s = (user_input or "").strip()
        low = s.lower()
        if low.startswith((nl_prefix or "?").lower()):
            q = s[len(nl_prefix or "?") :].strip()
            # Preview whether it's a question (answer) vs command-generation.
            is_question = bool(re.search(r"\b(what|why|how|when|where|explain)\b", q.lower())) or q.endswith("?")
            agent = "cliara_qa" if is_question else "nl_to_commands"
            # Use query text for complexity.
            return "cloud" if self._select_backend(agent, f"User's request: {q}") == "cloud" else "local"
        if low.startswith("explain "):
            return "cloud" if self._select_backend("explain", f"User question: {s}") == "cloud" else "local"
        # Heuristic: if the user is about to ask for a fix/explanation via NL prefix.
        if low.startswith((nl_prefix or "?").lower() + " ") and re.search(r"\bfix\b", low):
            return "cloud" if self._select_backend("fix", f"User's request: {s}") == "cloud" else "local"
        # Default route indicator: prefer cloud when available.
        return "cloud"

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, agent_type: str) -> str:
        """Return the model name to use for *agent_type*.

        Resolution order:
          1. Per-task config key   (e.g. config model_explain)
          2. Global config llm_model
          3. Provider default      (see _PROVIDER_DEFAULT_MODELS)
        """
        if self.config is not None:
            model = self.config.get_llm_model(agent_type)
            if model:
                # Hybrid routing: when we temporarily switch to Ollama, ignore
                # obviously cloud-only model ids (e.g. gpt-*, claude-*).
                if (self.provider or "") == "ollama" and self._model_looks_cloud_only(model):
                    model = None
            if model and model_id_matches_provider(model, self.provider or ""):
                return model
        return _PROVIDER_DEFAULT_MODELS.get(self.provider or "", "gpt-4o-mini")

    @staticmethod
    def _model_looks_cloud_only(model: str) -> bool:
        m = (model or "").strip().lower()
        if not m:
            return False
        return (
            m.startswith("gpt-")
            or m.startswith("o1")
            or m.startswith("o2")
            or m.startswith("o3")
            or m.startswith("o4")
            or m.startswith("chatgpt-")
            or m.startswith("claude-")
            or m.startswith("gemini")
            or m.startswith("text-embedding-")
        )

    def resolved_model_for_display(self) -> str:
        """Model name for banners and status (primary NL agent resolution)."""
        return self._resolve_model("nl_to_commands")

    def process_query(
        self,
        query: str,
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[List[str], str, DangerLevel]:
        """
        Convert natural language query to commands using LLM.
        
        Args:
            query: Natural language query
            context: Optional context (cwd, os, shell, etc.)
            stream_callback: Optional callback for each streamed token (OpenAI only).
        
        Returns:
            Tuple of (commands, explanation, danger_level)
        """
        if not self.llm_enabled:
            return self._stub_response(query)
        
        try:
            include_listing = self._should_include_directory_listing(query, context)
            context_info = self._build_context(context, include_directory_listing=include_listing)
            prompt = self._create_prompt(query, context_info)
            response = self._call_llm_stream("nl_to_commands", prompt, stream_callback)
            commands, explanation = self._parse_response(response)

            if not commands:
                retry_response = self._retry_nl_to_commands_json(prompt)
                if retry_response:
                    retry_commands, retry_explanation = self._parse_response(retry_response)
                    if retry_commands:
                        commands, explanation = retry_commands, retry_explanation
                    else:
                        explanation = retry_explanation

            if not commands:
                return [], explanation or "Could not parse LLM output into runnable commands", DangerLevel.SAFE

            level, dangerous = self.safety.check_commands(commands)
            return commands, explanation, level
        
        except Exception as e:
            print(f"[Error] LLM processing failed: {e}")
            return [], f"Error: {str(e)}", DangerLevel.SAFE

    def route_query_mode(self, query: str, context: Optional[dict] = None) -> str:
        """Route `?` query via LLM: returns either "answer" or "commands"."""
        if not self.llm_enabled:
            return "commands"

        try:
            ctx = self._build_context(context, include_directory_listing=False)
            prompt = self._create_router_prompt(query, ctx)
            response = self._call_llm("nl_router", prompt)
            return self._parse_router_route(response)
        except Exception:
            # Safe fallback: keep legacy executable path if routing fails.
            return "commands"

    @staticmethod
    def _parse_router_route(response: str) -> str:
        """Parse nl_router output and return normalized route string."""
        raw = NLHandler._extract_json(response)
        if raw:
            try:
                data = json.loads(raw)
                route = str(data.get("route", "")).strip().lower()
                if route in {"answer", "commands"}:
                    return route
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                pass

        text = (response or "").strip().lower()
        if re.search(r"\broute\b[^a-z]+answer\b", text):
            return "answer"
        if re.search(r"\broute\b[^a-z]+commands\b", text):
            return "commands"
        return "commands"

    def answer_query(
        self,
        query: str,
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Answer informational questions directly in plain text (no command execution)."""
        if not self.llm_enabled:
            return "LLM is not configured. Run setup-llm to enable direct natural-language answers."

        try:
            ctx = self._build_context(context, include_directory_listing=False)
            prompt = self._create_answer_prompt(query, ctx)
            return self._call_llm_stream("cliara_qa", prompt, stream_callback).strip()
        except Exception as e:
            return f"Error while answering query: {e}"

    def _retry_nl_to_commands_json(self, prompt: str) -> str:
        """One-shot repair call when initial nl_to_commands output is malformed."""
        strict_prompt = (
            prompt
            + "\n\nIMPORTANT: Your previous response was invalid for execution. "
            + "Return ONLY valid JSON with exactly these top-level keys: "
            + "commands (array of non-empty command strings) and explanation (short string). "
            + "No markdown, no prose outside JSON, no trailing commas."
        )
        try:
            return self._call_llm("nl_to_commands", strict_prompt)
        except Exception:
            return ""
    
    # ------------------------------------------------------------------
    # Shell detection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_shell_fallback() -> str:
        """Auto-detect the user's shell when no configured value is available."""
        if platform.system() == "Windows":
            pwsh = which("pwsh") or which("powershell")
            return pwsh if pwsh else "cmd.exe"
        return os.environ.get("SHELL", "/bin/bash")

    # ------------------------------------------------------------------
    # Directory listing for fuzzy-path resolution
    # ------------------------------------------------------------------

    _SKIP_DIRS = frozenset({
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
        "venv", ".env", ".idea", ".vscode", ".vs", ".mypy_cache",
        ".pytest_cache", ".tox", "dist", "build", ".next", ".nuxt",
        "target", ".cargo", ".gradle", "vendor", "coverage", ".coverage",
        "htmlcov", "bin", "obj",
    })
    _SKIP_SUFFIXES = (".egg-info", ".dist-info")

    @staticmethod
    def _looks_path_or_listing_intent(text: str) -> bool:
        """Heuristic: True when the query likely needs filesystem disambiguation."""
        q = (text or "").strip().lower()
        if not q:
            return False
        if "/" in q or "\\" in q or q.startswith("."):
            return True
        if "what is in" in q or "what's in" in q:
            return True
        return re.search(
            r"\b(folder|directory|dir|path|paths|file|files|list|listing|tree|inside|under|locate|where)\b",
            q,
        ) is not None

    def _should_include_directory_listing(
        self,
        query: str,
        context: Optional[dict] = None,
    ) -> bool:
        """Include directory listing only when likely helpful for path resolution."""
        if context and context.get("directory_listing"):
            return True
        return self._looks_path_or_listing_intent(query)

    def _should_include_directory_listing_for_macro(
        self,
        query: str,
        context: Optional[dict] = None,
    ) -> bool:
        """Like :meth:`_should_include_directory_listing`, plus build/test/project hints for macros."""
        if self._should_include_directory_listing(query, context):
            return True
        q = (query or "").strip().lower()
        if not q:
            return False
        return re.search(
            r"\b(tests?|test\s|build|lint|pyproject|cargo\.toml|makefile|"
            r"src/|app/|dist/|package\.json|project|package|monorepo|"
            r"module|import|script|eslint|prettier|pytest|jest)\b",
            q,
        ) is not None

    def _gather_directory_listing(
        self, cwd_path: str, max_depth: int = 2, max_entries: int = 80,
    ) -> str:
        """
        Build a compact directory tree (up to *max_depth* levels) starting
        from *cwd_path*.  The output is a human-readable indented listing
        that the LLM can use to resolve ambiguous path references.
        """
        root = Path(cwd_path)
        if not root.is_dir():
            return ""

        cache_key = str(root)
        now = time.monotonic()
        cached = self._dir_listing_cache.get(cache_key)
        if cached and (now - cached[0]) <= 5.0:
            return cached[1]

        lines: List[str] = []
        count = 0

        def _scan(directory: Path, indent: str, depth: int):
            nonlocal count
            if depth > max_depth or count >= max_entries:
                return
            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda e: (not e.is_dir(), e.name.lower()),
                )
            except (PermissionError, OSError):
                return

            for entry in entries:
                if count >= max_entries:
                    break
                name = entry.name
                if name.startswith("."):
                    continue
                if entry.is_dir():
                    lower = name.lower()
                    if lower in self._SKIP_DIRS:
                        continue
                    if any(lower.endswith(s) for s in self._SKIP_SUFFIXES):
                        continue
                    lines.append(f"{indent}{name}/")
                    count += 1
                    _scan(entry, indent + "  ", depth + 1)
                else:
                    lines.append(f"{indent}{name}")
                    count += 1

        _scan(root, "  ", 0)

        if count >= max_entries:
            lines.append(f"  ... ({count}+ entries, truncated)")

        listing = "\n".join(lines)
        self._dir_listing_cache[cache_key] = (now, listing)
        return listing

    # ------------------------------------------------------------------
    # Read-only git snapshot (grounding for NL / macros — not prescriptive)
    # ------------------------------------------------------------------

    def _gather_git_readonly_snapshot(self, cwd_path: str) -> str:
        """
        Run a few read-only ``git`` commands in *cwd_path* and return a short
        text block for the LLM. Fails empty if not a git work tree or ``git``
        is unavailable. Cached briefly to avoid repeated subprocess work.
        """
        root = Path(cwd_path).expanduser().resolve()
        if not (root / ".git").exists():
            return ""

        cache_key = str(root)
        now = time.monotonic()
        ttl = 3.0
        max_len = 3500
        cmd_timeout = 4.0

        cached = self._git_snapshot_cache.get(cache_key)
        if cached and (now - cached[0]) <= ttl:
            return cached[1]

        def _run(args: List[str]) -> str:
            try:
                r = subprocess.run(
                    ["git", *args],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=cmd_timeout,
                )
                if r.returncode != 0:
                    return ""
                return (r.stdout or "").strip()
            except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
                return ""

        parts: List[str] = []
        st = _run(["status", "-sb"])
        if st:
            parts.append(f"git status -sb:\n{st}")
        br = _run(["branch", "--show-current"])
        if br:
            parts.append(f"current branch (name): {br}")
        head = _run(["log", "-1", "--oneline"])
        if head:
            parts.append(f"HEAD (latest commit on this branch): {head}")
        rem = _run(["remote"])
        if rem:
            names = [x for x in rem.split() if x]
            if names:
                parts.append(f"git remotes: {', '.join(names)}")
        ab = _run(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
        if ab:
            m = re.match(r"^(\d+)\s+(\d+)$", ab.strip())
            if m:
                parts.append(
                    f"vs upstream (ahead/behind): {m.group(1)} ahead, {m.group(2)} behind"
                )

        out = "\n\n".join(parts)
        if len(out) > max_len:
            out = out[:max_len] + "\n... (truncated)"
        self._git_snapshot_cache[cache_key] = (now, out)
        return out

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(
        self,
        context: Optional[dict] = None,
        include_directory_listing: bool = False,
        include_git_snapshot: bool = False,
    ) -> dict:
        """Build context information for LLM.

        When *include_directory_listing* is True a compact filesystem
        snapshot of the cwd (depth 2) is included so the model can
        resolve ambiguous / fuzzy path references.

        When *include_git_snapshot* is True and the cwd is a git work tree, a
        read-only **git** snapshot (status, branch, last commit, remotes) is
        added under ``git_snapshot`` for grounding. The model still chooses
        commands; this is not a hard-coded macro.
        """
        ctx = context.copy() if context else {}

        ctx.setdefault("os", platform.system())
        ctx.setdefault("shell", self._detect_shell_fallback())
        ctx.setdefault("cwd", str(Path.cwd()))
        ctx.setdefault("runtime", "Cliara")

        # IDE bridge context (active editor file, workspace root, editor name)
        if "ide" not in ctx:
            try:
                from cliara.ide_bridge import peek_bridge

                bridge = peek_bridge()
                if bridge is not None:
                    ide_state = bridge.get_ide_state()
                    if ide_state and (ide_state.active_file or ide_state.workspace_root or ide_state.editor):
                        ctx["ide"] = ide_state.to_dict()
            except Exception:
                pass

        # Detect project type
        cwd = Path(ctx["cwd"])
        if (cwd / "package.json").exists():
            ctx["project_type"] = "node"
        elif (cwd / "requirements.txt").exists() or (cwd / "pyproject.toml").exists():
            ctx["project_type"] = "python"
        elif (cwd / "Cargo.toml").exists():
            ctx["project_type"] = "rust"
        if (cwd / "docker-compose.yml").exists():
            ctx["has_docker"] = True

        if (cwd / ".git").exists():
            ctx["has_git"] = True

        if include_directory_listing and "directory_listing" not in ctx:
            ctx["directory_listing"] = self._gather_directory_listing(ctx["cwd"])

        if include_git_snapshot and ctx.get("has_git"):
            snap = self._gather_git_readonly_snapshot(ctx["cwd"])
            if snap:
                ctx["git_snapshot"] = snap

        return ctx

    @staticmethod
    def _mentioned_cliara_builtins(query: str) -> List[str]:
        """Return built-in command tokens explicitly present in the NL query."""
        tokens = re.findall(r"[a-z0-9][a-z0-9._-]*", (query or "").lower())
        seen = []
        for tok in tokens:
            if tok in _CLIARA_BUILTIN_COMMANDS and tok not in seen:
                seen.append(tok)
        return seen
    
    def _create_prompt(self, query: str, context: dict) -> str:
        """Create the user message for the NL-to-commands agent.

        All behavioural rules live in the system prompt (nl_to_commands.md).
        This method only supplies the request and runtime context.
        """
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "unknown")
        runtime = context.get("runtime", "Cliara")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")
        dir_listing = context.get("directory_listing", "")
        mentioned_builtins = self._mentioned_cliara_builtins(query)

        prompt = f"User's request: {query}\n\nContext:\n"
        prompt += f"- Operating System: {os_name}\n"
        prompt += f"- Runtime: {runtime} interactive shell (Cliara intercepts built-ins before host shell)\n"
        prompt += f"- Host Shell: {shell}\n"
        prompt += f"- Current Directory: {cwd}\n"
        ide = context.get("ide") or {}
        if isinstance(ide, dict):
            af = (ide.get("active_file") or "").strip()
            wr = (ide.get("workspace_root") or "").strip()
            ed = (ide.get("editor") or "").strip()
            if ed:
                prompt += f"- IDE: {ed}\n"
            if wr:
                prompt += f"- IDE workspace: {wr}\n"
            if af:
                prompt += f"- IDE active file: {af}\n"
        prompt += "- Cliara built-ins include: help, explain, push, readme, deploy, session, config, theme, setup-llm, setup-ollama, macro aliases (mc/ml/ma/mr/mh).\n"

        if mentioned_builtins:
            prompt += (
                "- Built-in tokens found in this request: "
                + ", ".join(mentioned_builtins)
                + ". For help/meaning requests, prefer Cliara-native help commands.\n"
            )

        if project_type:
            prompt += f"- Project Type: {project_type}\n"
        if context.get("has_git"):
            prompt += "- Git repository detected\n"
        if context.get("has_docker"):
            prompt += "- Docker Compose detected\n"

        if dir_listing:
            prompt += f"\nDirectory listing (depth 2 from cwd):\n{dir_listing}\n"

        git_snap = (context.get("git_snapshot") or "").strip()
        if git_snap:
            prompt += (
                "\nRead-only **git** snapshot in the current directory (grounding only):\n"
                f"{git_snap}\n"
            )

        return prompt

    def _create_macro_prompt(self, query: str, context: dict) -> str:
        """User message for ``nl_macro_propose`` — same facts as :meth:`_create_prompt` plus macro framing.

        A git snapshot, when present, is already included in *base* via :meth:`_create_prompt`.
        """
        base = self._create_prompt(query, context)
        block = (
            "\n\n---\n"
            "Macro design: output a **reusable** multi-step shell workflow the user can save and run again "
            "in similar projects. Each command is one string in the JSON array, suitable for the host shell. "
            "If a read-only git snapshot appears above, use it only to interpret vague wording (e.g. what "
            '"latest", "clean", or "tip" might mean here); the suggested commands should still be generally '
            "useful routines, not a copy-paste of the snapshot text.\n"
        )
        return base + block

    def _create_answer_prompt(self, query: str, context: dict) -> str:
        """Create informational-answer prompt for direct autonomous responses."""
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "unknown")
        runtime = context.get("runtime", "Cliara")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")

        prompt = f"User question: {query}\n\nContext:\n"
        prompt += f"- Runtime: {runtime}\n"
        prompt += f"- Host shell: {shell}\n"
        prompt += f"- OS: {os_name}\n"
        prompt += f"- CWD: {cwd}\n"
        ide = context.get("ide") or {}
        if isinstance(ide, dict):
            af = (ide.get("active_file") or "").strip()
            wr = (ide.get("workspace_root") or "").strip()
            ed = (ide.get("editor") or "").strip()
            if ed:
                prompt += f"- IDE: {ed}\n"
            if wr:
                prompt += f"- IDE workspace: {wr}\n"
            if af:
                prompt += f"- IDE active file: {af}\n"
        if project_type:
            prompt += f"- Project type: {project_type}\n"
        prompt += (
            "- Cliara built-ins include: help, explain, push, readme, deploy, session, config, theme/themes, "
            "setup-llm, setup-ollama, macro aliases (mc/ml/ma/mr/mh).\n"
        )
        prompt += (
            "\nAnswer directly and autonomously. If asked about a Cliara command, explain it plainly "
            "with purpose, usage, and related commands. Do not ask the user to run a help command as the primary answer."
        )
        return prompt

    def _create_router_prompt(self, query: str, context: dict) -> str:
        """Create intent-routing prompt for the nl_router agent."""
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "unknown")
        runtime = context.get("runtime", "Cliara")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")

        prompt = f"User query: {query}\n\nContext:\n"
        prompt += f"- Runtime: {runtime}\n"
        prompt += f"- Host shell: {shell}\n"
        prompt += f"- OS: {os_name}\n"
        prompt += f"- CWD: {cwd}\n"
        ide = context.get("ide") or {}
        if isinstance(ide, dict):
            af = (ide.get("active_file") or "").strip()
            wr = (ide.get("workspace_root") or "").strip()
            ed = (ide.get("editor") or "").strip()
            if ed:
                prompt += f"- IDE: {ed}\n"
            if wr:
                prompt += f"- IDE workspace: {wr}\n"
            if af:
                prompt += f"- IDE active file: {af}\n"
        if project_type:
            prompt += f"- Project type: {project_type}\n"
        prompt += (
            "- Cliara built-ins include: help, explain, push, readme, deploy, session, config, theme/themes, "
            "setup-llm, setup-ollama, status, macro aliases (mc/ml/ma/mr/mh).\n"
        )
        prompt += (
            "\nClassify this into route=answer or route=commands and return strict JSON only."
        )
        return prompt
    
    def _call_llm_stream(
        self,
        agent_type: str,
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Call the LLM with transparent local/cloud routing.

        *stream_callback* streams for plain-text agents in
        ``_STREAMING_SAFE_AGENTS`` by default. For JSON-returning agents,
        streaming remains disabled unless the callback is explicitly marked as
        JSON-safe progress-only (attribute ``__cliara_json_safe__ = True``).

        Returns the full assistant reply as a single string.
        """
        backend = self._select_backend(agent_type, user_message)
        self._last_backend_used = backend

        # Cloud UX: redact + (once) preview; only prompt when redaction is uncertain.
        cloud_ok = bool(self.llm_client is not None and self.provider)
        local_ok = bool(self._local_enabled and self._local_llm_client is not None)
        routed_message = user_message
        if backend == "cloud" and cloud_ok:
            routed_message, redacted_n, fail_closed = self._redact_for_cloud_with_report(user_message)

            if not self._cloud_redaction_preview_shown:
                print_dim(f"→ cloud ({redacted_n} secrets redacted)")
                self._cloud_redaction_preview_shown = True

            if fail_closed:
                resp = input("send anyway? [y] ").strip().lower()
                if resp not in ("", "y", "yes"):
                    if local_ok:
                        backend = "local"
                    else:
                        raise RuntimeError("Cancelled cloud send (redaction uncertain).")

        # Execute on the selected backend.
        if backend == "local" and local_ok:
            with self._use_local_backend():
                return self._call_llm_stream_impl(agent_type, routed_message, stream_callback)

        try:
            return self._call_llm_stream_impl(agent_type, routed_message, stream_callback)
        except Exception as exc:
            # If the cloud backend fails (timeouts, DNS, provider down), fall
            # back to local when available so features like `mc ...` don't hang.
            if backend == "cloud" and local_ok:
                try:
                    print_dim("→ cloud failed; retrying locally")
                except Exception:
                    pass
                with self._use_local_backend():
                    return self._call_llm_stream_impl(agent_type, user_message, stream_callback)
            raise

    def _call_llm_stream_impl(
        self,
        agent_type: str,
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Original LLM call implementation (no routing)."""
        if agent_type not in AGENT_REGISTRY:
            raise ValueError(f"Unknown agent type: {agent_type}")
        cfg = AGENT_REGISTRY[agent_type]
        system: str = cfg["system"]
        temperature: float = cfg["temperature"]
        max_tokens: int = cfg["max_tokens"]
        model: str = self._resolve_model(agent_type)
        max_tokens = self._effective_max_tokens(agent_type, max_tokens)

        # Enforce safety: JSON agents can only stream to explicitly-marked
        # progress callbacks (no raw token rendering by default).
        allow_json_progress = bool(
            stream_callback is not None
            and getattr(stream_callback, "__cliara_json_safe__", False)
        )
        safe_cb = stream_callback if (agent_type in _STREAMING_SAFE_AGENTS or allow_json_progress) else None

        if self.provider in _OPENAI_COMPAT_PROVIDERS:
            return self._call_openai_compat(
                system,
                user_message,
                model,
                temperature,
                max_tokens,
                safe_cb,
                agent_type,
            )
        elif self.provider == "anthropic":
            return self._call_anthropic(
                system, user_message, model, temperature, max_tokens, safe_cb
            )
        else:
            raise Exception(f"Unsupported provider: {self.provider}")

    def _effective_max_tokens(self, agent_type: str, default_max_tokens: int) -> int:
        """Apply provider-specific caps so local models return faster by default."""
        base = max(1, int(default_max_tokens))
        if self.provider != "ollama":
            return base

        cap_raw: Any = None
        if self.config is not None:
            if agent_type == "nl_to_commands":
                cap_raw = self.config.get("ollama_max_tokens_nl", 320)
            elif agent_type == "nl_macro_propose":
                cap_raw = self.config.get("ollama_max_tokens_macro", 500)
            elif agent_type == "readme":
                cap_raw = self.config.get("ollama_max_tokens_readme", 8192)
            else:
                cap_raw = self.config.get("ollama_max_tokens_cap", 768)
        else:
            if agent_type == "nl_to_commands":
                cap_raw = 320
            elif agent_type == "nl_macro_propose":
                cap_raw = 500
            elif agent_type == "readme":
                cap_raw = 8192
            else:
                cap_raw = 768

        try:
            cap = int(cap_raw)
        except (TypeError, ValueError):
            return base
        if cap <= 0:
            return base
        return min(base, cap)

    def _openai_compat_error_message(self, err: Exception) -> str:
        """Turn upstream errors into a short message; add hints for known platform failures."""
        msg = str(err)
        out = f"{self.provider} API error: {err}"
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            out += (
                "\n  Hint: Request timed out. This is usually a network/DNS/proxy/firewall issue. "
                "If you're on a restricted network, try another provider (Groq/Gemini), "
                "or run Ollama locally (setup-ollama), then `use ollama`."
            )
        if "getaddrinfo failed" in msg.lower() or "name or service not known" in msg.lower():
            out += "\n  Hint: DNS lookup failed. Check connectivity and proxy settings."
        if "Application not found" in msg and "404" in msg:
            out += (
                "\n  Hint: The hosted API (often Railway) returned ΓÇ£Application not foundΓÇ¥. "
                "That usually means the gateway URL is wrong, the service is not reachable, "
                "or public networking was misconfigured ΓÇö not a problem with your macro text. "
                "Check CLIARA_GATEWAY_URL, try GET ΓÇª/health on the gateway host, or set "
                "OPENAI_API_KEY / GROQ_API_KEY to use a provider directly."
            )
        return out

    def _call_openai_compat(
        self,
        system: str,
        user_message: str,
        model: str,
        temperature: float,
        max_tokens: int,
        stream_callback: Optional[Callable[[str], None]],
        agent_type: str,
    ) -> str:
        """OpenAI / Ollama (OpenAI-compatible) completion ΓÇö with optional streaming."""
        request_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if self.provider == "ollama":
            extra_body: Dict[str, Any] = {}
            options: Dict[str, Any] = {}

            keep_alive_raw = "15m"
            num_ctx_raw: Any = 4096
            if self.config is not None:
                keep_alive_raw = self.config.get("ollama_keep_alive", "15m")
                num_ctx_raw = self.config.get("ollama_num_ctx", 4096)
                if agent_type == "readme":
                    try:
                        base_ctx = int(num_ctx_raw)
                    except (TypeError, ValueError):
                        base_ctx = 4096
                    readme_ctx_raw = self.config.get("ollama_num_ctx_readme", 32768)
                    try:
                        readme_ctx = int(readme_ctx_raw)
                    except (TypeError, ValueError):
                        readme_ctx = base_ctx
                    num_ctx_raw = max(base_ctx, readme_ctx)

            keep_alive = str(keep_alive_raw or "").strip()
            if keep_alive:
                extra_body["keep_alive"] = keep_alive

            try:
                num_ctx = int(num_ctx_raw)
                if num_ctx > 0:
                    options["num_ctx"] = num_ctx
            except (TypeError, ValueError):
                pass

            if options:
                extra_body["options"] = options
            if extra_body:
                request_kwargs["extra_body"] = extra_body

        try:
            if stream_callback is not None:
                stream = self.llm_client.chat.completions.create(stream=True, **request_kwargs)
                full_content: List[str] = []
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta_obj = chunk.choices[0].delta
                    piece = _openai_compat_text_from_content(
                        getattr(delta_obj, "content", None) if delta_obj else None
                    )
                    if piece:
                        stream_callback(piece)
                        full_content.append(piece)
                return "".join(full_content).strip()
            else:
                response = self.llm_client.chat.completions.create(**request_kwargs)
                msg = response.choices[0].message
                text = _openai_compat_text_from_content(getattr(msg, "content", None))
                return text.strip()
        except Exception as e:
            raise Exception(self._openai_compat_error_message(e)) from e

    def _call_anthropic(
        self,
        system: str,
        user_message: str,
        model: str,
        temperature: float,
        max_tokens: int,
        stream_callback: Optional[Callable[[str], None]],
    ) -> str:
        """Anthropic completion ΓÇö with optional streaming."""
        try:
            if stream_callback is not None:
                with self.llm_client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                ) as stream:
                    full_content: List[str] = []
                    for text in stream.text_stream:
                        stream_callback(text)
                        full_content.append(text)
                    return "".join(full_content).strip()
            else:
                response = self.llm_client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                return response.content[0].text.strip()
        except Exception as e:
            raise Exception(f"Anthropic API error: {e}")

    def _call_llm(self, agent_type: str, user_message: str) -> str:
        """Non-streaming LLM call. Convenience wrapper around _call_llm_stream."""
        return self._call_llm_stream(agent_type, user_message, stream_callback=None)

    def session_reflect_plan(self, briefing: str) -> List[Dict[str, Any]]:
        """
        session_reflect skill: return validated reflection steps (choice / text / long_text).
        Always returns at least the offline default plan.
        """
        default = _default_session_reflect_plan()
        if not self.llm_enabled:
            return default
        try:
            user_msg = (
                f"{briefing}\n\n"
                "Return only the JSON object with key \"steps\" as specified in your instructions."
            )
            text = self._call_llm("session_reflect", user_msg)
            raw = self._extract_json(text)
            if not raw:
                return default
            data = json.loads(raw)
            validated = _validate_session_reflect_steps(data)
            if validated and len(validated) >= 2:
                return validated
        except Exception:
            pass
        return default

    def chat_polish_bundle(self, bundle_markdown: str) -> str:
        """Optional: compress a Cliara chat export for Cursor/Copilot. Requires LLM."""
        if not self.llm_enabled:
            raise RuntimeError("LLM is not configured. Run setup-llm or set API keys.")
        return self._call_llm(
            "chat_polish",
            "Here is the Cliara context to compress:\n\n" + bundle_markdown,
        )

    @staticmethod
    def _slice_balanced_json_object(text: str, start: int) -> Optional[str]:
        """Return substring from *start* ('{') through matching '}', or None if unbalanced."""
        if start < 0 or start >= len(text) or text[start] != "{":
            return None
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """Find a complete JSON object in *text*.

        Tries every ``{`` position so a bad first slice (nested prose, invalid
        JSON) does not block a valid object later. Also tolerates trailing commas
        in one common failure mode from local models.
        """
        # Strip markdown fences first
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*", "", text)
        text = text.strip()

        # Fast path: the whole string is valid JSON
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        search_from = 0
        while search_from < len(text):
            start = text.find("{", search_from)
            if start == -1:
                break
            candidate = NLHandler._slice_balanced_json_object(text, start)
            if candidate:
                for fix in (candidate, re.sub(r",\s*}", "}", candidate)):
                    try:
                        json.loads(fix)
                        return fix
                    except json.JSONDecodeError:
                        continue
            search_from = start + 1
        return None

    @staticmethod
    def _response_snippet(text: str, max_chars: int = 220) -> str:
        """Compact single-line sample of model output for error reporting."""
        if not text:
            return "(empty response)"
        one = re.sub(r"\s+", " ", text).strip()
        if len(one) <= max_chars:
            return one
        return one[: max_chars - 3] + "..."

    @staticmethod
    def _looks_like_shell_command_line(line: str) -> bool:
        """Heuristic for extracting command lines from malformed plain-text replies."""
        s = (line or "").strip().strip("`")
        if not s:
            return False
        if s.startswith("#"):
            return False

        low = s.lower()
        if low.startswith(("commands", "explanation", "note", "output", "json", "here")):
            return False
        if "{" in s or "}" in s:
            return False

        # Remove list markers: "1.", "-", "*"
        s = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", s).strip()
        if not s:
            return False

        first = s.split()[0].strip().strip('"\'')
        if not re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", first):
            return False

        stop = {
            "the", "this", "that", "these", "those", "it", "you", "we", "i", "a", "an", "to", "for",
            "because", "please", "use", "run", "then", "and", "or", "if", "when", "return", "valid", "json",
        }
        if first.lower() in stop:
            return False

        return True

    def _parse_response(self, response: str) -> Tuple[List[str], str]:
        """Parse LLM response and extract commands.

        Handles local models that wrap JSON in prose or markdown fences.
        """
        raw = self._extract_json(response)
        if raw:
            try:
                data = json.loads(raw)
                commands = data.get("commands", [])
                explanation = data.get("explanation", "Generated commands")
                if isinstance(commands, str):
                    commands = [commands]
                if isinstance(commands, list):
                    commands = [str(c).strip() for c in commands if str(c).strip()]
                else:
                    commands = []
                if commands:
                    return commands, explanation
            except (json.JSONDecodeError, AttributeError):
                pass

        # Last-resort: pull shell-looking lines from plain text
        lines = response.split("\n")
        commands: List[str] = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line).strip().strip("`")
            if self._looks_like_shell_command_line(cleaned):
                commands.append(cleaned)
        if commands:
            return commands, "Generated from natural language query"
        sample = self._response_snippet(response)
        return [], f"Could not parse LLM output into commands. Model output sample: {sample}"
    
    def generate_commands_from_nl(
        self,
        nl_description: str,
        context: Optional[dict] = None,
        *,
        include_git_snapshot: bool = False,
    ) -> List[str]:
        """
        Generate commands from natural language description (for NL macros).
        
        Args:
            nl_description: Natural language description of what to do
            context: Optional context information
            include_git_snapshot: When True, attach read-only git snapshot if cwd is a repo (macro fallback path).
        
        Returns:
            List of shell commands
        """
        if not self.llm_enabled:
            return [f"# LLM not configured: {nl_description}"]
        
        try:
            include_listing = self._should_include_directory_listing(nl_description, context)
            context_info = self._build_context(
                context,
                include_directory_listing=include_listing,
                include_git_snapshot=include_git_snapshot,
            )
            prompt = self._create_prompt(nl_description, context_info)
            response = self._call_llm("nl_to_commands", prompt)
            commands, _ = self._parse_response(response)
            return commands if commands else [f"# Could not generate: {nl_description}"]
        except Exception as e:
            return [f"# Error generating commands: {str(e)}"]

    @staticmethod
    def _sanitize_macro_name(raw: Optional[str]) -> Optional[str]:
        """Normalize LLM-suggested macro name to a safe slug."""
        if raw is None:
            return None
        s = str(raw).strip().lower()
        if not s:
            return None
        s = re.sub(r"[\s_]+", "-", s)
        s = re.sub(r"[^a-z0-9-]+", "", s)
        s = re.sub(r"-+", "-", s).strip("-")
        if not s:
            return None
        if s[0].isdigit():
            s = "m-" + s
        if len(s) > 48:
            s = s[:48].rstrip("-")
        if len(s) < 2:
            return None
        return s

    def _fallback_macro_name_from_text(self, text: str) -> str:
        """Build a short slug from user text when the model omits macro_name."""
        words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())[:6]
        slug = "-".join(words) if words else "macro"
        out = self._sanitize_macro_name(slug)
        return out if out else "my-macro"

    def _parse_macro_proposal(self, response: str) -> Tuple[Optional[str], List[str], str, str]:
        """Parse nl_macro_propose JSON. Returns (name, commands, description, explanation)."""
        raw = self._extract_json(response)
        if not raw:
            return None, [], "", "Could not parse macro proposal"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, [], "", "Invalid JSON in macro proposal"

        name_raw = data.get("macro_name") or data.get("name") or ""
        name = self._sanitize_macro_name(str(name_raw) if name_raw else None)

        commands = data.get("commands", [])
        if isinstance(commands, str):
            commands = [commands] if commands.strip() else []
        if not isinstance(commands, list):
            commands = []
        commands = [str(c).strip() for c in commands if str(c).strip()]

        desc = data.get("description", "")
        if not isinstance(desc, str):
            desc = str(desc) if desc else ""
        desc = desc.strip()

        expl = data.get("explanation", "")
        if not isinstance(expl, str):
            expl = str(expl) if expl else ""
        expl = expl.strip()

        if not desc and expl:
            desc = expl.split(".")[0][:200]

        return name, commands, desc, expl

    def _parse_macro_proposal_loose(
        self, response: str, nl_description: str
    ) -> Tuple[Optional[str], List[str], str, str]:
        """
        Parse macro JSON; if that fails, accept nl_to_commands-shaped JSON or
        plain-text command lines from the same response.
        """
        name, commands, desc, expl = self._parse_macro_proposal(response)
        if commands:
            if not name:
                name = self._fallback_macro_name_from_text(nl_description)
            return name, commands, desc, expl

        cmd2, expl2 = self._parse_response(response)
        if cmd2:
            nm = self._fallback_macro_name_from_text(nl_description)
            d = (expl2 or "").split(".")[0][:200] if expl2 else ""
            return nm, cmd2, d, expl2 or ""

        return None, [], "", "Could not parse macro proposal"

    def propose_macro_from_nl(
        self,
        nl_description: str,
        context: Optional[dict] = None,
    ) -> Tuple[Optional[str], List[str], str, str]:
        """
        Infer macro name, ordered commands, and description from plain English.

        Returns:
            (macro_name, commands, description, explanation).
            On failure, macro_name is None, commands empty, explanation has the reason.
        """
        if not self.llm_enabled:
            return None, [], "", "LLM not configured"

        try:
            include_listing = self._should_include_directory_listing_for_macro(
                nl_description, context
            )
            context_info = self._build_context(
                context,
                include_directory_listing=include_listing,
                include_git_snapshot=True,
            )
            prompt = self._create_macro_prompt(nl_description, context_info)
            response = self._call_llm("nl_macro_propose", prompt)
            name, commands, desc, expl = self._parse_macro_proposal_loose(
                response, nl_description
            )
            if not commands:
                # Model returned unusable text ΓÇö fall back to command-only agent
                commands_fb = self.generate_commands_from_nl(
                    nl_description, context_info, include_git_snapshot=True
                )
                if (
                    commands_fb
                    and not (len(commands_fb) == 1 and str(commands_fb[0]).startswith("#"))
                ):
                    nm = self._fallback_macro_name_from_text(nl_description)
                    return (
                        nm,
                        commands_fb,
                        desc or nl_description[:200],
                        expl or "Used command generator after macro JSON was missing or invalid.",
                    )
                return name, [], desc, expl or "No commands generated"
            if not name:
                name = self._fallback_macro_name_from_text(nl_description)
            return name, commands, desc, expl
        except Exception as e:
            return None, [], "", f"Error: {e}"

    def explain_command(
        self,
        command: str,
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Explain a shell command in plain English using the LLM.

        Args:
            command: The shell command to explain
            context: Optional context
            stream_callback: Optional callback for each streamed token.

        Returns:
            A plain-English explanation string
        """
        if not self.llm_enabled:
            return self._stub_explain(command)

        try:
            context_info = self._build_context(context)
            os_name = context_info.get("os", "Unknown")
            shell = context_info.get("shell", "bash")

            prompt = f"""Explain this command briefly. Use short bullet points (plain "-" dashes) to break it down so it's easy to scan. No markdown formatting like bold, headers, or code blocks. Keep it concise ΓÇö no long paragraphs. If it's dangerous, mention that too.

OS: {os_name}, Shell: {shell}

Command: {command}"""

            response = self._call_llm_stream("explain", prompt, stream_callback)
            return response.strip()

        except Exception as e:
            return f"Error explaining command: {e}"

    def _stub_explain(self, command: str) -> str:
        """Provide a basic stub explanation when LLM is not available."""
        parts = command.split()
        if not parts:
            return "Empty command ΓÇö nothing to explain."

        base = parts[0]
        explanations = {
            "git": "A version control command. Use 'git --help' or visit https://git-scm.com/docs for details.",
            "ls": "Lists files and directories in the current (or specified) directory.",
            "cd": "Changes the current working directory.",
            "rm": "Removes (deletes) files or directories. Use with caution!",
            "cp": "Copies files or directories.",
            "mv": "Moves or renames files or directories.",
            "docker": "Manages Docker containers, images, and services.",
            "npm": "Node.js package manager for installing and managing JavaScript packages.",
            "pip": "Python package installer.",
            "python": "Runs a Python script or starts the Python interpreter.",
            "node": "Runs a JavaScript file or starts the Node.js REPL.",
            "curl": "Transfers data from or to a server using various protocols.",
            "chmod": "Changes file permissions.",
            "chown": "Changes file ownership.",
            "grep": "Searches for text patterns in files.",
            "find": "Searches for files and directories matching criteria.",
            "ssh": "Connects to a remote machine over a secure shell.",
            "kill": "Sends a signal to a process (usually to terminate it).",
        }

        hint = explanations.get(base, f"'{base}' is a shell command.")
        return (
            f"LLM not configured ΓÇö showing basic info only.\n\n"
            f"  Command: {command}\n"
            f"  Base program: {base}\n"
            f"  {hint}\n\n"
            f"Run 'setup-llm' to configure a free AI provider (Groq, Gemini, or Ollama)."
        )

    @staticmethod
    def _truncate_stream_for_prompt(text: str, max_lines: int = 80) -> str:
        """Truncate long stream text for LLM prompts (head + tail)."""
        if not text:
            return ""
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        head = max(1, (max_lines * 3) // 5)
        tail = max(1, max_lines - head - 3)
        omitted = len(lines) - head - tail
        return (
            "\n".join(lines[:head])
            + f"\n\n... ({omitted} lines omitted) ...\n\n"
            + "\n".join(lines[-tail:])
        )

    def explain_terminal_output(
        self,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Explain a finished command: line + exit code + captured stdout/stderr
        in one narrative (system prompt in explain_output agent).

        Args:
            command: Shell command that ran
            exit_code: Process exit code
            stdout: Captured standard output
            stderr: Captured standard error
            context: Optional cwd/os/shell
            stream_callback: Optional token streamer for the console

        Returns:
            Plain-text explanation
        """
        if not self.llm_enabled:
            return self._stub_explain_terminal_output(
                command, exit_code, stdout, stderr
            )

        context_info = self._build_context(context or {})
        os_name = context_info.get("os", "Unknown")
        shell = context_info.get("shell", "bash")
        cwd = context_info.get("cwd", "")

        # Local models have smaller context windows; reduce I/O line budget.
        _io_lines = 30 if self.provider == "ollama" else 70
        out_t = self._truncate_stream_for_prompt(stdout or "", _io_lines)
        err_t = self._truncate_stream_for_prompt(stderr or "", _io_lines)
        out_lines = len((stdout or "").splitlines())
        err_lines = len((stderr or "").splitlines())

        out_block = f"Stdout ({out_lines} lines, may be truncated):\n{out_t or '(empty)'}"
        err_block = f"Stderr ({err_lines} lines, may be truncated):\n{err_t or '(empty)'}"

        prompt = f"""Command that ran:
{command}

Exit code:
{exit_code}

{out_block}

{err_block}

Context:
- OS: {os_name}
- Shell: {shell}
- Working directory: {cwd}
"""
        try:
            response = self._call_llm_stream(
                "explain_output", prompt, stream_callback
            )
            return (response or "").strip()
        except Exception as e:
            return f"Error explaining output: {e}"

    def _stub_explain_terminal_output(
        self,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> str:
        lines = [
            "LLM not configured ΓÇö stub summary only.",
            f"- Command: {command}",
            f"- Exit code: {exit_code}",
        ]
        o = (stdout or "").strip()
        e = (stderr or "").strip()
        if o:
            snippet = o[:400] + ("ΓÇª" if len(o) > 400 else "")
            lines.append(f"- Stdout ({len(o)} chars): {snippet!r}")
        else:
            lines.append("- Stdout: (empty)")
        if e:
            snippet = e[:400] + ("ΓÇª" if len(e) > 400 else "")
            lines.append(f"- Stderr ({len(e)} chars): {snippet!r}")
        else:
            lines.append("- Stderr: (empty)")
        lines.append(
            "Run 'setup-llm' or log in to Cliara Cloud for a full explanation."
        )
        return "\n".join(lines)

    def summarize_command_for_history(
        self,
        command: str,
        context: Optional[dict] = None,
    ) -> str:
        """
        Generate a one-sentence summary of a command for semantic history search.
        Used when adding commands to the semantic store.

        Args:
            command: The shell command to summarize
            context: Optional context (cwd, os, shell)

        Returns:
            A short sentence (under ~100 chars), or empty string on failure/LLM disabled.
        """
        if not self.llm_enabled:
            return ""
        if not (command or command.strip()):
            return ""

        # Local models benefit from a very compact prompt — they run on every
        # command in a background thread, so keeping prompts tiny matters.
        is_local = (self.provider == "ollama")
        cmd_limit = 300 if is_local else 2000

        cmd_for_prompt = command.strip()
        if len(cmd_for_prompt) > cmd_limit:
            cmd_for_prompt = cmd_for_prompt[:cmd_limit] + " ..."
        try:
            if is_local:
                # Stripped-down prompt: the system prompt carries the task description.
                prompt = f"Command: {cmd_for_prompt}"
            else:
                context_info = self._build_context(context) if context else {}
                os_name = context_info.get("os", "Unknown")
                shell = context_info.get("shell", "bash")
                prompt = f"OS: {os_name}, Shell: {shell}\n\nCommand: {cmd_for_prompt}"
            response = self._call_llm("history_summary", prompt)
            summary = (response or "").strip()
            if len(summary) > 150:
                summary = summary[:147] + "..."
            return summary
        except Exception:
            return ""

    def search_history_by_intent(
        self,
        entries: List[Dict[str, Any]],
        query: str,
        max_entries_in_prompt: int = 100,
        max_chars: int = 12000,
    ) -> List[Dict[str, Any]]:
        """
        Given a list of semantic history entries and a natural language query,
        return the entries that match the user's intent (summary-only path).

        Args:
            entries: List of dicts with at least "command", "summary", "timestamp"
            query: User's search question (e.g. "when did I fix the login bug")
            max_entries_in_prompt: Cap how many entries to send to the LLM
            max_chars: Approximate cap on total prompt length

        Returns:
            Subset of entries that match, in order of appearance in response.
        """
        if not self.llm_enabled or not entries or not (query or "").strip():
            return []
        # Use most recent entries
        entries = entries[-max_entries_in_prompt:]
        lines = []
        total = 0
        for i, e in enumerate(entries, 1):
            cmd = (e.get("command") or "").strip()
            summary = (e.get("summary") or "").strip()
            ts = (e.get("timestamp") or "").strip()
            line = f"{i}. Command: {cmd}"
            if summary:
                line += f" | Summary: {summary}"
            if ts:
                line += f" | Time: {ts}"
            lines.append(line)
            total += len(line) + 1
            if total >= max_chars:
                entries = entries[: i]
                break
        prompt = "Past commands (number, command, summary, time):\n\n" + "\n".join(lines)
        prompt += f"\n\nUser's question: {query.strip()}\n\nReply with only the numbers of matching entries, comma-separated (e.g. 2, 5, 7), or NONE."
        try:
            response = self._call_llm("history_search", prompt)
            response = (response or "").strip().upper()
            if "NONE" in response or not response:
                return []
            # Parse "1, 3, 5" or "1,3,5"
            indices = []
            for part in response.replace(",", " ").split():
                part = part.strip()
                if part.isdigit():
                    idx = int(part)
                    if 1 <= idx <= len(entries) and idx not in indices:
                        indices.append(idx)
            result = [entries[i - 1] for i in indices]
            return result
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Embedding-based semantic search
    # ------------------------------------------------------------------

    def get_embedding(self, text: str) -> Optional[List[float]]:
        """Fetch an embedding vector for *text*.

        Uses OpenAI or the primary Ollama client, or a dedicated OpenAI client
        from ``OPENAI_API_KEY`` when chat uses another provider.  Returns None
        on failure.

        Results are cached per-instance for _EMBEDDING_CACHE_TTL seconds so that
        repeated "? find ..." queries with identical wording don't re-hit the
        embedding API. The cache is bounded to _EMBEDDING_CACHE_MAX entries.
        """
        key = (text or "").strip()
        if not key:
            return None

        # Fast path: serve from cache if still fresh.
        now = time.monotonic()
        cached = self._embedding_cache.get(key)
        if cached is not None and (now - cached[0]) <= self._EMBEDDING_CACHE_TTL:
            return cached[1]

        client = self._client_for_embeddings()
        if client is None:
            return None
        if self.llm_client is client and self.provider == "ollama":
            model = "nomic-embed-text"
        else:
            model = EMBEDDING_MODEL
        try:
            resp = client.embeddings.create(
                model=model,
                input=key,
            )
            vec = resp.data[0].embedding

            # Evict oldest entry when cache is full before inserting.
            if len(self._embedding_cache) >= self._EMBEDDING_CACHE_MAX:
                oldest_key = min(self._embedding_cache, key=lambda k: self._embedding_cache[k][0])
                del self._embedding_cache[oldest_key]
            self._embedding_cache[key] = (now, vec)
            return vec
        except Exception:
            return None

    @staticmethod
    def history_entry_key(e: Dict[str, Any]) -> Tuple[str, str]:
        """Stable id for deduping history rows."""
        return (str(e.get("command", "")), str(e.get("timestamp", "")))

    def keyword_history_candidates(
        self,
        entries: List[Dict[str, Any]],
        query: str,
        top_m: int = 24,
    ) -> List[Dict[str, Any]]:
        """
        Rank entries by simple token overlap between *query* and command+summary.
        """
        q = (query or "").strip().lower()
        if not q or not entries:
            return []
        tokens = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1]
        if not tokens:
            return []

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for e in entries:
            cmd = (e.get("command") or "").strip().lower()
            summary = (e.get("summary") or "").strip().lower()
            hay = f"{cmd} {summary}"
            score = 0.0
            for t in tokens:
                if t in hay:
                    score += 1.0 + min(hay.count(t), 4) * 0.15
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[: max(1, top_m)]]

    @staticmethod
    def _history_query_tokens(query: str) -> List[str]:
        q = (query or "").strip().lower()
        if not q:
            return []
        raw = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1]
        if not raw:
            return []
        stop = {
            "when",
            "what",
            "where",
            "why",
            "how",
            "did",
            "do",
            "does",
            "i",
            "me",
            "my",
            "we",
            "you",
            "the",
            "a",
            "an",
            "to",
            "of",
            "in",
            "on",
            "for",
            "with",
            "and",
            "or",
            "last",
            "recent",
            "recently",
            "latest",
            "most",
            "time",
            "run",
            "ran",
            "command",
            "commands",
            "builds",
        }
        tokens = [t for t in raw if t not in stop]
        # De-dupe while keeping order.
        seen = set()
        out: List[str] = []
        for t in tokens:
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    @staticmethod
    def _is_last_intent_query(query: str) -> bool:
        q = (query or "").strip().lower()
        if not q:
            return False
        # Handle common phrasing: "when did I last ...", "last time", "most recent".
        return bool(
            re.search(r"\b(last|latest|recently|most\s+recent)\b", q)
            and re.search(r"\b(when|what)\b", q)
        ) or bool(re.search(r"\bwhen\s+did\s+i\s+last\b", q))

    @staticmethod
    def _parse_history_timestamp(ts: str):
        if not ts:
            return None
        try:
            from datetime import datetime

            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None

    def _keyword_score_for_entry(self, entry: Dict[str, Any], tokens: List[str]) -> float:
        if not tokens:
            return 0.0
        cmd = (entry.get("command") or "").strip().lower()
        summary = (entry.get("summary") or "").strip().lower()
        if not (cmd or summary):
            return 0.0
        score = 0.0
        for t in tokens:
            # Prefer command matches over summary matches.
            if re.search(r"\b" + re.escape(t) + r"\b", cmd):
                score += 1.0
            elif t in cmd:
                score += 0.65
            if re.search(r"\b" + re.escape(t) + r"\b", summary):
                score += 0.45
            elif t in summary:
                score += 0.25

        # Heuristic: for queries like "last build", de-boost "pip install build" matches
        # where the token is likely a package name, not the user's action.
        if re.search(r"\bpip(?:3)?\s+install\b", cmd) and "install" not in tokens:
            score *= 0.55

        # Heuristic: boost canonical action commands.
        if "build" in tokens and re.search(r"\bpython(?:\.exe)?\s+-m\s+build\b", cmd):
            score += 0.75

        # Normalise to [0, 1] by token count (roughly).
        denom = max(1.0, float(len(tokens)) * 1.2)
        return max(0.0, min(1.0, score / denom))

    def rerank_history_matches(self, matches: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Re-rank history results to be more useful than pure cosine similarity.

        - Adds lightweight keyword overlap scoring.
        - Adds a small recency boost.
        - Special-cases "when did I last ..." to prefer the most recent relevant hit.
        """
        if not matches or not (query or "").strip():
            return matches

        tokens = self._history_query_tokens(query)
        is_last = self._is_last_intent_query(query)

        # Precompute timestamp recency within this candidate set.
        parsed = [self._parse_history_timestamp((e.get("timestamp") or "").strip()) for e in matches]
        valid_times = [t for t in parsed if t is not None]
        t_min = min(valid_times) if valid_times else None
        t_max = max(valid_times) if valid_times else None

        def _recency_norm(t) -> float:
            if t is None or t_min is None or t_max is None or t_min == t_max:
                return 0.0
            span = (t_max - t_min).total_seconds()
            if span <= 0:
                return 0.0
            return max(0.0, min(1.0, (t - t_min).total_seconds() / span))

        scored: List[Tuple[float, Dict[str, Any], float, float, Any]] = []
        for e, t in zip(matches, parsed):
            emb = e.get("_embedding_score")
            try:
                emb_f = float(emb) if emb is not None else 0.0
            except Exception:
                emb_f = 0.0
            # Cosine similarity is typically [0.3..0.9] after filtering; clamp anyway.
            emb_f = max(0.0, min(1.0, emb_f))
            kw = self._keyword_score_for_entry(e, tokens)
            rec = _recency_norm(t)

            if is_last and tokens:
                rank = 0.75 * kw + 0.15 * rec + 0.10 * emb_f
            else:
                rank = 0.65 * emb_f + 0.30 * kw + 0.05 * rec

            out_e = dict(e)
            out_e["_keyword_score"] = kw
            out_e["_rank_score"] = rank
            scored.append((rank, out_e, kw, emb_f, t))

        # For "last" queries, prefer most-recent among clearly relevant hits.
        if is_last:
            rel = [s for s in scored if s[2] >= 0.25]
            base = rel if rel else scored
            base.sort(key=lambda x: (x[2], x[4] or 0, x[0]), reverse=True)
            return [e for _rank, e, _kw, _emb, _t in base]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _rank, e, _kw, _emb, _t in scored]

    def merge_embedding_keyword_results(
        self,
        vector_matches: List[Dict[str, Any]],
        all_entries: List[Dict[str, Any]],
        query: str,
        target_k: int,
        keyword_pool: int = 24,
    ) -> List[Dict[str, Any]]:
        """
        Keep vector ordering, then pad with keyword candidates not already present
        until *target_k*. If vectors are empty, return keyword hits only.
        """
        target_k = max(1, int(target_k))
        seen = {self.history_entry_key(e) for e in vector_matches}
        out: List[Dict[str, Any]] = list(vector_matches)
        if len(out) >= target_k:
            return out[:target_k]
        extra = self.keyword_history_candidates(all_entries, query, top_m=keyword_pool)
        for e in extra:
            k = self.history_entry_key(e)
            if k in seen:
                continue
            seen.add(k)
            out.append(e)
            if len(out) >= target_k:
                break
        return out[:target_k]

    def search_history_by_embeddings(
        self,
        entries: List[Dict[str, Any]],
        query: str,
        top_k: int = 10,
        min_score: float = 0.30,
        adaptive: bool = False,
        adaptive_frac: float = 0.82,
    ) -> List[Dict[str, Any]]:
        """
        Vector-similarity search over semantic history entries.

        Entries that already have an ``embedding`` field are ranked by cosine
        similarity to the query embedding.  Entries without embeddings are
        excluded from this path (they were added before the feature was
        enabled).

        Returns up to *top_k* entries in descending similarity order.
        *min_score* is the minimum cosine similarity; if *adaptive* is True,
        the cutoff is ``max(min_score, best_score * adaptive_frac)``.
        Returns an empty list if no embeddings are stored yet or the API call
        fails (caller should fall back to summary-only search).
        """
        if not self.supports_embedding_api() or not entries or not (query or "").strip():
            return []

        # Filter to only entries that have embeddings
        with_emb = [e for e in entries if e.get("embedding")]
        if not with_emb:
            return []

        query_emb = self.get_embedding(query)
        if not query_emb:
            return []

        q = np.asarray(query_emb, dtype=np.float32)
        try:
            M = np.stack(
                [np.asarray(e["embedding"], dtype=np.float32) for e in with_emb],
                axis=0,
            )
        except ValueError:
            return []

        if M.ndim != 2 or M.shape[1] != q.shape[0]:
            return []

        q_norm = q / (np.linalg.norm(q) + 1e-12)
        row_norms = np.linalg.norm(M, axis=1, keepdims=True)
        M_norm = M / (row_norms + 1e-12)
        scores = M_norm @ q_norm

        order = np.argsort(-scores)
        top_k = max(1, int(top_k))
        if order.size == 0:
            return []
        best = float(scores[int(order[0])])
        if adaptive and adaptive_frac > 0:
            threshold = max(float(min_score), best * float(adaptive_frac))
        else:
            threshold = float(min_score)
        out: List[Dict[str, Any]] = []
        for rank in range(min(top_k, int(order.size))):
            i = int(order[rank])
            if float(scores[i]) >= threshold:
                e = dict(with_emb[i])
                e["_embedding_score"] = float(scores[i])
                out.append(e)
        return out

    # ------------------------------------------------------------------
    # Commit-message extraction helpers
    # ------------------------------------------------------------------

    def _extract_commit_message(self, response: str) -> str:
        """Multi-strategy extraction — returns a ready-to-use CC-format line.

        Strategies tried in order (inspired by aicommits / gptcommit approach):
          1. JSON  ``{"message": "..."}``  — explicit structured output
          2. Exact CC-format regex via _first_usable_commit_line
          3. Smart plain-text: take the first reasonable line and auto-prefix CC type

        Returns "" only when the response is empty or completely garbled.
        """
        text = (response or "").strip()
        if not text:
            return ""

        # --- Strategy 1: JSON {"message": "..."} ---
        raw_json = self._extract_json(text)
        if raw_json:
            try:
                data = json.loads(raw_json)
                msg = (data.get("message") or "").strip().strip("'\"")
                if msg and len(msg) >= 10:
                    if not _CC_LINE.match(msg):
                        cc_type = _infer_cc_type(msg)
                        msg = f"{cc_type}: {msg[0].lower()}{msg[1:]}"
                    return msg[:120]
            except Exception:
                pass

        # --- Strategy 2: Exact CC-format regex (handles preamble, fences, etc.) ---
        msg = _first_usable_commit_line(text)
        if msg:
            return msg

        # --- Strategy 3: Smart plain-text — preserve model's description, infer type ---
        # Used when the model returns a valid description but without the CC prefix.
        # This means the model's actual intent IS used — we just fix the format.
        for line in text.splitlines():
            s = line.strip().strip("'\"").strip("`").strip()
            if not s:
                continue
            if _MD_FENCE_LINE.match(s):
                continue
            if len(s) < 10 or len(s) > 120:
                continue
            if _COMMIT_PREAMBLE_RE.match(s):
                continue
            # Skip lines that are clearly not a commit description
            if s.startswith(("#", "{", "[", "<", "---")):
                continue
            # If it already looks like a CC line, use it directly.
            if _CC_LINE.match(s):
                return s
            # Plain description: infer CC type and prefix.
            cc_type = _infer_cc_type(s)
            # Lower-case the first letter of the description (CC convention).
            desc = s[0].lower() + s[1:] if s else s
            return f"{cc_type}: {desc}"

        return ""

    # ------------------------------------------------------------------
    # Commit-message generation (smart push)
    # ------------------------------------------------------------------

    def generate_commit_message(
        self,
        diff_stat: str,
        diff_content: str,
        files: List[str],
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Generate a conventional commit message from a git diff.

        Args:
            diff_stat:    Output of ``git diff --cached --stat``
            diff_content: Output of ``git diff --cached`` (may be truncated)
            files:        List of changed file paths
            context:      Optional dict with branch, cwd, os, etc.
            stream_callback: Optional callback for each streamed token.

        Returns:
            A single-line conventional commit message.
        """
        if not self.llm_enabled:
            return self._stub_commit_message(files, context)

        try:
            ctx = self._build_context(context)
            branch = (context or {}).get("branch", "main")

            # Local models (Ollama) are slow on large inputs — keep context tight.
            is_local = (self.provider == "ollama")
            diff_char_limit = 600 if is_local else 3000
            file_display_limit = 8 if is_local else 50
            max_retries = 1 if is_local else 2

            diff_truncated = diff_content[:diff_char_limit]
            if len(diff_content) > diff_char_limit:
                diff_truncated += "\n... (diff truncated)"

            shown_files = files[:file_display_limit]
            file_list = "\n".join(f"  - {f}" for f in shown_files)
            if len(files) > file_display_limit:
                file_list += f"\n  ... ({len(files) - file_display_limit} more)"

            if is_local:
                # Minimal prompt for Ollama — system prompt carries all rules.
                prompt = (
                    f"Branch: {branch}\nFiles ({len(files)}):\n{file_list}\n\nStat:\n{diff_stat}"
                )
                if diff_truncated:
                    prompt += f"\n\nDiff:\n{diff_truncated}"
            else:
                # Cloud: ask for JSON output so parsing is reliable regardless of
                # what preamble or formatting the model chooses to add.
                # Inspired by aicommits / gptcommit structured-output approach.
                prompt = (
                    f"Branch: {branch}\n\n"
                    f"Files changed:\n{file_list}\n\n"
                    f"Diff summary:\n{diff_stat}\n\n"
                    f"Diff (may be truncated):\n{diff_truncated}\n\n"
                    'Return ONLY this JSON object and nothing else:\n'
                    '{"message": "<type>: <short imperative description>"}\n\n'
                    "where <type> is one of: feat fix refactor docs style test chore perf ci build revert"
                )

            last_response = ""
            for attempt in range(max_retries):
                response = self._call_llm_stream("commit_message", prompt, stream_callback)
                last_response = response
                msg = self._extract_commit_message(response)
                if msg:
                    return msg
                if attempt < max_retries - 1:
                    time.sleep(0.3)

            # _extract_commit_message already tried all strategies including
            # smart type inference. If it still returns "" the model gave us
            # nothing usable — only then fall back to the stub.
            if last_response.strip():
                # The model responded but we couldn't extract a clean line.
                # Take whatever we have and force-prefix it so push can proceed.
                raw = last_response.strip().splitlines()[0][:80].strip("'\"` ")
                if raw:
                    return f"{_infer_cc_type(raw)}: {raw[0].lower()}{raw[1:]}"

            print_dim(
                "  Could not get a commit message from the model; using heuristic. "
                "Check the API key, model id, and provider health."
            )
            return self._stub_commit_message(files, context)

        except Exception as e:
            err = str(e).strip()
            if len(err) > 160:
                err = err[:157] + "..."
            print_dim(f"  Commit message generation failed ({err}); using heuristic.")
            return self._stub_commit_message(files, context)


    def _stub_commit_message(
        self, files: List[str], context: Optional[dict] = None
    ) -> str:
        """
        Best-effort commit message when the LLM is unavailable.

        Inspects file extensions and names to pick a conventional type.
        """
        import os.path

        if not files:
            return "chore: update project files"

        branch = (context or {}).get("branch", "")

        # Categorise files
        docs = []
        tests = []
        configs = []
        source = []

        doc_exts = {".md", ".rst", ".txt", ".adoc"}
        config_names = {
            "pyproject.toml", "setup.cfg", "setup.py", "package.json",
            "tsconfig.json", ".eslintrc", ".prettierrc", "Makefile",
            "Dockerfile", "docker-compose.yml", ".github",
            ".gitignore", "requirements.txt", "Cargo.toml",
        }

        for f in files:
            base = os.path.basename(f)
            ext = os.path.splitext(f)[1].lower()

            if "test" in f.lower() or f.lower().startswith("tests/"):
                tests.append(f)
            elif ext in doc_exts:
                docs.append(f)
            elif base in config_names or f.startswith("."):
                configs.append(f)
            else:
                source.append(f)

        # Pick the dominant category
        if docs and not source and not tests:
            names = ", ".join(os.path.basename(f) for f in docs[:3])
            return f"docs: update {names}"
        if tests and not source and not docs:
            return "test: update tests"
        if configs and not source and not docs and not tests:
            return "chore: update configuration"

        # Branch name hint
        if "fix" in branch.lower() or "bug" in branch.lower():
            prefix = "fix"
        elif "feat" in branch.lower() or "feature" in branch.lower():
            prefix = "feat"
        else:
            prefix = "chore"

        if len(files) == 1:
            name = os.path.basename(files[0])
            return f"{prefix}: update {name}"

        return f"{prefix}: update {len(files)} files"

    # ------------------------------------------------------------------
    # Deploy steps (no platform detected ΓÇö user describes, deploy agent suggests steps)
    # ------------------------------------------------------------------

    def generate_deploy_steps(
        self,
        description: str,
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> List[str]:
        """
        Generate an ordered list of deploy steps (shell commands) from the user's
        description and project context. Uses the deploy agent.

        Returns:
            List of shell commands, or a single comment line if LLM disabled/failed.
        """
        if not self.llm_enabled:
            return [f"# LLM not configured: {description}"]

        try:
            context_info = self._build_context(context)
            prompt = self._create_deploy_prompt(description, context_info)
            response = self._call_llm_stream("deploy", prompt, stream_callback)
            raw = self._extract_json(response)
            if not raw:
                return [f"# Could not parse deploy steps from response"]
            data = json.loads(raw)
            commands = data.get("commands", [])
            if isinstance(commands, str):
                commands = [commands]
            return commands if commands else [f"# Could not generate deploy steps: {description}"]
        except Exception as e:
            return [f"# Error generating deploy steps: {str(e)}"]

    def _create_deploy_prompt(self, description: str, context: dict) -> str:
        """Build user message for the deploy agent."""
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "bash")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")

        prompt = f"""User's deploy description: {description}

Context:
- OS: {os_name}
- Shell: {shell}
- Current directory: {cwd}
"""
        if project_type:
            prompt += f"- Project type: {project_type}\n"
        if context.get("has_git"):
            prompt += "- Git repository detected\n"
        if context.get("has_docker"):
            prompt += "- Docker Compose detected\n"

        prompt += """
Return ONLY valid JSON in this format: {"commands": ["step1", "step2", ...]}
Each step is a single shell command. Be concise and project-appropriate."""
        return prompt

    # ------------------------------------------------------------------
    # README generation
    # ------------------------------------------------------------------

    def generate_readme(
        self,
        cwd: Optional[Path] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """
        Generate a README.md for the project at *cwd* using thorough context
        gathering and the readme agent.

        Returns:
            Generated README markdown, or None if LLM disabled/failed.
        """
        if not self.llm_enabled:
            return None
        try:
            from cliara.readme_context import gather_context, format_context_for_prompt
            root = (cwd or Path.cwd()).resolve()
            context = gather_context(root)
            if context.get("error"):
                return None
            prompt = format_context_for_prompt(context)
            response = self._call_llm_stream("readme", prompt, stream_callback)
            return (response or "").strip()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Error Translation (intercept stderr ΓåÆ plain-English explanation)
    # ------------------------------------------------------------------

    def translate_error(
        self,
        command: str,
        exit_code: int,
        stderr: str,
        context: Optional[dict] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """
        Translate a command's stderr into a plain-English explanation
        with an optional suggested fix.

        Args:
            command: The shell command that failed
            exit_code: The process exit code
            stderr: Captured stderr output
            context: Optional context (cwd, os, shell, etc.)
            stream_callback: Optional callback for each streamed token.

        Returns:
            Dict with keys:
                explanation (str): Plain-English explanation
                fix_commands (List[str]): Suggested fix commands (may be empty)
                fix_explanation (str): What the fix does (empty if no fix)
        """
        if not self.llm_enabled:
            return self._stub_error_translation(command, exit_code, stderr)

        try:
            context_info = self._build_context(context)
            prompt = self._create_error_prompt(command, exit_code, stderr, context_info)
            response = self._call_llm_stream("fix", prompt, stream_callback)
            return self._parse_error_response(response)
        except Exception as e:
            return {
                "explanation": f"Could not analyze error: {e}",
                "fix_commands": [],
                "fix_explanation": "",
            }

    def _create_error_prompt(
        self, command: str, exit_code: int, stderr: str, context: dict
    ) -> str:
        """Build the LLM prompt for error translation."""
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "bash")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")

        # Truncate very long stderr. Local models have smaller context windows,
        # so use a tighter budget there (30 lines total vs 90 for cloud).
        lines = stderr.splitlines()
        if self.provider == "ollama":
            head, tail, limit = 20, 10, 30
        else:
            head, tail, limit = 60, 30, 100
        if len(lines) > limit:
            truncated = (
                "\n".join(lines[:head])
                + f"\n\n... ({len(lines) - head - tail} lines omitted) ...\n\n"
                + "\n".join(lines[-tail:])
            )
        else:
            truncated = stderr

        prompt = f"""A shell command failed. Analyse the error output and respond with a helpful explanation and, if possible, a concrete fix.

Command: {command}
Exit code: {exit_code}

Error output:
{truncated}

Context:
- OS: {os_name}
- Shell: {shell}
- Working directory: {cwd}
"""
        ide = context.get("ide") or {}
        if isinstance(ide, dict):
            ed = (ide.get("editor") or "").strip()
            wr = (ide.get("workspace_root") or "").strip()
            af = (ide.get("active_file") or "").strip()
            if ed:
                prompt += f"- IDE: {ed}\n"
            if wr:
                prompt += f"- IDE workspace: {wr}\n"
            if af:
                prompt += f"- IDE active file: {af}\n"
        if project_type:
            prompt += f"- Project type: {project_type}\n"
        if context.get("has_git"):
            prompt += "- Inside a Git repository\n"

        prompt += """
Respond with ONLY valid JSON in this exact format (no markdown, no code blocks):
{
  "explanation": "One or two sentences in plain English explaining what went wrong and why.",
  "fix_commands": ["command1", "command2"],
  "fix_explanation": "Brief description of what the fix commands do."
}

Rules:
- explanation should be concise, beginner-friendly, and avoid jargon where possible.
- fix_commands should contain concrete, runnable commands for the user's OS and shell. Leave the array empty if there is no clear automated fix.
- fix_explanation should summarise the fix in one sentence. Leave empty string if no fix.
- Return ONLY the JSON. No commentary, no markdown fences.
"""
        return prompt

    def _parse_error_response(self, response: str) -> Dict:
        """Parse the LLM's JSON response for error translation."""
        raw = self._extract_json(response)
        if raw:
            try:
                data = json.loads(raw)
                return {
                    "explanation": data.get("explanation", "Unknown error."),
                    "fix_commands": data.get("fix_commands", []),
                    "fix_explanation": data.get("fix_explanation", ""),
                }
            except (json.JSONDecodeError, AttributeError):
                pass
        return {
            "explanation": response[:500] if response else "Could not parse error analysis.",
            "fix_commands": [],
            "fix_explanation": "",
        }

    def _stub_error_translation(
        self, command: str, exit_code: int, stderr: str
    ) -> Dict:
        """
        Pattern-match common errors when the LLM is unavailable.
        Returns a best-effort explanation and fix.
        """
        stderr_lower = stderr.lower()
        explanation = ""
        fix_commands: List[str] = []
        fix_explanation = ""

        # --- npm / Node errors ---
        if "eresolve" in stderr_lower or "peer dep" in stderr_lower:
            explanation = (
                "npm could not resolve the dependency tree because some packages "
                "require conflicting versions of a shared dependency (a peer-dependency conflict)."
            )
            fix_commands = ["npm install --legacy-peer-deps"]
            fix_explanation = "Re-run install while ignoring peer-dependency conflicts."

        elif "eacces" in stderr_lower or "permission denied" in stderr_lower:
            explanation = (
                "The command failed because it does not have permission to access "
                "a file or directory. You may need elevated privileges."
            )
            if "npm" in command:
                fix_commands = ["npm install --prefix ."]
                fix_explanation = "Install to current directory to avoid system-level permission issues."

        elif "enoent" in stderr_lower or "no such file or directory" in stderr_lower:
            explanation = (
                "A file or directory referenced by the command does not exist. "
                "Double-check the path or ensure required files are present."
            )

        elif "eaddrinuse" in stderr_lower or "address already in use" in stderr_lower:
            import re as _re
            port_match = _re.search(r"(?:port\s*|:)(\d{2,5})", stderr_lower)
            port = port_match.group(1) if port_match else "PORT"
            explanation = (
                f"Port {port} is already in use by another process. "
                "You need to stop that process or use a different port."
            )
            import platform
            if platform.system() == "Windows":
                fix_commands = [
                    f'netstat -ano | findstr ":{port}"',
                ]
                fix_explanation = f"Find the process using port {port} so you can stop it."
            else:
                fix_commands = [f"lsof -ti :{port} | xargs kill -9"]
                fix_explanation = f"Kill the process occupying port {port}."

        # --- Python errors ---
        elif "modulenotfounderror" in stderr_lower or "no module named" in stderr_lower:
            import re as _re
            mod_match = _re.search(r"no module named ['\"]?([a-zA-Z0-9_.]+)", stderr_lower)
            mod = mod_match.group(1) if mod_match else "the_module"
            explanation = (
                f"Python cannot find the module '{mod}'. "
                "It may not be installed in your current environment."
            )
            fix_commands = [f"pip install {mod}"]
            fix_explanation = f"Install the missing '{mod}' package."

        elif "syntaxerror" in stderr_lower:
            explanation = (
                "Python encountered a syntax error ΓÇö there is likely a typo, "
                "missing colon, or unmatched bracket in the source code."
            )

        # --- Git errors ---
        elif "fatal: not a git repository" in stderr_lower:
            explanation = (
                "This directory is not a Git repository. "
                "You need to initialise one or navigate to an existing repo."
            )
            fix_commands = ["git init"]
            fix_explanation = "Initialise a new Git repository in the current directory."

        elif "fatal: remote origin already exists" in stderr_lower:
            explanation = "A remote named 'origin' is already configured for this repository."
            fix_commands = ["git remote -v"]
            fix_explanation = "List existing remotes to decide next steps."

        elif "merge conflict" in stderr_lower or "conflict" in stderr_lower and "git" in command:
            explanation = (
                "Git encountered merge conflicts ΓÇö the same lines were changed in "
                "both branches. You need to resolve them manually."
            )

        # --- Docker errors ---
        elif "cannot connect to the docker daemon" in stderr_lower:
            explanation = (
                "Docker is not running. Start the Docker daemon or Docker Desktop first."
            )

        # --- Generic fallback ---
        elif "command not found" in stderr_lower or "'.' is not recognized" in stderr_lower:
            base = command.split()[0] if command.split() else command
            explanation = (
                f"'{base}' is not installed or not on your PATH. "
                "You may need to install it or check your environment."
            )

        else:
            # No pattern matched ΓÇö give a generic message
            # Pull the last non-empty stderr line as a summary
            last_line = ""
            for line in reversed(stderr.strip().splitlines()):
                stripped = line.strip()
                if stripped:
                    last_line = stripped
                    break
            explanation = (
                f"The command exited with code {exit_code}. "
                f"Last error line: {last_line}"
                if last_line
                else f"The command exited with code {exit_code}."
            )

        return {
            "explanation": explanation,
            "fix_commands": fix_commands,
            "fix_explanation": fix_explanation,
        }

    def _stub_response(self, query: str) -> Tuple[List[str], str, DangerLevel]:
        """
        Stub responses when LLM is not enabled.
        
        Args:
            query: Natural language query
        
        Returns:
            Tuple of (commands, explanation, danger_level)
        """
        query_lower = query.lower()
        
        # Some hardcoded examples for demo
        if "port" in query_lower and "kill" in query_lower:
            import re
            port_match = re.search(r'\d{4,5}', query)
            port = port_match.group() if port_match else "3000"
            
            commands = [f"lsof -ti :{port} | xargs kill -9"]
            explanation = f"Kill process using port {port}"
            level = DangerLevel.DANGEROUS
            
        elif "node_modules" in query_lower and "clean" in query_lower:
            commands = ["rm -rf node_modules", "npm install"]
            explanation = "Remove node_modules and reinstall dependencies"
            level = DangerLevel.DANGEROUS
            
        elif "git" in query_lower and "status" in query_lower:
            commands = ["git status -s"]
            explanation = "Show git status"
            level = DangerLevel.SAFE
            
        elif "docker" in query_lower and "restart" in query_lower:
            commands = ["docker-compose down", "docker-compose up -d"]
            explanation = "Restart docker containers"
            level = DangerLevel.CAUTION
            
        else:
            commands = []
            explanation = (
                "LLM not configured. Run 'setup-llm' to set up a free provider, "
                "or set GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / "
                "OLLAMA_BASE_URL in your .env file."
            )
            level = DangerLevel.SAFE
        
        return commands, explanation, level

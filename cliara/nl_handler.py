"""
Natural Language handler for Cliara (Phase 2).
Converts natural language queries to shell commands using LLM.
"""

import json
import os
import platform
import re
from pathlib import Path

import numpy as np
from shutil import which
from typing import List, Tuple, Optional, Dict, Any, Callable

from cliara.safety import SafetyChecker, DangerLevel
from cliara.agents import AGENT_REGISTRY
from cliara.auth import get_gateway_url

EMBEDDING_MODEL = "text-embedding-3-small"

# Default model used when no per-task or global override is configured.
_PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "openai":    "gpt-4o-mini",
    "anthropic": "claude-3-haiku-20240307",
    "ollama":    "gemma4",
    "groq":      "llama-3.3-70b-versatile",
    "gemini":    "gemini-1.5-flash",
    "cliara":    "llama-3.3-70b-versatile",  # Gateway picks the best available model
}

# Providers that use the OpenAI-compatible client (openai SDK with custom base_url)
_OPENAI_COMPAT_PROVIDERS = frozenset({"openai", "ollama", "groq", "gemini", "cliara"})

# Base URLs for OpenAI-compatible cloud providers (not ollama — that's dynamic)
# Cliara URL comes from auth.py (single source of truth; respects CLIARA_GATEWAY_URL env).
_PROVIDER_BASE_URLS: Dict[str, str] = {
    "groq":   "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "cliara": get_gateway_url(),
}

# Agents whose output is plain text and can be streamed token-by-token to the
# console.  JSON-returning agents must NOT stream to the console because raw
# JSON appearing mid-parse gives broken UX.
_STREAMING_SAFE_AGENTS = frozenset({
    "explain",
    "explain_output",
    "commit_message",
    "copilot_explain",
    "readme",
    "chat_polish",
})


def _default_session_reflect_plan() -> List[Dict[str, Any]]:
    """Offline reflection flow when LLM is unavailable or JSON parse fails."""
    return [
        {
            "id": "session_shape",
            "kind": "choice",
            "question": "How would you describe this session for someone who only reads your reflection later?",
            "hint": "Pick the closest fit.",
            "options": [
                "Exploring or learning — no single deliverable yet",
                "Made progress — more work planned for later",
                "Completed a concrete task or milestone",
                "Blocked, interrupted, or mostly troubleshooting",
            ],
        },
        {
            "id": "what_mattered",
            "kind": "long_text",
            "question": "In plain language, what did you accomplish or learn, and why does it matter?",
            "hint": "This is the main story — not a list of commands.",
        },
        {
            "id": "risks_or_deps",
            "kind": "text",
            "question": "Any blockers, dependencies, or risks the next person should know? (one line, or skip)",
            "hint": "Optional.",
        },
        {
            "id": "next_move",
            "kind": "text",
            "question": "What is the single most useful next step when work resumes?",
            "hint": "Be specific if you can.",
        },
    ]


def _validate_session_reflect_steps(data: Any) -> Optional[List[Dict[str, Any]]]:
    """Validate LLM JSON for session_reflect; return sanitized steps or None."""
    if not isinstance(data, dict):
        return None
    steps_in = data.get("steps")
    if not isinstance(steps_in, list):
        return None
    out: List[Dict[str, Any]] = []
    for raw in steps_in:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind not in ("choice", "text", "long_text"):
            continue
        q = raw.get("question")
        if not isinstance(q, str) or not q.strip():
            continue
        sid = raw.get("id")
        if not isinstance(sid, str) or not sid.strip():
            sid = "step_%d" % len(out)
        step: Dict[str, Any] = {
            "id": sid.strip()[:80],
            "kind": kind,
            "question": q.strip()[:1200],
        }
        hint = raw.get("hint")
        if isinstance(hint, str) and hint.strip():
            step["hint"] = hint.strip()[:400]
        if kind == "choice":
            opts = raw.get("options")
            if not isinstance(opts, list):
                continue
            clean = [str(o).strip()[:500] for o in opts if str(o).strip()]
            if len(clean) < 2:
                continue
            step["options"] = clean[:6]
        out.append(step)
        if len(out) >= 8:
            break
    if len(out) < 2:
        return None
    return out


class NLHandler:
    """Handles natural language to command conversion using LLM."""

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
        # Lazy OpenAI client for embeddings when chat uses another provider (e.g. Groq).
        self._openai_embedding_client: Optional[Any] = None

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

    def initialize_llm(self, provider: str, api_key: str, base_url: Optional[str] = None) -> bool:
        """
        Initialize LLM client.

        Args:
            provider:  "openai" | "anthropic" | "ollama"
            api_key:   API key (use any non-empty string for ollama)
            base_url:  Base URL override (required for ollama, optional for openai-compatible)
        """
        if not api_key:
            return False

        try:
            if provider in _OPENAI_COMPAT_PROVIDERS:
                from openai import OpenAI
                kwargs: Dict[str, Any] = {"api_key": api_key}
                if provider == "ollama":
                    url = base_url or "http://localhost:11434"
                    kwargs["base_url"] = url.rstrip("/") + "/v1"
                    # Probe Ollama before marking as ready — fail fast with a
                    # clear message rather than hanging on the first query.
                    import urllib.request
                    import urllib.error
                    try:
                        urllib.request.urlopen(url.rstrip("/"), timeout=3)
                    except urllib.error.URLError:
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
                return model
        return _PROVIDER_DEFAULT_MODELS.get(self.provider or "", "gpt-4o-mini")

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
            context_info = self._build_context(context, include_directory_listing=True)
            prompt = self._create_prompt(query, context_info)
            response = self._call_llm_stream("nl_to_commands", prompt, stream_callback)
            commands, explanation = self._parse_response(response)

            if not commands:
                return [], "Could not generate commands from query", DangerLevel.SAFE

            level, dangerous = self.safety.check_commands(commands)
            return commands, explanation, level
        
        except Exception as e:
            print(f"[Error] LLM processing failed: {e}")
            return [], f"Error: {str(e)}", DangerLevel.SAFE
    
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

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(
        self,
        context: Optional[dict] = None,
        include_directory_listing: bool = False,
    ) -> dict:
        """Build context information for LLM.

        When *include_directory_listing* is True a compact filesystem
        snapshot of the cwd (depth 2) is included so the model can
        resolve ambiguous / fuzzy path references.
        """
        ctx = context.copy() if context else {}

        ctx.setdefault("os", platform.system())
        ctx.setdefault("shell", self._detect_shell_fallback())
        ctx.setdefault("cwd", str(Path.cwd()))

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

        return ctx
    
    def _create_prompt(self, query: str, context: dict) -> str:
        """Create the user message for the NL-to-commands agent.

        All behavioural rules live in the system prompt (nl_to_commands.txt).
        This method only supplies the request and runtime context.
        """
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "unknown")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")
        dir_listing = context.get("directory_listing", "")

        prompt = f"User's request: {query}\n\nContext:\n"
        prompt += f"- Operating System: {os_name}\n"
        prompt += f"- Shell: {shell}\n"
        prompt += f"- Current Directory: {cwd}\n"

        if project_type:
            prompt += f"- Project Type: {project_type}\n"
        if context.get("has_git"):
            prompt += "- Git repository detected\n"
        if context.get("has_docker"):
            prompt += "- Docker Compose detected\n"

        if dir_listing:
            prompt += f"\nDirectory listing (depth 2 from cwd):\n{dir_listing}\n"

        return prompt
    
    def _call_llm_stream(
        self,
        agent_type: str,
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Call the LLM; optionally stream tokens to *stream_callback*.

        *stream_callback* should only be supplied for agents in
        ``_STREAMING_SAFE_AGENTS`` (plain-text output).  JSON-returning agents
        must not stream to the console.

        Returns the full assistant reply as a single string.
        """
        if agent_type not in AGENT_REGISTRY:
            raise ValueError(f"Unknown agent type: {agent_type}")
        cfg = AGENT_REGISTRY[agent_type]
        system: str = cfg["system"]
        temperature: float = cfg["temperature"]
        max_tokens: int = cfg["max_tokens"]
        model: str = self._resolve_model(agent_type)

        # Enforce safety: never stream JSON agents to the console
        safe_cb = stream_callback if agent_type in _STREAMING_SAFE_AGENTS else None

        if self.provider in _OPENAI_COMPAT_PROVIDERS:
            return self._call_openai_compat(
                system, user_message, model, temperature, max_tokens, safe_cb
            )
        elif self.provider == "anthropic":
            return self._call_anthropic(
                system, user_message, model, temperature, max_tokens, safe_cb
            )
        else:
            raise Exception(f"Unsupported provider: {self.provider}")

    def _openai_compat_error_message(self, err: Exception) -> str:
        """Turn upstream errors into a short message; add hints for known platform failures."""
        msg = str(err)
        out = f"{self.provider} API error: {err}"
        if "Application not found" in msg and "404" in msg:
            out += (
                "\n  Hint: The hosted API (often Railway) returned “Application not found”. "
                "That usually means the gateway URL is wrong, the service is not reachable, "
                "or public networking was misconfigured — not a problem with your macro text. "
                "Check CLIARA_GATEWAY_URL, try GET …/health on the gateway host, or set "
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
    ) -> str:
        """OpenAI / Ollama (OpenAI-compatible) completion — with optional streaming."""
        try:
            if stream_callback is not None:
                stream = self.llm_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                full_content: List[str] = []
                for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        stream_callback(delta)
                        full_content.append(delta)
                return "".join(full_content).strip()
            else:
                response = self.llm_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return response.choices[0].message.content.strip()
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
        """Anthropic completion — with optional streaming."""
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
                return commands, explanation
            except (json.JSONDecodeError, AttributeError):
                pass

        # Last-resort: pull shell-looking lines from plain text
        lines = response.split("\n")
        commands = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if any(kw in line.lower() for kw in [
                "echo", "ls", "get-childitem", "dir", "cd", "git",
                "npm", "docker", "python", "node", "pip", "mv", "cp",
                "rm", "mkdir", "curl", "wget", "find", "grep",
            ]):
                commands.append(line)
        if commands:
            return commands, "Generated from natural language query"
        return [], "Could not parse LLM response"
    
    def generate_commands_from_nl(self, nl_description: str, context: Optional[dict] = None) -> List[str]:
        """
        Generate commands from natural language description (for NL macros).
        
        Args:
            nl_description: Natural language description of what to do
            context: Optional context information
        
        Returns:
            List of shell commands
        """
        if not self.llm_enabled:
            return [f"# LLM not configured: {nl_description}"]
        
        try:
            context_info = self._build_context(context, include_directory_listing=True)
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
            context_info = self._build_context(context, include_directory_listing=True)
            prompt = self._create_prompt(nl_description, context_info)
            response = self._call_llm("nl_macro_propose", prompt)
            name, commands, desc, expl = self._parse_macro_proposal_loose(
                response, nl_description
            )
            if not commands:
                # Model returned unusable text — fall back to command-only agent
                commands_fb = self.generate_commands_from_nl(nl_description, context)
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

            prompt = f"""Explain this command briefly. Use short bullet points (plain "-" dashes) to break it down so it's easy to scan. No markdown formatting like bold, headers, or code blocks. Keep it concise — no long paragraphs. If it's dangerous, mention that too.

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
            return "Empty command — nothing to explain."

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
            f"LLM not configured — showing basic info only.\n\n"
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

        out_t = self._truncate_stream_for_prompt(stdout or "", 70)
        err_t = self._truncate_stream_for_prompt(stderr or "", 70)
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
            "LLM not configured — stub summary only.",
            f"- Command: {command}",
            f"- Exit code: {exit_code}",
        ]
        o = (stdout or "").strip()
        e = (stderr or "").strip()
        if o:
            snippet = o[:400] + ("…" if len(o) > 400 else "")
            lines.append(f"- Stdout ({len(o)} chars): {snippet!r}")
        else:
            lines.append("- Stdout: (empty)")
        if e:
            snippet = e[:400] + ("…" if len(e) > 400 else "")
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
        # Truncate very long commands for API
        cmd_for_prompt = command.strip()
        if len(cmd_for_prompt) > 2000:
            cmd_for_prompt = cmd_for_prompt[:2000] + " ..."
        try:
            context_info = self._build_context(context) if context else {}
            os_name = context_info.get("os", "Unknown")
            shell = context_info.get("shell", "bash")
            prompt = f"""OS: {os_name}, Shell: {shell}

Command: {cmd_for_prompt}"""
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
        """
        if not text.strip():
            return None
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
                input=text.strip(),
            )
            return resp.data[0].embedding
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
                out.append(with_emb[i])
        return out

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

            # Truncate diff to ~3 000 chars to stay within token limits
            diff_truncated = diff_content[:3000]
            if len(diff_content) > 3000:
                diff_truncated += "\n\n... (diff truncated) ..."

            file_list = "\n".join(f"  - {f}" for f in files)

            prompt = f"""Analyse the following git diff and generate ONE conventional commit message.

Branch: {branch}

Files changed:
{file_list}

Diff summary:
{diff_stat}

Diff (may be truncated):
{diff_truncated}

Rules (Conventional Commits):
- Format: type: description (lowercase type; imperative description; no trailing period)
- Core types: feat (new feature), fix (bug fix), refactor (restructure, no behavior change),
  docs (documentation only), style (formatting/lint, no logic), test (tests), chore (misc/deps/config)
- Extras: perf (performance), ci (CI/CD), build (build/packaging/deps), revert (undo a commit)
- Prefer the primary change type (usually feat or fix) when several apply
- Keep the line reasonably short (~50–72 chars when practical)
- Be specific about what the diff actually changes
- Return ONLY the commit message — one line, no quotes, no explanation"""

            response = self._call_llm_stream("commit_message", prompt, stream_callback)
            # Strip any surrounding quotes the model might add
            msg = (response or "").strip().strip("\"'")
            return msg

        except Exception as e:
            # Fall back to stub on any failure
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
    # Deploy steps (no platform detected — user describes, deploy agent suggests steps)
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
    # Error Translation (intercept stderr → plain-English explanation)
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

        # Truncate very long stderr: keep first 60 + last 30 lines
        lines = stderr.splitlines()
        if len(lines) > 100:
            truncated = (
                "\n".join(lines[:60])
                + f"\n\n... ({len(lines) - 90} lines omitted) ...\n\n"
                + "\n".join(lines[-30:])
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
                "Python encountered a syntax error — there is likely a typo, "
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
                "Git encountered merge conflicts — the same lines were changed in "
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
            # No pattern matched — give a generic message
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

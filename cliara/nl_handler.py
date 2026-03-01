"""
Natural Language handler for Cliara (Phase 2).
Converts natural language queries to shell commands using LLM.
"""

import json
import os
import platform
import re
from pathlib import Path
from shutil import which
from typing import List, Tuple, Optional, Dict, Any, Callable

from cliara.safety import SafetyChecker, DangerLevel
from cliara.agents import AGENT_REGISTRY

LLM_MODEL = "gpt-4o-mini"
EMBEDDING_MODEL = "text-embedding-3-small"


class NLHandler:
    """Handles natural language to command conversion using LLM."""
    
    def __init__(self, safety_checker: SafetyChecker):
        """
        Initialize NL handler.
        
        Args:
            safety_checker: Safety checker instance
        """
        self.safety = safety_checker
        self.llm_enabled = False
        self.llm_client = None
        self.provider = None
    
    def initialize_llm(self, provider: str, api_key: str):
        """
        Initialize LLM client.
        
        Args:
            provider: "openai" or "anthropic"
            api_key: API key for the provider
        """
        if not api_key:
            return False
        
        try:
            if provider == "openai":
                from openai import OpenAI
                self.llm_client = OpenAI(api_key=api_key)
                self.provider = "openai"
                self.llm_enabled = True
                return True
            elif provider == "anthropic":
                # Future: Add Anthropic support
                print("[Warning] Anthropic support coming soon")
                return False
            else:
                print(f"[Error] Unknown LLM provider: {provider}")
                return False
        except ImportError:
            print("[Error] OpenAI package not installed. Run: pip install openai")
            return False
        except Exception as e:
            print(f"[Error] Failed to initialize LLM: {e}")
            return False
    
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
        """
        Call LLM API; optionally stream chunks to stream_callback. Returns full assistant content.
        When stream_callback is None, uses non-streaming create() (current behavior).
        """
        if agent_type not in AGENT_REGISTRY:
            raise ValueError(f"Unknown agent type: {agent_type}")
        cfg = AGENT_REGISTRY[agent_type]
        system = cfg["system"]
        temperature = cfg["temperature"]
        max_tokens = cfg["max_tokens"]

        if self.provider == "openai":
            try:
                if stream_callback is not None:
                    stream = self.llm_client.chat.completions.create(
                        model=LLM_MODEL,
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
                        model=LLM_MODEL,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_message},
                        ],
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    return response.choices[0].message.content.strip()
            except Exception as e:
                raise Exception(f"OpenAI API error: {e}")
        else:
            if stream_callback is not None:
                # Non-OpenAI provider: fall back to non-streaming
                return self._call_llm_stream(agent_type, user_message, stream_callback=None)
            raise Exception(f"Unsupported provider: {self.provider}")

    def _call_llm(self, agent_type: str, user_message: str) -> str:
        """Call LLM API with the given agent's system prompt and params. Returns assistant content."""
        return self._call_llm_stream(agent_type, user_message, stream_callback=None)

    def _parse_response(self, response: str) -> Tuple[List[str], str]:
        """Parse LLM response and extract commands."""
        # Try to extract JSON from response
        # Sometimes LLM wraps JSON in markdown code blocks
        
        # Remove markdown code blocks if present
        response = re.sub(r'```json\s*', '', response)
        response = re.sub(r'```\s*', '', response)
        response = response.strip()
        
        try:
            data = json.loads(response)
            commands = data.get("commands", [])
            explanation = data.get("explanation", "Generated commands")
            
            # Ensure commands is a list
            if isinstance(commands, str):
                commands = [commands]
            
            return commands, explanation
        except json.JSONDecodeError:
            # Try to extract commands from plain text
            # Look for command-like patterns
            lines = response.split('\n')
            commands = []
            for line in lines:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue
                # If it looks like a command, add it
                if any(keyword in line.lower() for keyword in ['echo', 'ls', 'cd', 'git', 'npm', 'docker', 'python', 'node']):
                    commands.append(line)
            
            if commands:
                return commands, "Generated from natural language query"
            else:
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
            f"Set OPENAI_API_KEY in your .env file for detailed, AI-powered explanations."
        )

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
        """
        Fetch an embedding vector for *text* via the OpenAI embeddings API.
        Returns None if the client is not initialised or the call fails.
        """
        if not self.llm_enabled or self.provider != "openai" or not text.strip():
            return None
        try:
            resp = self.llm_client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=text.strip(),
            )
            return resp.data[0].embedding
        except Exception:
            return None

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """Cosine similarity between two equal-length vectors (pure-Python, no deps)."""
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def search_history_by_embeddings(
        self,
        entries: List[Dict[str, Any]],
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Vector-similarity search over semantic history entries.

        Entries that already have an ``embedding`` field are ranked by cosine
        similarity to the query embedding.  Entries without embeddings are
        excluded from this path (they were added before the feature was
        enabled).

        Returns up to *top_k* entries in descending similarity order.
        Returns an empty list if no embeddings are stored yet or the API call
        fails (caller should fall back to summary-only search).
        """
        if not self.llm_enabled or not entries or not (query or "").strip():
            return []

        # Filter to only entries that have embeddings
        with_emb = [e for e in entries if e.get("embedding")]
        if not with_emb:
            return []

        query_emb = self.get_embedding(query)
        if not query_emb:
            return []

        scored = [
            (self._cosine_similarity(query_emb, e["embedding"]), e)
            for e in with_emb
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        # Only return entries with a meaningful similarity score
        threshold = 0.30
        return [e for score, e in scored[:top_k] if score >= threshold]

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

Rules:
- Use conventional commit format:  type: description
- Allowed types: feat, fix, docs, style, refactor, test, chore, build, ci, perf
- Keep the description under 72 characters total (including the type prefix)
- Use imperative mood ("add" not "added", "fix" not "fixed")
- Be specific about what actually changed
- If there are multiple kinds of changes, pick the most significant type
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
            response = re.sub(r"```json\s*", "", response)
            response = re.sub(r"```\s*", "", response)
            response = response.strip()
            data = json.loads(response)
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
        # Strip markdown fences if the model added them anyway
        response = re.sub(r"```json\s*", "", response)
        response = re.sub(r"```\s*", "", response)
        response = response.strip()

        try:
            data = json.loads(response)
            return {
                "explanation": data.get("explanation", "Unknown error."),
                "fix_commands": data.get("fix_commands", []),
                "fix_explanation": data.get("fix_explanation", ""),
            }
        except json.JSONDecodeError:
            # Fallback: treat the whole response as the explanation
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
            explanation = "LLM not configured. Set OPENAI_API_KEY in .env file to enable natural language."
            level = DangerLevel.SAFE
        
        return commands, explanation, level

"""
Natural Language handler for Cliara (Phase 2).
Converts natural language queries to shell commands using LLM.
"""

import json
import re
from typing import List, Tuple, Optional, Dict
from cliara.safety import SafetyChecker, DangerLevel


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
    
    def process_query(self, query: str, context: Optional[dict] = None) -> Tuple[List[str], str, DangerLevel]:
        """
        Convert natural language query to commands using LLM.
        
        Args:
            query: Natural language query
            context: Optional context (cwd, os, shell, etc.)
        
        Returns:
            Tuple of (commands, explanation, danger_level)
        """
        if not self.llm_enabled:
            return self._stub_response(query)
        
        try:
            # Build context information
            context_info = self._build_context(context)
            
            # Create prompt
            prompt = self._create_prompt(query, context_info)
            
            # Call LLM
            response = self._call_llm(prompt)
            
            # Parse response
            commands, explanation = self._parse_response(response)
            
            if not commands:
                return [], "Could not generate commands from query", DangerLevel.SAFE
            
            # Safety check
            level, dangerous = self.safety.check_commands(commands)
            
            return commands, explanation, level
        
        except Exception as e:
            print(f"[Error] LLM processing failed: {e}")
            return [], f"Error: {str(e)}", DangerLevel.SAFE
    
    def _build_context(self, context: Optional[dict]) -> dict:
        """Build context information for LLM."""
        import os
        import platform
        from pathlib import Path
        
        ctx = context or {}
        
        # Add system info
        ctx.setdefault("os", platform.system())
        ctx.setdefault("shell", os.environ.get("SHELL", "bash"))
        ctx.setdefault("cwd", str(Path.cwd()))
        
        # Detect project type
        cwd = Path(ctx["cwd"])
        if (cwd / "package.json").exists():
            ctx["project_type"] = "node"
        elif (cwd / "requirements.txt").exists() or (cwd / "pyproject.toml").exists():
            ctx["project_type"] = "python"
        elif (cwd / "Cargo.toml").exists():
            ctx["project_type"] = "rust"
        elif (cwd / "docker-compose.yml").exists():
            ctx["has_docker"] = True
        
        # Check for git
        if (cwd / ".git").exists():
            ctx["has_git"] = True
        
        return ctx
    
    def _create_prompt(self, query: str, context: dict) -> str:
        """Create prompt for LLM."""
        os_name = context.get("os", "Unknown")
        shell = context.get("shell", "bash")
        cwd = context.get("cwd", "")
        project_type = context.get("project_type", "")
        
        prompt = f"""You are a helpful assistant that converts natural language requests into shell commands.

User's request: {query}

Context:
- Operating System: {os_name}
- Shell: {shell}
- Current Directory: {cwd}
"""
        
        if project_type:
            prompt += f"- Project Type: {project_type}\n"
        if context.get("has_git"):
            prompt += "- Git repository detected\n"
        if context.get("has_docker"):
            prompt += "- Docker Compose detected\n"
        
        prompt += """
Instructions:
1. Generate the most appropriate shell command(s) for the user's request
2. Consider the OS and shell type
3. Return ONLY valid JSON in this exact format:
{
  "commands": ["command1", "command2"],
  "explanation": "Brief explanation of what these commands do"
}

Rules:
- Return commands that work on the specified OS and shell
- Use appropriate commands for Windows (PowerShell/cmd) vs Unix (bash/zsh)
- If multiple commands are needed, return them as an array
- Keep commands simple and safe
- Do NOT include any markdown formatting or code blocks
- Return ONLY the JSON, nothing else

JSON Response:"""
        
        return prompt
    
    def _call_llm(self, prompt: str) -> str:
        """Call LLM API and return response."""
        if self.provider == "openai":
            try:
                response = self.llm_client.chat.completions.create(
                    model="gpt-4o-mini",  # Using mini for cost efficiency
                    messages=[
                        {"role": "system", "content": "You are a terminal command explainer: when a user provides a terminal command, explain in simple beginner-friendly language what it does in one clear sentence, briefly break down its parts (command, flags, arguments), give one simple real-world example of when it’s used, keep the response concise (max 6–8 short lines), avoid deep theory or edge cases, and include a short warning if the command is potentially dangerous."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,  # Lower temperature for more consistent output
                    max_tokens=500
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                raise Exception(f"OpenAI API error: {e}")
        else:
            raise Exception(f"Unsupported provider: {self.provider}")
    
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
            context_info = self._build_context(context)
            prompt = self._create_prompt(nl_description, context_info)
            response = self._call_llm(prompt)
            commands, _ = self._parse_response(response)
            return commands if commands else [f"# Could not generate: {nl_description}"]
        except Exception as e:
            return [f"# Error generating commands: {str(e)}"]
    
    def explain_command(self, command: str, context: Optional[dict] = None) -> str:
        """
        Explain a shell command in plain English using the LLM.

        Args:
            command: The shell command to explain

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

            response = self._call_llm_explain(prompt)
            return response.strip()

        except Exception as e:
            return f"Error explaining command: {e}"

    def _call_llm_explain(self, prompt: str) -> str:
        """Call LLM API for an explain request (returns plain text)."""
        if self.provider == "openai":
            try:
                response = self.llm_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You explain shell commands briefly in plain English. "
                                "Use short bullet points with plain dashes to keep things readable. "
                                "No markdown formatting like bold, headers, or code blocks."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=200,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                raise Exception(f"OpenAI API error: {e}")
        else:
            raise Exception(f"Unsupported provider: {self.provider}")

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

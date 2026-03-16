"""
Configuration management for Cliara.
Handles user settings, first-run setup, and persistent configuration.
"""

import json
import os
import platform
from pathlib import Path

from cliara.file_lock import with_file_lock
from typing import Dict, Any, Optional

# Load .env files if they exist.
# Priority (highest wins): system env → project .env → ~/.cliara/.env
# We load lowest-priority first so higher-priority sources can override.
try:
    from dotenv import load_dotenv, find_dotenv
    # 1. User-level env (~/.cliara/.env) — lowest priority; set only if not already set
    _user_env_path = Path.home() / ".cliara" / ".env"
    if _user_env_path.exists():
        load_dotenv(_user_env_path, override=False)
    # 2. Project-level .env — overrides user env (but not system env vars already exported)
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=True)
    else:
        load_dotenv(override=True)
except ImportError:
    pass  # python-dotenv not installed, skip


class Config:
    """Manages Cliara configuration and settings."""
    
    DEFAULT_CONFIG = {
        "shell": None,  # Auto-detected
        "os": None,  # Auto-detected
        "nl_prefix": "?",
        "macro_storage": "~/.cliara/macros.json",
        "storage_backend": "json",  # "json" or "postgres"
        "history_size": 1000,
        "safety_checks": True,
        "auto_confirm_safe": False,
        "error_translation": True,  # Analyse stderr on failure and show plain-English fix
        "diff_preview": True,  # Show what destructive commands will affect before running
        "notify_after_seconds": 30,  # Desktop notification when a command takes longer than this
        "spinner_delay_seconds": 3,  # Show inline spinner after this many seconds (0 to disable)
        "clear_show_header": True,  # After clear/cls, show a minimal "Cliara ready" line
        "prompt_style": "cliara",
        "theme": "dracula",  # dracula | monokai | nord | solarized | catppuccin | light
        "llm_provider": None,  # "openai" | "anthropic" | "ollama"
        "llm_api_key": None,  # Never persisted to disk — comes from env
        "llm_model": None,    # Global model override; None = provider default
        # Per-task model overrides — None means fall back to llm_model then provider default
        # Examples: "gpt-4o" for fix/nl, "gpt-4o-mini" for explain/history, "llama3.2" for ollama
        "model_nl": None,       # ? natural-language → commands
        "model_fix": None,      # Error translation & fix suggestions
        "model_explain": None,  # explain <command>
        "model_commit": None,   # smart push commit message
        "model_deploy": None,   # deploy step generation
        "model_readme": None,   # readme generation
        "model_history": None,  # history summarisation & search
        "model_copilot": None,  # CopilotGate explain
        # Ollama (local LLM) settings
        "ollama_base_url": "http://localhost:11434",  # Ollama server URL
        "first_run_complete": False,
        "llm_wizard_dismissed": False,  # True after user deliberately skips the setup wizard
        "regression_snapshots": True,  # Capture success snapshots; on failure compare and suggest ? why
        "stream_llm": True,  # Stream LLM responses token-by-token when enabled
        # Semantic history search (? find / ? when did I ...)
        "semantic_history_enabled": True,
        "semantic_history_max_entries": 500,
        "semantic_history_use_embeddings": True,
        "semantic_history_summary_on_add": True,
        # Copilot Gate — AI-command interception layer
        "copilot_gate": True,
        "copilot_gate_mode": "auto",            # "auto" | "explicit" | "all"
        "copilot_gate_auto_approve_safe": True,  # Auto-execute SAFE pasted commands
        # PostgreSQL configuration (optional)
        "postgres": {
            "host": "localhost",
            "port": 5432,
            "database": "cliara",
            "user": "cliara",
            "password": "",  # Should be in environment variable
        },
        "connection_string": None,  # Alternative: full connection string
        # Startup banner: compact after a few launches, full once per day (see CLIARA_POLISH §3)
        "launch_count": 0,
        "last_banner_date": None,  # "YYYY-MM-DD" when full banner was last shown
    }
    
    def __init__(self, config_dir: Optional[str] = None):
        """
        Initialize config system.
        
        Args:
            config_dir: Override default config directory
        """
        if config_dir:
            self.config_dir = Path(config_dir).expanduser()
        else:
            self.config_dir = Path.home() / ".cliara"
        
        self.config_file = self.config_dir / "config.json"
        self.macros_dir = self.config_dir / "macros.json"
        
        self._ensure_directories()
        self.settings = self._load_config()
        self._load_env_vars()
    
    def _ensure_directories(self):
        """Create config directory if it doesn't exist."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
    
    def _detect_shell(self) -> str:
        """Auto-detect the user's default shell."""
        system = platform.system()
        
        if system == "Windows":
            # Check for PowerShell, fallback to cmd
            pwsh_path = self._find_executable("pwsh") or self._find_executable("powershell")
            return pwsh_path if pwsh_path else "cmd.exe"
        else:
            # Unix-like: check SHELL env var
            shell = os.environ.get("SHELL", "/bin/bash")
            return shell
    
    def _find_executable(self, name: str) -> Optional[str]:
        """Find executable in PATH."""
        from shutil import which
        return which(name)
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file or create default."""
        if self.config_file.exists():
            try:
                with with_file_lock(self.config_file):
                    with open(self.config_file, 'r') as f:
                        loaded = json.load(f)
                # Merge with defaults (in case new keys added)
                config = self.DEFAULT_CONFIG.copy()
                config.update(loaded)
                return config
            except json.JSONDecodeError:
                print("[Warning] Config file corrupted, using defaults")
                return self.DEFAULT_CONFIG.copy()
        else:
            return self.DEFAULT_CONFIG.copy()
    
    def save(self):
        """Save current configuration to file."""
        # Create a copy without sensitive data
        settings_to_save = self.settings.copy()
        # Never save API keys to config file - they come from .env
        settings_to_save.pop("llm_api_key", None)
        
        # Never save passwords in postgres config - they come from .env
        if "postgres" in settings_to_save and isinstance(settings_to_save["postgres"], dict):
            postgres_copy = settings_to_save["postgres"].copy()
            postgres_copy.pop("password", None)
            settings_to_save["postgres"] = postgres_copy
        
        # Never save connection strings with passwords
        if "connection_string" in settings_to_save and settings_to_save["connection_string"]:
            conn_str = settings_to_save["connection_string"]
            # Remove password from connection string if present
            if "@" in conn_str and ":" in conn_str.split("@")[0]:
                # Format: postgresql://user:<pass>@host
                parts = conn_str.split("@")
                auth_part = parts[0].split("://")[1] if "://" in parts[0] else parts[0]
                if ":" in auth_part:
                    user = auth_part.split(":")[0]
                    settings_to_save["connection_string"] = conn_str.replace(
                        f"{user}:{auth_part.split(':')[1]}", user
                    )
        
        with with_file_lock(self.config_file):
            with open(self.config_file, 'w') as f:
                json.dump(settings_to_save, f, indent=2)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        return self.settings.get(key, default)
    
    def set(self, key: str, value: Any):
        """Set a configuration value and save."""
        self.settings[key] = value
        self.save()
    
    def is_first_run(self) -> bool:
        """Check if this is the first time running Cliara."""
        return not self.settings.get("first_run_complete", False)
    
    def complete_first_run(self):
        """Mark first-run setup as complete."""
        self.set("first_run_complete", True)
    
    def setup_first_run(self):
        """Interactive first-run setup."""
        try:
            from cliara.install_logo import print_install_logo
            try:
                from importlib.metadata import version as _pkg_version
                _ver = _pkg_version("cliara")
            except Exception:
                _ver = ""
            print_install_logo(version=_ver)
        except Exception:
            print("\n" + "=" * 60)
            print("  Welcome to Cliara!")
            print("=" * 60 + "\n")

        print("  Let's get you set up...\n")
        
        # Detect and confirm shell
        detected_shell = self._detect_shell()
        detected_os = platform.system()
        
        print(f"Detected OS: {detected_os}")
        print(f"Detected shell: {detected_shell}")
        
        confirm = input("\nUse these settings? (y/n): ").strip().lower()
        
        if confirm in ['y', 'yes', '']:
            self.settings["shell"] = detected_shell
            self.settings["os"] = detected_os
        else:
            custom_shell = input("Enter your shell path: ").strip()
            self.settings["shell"] = custom_shell if custom_shell else detected_shell
            self.settings["os"] = detected_os
        
        print("\n" + "="*60)
        print("  Quick Start Guide")
        print("="*60)
        print("\n1. Normal commands work as usual:")
        print("   cliara > ls -la")
        print("   cliara > cd projects")
        print("\n2. Use ? for natural language (Phase 2):")
        print("   cliara > ? kill whatever is using port 3000")
        print("\n3. Create and run macros:")
        print("   cliara > macro add mycommand")
        print("   cliara > macro run mycommand")
        print("\n4. Save your last command as a macro:")
        print("   cliara > macro save last as quickfix")
        print("\nType 'help' anytime for more info!")
        print("="*60 + "\n")
        
        self.complete_first_run()
        self.save()
    
    def get_macros_path(self) -> Path:
        """Get the path to macros storage."""
        macro_path = self.settings.get("macro_storage", "~/.cliara/macros.json")
        return Path(macro_path).expanduser()
    
    def _load_env_vars(self):
        """Load environment variables into config.

        Provider priority (first match wins):
          0. CLIARA_TOKEN env var — explicit override to force the gateway
          1. Ollama  — local, no key needed
          2. Anthropic  (BYOK)
          3. Groq        (BYOK)
          4. Gemini      (BYOK)
          5. OpenAI      (BYOK)
          6. ~/.cliara/token.json — set by `cliara login`; used when no BYOK key
             is configured so logged-in users get the gateway automatically

        BYOK keys (2-5) intentionally rank above the token file so that a
        power user who sets OPENAI_API_KEY bypasses the cloud proxy without
        any extra configuration.
        """
        # Explicit env var override always wins — useful for CI or forcing the gateway
        cliara_token = os.getenv("CLIARA_TOKEN")
        if cliara_token:
            self.settings["llm_provider"] = "cliara"
            self.settings["llm_api_key"] = cliara_token
            return

        # Ollama takes precedence when OLLAMA_BASE_URL or OLLAMA_HOST is set —
        # no API key required (uses a dummy value so the client initialises).
        ollama_url = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST")
        if ollama_url:
            self.settings["llm_provider"] = "ollama"
            self.settings["ollama_base_url"] = ollama_url.rstrip("/")
            self.settings["llm_api_key"] = "ollama"  # pragma: allowlist secret
            return

        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            self.settings["llm_provider"] = "anthropic"
            self.settings["llm_api_key"] = anthropic_key
            return

        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            self.settings["llm_provider"] = "groq"
            self.settings["llm_api_key"] = groq_key
            return

        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            self.settings["llm_provider"] = "gemini"
            self.settings["llm_api_key"] = gemini_key
            return

        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            self.settings["llm_provider"] = "openai"
            self.settings["llm_api_key"] = openai_key
            return

        # No BYOK key found — check whether the user has logged in via
        # `cliara login`.  This is intentionally last so BYOK always wins.
        self._try_load_cliara_token()

    def _try_load_cliara_token(self) -> None:
        """
        Attempt to load a stored Cliara Cloud token from ~/.cliara/token.json.

        On success, sets provider to "cliara" with the token as the API key.
        Silently does nothing on any failure so startup is never blocked.
        """
        try:
            from cliara.auth import get_valid_token
            token = get_valid_token()
            if token:
                self.settings["llm_provider"] = "cliara"
                self.settings["llm_api_key"] = token
        except Exception:
            # Import error, file corruption, network error during refresh, etc.
            # Never crash startup over a missing/bad token file.
            pass
    
    def get_llm_api_key(self) -> Optional[str]:
        """Get LLM API key from environment or config (including token file)."""
        if os.getenv("CLIARA_TOKEN"):
            return os.getenv("CLIARA_TOKEN")
        if os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST"):
            return "ollama"
        for env_var in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
            val = os.getenv(env_var)
            if val:
                return val
        # Fall back to what _load_env_vars resolved (may be a Cliara token)
        return self.settings.get("llm_api_key")

    def get_llm_provider(self) -> Optional[str]:
        """Get LLM provider name (including Cliara Cloud from token file)."""
        if os.getenv("CLIARA_TOKEN"):
            return "cliara"
        if os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST"):
            return "ollama"
        if os.getenv("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.getenv("GROQ_API_KEY"):
            return "groq"
        if os.getenv("GEMINI_API_KEY"):
            return "gemini"
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        # Fall back to what _load_env_vars resolved (may be "cliara" from token file)
        return self.settings.get("llm_provider")

    def get_ollama_base_url(self) -> str:
        """Get the Ollama server base URL."""
        env_url = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST")
        if env_url:
            return env_url.rstrip("/")
        return self.settings.get("ollama_base_url", "http://localhost:11434")

    def get_llm_model(self, agent_type: Optional[str] = None) -> Optional[str]:
        """Resolve the model to use for *agent_type*.

        Resolution order:
          1. Per-task config key  (e.g. ``model_explain``)
          2. Global ``llm_model`` override
          3. None → caller applies the provider default
        """
        _AGENT_CONFIG_KEYS: Dict[str, str] = {
            "nl_to_commands": "model_nl",
            "fix":            "model_fix",
            "explain":        "model_explain",
            "history_summary":"model_history",
            "history_search": "model_history",
            "commit_message": "model_commit",
            "deploy":         "model_deploy",
            "readme":         "model_readme",
            "copilot_explain":"model_copilot",
        }
        if agent_type:
            key = _AGENT_CONFIG_KEYS.get(agent_type)
            if key:
                model = self.settings.get(key)
                if model:
                    return model
        return self.settings.get("llm_model") or None
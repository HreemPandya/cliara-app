"""
Configuration management for Cliara.
Handles user settings, first-run setup, and persistent configuration.
"""

import json
import os
import platform
from pathlib import Path
from typing import Dict, Any, Optional

# Load .env file if it exists - search in common locations
try:
    from dotenv import load_dotenv, find_dotenv
    # Try to find .env file starting from current directory
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=True)
    else:
        # Fallback: try to load from current directory
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
        "prompt_style": "cliara",
        "llm_provider": None,  # "openai" or "anthropic" (Phase 2)
        "llm_api_key": None,  # Encrypted in real impl (Phase 2)
        "first_run_complete": False,
        # PostgreSQL configuration (optional)
        "postgres": {
            "host": "localhost",
            "port": 5432,
            "database": "cliara",
            "user": "cliara",
            "password": "",  # Should be in environment variable
        },
        "connection_string": None,  # Alternative: full connection string
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
        print("\n" + "="*60)
        print("  Welcome to Cliara!")
        print("  Let's get you set up...")
        print("="*60 + "\n")
        
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
        """Load environment variables into config."""
        # Load OpenAI API key from .env if present
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            self.settings["llm_provider"] = "openai"
            self.settings["llm_api_key"] = openai_key
            # Don't save API key to config file for security
            # It will be loaded from .env each time
    
    def get_llm_api_key(self) -> Optional[str]:
        """Get LLM API key from environment or config."""
        # First check environment (most secure)
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            return api_key
        # Fallback to config (less secure, but allows manual setup)
        return self.settings.get("llm_api_key")
    
    def get_llm_provider(self) -> Optional[str]:
        """Get LLM provider name."""
        # Auto-detect from env vars
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        if os.getenv("ANTHROPIC_API_KEY"):
            return "anthropic"
        # Fallback to config
        return self.settings.get("llm_provider")
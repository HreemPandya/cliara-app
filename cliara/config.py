"""
Configuration management for Cliara.
Handles user settings, first-run setup, and persistent configuration.
"""

import json
import os
import platform
from pathlib import Path
from typing import Dict, Any, Optional


class Config:
    """Manages Cliara configuration and settings."""
    
    DEFAULT_CONFIG = {
        "shell": None,  # Auto-detected
        "os": None,  # Auto-detected
        "nl_prefix": "?",
        "macro_storage": "~/.cliara/macros.json",
        "history_size": 1000,
        "safety_checks": True,
        "auto_confirm_safe": False,
        "prompt_style": "cliara",
        "llm_provider": None,  # "openai" or "anthropic" (Phase 2)
        "llm_api_key": None,  # Encrypted in real impl (Phase 2)
        "first_run_complete": False,
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
        with open(self.config_file, 'w') as f:
            json.dump(self.settings, f, indent=2)
    
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
        print("   cliara ❯ ls -la")
        print("   cliara ❯ cd projects")
        print("\n2. Use ? for natural language (Phase 2):")
        print("   cliara ❯ ? kill whatever is using port 3000")
        print("\n3. Create and run macros:")
        print("   cliara ❯ macro add mycommand")
        print("   cliara ❯ macro run mycommand")
        print("\n4. Save your last command as a macro:")
        print("   cliara ❯ macro save last as quickfix")
        print("\nType 'help' anytime for more info!")
        print("="*60 + "\n")
        
        self.complete_first_run()
        self.save()
    
    def get_macros_path(self) -> Path:
        """Get the path to macros storage."""
        macro_path = self.settings.get("macro_storage", "~/.cliara/macros.json")
        return Path(macro_path).expanduser()

"""
Macro management system for Cliara.
Handles creation, storage, retrieval, and execution of command macros.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import getpass

from cliara.storage import StorageBackend
from cliara.storage.factory import get_storage_backend


class Macro:
    """Represents a single macro."""
    
    def __init__(self, name: str, commands: List[str], description: str = "", 
                 created: Optional[str] = None, tags: Optional[List[str]] = None):
        self.name = name
        self.commands = commands if isinstance(commands, list) else [commands]
        self.description = description
        self.created = created or datetime.now().isoformat()
        self.tags = tags or []
        self.run_count = 0
        self.last_run = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return {
            "commands": self.commands,
            "description": self.description,
            "created": self.created,
            "tags": self.tags,
            "run_count": self.run_count,
            "last_run": self.last_run,
        }
    
    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> 'Macro':
        """Create Macro from dictionary."""
        macro = cls(
            name=name,
            commands=data.get("commands", []),
            description=data.get("description", ""),
            created=data.get("created"),
            tags=data.get("tags", [])
        )
        macro.run_count = data.get("run_count", 0)
        macro.last_run = data.get("last_run")
        return macro
    
    def mark_run(self):
        """Mark this macro as run (update stats)."""
        self.run_count += 1
        self.last_run = datetime.now().isoformat()
    
    def save(self, storage: StorageBackend, user_id: Optional[str] = None):
        """Save this macro to storage."""
        storage.add(self, user_id=user_id)


class MacroManager:
    """Manages macro storage and operations."""
    
    def __init__(self, storage_backend: Optional[StorageBackend] = None, 
                 storage_path: Optional[Path] = None, config: Optional[Dict[str, Any]] = None):
        """
        Initialize macro manager.
        
        Args:
            storage_backend: Optional StorageBackend instance (if None, will create from config)
            storage_path: Path to macros JSON file (legacy, for backward compatibility)
            config: Configuration dictionary for storage backend creation
        """
        if storage_backend:
            self.storage = storage_backend
        elif config:
            self.storage = get_storage_backend(config)
        else:
            # Default to JSON for backward compatibility
            if storage_path:
                storage_path = Path(storage_path).expanduser()
            else:
                storage_path = Path.home() / ".cliara" / "macros.json"
            from cliara.storage.json_backend import JSONStorage
            self.storage = JSONStorage(storage_path)
        
        # Get current user ID (for multi-user support)
        self.user_id = self._get_user_id()
    
    def _get_user_id(self) -> Optional[str]:
        """Get current user ID."""
        # For now, use system username
        # In future, could be from config or authentication
        try:
            return getpass.getuser()
        except:
            return None
    
    def add(self, name: str, commands: List[str], description: str = "", 
            tags: Optional[List[str]] = None) -> Macro:
        """
        Add or update a macro.
        
        Args:
            name: Macro name
            commands: List of commands
            description: Human-readable description
            tags: Optional tags for organization
        
        Returns:
            Created/updated Macro object
        """
        macro = Macro(name, commands, description, tags=tags)
        self.storage.add(macro, user_id=self.user_id)
        return macro
    
    def get(self, name: str) -> Optional[Macro]:
        """Get a macro by name."""
        return self.storage.get(name, user_id=self.user_id)
    
    def delete(self, name: str) -> bool:
        """
        Delete a macro.
        
        Returns:
            True if deleted, False if not found
        """
        return self.storage.delete(name, user_id=self.user_id)
    
    def list_all(self) -> Dict[str, Macro]:
        """Return all macros."""
        return self.storage.list_all(user_id=self.user_id)
    
    def search(self, query: str) -> List[Macro]:
        """
        Search macros by name, description, or tags.
        
        Args:
            query: Search query
        
        Returns:
            List of matching macros
        """
        return self.storage.search(query, user_id=self.user_id)
    
    def find_fuzzy(self, query: str, threshold: int = 70) -> Optional[str]:
        """
        Find macro using fuzzy matching.
        
        Args:
            query: Search query
            threshold: Minimum similarity score (0-100)
        
        Returns:
            Best matching macro name or None
        """
        try:
            from thefuzz import fuzz
            
            query_normalized = query.lower().strip()
            best_match = None
            best_score = threshold
            
            macros = self.list_all()
            for name in macros.keys():
                score = fuzz.ratio(query_normalized, name.lower())
                if score > best_score:
                    best_score = score
                    best_match = name
            
            return best_match
        except ImportError:
            # Fallback to exact match if thefuzz not installed
            return query if self.exists(query) else None
    
    def exists(self, name: str) -> bool:
        """Check if a macro exists."""
        return self.storage.exists(name, user_id=self.user_id)
    
    def count(self) -> int:
        """Return number of macros."""
        return self.storage.count(user_id=self.user_id)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get macro statistics."""
        macros = self.list_all()
        if not macros:
            return {
                "total": 0,
                "most_used": None,
                "recently_used": None,
            }
        
        most_used = max(macros.values(), key=lambda m: m.run_count)
        recently_used_list = [m for m in macros.values() if m.last_run]
        recently_used = max(recently_used_list, key=lambda m: m.last_run) if recently_used_list else None
        
        return {
            "total": len(macros),
            "most_used": most_used.name if most_used.run_count > 0 else None,
            "recently_used": recently_used.name if recently_used else None,
        }
    
    def export_macro(self, name: str) -> Optional[Dict[str, Any]]:
        """Export a single macro as dictionary."""
        macro = self.get(name)
        if macro:
            return {name: macro.to_dict()}
        return None
    
    def import_macro(self, name: str, data: Dict[str, Any]) -> Macro:
        """Import a macro from dictionary."""
        macro = Macro.from_dict(name, data)
        self.storage.add(macro, user_id=self.user_id)
        return macro

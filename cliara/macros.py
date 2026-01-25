"""
Macro management system for Cliara.
Handles creation, storage, retrieval, and execution of command macros.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime


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


class MacroManager:
    """Manages macro storage and operations."""
    
    def __init__(self, storage_path: Optional[Path] = None):
        """
        Initialize macro manager.
        
        Args:
            storage_path: Path to macros JSON file
        """
        if storage_path:
            self.storage_path = Path(storage_path).expanduser()
        else:
            self.storage_path = Path.home() / ".cliara" / "macros.json"
        
        self._ensure_storage()
        self.macros: Dict[str, Macro] = self._load_macros()
    
    def _ensure_storage(self):
        """Ensure storage directory and file exist."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self.storage_path.write_text("{}")
    
    def _load_macros(self) -> Dict[str, Macro]:
        """Load macros from storage."""
        try:
            with open(self.storage_path, 'r') as f:
                data = json.load(f)
                return {
                    name: Macro.from_dict(name, macro_data)
                    for name, macro_data in data.items()
                }
        except (json.JSONDecodeError, FileNotFoundError):
            return {}
    
    def _save_macros(self):
        """Save macros to storage."""
        data = {name: macro.to_dict() for name, macro in self.macros.items()}
        with open(self.storage_path, 'w') as f:
            json.dump(data, f, indent=2)
    
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
        self.macros[name] = macro
        self._save_macros()
        return macro
    
    def get(self, name: str) -> Optional[Macro]:
        """Get a macro by name."""
        return self.macros.get(name)
    
    def delete(self, name: str) -> bool:
        """
        Delete a macro.
        
        Returns:
            True if deleted, False if not found
        """
        if name in self.macros:
            del self.macros[name]
            self._save_macros()
            return True
        return False
    
    def list_all(self) -> Dict[str, Macro]:
        """Return all macros."""
        return self.macros.copy()
    
    def search(self, query: str) -> List[Macro]:
        """
        Search macros by name, description, or tags.
        
        Args:
            query: Search query
        
        Returns:
            List of matching macros
        """
        query_lower = query.lower()
        results = []
        
        for macro in self.macros.values():
            if (query_lower in macro.name.lower() or
                query_lower in macro.description.lower() or
                any(query_lower in tag.lower() for tag in macro.tags)):
                results.append(macro)
        
        return results
    
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
            
            for name in self.macros.keys():
                score = fuzz.ratio(query_normalized, name.lower())
                if score > best_score:
                    best_score = score
                    best_match = name
            
            return best_match
        except ImportError:
            # Fallback to exact match if thefuzz not installed
            return query if query in self.macros else None
    
    def exists(self, name: str) -> bool:
        """Check if a macro exists."""
        return name in self.macros
    
    def count(self) -> int:
        """Return number of macros."""
        return len(self.macros)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get macro statistics."""
        if not self.macros:
            return {
                "total": 0,
                "most_used": None,
                "recently_used": None,
            }
        
        most_used = max(self.macros.values(), key=lambda m: m.run_count)
        recently_used_list = [m for m in self.macros.values() if m.last_run]
        recently_used = max(recently_used_list, key=lambda m: m.last_run) if recently_used_list else None
        
        return {
            "total": len(self.macros),
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
        self.macros[name] = macro
        self._save_macros()
        return macro

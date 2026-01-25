"""
Macro storage and CRUD operations.
Handles loading, saving, and managing macros in JSON format.
"""

import json
import os
from typing import Dict, List, Optional, Any
from pathlib import Path


class MacroStore:
    """Manages macro storage and retrieval."""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.macros_file = self.data_dir / "macros.json"
        self._ensure_data_dir()
        self.macros = self._load_macros()
    
    def _ensure_data_dir(self):
        """Ensure data directory exists."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.macros_file.exists():
            self.macros_file.write_text("{}")
    
    def _load_macros(self) -> Dict[str, Any]:
        """Load macros from JSON file."""
        try:
            with open(self.macros_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}
    
    def _save_macros(self):
        """Save macros to JSON file."""
        with open(self.macros_file, 'w', encoding='utf-8') as f:
            json.dump(self.macros, f, indent=2, ensure_ascii=False)
    
    def add_macro(self, name: str, description: str, steps: List[Dict[str, str]]):
        """
        Add or update a macro.
        
        Args:
            name: Macro trigger phrase
            description: Human-readable description
            steps: List of command steps, each with 'type' and 'value'
        """
        self.macros[name] = {
            "description": description,
            "steps": steps
        }
        self._save_macros()
    
    def get_macro(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a macro by name (exact match)."""
        return self.macros.get(name)
    
    def delete_macro(self, name: str) -> bool:
        """
        Delete a macro by name.
        
        Returns:
            True if deleted, False if not found
        """
        if name in self.macros:
            del self.macros[name]
            self._save_macros()
            return True
        return False
    
    def list_macros(self) -> Dict[str, Any]:
        """Return all macros."""
        return self.macros.copy()
    
    def find_macro_fuzzy(self, query: str, threshold: int = 80) -> Optional[str]:
        """
        Find a macro using fuzzy matching.
        
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
            
            for macro_name in self.macros.keys():
                score = fuzz.ratio(query_normalized, macro_name.lower())
                if score > best_score:
                    best_score = score
                    best_match = macro_name
            
            return best_match
        except ImportError:
            # Fallback to exact match if thefuzz not installed
            return query if query in self.macros else None
    
    def macro_exists(self, name: str) -> bool:
        """Check if a macro exists."""
        return name in self.macros
    
    def count(self) -> int:
        """Return number of macros."""
        return len(self.macros)

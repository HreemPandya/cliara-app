"""
JSON storage backend (current implementation).
Maintained for backward compatibility and as fallback.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING
from cliara.storage import StorageBackend

if TYPE_CHECKING:
    from cliara.macros import Macro


class JSONStorage(StorageBackend):
    """JSON file-based storage backend."""
    
    def __init__(self, storage_path: Path):
        """
        Initialize JSON storage.
        
        Args:
            storage_path: Path to macros.json file
        """
        self.storage_path = Path(storage_path).expanduser()
        self._ensure_storage()
        self.macros: Dict[str, Macro] = self._load_macros()
    
    def _ensure_storage(self):
        """Ensure storage directory and file exist."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self.storage_path.write_text("{}")
    
    def _load_macros(self) -> Dict[str, 'Macro']:
        """Load macros from JSON file."""
        from cliara.macros import Macro  # Import here to avoid circular import
        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {
                    name: Macro.from_dict(name, macro_data)
                    for name, macro_data in data.items()
                }
        except (json.JSONDecodeError, FileNotFoundError):
            return {}
    
    def _save_macros(self):
        """Save macros to JSON file."""
        data = {name: macro.to_dict() for name, macro in self.macros.items()}
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def get(self, name: str, user_id: Optional[str] = None) -> Optional['Macro']:
        """Get a macro by name."""
        return self.macros.get(name)
    
    def add(self, macro: 'Macro', user_id: Optional[str] = None) -> 'Macro':
        """Add or update a macro."""
        self.macros[macro.name] = macro
        self._save_macros()
        return macro
    
    def delete(self, name: str, user_id: Optional[str] = None) -> bool:
        """Delete a macro."""
        if name in self.macros:
            del self.macros[name]
            self._save_macros()
            return True
        return False
    
    def list_all(self, user_id: Optional[str] = None) -> Dict[str, 'Macro']:
        """List all macros."""
        return self.macros.copy()
    
    def search(self, query: str, user_id: Optional[str] = None) -> List['Macro']:
        """Search macros."""
        query_lower = query.lower()
        results = []
        
        for macro in self.macros.values():
            if (query_lower in macro.name.lower() or
                query_lower in macro.description.lower() or
                any(query_lower in tag.lower() for tag in macro.tags)):
                results.append(macro)
        
        return results
    
    def exists(self, name: str, user_id: Optional[str] = None) -> bool:
        """Check if macro exists."""
        return name in self.macros
    
    def count(self, user_id: Optional[str] = None) -> int:
        """Get total count."""
        return len(self.macros)

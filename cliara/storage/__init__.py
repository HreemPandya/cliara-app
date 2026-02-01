"""
Storage abstraction layer for Cliara.
Supports multiple storage backends: JSON, PostgreSQL.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from cliara.macros import Macro


class StorageBackend(ABC):
    """Abstract base class for all storage backends."""
    
    @abstractmethod
    def get(self, name: str, user_id: Optional[str] = None) -> Optional['Macro']:
        """
        Get a macro by name.
        
        Args:
            name: Macro name
            user_id: Optional user ID for multi-user support
        
        Returns:
            Macro object or None if not found
        """
        pass
    
    @abstractmethod
    def add(self, macro: 'Macro', user_id: Optional[str] = None) -> 'Macro':
        """
        Add or update a macro.
        
        Args:
            macro: Macro object to save
            user_id: Optional user ID
        
        Returns:
            Saved Macro object
        """
        pass
    
    @abstractmethod
    def delete(self, name: str, user_id: Optional[str] = None) -> bool:
        """
        Delete a macro.
        
        Args:
            name: Macro name
            user_id: Optional user ID
        
        Returns:
            True if deleted, False if not found
        """
        pass
    
    @abstractmethod
    def list_all(self, user_id: Optional[str] = None) -> Dict[str, 'Macro']:
        """
        List all macros.
        
        Args:
            user_id: Optional user ID to filter by
        
        Returns:
            Dictionary of macro_name -> Macro
        """
        pass
    
    @abstractmethod
    def search(self, query: str, user_id: Optional[str] = None) -> List['Macro']:
        """
        Search macros by name, description, or tags.
        
        Args:
            query: Search query
            user_id: Optional user ID to filter by
        
        Returns:
            List of matching Macro objects
        """
        pass
    
    @abstractmethod
    def exists(self, name: str, user_id: Optional[str] = None) -> bool:
        """
        Check if a macro exists.
        
        Args:
            name: Macro name
            user_id: Optional user ID
        
        Returns:
            True if exists, False otherwise
        """
        pass
    
    @abstractmethod
    def count(self, user_id: Optional[str] = None) -> int:
        """
        Get total number of macros.
        
        Args:
            user_id: Optional user ID to filter by
        
        Returns:
            Number of macros
        """
        pass

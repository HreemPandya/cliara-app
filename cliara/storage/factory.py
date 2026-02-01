"""
Storage factory for creating storage backends.
"""

from pathlib import Path
from typing import Optional, Dict, Any
from cliara.storage import StorageBackend
from cliara.storage.json_backend import JSONStorage
from cliara.storage.postgres_backend import PostgresStorage


def get_storage_backend(config: Dict[str, Any]) -> StorageBackend:
    """
    Factory function to create appropriate storage backend.
    
    Args:
        config: Configuration dictionary with storage settings
    
    Returns:
        StorageBackend instance
    
    Configuration options:
        - storage_backend: "json" (default) or "postgres"
        - For JSON: storage_path (path to macros.json)
        - For Postgres: connection_string or individual params (host, port, database, user, password)
    """
    backend_type = config.get("storage_backend", "json").lower()
    
    if backend_type == "json":
        storage_path = config.get("storage_path") or config.get("macro_storage", "~/.cliara/macros.json")
        return JSONStorage(Path(storage_path))
    
    elif backend_type == "postgres":
        # Try connection string first
        connection_string = config.get("connection_string")
        if connection_string:
            return PostgresStorage(connection_string=connection_string)
        
        # Otherwise use individual parameters
        postgres_config = config.get("postgres", {})
        return PostgresStorage(**postgres_config)
    
    else:
        raise ValueError(
            f"Unknown storage backend: {backend_type}. "
            f"Supported: 'json', 'postgres'"
        )

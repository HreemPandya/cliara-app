"""
PostgreSQL storage backend for Cliara.
Supports millions of macros with fast queries and multi-user support.
"""

import json
import os
from typing import Dict, List, Optional
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import SimpleConnectionPool
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from cliara.storage import StorageBackend
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cliara.macros import Macro


class PostgresStorage(StorageBackend):
    """PostgreSQL-based storage backend."""
    
    def __init__(self, connection_string: Optional[str] = None, **kwargs):
        """
        Initialize PostgreSQL storage.
        
        Args:
            connection_string: PostgreSQL connection string
                Format: postgresql://user:password@host:port/database
            **kwargs: Alternative connection parameters
                - host, port, database, user, password
        """
        if not PSYCOPG2_AVAILABLE:
            raise ImportError(
                "psycopg2 is required for PostgreSQL backend. "
                "Install with: pip install psycopg2-binary"
            )
        
        # Build connection string
        if connection_string:
            self.connection_string = connection_string
        else:
            # Build from kwargs or environment
            host = kwargs.get('host') or os.getenv('POSTGRES_HOST', 'localhost')
            port = kwargs.get('port') or os.getenv('POSTGRES_PORT', '5432')
            database = kwargs.get('database') or os.getenv('POSTGRES_DB', 'cliara')
            user = kwargs.get('user') or os.getenv('POSTGRES_USER', 'cliara')
            password = kwargs.get('password') or os.getenv('POSTGRES_PASSWORD', '')
            
            self.connection_string = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        
        # Test connection and initialize schema
        self._init_schema()
    
    def _get_connection(self):
        """Get database connection."""
        return psycopg2.connect(self.connection_string)
    
    def _init_schema(self):
        """Initialize database schema."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                # Create macros table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS macros (
                        id SERIAL PRIMARY KEY,
                        user_id TEXT,
                        name TEXT NOT NULL,
                        commands JSONB NOT NULL,
                        description TEXT,
                        tags TEXT[],
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        run_count INTEGER DEFAULT 0,
                        last_run TIMESTAMP,
                        is_public BOOLEAN DEFAULT FALSE,
                        UNIQUE(user_id, name)
                    )
                """)
                
                # Create indexes for performance
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_macros_user_name 
                    ON macros(user_id, name)
                """)
                
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_macros_tags 
                    ON macros USING GIN(tags)
                """)
                
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_macros_public 
                    ON macros(is_public) WHERE is_public = TRUE
                """)
                
                # Full-text search index (removed - can cause issues, search uses ILIKE instead)
                # Full-text search is handled via ILIKE queries in search() method
                
                conn.commit()
        finally:
            conn.close()
    
    def get(self, name: str, user_id: Optional[str] = None) -> Optional['Macro']:
        """Get a macro by name."""
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if user_id:
                    cur.execute("""
                        SELECT name, commands, description, tags, 
                               created_at, run_count, last_run
                        FROM macros
                        WHERE name = %s AND user_id = %s
                    """, (name, user_id))
                else:
                    cur.execute("""
                        SELECT name, commands, description, tags, 
                               created_at, run_count, last_run
                        FROM macros
                        WHERE name = %s AND user_id IS NULL
                    """, (name,))
                
                row = cur.fetchone()
                if row:
                    return self._row_to_macro(row)
                return None
        finally:
            conn.close()
    
    def add(self, macro: 'Macro', user_id: Optional[str] = None) -> 'Macro':
        """Add or update a macro."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO macros 
                    (user_id, name, commands, description, tags, 
                     created_at, updated_at, run_count, last_run)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
                    ON CONFLICT (user_id, name) 
                    DO UPDATE SET
                        commands = EXCLUDED.commands,
                        description = EXCLUDED.description,
                        tags = EXCLUDED.tags,
                        updated_at = CURRENT_TIMESTAMP,
                        run_count = EXCLUDED.run_count,
                        last_run = EXCLUDED.last_run
                """, (
                    user_id,
                    macro.name,
                    json.dumps(macro.commands),
                    macro.description,
                    macro.tags,
                    macro.created,
                    macro.run_count,
                    macro.last_run
                ))
                conn.commit()
            return macro
        finally:
            conn.close()
    
    def delete(self, name: str, user_id: Optional[str] = None) -> bool:
        """Delete a macro."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "DELETE FROM macros WHERE name = %s AND user_id = %s",
                        (name, user_id)
                    )
                else:
                    cur.execute(
                        "DELETE FROM macros WHERE name = %s AND user_id IS NULL",
                        (name,)
                    )
                deleted = cur.rowcount > 0
                conn.commit()
                return deleted
        finally:
            conn.close()
    
    def list_all(self, user_id: Optional[str] = None) -> Dict[str, 'Macro']:
        """List all macros for a user."""
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if user_id:
                    cur.execute("""
                        SELECT name, commands, description, tags, 
                               created_at, run_count, last_run
                        FROM macros
                        WHERE user_id = %s
                        ORDER BY name
                    """, (user_id,))
                else:
                    cur.execute("""
                        SELECT name, commands, description, tags, 
                               created_at, run_count, last_run
                        FROM macros
                        WHERE user_id IS NULL
                        ORDER BY name
                    """)
                
                rows = cur.fetchall()
                return {row['name']: self._row_to_macro(row) for row in rows}
        finally:
            conn.close()
    
    def search(self, query: str, user_id: Optional[str] = None) -> List['Macro']:
        """Full-text search macros."""
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Use PostgreSQL full-text search
                search_query = f"%{query}%"
                
                if user_id:
                    cur.execute("""
                        SELECT name, commands, description, tags, 
                               created_at, run_count, last_run
                        FROM macros
                        WHERE user_id = %s
                        AND (
                            name ILIKE %s OR
                            description ILIKE %s OR
                            EXISTS (
                                SELECT 1 FROM unnest(tags) AS tag 
                                WHERE tag ILIKE %s
                            )
                        )
                        ORDER BY 
                            CASE 
                                WHEN name ILIKE %s THEN 1
                                WHEN description ILIKE %s THEN 2
                                ELSE 3
                            END,
                            name
                    """, (user_id, search_query, search_query, search_query, 
                          search_query, search_query))
                else:
                    cur.execute("""
                        SELECT name, commands, description, tags, 
                               created_at, run_count, last_run
                        FROM macros
                        WHERE user_id IS NULL
                        AND (
                            name ILIKE %s OR
                            description ILIKE %s OR
                            EXISTS (
                                SELECT 1 FROM unnest(tags) AS tag 
                                WHERE tag ILIKE %s
                            )
                        )
                        ORDER BY 
                            CASE 
                                WHEN name ILIKE %s THEN 1
                                WHEN description ILIKE %s THEN 2
                                ELSE 3
                            END,
                            name
                    """, (search_query, search_query, search_query, 
                          search_query, search_query))
                
                rows = cur.fetchall()
                return [self._row_to_macro(row) for row in rows]
        finally:
            conn.close()
    
    def exists(self, name: str, user_id: Optional[str] = None) -> bool:
        """Check if macro exists."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT 1 FROM macros WHERE name = %s AND user_id = %s LIMIT 1",
                        (name, user_id)
                    )
                else:
                    cur.execute(
                        "SELECT 1 FROM macros WHERE name = %s AND user_id IS NULL LIMIT 1",
                        (name,)
                    )
                return cur.fetchone() is not None
        finally:
            conn.close()
    
    def count(self, user_id: Optional[str] = None) -> int:
        """Get total count of macros."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT COUNT(*) FROM macros WHERE user_id = %s",
                        (user_id,)
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM macros WHERE user_id IS NULL"
                    )
                return cur.fetchone()[0]
        finally:
            conn.close()
    
    def _row_to_macro(self, row: dict) -> 'Macro':
        """Convert database row to Macro object."""
        from cliara.macros import Macro  # Import here to avoid circular import
        
        # JSONB is returned as dict/list by psycopg2, but we need to ensure it's a list
        commands = row['commands']
        if isinstance(commands, str):
            commands = json.loads(commands)
        elif not isinstance(commands, list):
            commands = list(commands) if commands else []
        
        macro = Macro(
            name=row['name'],
            commands=commands,
            description=row['description'] or "",
            tags=list(row['tags']) if row['tags'] else [],
            created=row['created_at'].isoformat() if row['created_at'] else None
        )
        macro.run_count = row['run_count'] or 0
        macro.last_run = row['last_run'].isoformat() if row['last_run'] else None
        return macro
    
    def get_public_macros(self, limit: int = 100) -> List['Macro']:
        """Get public macros (for marketplace feature)."""
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT name, commands, description, tags, 
                           created_at, run_count, last_run
                    FROM macros
                    WHERE is_public = TRUE
                    ORDER BY run_count DESC, created_at DESC
                    LIMIT %s
                """, (limit,))
                
                rows = cur.fetchall()
                return [self._row_to_macro(row) for row in rows]
        finally:
            conn.close()
    
    def set_public(self, name: str, user_id: str, is_public: bool) -> bool:
        """Set macro public/private status."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE macros
                    SET is_public = %s
                    WHERE name = %s AND user_id = %s
                """, (is_public, name, user_id))
                updated = cur.rowcount > 0
                conn.commit()
                return updated
        finally:
            conn.close()

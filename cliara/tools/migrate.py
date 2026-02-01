"""
Migration tool to migrate macros from JSON to PostgreSQL.
"""

import json
import sys
from pathlib import Path
from typing import Optional

from cliara.macros import MacroManager, Macro
from cliara.storage.json_backend import JSONStorage
from cliara.storage.postgres_backend import PostgresStorage
from cliara.config import Config


def migrate_json_to_postgres(
    json_path: Path,
    postgres_config: dict,
    dry_run: bool = False
) -> int:
    """
    Migrate macros from JSON to PostgreSQL.
    
    Args:
        json_path: Path to source JSON file
        postgres_config: PostgreSQL configuration dict
        dry_run: If True, don't actually migrate, just show what would be migrated
    
    Returns:
        Number of macros migrated
    """
    # Load from JSON
    json_storage = JSONStorage(json_path)
    json_macros = json_storage.list_all()
    
    if not json_macros:
        print(f"No macros found in {json_path}")
        return 0
    
    print(f"Found {len(json_macros)} macros in JSON file")
    
    if dry_run:
        print("\n[DRY RUN] Would migrate the following macros:")
        for name, macro in json_macros.items():
            print(f"  - {name}: {len(macro.commands)} command(s)")
        return len(json_macros)
    
    # Connect to PostgreSQL
    try:
        postgres_storage = PostgresStorage(**postgres_config)
    except Exception as e:
        print(f"[Error] Failed to connect to PostgreSQL: {e}")
        print("\nMake sure PostgreSQL is running and credentials are correct.")
        return 0
    
    # Migrate each macro
    migrated = 0
    failed = 0
    
    print("\nMigrating macros...")
    for name, macro in json_macros.items():
        try:
            postgres_storage.add(macro)
            migrated += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
    
    print(f"\nMigration complete: {migrated} migrated, {failed} failed")
    return migrated


def main():
    """CLI entry point for migration."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Migrate macros from JSON to PostgreSQL"
    )
    parser.add_argument(
        "--json-path",
        type=Path,
        help="Path to source JSON file (default: ~/.cliara/macros.json)"
    )
    parser.add_argument(
        "--connection-string",
        help="PostgreSQL connection string (postgresql://user:pass@host:port/db)"
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="PostgreSQL host (default: localhost)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5432,
        help="PostgreSQL port (default: 5432)"
    )
    parser.add_argument(
        "--database",
        default="cliara",
        help="PostgreSQL database name (default: cliara)"
    )
    parser.add_argument(
        "--user",
        default="cliara",
        help="PostgreSQL user (default: cliara)"
    )
    parser.add_argument(
        "--password",
        help="PostgreSQL password (or set POSTGRES_PASSWORD env var)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without actually migrating"
    )
    
    args = parser.parse_args()
    
    # Determine JSON path
    if args.json_path:
        json_path = Path(args.json_path).expanduser()
    else:
        config = Config()
        json_path = config.get_macros_path()
    
    if not json_path.exists():
        print(f"[Error] JSON file not found: {json_path}")
        sys.exit(1)
    
    # Build PostgreSQL config
    if args.connection_string:
        postgres_config = {"connection_string": args.connection_string}
    else:
        import os
        postgres_config = {
            "host": args.host,
            "port": args.port,
            "database": args.database,
            "user": args.user,
            "password": args.password or os.getenv("POSTGRES_PASSWORD", ""),
        }
    
    # Run migration
    migrated = migrate_json_to_postgres(
        json_path,
        postgres_config,
        dry_run=args.dry_run
    )
    
    if migrated > 0 and not args.dry_run:
        print("\n✓ Migration successful!")
        print("\nTo use PostgreSQL, update your ~/.cliara/config.json:")
        print(json.dumps({
            "storage_backend": "postgres",
            "postgres": {
                "host": postgres_config.get("host", "localhost"),
                "port": postgres_config.get("port", 5432),
                "database": postgres_config.get("database", "cliara"),
                "user": postgres_config.get("user", "cliara"),
            }
        }, indent=2))
        print("\n(Password should be set via POSTGRES_PASSWORD environment variable)")


if __name__ == "__main__":
    main()

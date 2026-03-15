"""
Cross-platform file locking for multi-instance support.

Allows multiple Cliara processes to run simultaneously (like multiple PowerShell
windows) by using file locks when reading/writing shared state files.
"""

from pathlib import Path
from typing import Optional

_LOCK_TIMEOUT = 10  # seconds to wait for lock before giving up


def _get_lock_path(file_path: Path) -> Path:
    """Return the lock file path for a given data file."""
    return file_path.parent / f".{file_path.name}.lock"


def with_file_lock(file_path: Path, timeout: float = _LOCK_TIMEOUT):
    """
    Context manager that acquires an exclusive lock on the given file path.
    Use when reading or writing shared state files.

    Example:
        with with_file_lock(history_file):
            data = history_file.read_text()
    """
    try:
        from filelock import FileLock
    except ImportError:
        # filelock not installed — no locking (single-instance behavior)
        from contextlib import nullcontext
        return nullcontext()

    lock_path = _get_lock_path(file_path)
    return FileLock(lock_path, timeout=timeout)

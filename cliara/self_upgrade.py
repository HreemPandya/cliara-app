"""
Detect and run pip installs/upgrades for the cliara package from inside Cliara.

Uses the same interpreter as the running session (``sys.executable -m pip``) so
the correct environment is updated. On Windows, replacing files while this
process has the package loaded may still fail; callers should surface the
fallback guidance in ``windows_replace_failure_hint``.
"""

from __future__ import annotations

import re
import sys
from typing import List

# Standalone package name only (not cliara-foo, not paths containing cliara).
_CLIARA_PKG = re.compile(r"(?<![\w-])cliara(?![\w-])")

# pip invoked as ``pip`` / ``pip3`` / ``python -m pip`` at the start of a segment
_PIP_PREFIX = re.compile(
    r"(?:^|[;&|`\n]|\|\||&&)\s*(?:"
    r"python(?:\d(?:\.\d+)?)?\s+-m\s+pip"
    r"|pip3?"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)


def mentions_cliara_package(command: str) -> bool:
    return bool(_CLIARA_PKG.search(command))


def is_cliara_pip_install_command(command: str) -> bool:
    """
    True if *command* looks like installing/upgrading the PyPI ``cliara`` package
    via pip (not ``pip uninstall``).
    """
    cmd = command.strip()
    if not cmd or not mentions_cliara_package(cmd):
        return False
    low = cmd.lower()
    if re.search(r"\buninstall\b", low):
        return False
    if not _PIP_PREFIX.search(cmd):
        return False
    if not re.search(r"\binstall\b", low):
        return False
    return True


# Flags copied from the user's line onto ``pip install --upgrade cliara``.
_PIP_FLAGS = (
    "--user",
    "--break-system-packages",
    "--pre",
    "--force-reinstall",
    "--no-deps",
    "--no-cache-dir",
    "-q",
    "-v",
    "-vv",
    "-vvv",
)


def build_pip_upgrade_cliara_argv(original_command: str) -> List[str]:
    """
    Build argv for ``python -m pip install --upgrade … cliara`` from a shell line.

    Preserves a small set of common pip flags if present on the original line.
    """
    argv: List[str] = [sys.executable, "-m", "pip", "install", "--upgrade"]
    low = original_command.lower()
    for flag in _PIP_FLAGS:
        if re.search(re.escape(flag) + r"(?:\s|$)", low):
            if flag not in argv:
                argv.append(flag)
    argv.append("cliara")
    return argv


def windows_replace_failure_hint() -> str:
    return (
        "On Windows, pip often cannot replace Cliara while this process is running "
        "(files are still in use). Quit all Cliara windows, then in a new terminal run:\n"
        f"  {sys.executable} -m pip install --upgrade cliara\n"
        "Or install with pipx so the app runs in an isolated environment."
    )


def stderr_suggests_file_in_use(stderr: str) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    needles = (
        "winerror 32",
        "being used by another process",
        "cannot access the file",
        "permission denied",
        "access is denied",
        "file is being used",
        "text file busy",
    )
    return any(n in s for n in needles)

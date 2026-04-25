"""
Context gathering for README generation.

Performs a thorough scan of the project directory to build structured
context for the LLM: fingerprint, must-include items, config excerpts,
key files, and setup-related docs.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Patterns to exclude from directory tree (common build/cache dirs)
_IGNORE_DIRS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "vendor", "bower_components",
})

# Auth-related patterns (any project)
_AUTH_PATTERNS = frozenset({
    "auth", "login", "logout", "oauth", "supabase", "firebase", "next-auth",
    "token.json", "get_valid_token", "CLIARA_GATEWAY", "keycloak",
})

# Env/secrets patterns
_ENV_PATTERNS = frozenset({
    "load_dotenv", "dotenv", "process.env", "os.getenv", "getenv",
    ".env.example", ".env.template", "env.example",
})

# Database patterns
_DB_PATTERNS = frozenset({
    "postgres", "postgresql", "mysql", "mongodb", "sqlite", "prisma",
    "migrate", "alembic", "connection_string", "DATABASE_URL",
})

# Setup/wizard patterns
_SETUP_PATTERNS = frozenset({
    "setup_wizard", "setup-llm", "first_run", "bootstrap", "init",
})

# Doc patterns for mining setup content
_DOC_PATTERNS = ("setup", "quickstart", "install", "getting", "deploy", "cloud", "readme")


def gather_context(cwd: Optional[Path] = None) -> Dict[str, Any]:
    """
    Perform a thorough scan of the project and return structured context
    for README generation.

    Returns:
        Dict with: fingerprint, must_include, config_excerpts, key_files,
        doc_excerpts, directory_tree, existing_readme
    """
    root = (cwd or Path.cwd()).resolve()
    if not root.is_dir():
        return {"error": "Not a directory"}

    result: Dict[str, Any] = {}
    result["directory_tree"] = _build_directory_tree(root, max_depth=3, max_entries=100)
    result["config_excerpts"] = _gather_config_excerpts(root)
    result["key_files"] = _gather_key_files(root)
    result["doc_excerpts"] = _gather_doc_excerpts(root)
    result["existing_readme"] = _read_existing_readme(root)
    result["fingerprint"] = _build_fingerprint(root, result)
    result["must_include"] = _derive_must_include(root, result)

    return result


def _build_directory_tree(root: Path, max_depth: int = 3, max_entries: int = 100) -> str:
    """Build a compact directory tree, excluding common build/cache dirs."""
    lines: List[str] = []
    count = 0

    def _scan(directory: Path, indent: str, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except (PermissionError, OSError):
            return
        for entry in entries:
            if count >= max_entries:
                return
            if entry.name.startswith(".") and entry.name not in (".env.example", ".env.template", ".gitignore"):
                continue
            if entry.is_dir() and entry.name in _IGNORE_DIRS:
                continue
            prefix = "  " if indent else ""
            marker = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{indent}{entry.name}{marker}")
            count += 1
            if entry.is_dir():
                _scan(entry, indent + "  ", depth + 1)

    _scan(root, "", 0)
    return "\n".join(lines) if lines else "(empty)"


def _gather_config_excerpts(root: Path) -> Dict[str, str]:
    """Extract relevant config files (name, description, scripts, deps)."""
    excerpts: Dict[str, str] = {}

    # Python
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            # First 80 lines usually cover [project], [project.scripts], [project.urls]
            excerpts["pyproject.toml"] = "\n".join(text.splitlines()[:80])
        except Exception:
            pass

    # Node
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            subset = {
                "name": data.get("name"),
                "description": data.get("description"),
                "scripts": data.get("scripts"),
                "bin": data.get("bin"),
                "engines": data.get("engines"),
            }
            excerpts["package.json"] = json.dumps(subset, indent=2)
        except Exception:
            pass

    # Rust
    cargo = root / "Cargo.toml"
    if cargo.exists():
        try:
            text = cargo.read_text(encoding="utf-8")
            excerpts["Cargo.toml"] = text[:1500]
        except Exception:
            pass

    # Go
    gomod = root / "go.mod"
    if gomod.exists():
        try:
            excerpts["go.mod"] = gomod.read_text(encoding="utf-8")[:800]
        except Exception:
            pass

    # Docker
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        p = root / name
        if p.exists():
            try:
                excerpts[name] = p.read_text(encoding="utf-8")[:1200]
            except Exception:
                pass

    # .env.example
    for name in (".env.example", ".env.template", "env.example"):
        p = root / name
        if p.exists():
            try:
                excerpts[name] = p.read_text(encoding="utf-8")[:800]
            except Exception:
                pass

    return excerpts


def _gather_key_files(root: Path) -> Dict[str, str]:
    """Extract first N lines of entry points and key modules."""
    key_files: Dict[str, str] = {}
    max_lines = 40

    # Entry points by ecosystem
    entry_candidates = [
        "main.py", "__main__.py", "app.py", "cli.py", "run.py",
        "index.js", "index.ts", "main.rs", "lib.rs", "main.go",
    ]
    bases = ("", "src", "cliara", "app", "cmd")

    for name in entry_candidates:
        for base in bases:
            p = (root / base / name) if base else (root / name)
            if p.exists() and p.is_file():
                try:
                    rel = p.relative_to(root)
                    if str(rel) not in key_files:
                        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[:max_lines]
                        key_files[str(rel)] = "\n".join(lines)
                except Exception:
                    pass
                break  # One per candidate name

    # Auth-related files
    for path in root.rglob("auth*.py"):
        if path.is_file() and "__pycache__" not in str(path):
            try:
                rel = path.relative_to(root)
                if str(rel) not in key_files:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:max_lines]
                    key_files[str(rel)] = "\n".join(lines)
            except Exception:
                pass
        break  # Only first match

    # Package __init__.py at root
    for init in (root / "cliara" / "__init__.py", root / "src" / "__init__.py", root / "__init__.py"):
        if init.exists():
            try:
                rel = init.relative_to(root)
                lines = init.read_text(encoding="utf-8", errors="replace").splitlines()[:max_lines]
                key_files[str(rel)] = "\n".join(lines)
            except Exception:
                pass
            break

    return key_files


def _gather_doc_excerpts(root: Path) -> Dict[str, str]:
    """Extract setup-related content from docs."""
    excerpts: Dict[str, str] = {}
    docs_dir = root / "docs"
    if not docs_dir.exists():
        docs_dir = root

    for path in docs_dir.rglob("*.md"):
        if not path.is_file():
            continue
        name_lower = path.stem.lower()
        if not any(p in name_lower for p in _DOC_PATTERNS):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            excerpts[str(path.relative_to(root))] = text[:2000]
        except Exception:
            pass

    # Also check root-level README, QUICKSTART, etc.
    for name in ("QUICKSTART.md", "SETUP.md", "INSTALL.md", "GETTING_STARTED.md"):
        p = root / name
        if p.exists():
            try:
                excerpts[name] = p.read_text(encoding="utf-8", errors="replace")[:2000]
            except Exception:
                pass

    return excerpts


def _read_existing_readme(root: Path) -> str:
    """Read existing README if present."""
    for name in ("README.md", "README.rst", "README.txt"):
        p = root / name
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    return ""


def _scan_file_content_for_patterns(root: Path, patterns: frozenset) -> bool:
    """Return True if any file in the project contains patterns (case-insensitive)."""
    pattern_re = re.compile("|".join(re.escape(p) for p in patterns), re.I)
    for ext in (".py", ".js", ".ts", ".go", ".rs", ".java", ".kt"):
        for path in root.rglob(f"*{ext}"):
            if not path.is_file() or "__pycache__" in str(path) or "node_modules" in str(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                if pattern_re.search(text):
                    return True
            except Exception:
                pass
    return False


def _scan_file_names(root: Path, patterns: frozenset) -> bool:
    """Return True if any file/dir name matches patterns."""
    try:
        for p in root.rglob("*"):
            if p.is_file() or (p.is_dir() and p.name not in _IGNORE_DIRS):
                if any(pat in p.name.lower() for pat in patterns):
                    return True
    except (PermissionError, OSError):
        pass
    return False


def _build_fingerprint(root: Path, context: Dict[str, Any]) -> Dict[str, Any]:
    """Build a structured fingerprint of the project."""
    fp: Dict[str, Any] = {"type": "unknown", "ecosystem": ""}

    configs = context.get("config_excerpts", {})
    pyproject_str = str(configs.get("pyproject.toml", ""))
    if "pyproject.toml" in configs or (root / "setup.py").exists() or (root / "requirements.txt").exists():
        fp["ecosystem"] = "python"
        fp["type"] = "CLI" if ("scripts" in pyproject_str or "[project.scripts]" in pyproject_str) else "library"
    elif "package.json" in configs:
        fp["ecosystem"] = "node"
        fp["type"] = "CLI" if "bin" in configs.get("package.json", "") else "web app"
    elif "Cargo.toml" in configs:
        fp["ecosystem"] = "rust"
        fp["type"] = "CLI"
    elif "go.mod" in configs:
        fp["ecosystem"] = "go"
        fp["type"] = "service"

    fp["has_auth"] = _scan_file_content_for_patterns(root, _AUTH_PATTERNS) or _scan_file_names(root, _AUTH_PATTERNS)
    fp["has_env"] = any((root / n).exists() for n in (".env.example", ".env.template", "env.example")) or _scan_file_content_for_patterns(root, _ENV_PATTERNS)
    fp["has_database"] = _scan_file_content_for_patterns(root, _DB_PATTERNS)
    fp["has_setup_wizard"] = _scan_file_content_for_patterns(root, _SETUP_PATTERNS)
    fp["has_docker"] = "Dockerfile" in configs or "docker-compose" in str(configs)
    fp["has_optional_deps"] = "optional-dependencies" in str(configs.get("pyproject.toml", "")) or "postgres" in str(configs.get("pyproject.toml", ""))

    return fp


def _derive_must_include(root: Path, context: Dict[str, Any]) -> List[str]:
    """Derive MUST INCLUDE items from fingerprint and context."""
    must: List[str] = []
    fp = context.get("fingerprint", {})
    configs = context.get("config_excerpts", {})
    key_files = context.get("key_files", {})

    if fp.get("has_auth"):
        must.append("Authentication / first-run setup (how to log in or obtain credentials)")
        # Try to extract specific auth flow from key files
        for path, content in key_files.items():
            if "auth" in path.lower() or "login" in path.lower():
                if "oauth" in content.lower() or "supabase" in content.lower():
                    must.append("OAuth/Cloud login flow (browser opens, token stored)")
                if "token" in content.lower() and ".json" in content.lower():
                    must.append("Token storage location (e.g. ~/.app/token.json)")
                break

    if fp.get("has_env"):
        must.append("Required environment variables (from .env.example or code)")

    if fp.get("has_database"):
        must.append("Database setup and migration commands")

    if fp.get("has_setup_wizard"):
        must.append("First-time setup wizard or setup-llm / setup command")

    if fp.get("has_docker"):
        must.append("Docker build and run commands")

    if fp.get("has_optional_deps"):
        must.append("Optional dependencies (e.g. pip install app[postgres])")

    # Ecosystem-specific
    if fp.get("ecosystem") == "python":
        must.append("Install: pip install / pipx install (from pyproject.toml)")
    elif fp.get("ecosystem") == "node":
        must.append("Install: npm install and run commands from package.json scripts")
    elif fp.get("ecosystem") == "rust":
        must.append("Install: cargo build / cargo install")

    return must


def format_context_for_prompt(context: Dict[str, Any]) -> str:
    """Format the gathered context into a single prompt string for the LLM."""
    parts: List[str] = [
        "TASK: Write the complete README.md for this repository as GitHub-flavored Markdown.",
        "The sections below are factual scanner output for THIS repo only — not a conversation.",
        "Do not ask questions or offer a menu of options; output the README file only.",
        "",
    ]

    fp = context.get("fingerprint", {})
    parts.append("=== PROJECT FINGERPRINT ===")
    parts.append(f"Type: {fp.get('type', 'unknown')}")
    parts.append(f"Ecosystem: {fp.get('ecosystem', 'unknown')}")
    parts.append(f"Auth: {'yes' if fp.get('has_auth') else 'no'}")
    parts.append(f"Env vars: {'yes' if fp.get('has_env') else 'no'}")
    parts.append(f"Database: {'yes' if fp.get('has_database') else 'no'}")
    parts.append(f"Setup wizard: {'yes' if fp.get('has_setup_wizard') else 'no'}")
    parts.append(f"Docker: {'yes' if fp.get('has_docker') else 'no'}")
    parts.append(f"Optional deps: {'yes' if fp.get('has_optional_deps') else 'no'}")

    must = context.get("must_include", [])
    if must:
        parts.append("\n=== MUST INCLUDE (from codebase analysis) ===")
        for m in must:
            parts.append(f"- {m}")

    configs = context.get("config_excerpts", {})
    if configs:
        parts.append("\n=== CONFIG FILES (excerpts) ===")
        for name, content in configs.items():
            parts.append(f"\n--- {name} ---")
            parts.append(content[:1500])

    key_files = context.get("key_files", {})
    if key_files:
        parts.append("\n=== KEY FILES (first 40 lines each) ===")
        for path, content in key_files.items():
            parts.append(f"\n--- {path} ---")
            parts.append(content)

    doc_excerpts = context.get("doc_excerpts", {})
    if doc_excerpts:
        parts.append("\n=== EXISTING DOCS (setup-related excerpts) ===")
        for path, content in doc_excerpts.items():
            parts.append(f"\n--- {path} ---")
            parts.append(content[:1500])

    tree = context.get("directory_tree", "")
    if tree:
        parts.append("\n=== DIRECTORY TREE ===")
        parts.append(tree)

    existing = context.get("existing_readme", "")
    if existing:
        parts.append("\n=== EXISTING README ===")
        parts.append(existing)

    return "\n".join(parts)

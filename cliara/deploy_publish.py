"""
Version-aware pre-flight for package publishing (npm, PyPI, crates.io).

Publishing a version that already exists on the registry is the single most
common publish failure. Before running a publish, Cliara reads the current
version from the project manifest, asks the registry whether that version is
already published, and (if so) offers to bump it - so the user never has to
remember to bump the version first.

Network and file parsing are all best-effort: any failure degrades to
"unknown / leave it alone" rather than blocking the deploy.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # Python 3.11+
    import tomllib as _tomllib
except Exception:  # pragma: no cover - exercised only on <3.11
    _tomllib = None


_USER_AGENT = "cliara-deploy (https://github.com/; version preflight)"


@dataclass
class PublishInfo:
    """Resolved publish metadata for one package."""

    platform: str           # "npm" | "pypi" | "crates.io"
    package_name: str
    current_version: str
    manifest_path: Path


# ------------------------------------------------------------------
# Manifest reading
# ------------------------------------------------------------------

def _read_toml(path: Path) -> dict:
    """Parse a TOML file with tomllib when available; {} on any failure."""
    if _tomllib is None:
        return {}
    try:
        with path.open("rb") as fh:
            return _tomllib.load(fh)
    except Exception:
        return {}


def _toml_field_regex(text: str, key: str) -> str:
    """
    Crude single-key lookup for TOML on Python <3.11 (no tomllib).

    Matches ``key = "value"`` ignoring leading whitespace. Returns "" if absent.
    """
    m = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else ""


def _npm_info(cwd: Path) -> Optional[PublishInfo]:
    pkg_path = cwd / "package.json"
    if not pkg_path.exists():
        return None
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    name = data.get("name", "")
    version = data.get("version", "")
    if not name or not version:
        return None
    return PublishInfo("npm", name, version, pkg_path)


def _pypi_info(cwd: Path) -> Optional[PublishInfo]:
    toml_path = cwd / "pyproject.toml"
    if not toml_path.exists():
        return None
    data = _read_toml(toml_path)
    name = version = ""
    if data:
        project = data.get("project", {}) or {}
        name = project.get("name", "") or ""
        version = project.get("version", "") or ""
        if not name or not version:
            poetry = (data.get("tool", {}) or {}).get("poetry", {}) or {}
            name = name or poetry.get("name", "") or ""
            version = version or poetry.get("version", "") or ""
    if not name or not version:
        # Fallback for <3.11 or dynamic layouts: line-level regex.
        text = toml_path.read_text(encoding="utf-8", errors="replace")
        name = name or _toml_field_regex(text, "name")
        version = version or _toml_field_regex(text, "version")
    if not name or not version:
        return None
    return PublishInfo("pypi", name, version, toml_path)


def _cargo_info(cwd: Path) -> Optional[PublishInfo]:
    toml_path = cwd / "Cargo.toml"
    if not toml_path.exists():
        return None
    data = _read_toml(toml_path)
    name = version = ""
    if data:
        package = data.get("package", {}) or {}
        name = package.get("name", "") or ""
        version = package.get("version", "") or ""
    if not name or not version:
        text = toml_path.read_text(encoding="utf-8", errors="replace")
        name = name or _toml_field_regex(text, "name")
        version = version or _toml_field_regex(text, "version")
    if not name or not version:
        return None
    return PublishInfo("crates.io", name, version, toml_path)


def read_publish_info(cwd: Path, platform: str) -> Optional[PublishInfo]:
    """Read name/version for the given publish *platform*, or None."""
    if platform == "npm":
        return _npm_info(cwd)
    if platform == "pypi":
        return _pypi_info(cwd)
    if platform == "crates.io":
        return _cargo_info(cwd)
    return None


# ------------------------------------------------------------------
# Registry "is this version already published?" probes
# ------------------------------------------------------------------

def _http_status(url: str, timeout: float = 8.0) -> Optional[int]:
    """Return the HTTP status code for a GET, or None on network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def is_version_published(info: PublishInfo, timeout: float = 8.0) -> Optional[bool]:
    """
    Ask the registry whether ``info.current_version`` already exists.

    Returns True/False, or None when the registry could not be reached
    (so callers can proceed without falsely blocking).
    """
    if not info.package_name or not info.current_version:
        return None
    if info.platform == "npm":
        # Scoped names (@scope/pkg) must url-encode the slash for the registry.
        encoded = urllib.parse.quote(info.package_name, safe="")
        url = f"https://registry.npmjs.org/{encoded}/{info.current_version}"
    elif info.platform == "pypi":
        url = f"https://pypi.org/pypi/{info.package_name}/{info.current_version}/json"
    elif info.platform == "crates.io":
        url = f"https://crates.io/api/v1/crates/{info.package_name}/{info.current_version}"
    else:
        return None

    status = _http_status(url, timeout=timeout)
    if status is None:
        return None
    if status == 200:
        return True
    if status == 404:
        return False
    # Any other status (e.g. 403/5xx) is inconclusive.
    return None


# ------------------------------------------------------------------
# Version bumping
# ------------------------------------------------------------------

def bump_semver(version: str, part: str = "patch") -> Optional[str]:
    """
    Bump a dotted numeric version. Supports X.Y.Z and X.Y (any trailing
    pre-release/build suffix on X.Y.Z is dropped). Returns None if the
    version is not numerically bumpable.
    """
    part = part.lower()
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version.strip())
    if m:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m2 = re.match(r"^(\d+)\.(\d+)\s*$", version.strip())
        if not m2:
            return None
        major, minor, patch = int(m2.group(1)), int(m2.group(2)), 0
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:  # patch (default)
        patch += 1
    return f"{major}.{minor}.{patch}"


def write_new_version(info: PublishInfo, new_version: str) -> bool:
    """
    Replace the current version with *new_version* in the manifest, preserving
    surrounding formatting. Returns True on success.
    """
    try:
        text = info.manifest_path.read_text(encoding="utf-8")
    except Exception:
        return False

    old = re.escape(info.current_version)
    if info.platform == "npm":
        # "version": "x.y.z"
        pattern = rf'("version"\s*:\s*")({old})(")'
    else:
        # version = "x.y.z"  (pyproject.toml / Cargo.toml)
        pattern = rf'(version\s*=\s*["\'])({old})(["\'])'

    new_text, count = re.subn(pattern, rf"\g<1>{new_version}\g<3>", text, count=1)
    if count == 0:
        return False
    try:
        info.manifest_path.write_text(new_text, encoding="utf-8")
    except Exception:
        return False
    return True

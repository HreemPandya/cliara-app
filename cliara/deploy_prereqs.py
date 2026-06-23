"""
Pre-flight prerequisite metadata for Cliara deploys.

For each deploy platform Cliara knows:
  * which CLI tool(s) must be installed (and how to install them per-OS), and
  * how to tell whether the user is authenticated (and how to log in).

This turns the two most common first-time deploy failures
("command not found" and "you are not logged in") into guided, fixable
steps so a user never has to know a platform's tooling in advance.

This module is intentionally side-effect free except for the small
``is_installed`` / ``is_authenticated`` / ``docker_daemon_running`` probes,
which keeps it easy to unit-test.
"""

import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _os_key() -> str:
    """Return a normalised OS key: 'windows', 'macos', or 'linux'."""
    sysname = _platform.system()
    if sysname == "Windows":
        return "windows"
    if sysname == "Darwin":
        return "macos"
    return "linux"


@dataclass
class CliRequirement:
    """A CLI tool a deploy platform depends on."""

    binary: str                                     # executable name on PATH, e.g. "vercel"
    display_name: str                               # human label, e.g. "Vercel CLI"
    install: Dict[str, str] = field(default_factory=dict)   # os_key -> install command; "*" = fallback
    auth_check: Optional[List[str]] = None          # argv to probe auth; rc 0 == authed (see needs_stdout)
    auth_check_needs_stdout: bool = False           # also require non-empty stdout to count as authed
    login_cmd: Optional[str] = None                 # command to authenticate, e.g. "vercel login"
    docs_url: str = ""

    def install_command(self) -> Optional[str]:
        """Best install command for the current OS, or None."""
        return self.install.get(_os_key()) or self.install.get("*")

    def install_is_url(self) -> bool:
        """True when the only 'install' guidance is a manual URL, not a runnable command."""
        cmd = self.install_command()
        return bool(cmd) and cmd.strip().lower().startswith("http")

    def is_installed(self) -> bool:
        """True when *binary* is resolvable on PATH right now."""
        return shutil.which(self.binary) is not None


def is_authenticated(req: CliRequirement, cwd: Optional[str] = None) -> Optional[bool]:
    """
    Probe whether the user is authenticated for *req*.

    Returns:
        True  - definitely authenticated
        False - definitely not authenticated
        None  - unknown (no check defined, or the probe itself failed to run)
    """
    if not req.auth_check:
        return None
    try:
        result = subprocess.run(
            req.auth_check,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, cwd=cwd,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return False
    if req.auth_check_needs_stdout and not (result.stdout or "").strip():
        return False
    return True


def docker_daemon_running() -> bool:
    """True when the local Docker daemon answers ``docker info``."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


def _docker_requirement() -> CliRequirement:
    return CliRequirement(
        binary="docker",
        display_name="Docker",
        install={
            "macos": "brew install --cask docker",
            "*": "https://docs.docker.com/get-docker/",
        },
        # Docker auth/daemon are handled specially by the deploy flow
        # (daemon probe + registry login at push time), so no auth_check here.
        login_cmd="docker login",
        docs_url="https://docs.docker.com/get-docker/",
    )


# Platform name (as produced by deploy_detector) -> required CLI tools, in order.
_PLATFORM_REQUIREMENTS: Dict[str, List[CliRequirement]] = {
    "vercel": [
        CliRequirement(
            binary="vercel", display_name="Vercel CLI",
            install={"*": "npm install -g vercel"},
            auth_check=["vercel", "whoami"],
            login_cmd="vercel login",
            docs_url="https://vercel.com/docs/cli",
        )
    ],
    "netlify": [
        CliRequirement(
            binary="netlify", display_name="Netlify CLI",
            install={"*": "npm install -g netlify-cli"},
            auth_check=["netlify", "status"],
            login_cmd="netlify login",
            docs_url="https://docs.netlify.com/cli/get-started/",
        )
    ],
    "fly.io": [
        CliRequirement(
            binary="fly", display_name="Fly.io CLI (flyctl)",
            install={
                "windows": 'powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"',
                "macos": "brew install flyctl",
                "linux": "curl -L https://fly.io/install.sh | sh",
                "*": "curl -L https://fly.io/install.sh | sh",
            },
            auth_check=["fly", "auth", "whoami"],
            login_cmd="fly auth login",
            docs_url="https://fly.io/docs/flyctl/install/",
        )
    ],
    "railway": [
        CliRequirement(
            binary="railway", display_name="Railway CLI",
            install={"*": "npm install -g @railway/cli"},
            auth_check=["railway", "whoami"],
            login_cmd="railway login",
            docs_url="https://docs.railway.app/develop/cli",
        )
    ],
    # Render deploys by pushing to git; no platform CLI or login is required.
    "render": [],
    "serverless": [
        CliRequirement(
            binary="serverless", display_name="Serverless Framework",
            install={"*": "npm install -g serverless"},
            docs_url="https://www.serverless.com/framework/docs",
        ),
        CliRequirement(
            binary="aws", display_name="AWS CLI",
            install={"macos": "brew install awscli", "*": "pip install awscli"},
            auth_check=["aws", "sts", "get-caller-identity"],
            login_cmd="aws configure",
            docs_url="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
        ),
    ],
    "aws-sam": [
        CliRequirement(
            binary="sam", display_name="AWS SAM CLI",
            install={"macos": "brew install aws-sam-cli", "*": "pip install aws-sam-cli"},
            docs_url="https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html",
        ),
        CliRequirement(
            binary="aws", display_name="AWS CLI",
            install={"macos": "brew install awscli", "*": "pip install awscli"},
            auth_check=["aws", "sts", "get-caller-identity"],
            login_cmd="aws configure",
            docs_url="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
        ),
    ],
    "gcloud": [
        CliRequirement(
            binary="gcloud", display_name="Google Cloud SDK",
            install={
                "macos": "brew install --cask google-cloud-sdk",
                "*": "https://cloud.google.com/sdk/docs/install",
            },
            auth_check=["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            auth_check_needs_stdout=True,
            login_cmd="gcloud auth login",
            docs_url="https://cloud.google.com/sdk/docs/install",
        )
    ],
    "heroku": [
        CliRequirement(
            binary="heroku", display_name="Heroku CLI",
            install={
                "macos": "brew tap heroku/brew && brew install heroku",
                "*": "npm install -g heroku",
            },
            auth_check=["heroku", "auth:whoami"],
            login_cmd="heroku login",
            docs_url="https://devcenter.heroku.com/articles/heroku-cli",
        )
    ],
    "docker": [_docker_requirement()],
    "docker-compose": [_docker_requirement()],
    "npm": [
        CliRequirement(
            binary="npm", display_name="npm",
            install={"*": "https://nodejs.org/en/download"},
            auth_check=["npm", "whoami"],
            login_cmd="npm login",
            docs_url="https://docs.npmjs.com/cli/commands/npm-publish",
        )
    ],
    "pypi": [
        CliRequirement(
            binary="python", display_name="Python",
            install={"*": "https://www.python.org/downloads/"},
            # PyPI auth is a token pasted at the twine password prompt; nothing to pre-check.
            docs_url="https://packaging.python.org/en/latest/tutorials/packaging-projects/",
        )
    ],
    "crates.io": [
        CliRequirement(
            binary="cargo", display_name="Rust / Cargo",
            install={"*": "https://rustup.rs"},
            # `cargo login` stores a token; crates.io has no whoami to probe.
            login_cmd="cargo login",
            docs_url="https://doc.rust-lang.org/cargo/reference/publishing.html",
        )
    ],
}


def get_requirements(platform_name: str) -> List[CliRequirement]:
    """Return the CLI requirements for *platform_name* (empty if unknown)."""
    return list(_PLATFORM_REQUIREMENTS.get(platform_name, []))

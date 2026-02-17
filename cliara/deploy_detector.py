"""
Deploy target detection and plan generation for Cliara.

Scans the current project directory for deployment platform config files
(Vercel, Fly.io, Netlify, etc.) and project type markers (package.json,
pyproject.toml, Cargo.toml, Dockerfile) to produce a DeployPlan — an
ordered list of shell commands that carry out the deployment.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class DeployPlan:
    """A detected (or user-defined) deployment plan."""

    platform: str                       # e.g. "vercel", "docker", "npm"
    steps: List[str] = field(default_factory=list)
    project_name: str = ""
    framework: str = ""
    detected_from: str = ""             # which file triggered detection
    needs_build: bool = False           # whether a separate build step exists

    @property
    def summary_line(self) -> str:
        parts = [self.platform.title()]
        if self.framework:
            parts.append(f"({self.framework})")
        if self.detected_from:
            parts.append(f"— detected from {self.detected_from}")
        return " ".join(parts)


# ------------------------------------------------------------------
# Detection helpers
# ------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    """Read a JSON file, returning {} on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _detect_node_framework(cwd: Path) -> str:
    """Detect the JavaScript / TypeScript framework from package.json."""
    pkg = _read_json(cwd / "package.json")
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "next" in deps:
        return "Next.js"
    if "nuxt" in deps or "nuxt3" in deps:
        return "Nuxt"
    if "gatsby" in deps:
        return "Gatsby"
    if "@angular/core" in deps:
        return "Angular"
    if "svelte" in deps or "@sveltejs/kit" in deps:
        return "SvelteKit"
    if "react" in deps:
        return "React"
    if "vue" in deps:
        return "Vue"
    return "Node.js"


def _node_has_build_script(cwd: Path) -> bool:
    pkg = _read_json(cwd / "package.json")
    return "build" in pkg.get("scripts", {})


def _node_project_name(cwd: Path) -> str:
    pkg = _read_json(cwd / "package.json")
    return pkg.get("name", cwd.name)


def _node_is_private(cwd: Path) -> bool:
    pkg = _read_json(cwd / "package.json")
    return pkg.get("private", True)


def _python_project_name(cwd: Path) -> str:
    toml = cwd / "pyproject.toml"
    if toml.exists():
        try:
            text = toml.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("name"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return cwd.name


# ------------------------------------------------------------------
# Platform-specific detectors (checked first — highest priority)
# ------------------------------------------------------------------

_PLATFORM_DETECTORS: List = []   # populated below


def _register(fn):
    """Decorator that adds a detector function to the registry."""
    _PLATFORM_DETECTORS.append(fn)
    return fn


@_register
def _detect_vercel(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "vercel.json").exists() or (cwd / ".vercel").is_dir():
        build_step = []
        if (cwd / "package.json").exists() and _node_has_build_script(cwd):
            build_step = ["npm run build"]
        return DeployPlan(
            platform="vercel",
            steps=build_step + ["vercel --prod"],
            project_name=_node_project_name(cwd) if (cwd / "package.json").exists() else cwd.name,
            framework=_detect_node_framework(cwd) if (cwd / "package.json").exists() else "",
            detected_from="vercel.json" if (cwd / "vercel.json").exists() else ".vercel/",
            needs_build=bool(build_step),
        )
    return None


@_register
def _detect_netlify(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "netlify.toml").exists() or (cwd / ".netlify").is_dir():
        build_step = []
        if (cwd / "package.json").exists() and _node_has_build_script(cwd):
            build_step = ["npm run build"]
        return DeployPlan(
            platform="netlify",
            steps=build_step + ["netlify deploy --prod"],
            project_name=_node_project_name(cwd) if (cwd / "package.json").exists() else cwd.name,
            framework=_detect_node_framework(cwd) if (cwd / "package.json").exists() else "",
            detected_from="netlify.toml" if (cwd / "netlify.toml").exists() else ".netlify/",
            needs_build=bool(build_step),
        )
    return None


@_register
def _detect_fly(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "fly.toml").exists():
        return DeployPlan(
            platform="fly.io",
            steps=["fly deploy"],
            project_name=cwd.name,
            detected_from="fly.toml",
        )
    return None


@_register
def _detect_railway(cwd: Path) -> Optional[DeployPlan]:
    for name in ("railway.json", "railway.toml"):
        if (cwd / name).exists():
            return DeployPlan(
                platform="railway",
                steps=["railway up"],
                project_name=cwd.name,
                detected_from=name,
            )
    return None


@_register
def _detect_render(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "render.yaml").exists():
        return DeployPlan(
            platform="render",
            steps=["git push"],
            project_name=cwd.name,
            detected_from="render.yaml",
        )
    return None


@_register
def _detect_serverless(cwd: Path) -> Optional[DeployPlan]:
    for name in ("serverless.yml", "serverless.yaml", "serverless.ts"):
        if (cwd / name).exists():
            return DeployPlan(
                platform="serverless",
                steps=["serverless deploy"],
                project_name=cwd.name,
                detected_from=name,
            )
    return None


@_register
def _detect_sam(cwd: Path) -> Optional[DeployPlan]:
    tmpl = cwd / "template.yaml"
    if tmpl.exists():
        try:
            text = tmpl.read_text(encoding="utf-8")
            if "AWS::Serverless" in text or "Transform" in text:
                return DeployPlan(
                    platform="aws-sam",
                    steps=["sam build", "sam deploy"],
                    project_name=cwd.name,
                    detected_from="template.yaml",
                    needs_build=True,
                )
        except Exception:
            pass
    return None


@_register
def _detect_gcloud(cwd: Path) -> Optional[DeployPlan]:
    app_yaml = cwd / "app.yaml"
    if app_yaml.exists():
        try:
            text = app_yaml.read_text(encoding="utf-8")
            if "runtime:" in text:
                return DeployPlan(
                    platform="gcloud",
                    steps=["gcloud app deploy"],
                    project_name=cwd.name,
                    detected_from="app.yaml",
                )
        except Exception:
            pass
    return None


@_register
def _detect_heroku(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "Procfile").exists():
        return DeployPlan(
            platform="heroku",
            steps=["git push heroku main"],
            project_name=cwd.name,
            detected_from="Procfile",
        )
    return None


# ------------------------------------------------------------------
# Generic project-type detectors (checked after platforms)
# ------------------------------------------------------------------

_PROJECT_DETECTORS: List = []


def _register_project(fn):
    _PROJECT_DETECTORS.append(fn)
    return fn


@_register_project
def _detect_docker_compose(cwd: Path) -> Optional[DeployPlan]:
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (cwd / name).exists():
            prod_name = name.replace("compose", "compose.prod")
            if (cwd / prod_name).exists():
                return DeployPlan(
                    platform="docker-compose",
                    steps=[f"docker compose -f {prod_name} up -d --build"],
                    project_name=cwd.name,
                    detected_from=prod_name,
                    needs_build=True,
                )
            return DeployPlan(
                platform="docker-compose",
                steps=[f"docker compose -f {name} up -d --build"],
                project_name=cwd.name,
                detected_from=name,
                needs_build=True,
            )
    return None


@_register_project
def _detect_dockerfile(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "Dockerfile").exists():
        tag = cwd.name.lower().replace(" ", "-")
        return DeployPlan(
            platform="docker",
            steps=[
                f"docker build -t {tag} .",
                f"docker push {tag}",
            ],
            project_name=cwd.name,
            detected_from="Dockerfile",
            needs_build=True,
        )
    return None


@_register_project
def _detect_npm_publish(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "package.json").exists() and not _node_is_private(cwd):
        build_step = ["npm run build"] if _node_has_build_script(cwd) else []
        return DeployPlan(
            platform="npm",
            steps=build_step + ["npm publish"],
            project_name=_node_project_name(cwd),
            framework=_detect_node_framework(cwd),
            detected_from="package.json (private: false)",
            needs_build=bool(build_step),
        )
    return None


@_register_project
def _detect_python_publish(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "pyproject.toml").exists() or (cwd / "setup.py").exists():
        marker = "pyproject.toml" if (cwd / "pyproject.toml").exists() else "setup.py"
        return DeployPlan(
            platform="pypi",
            steps=["python -m build", "twine upload dist/*"],
            project_name=_python_project_name(cwd),
            detected_from=marker,
            needs_build=True,
        )
    return None


@_register_project
def _detect_cargo_publish(cwd: Path) -> Optional[DeployPlan]:
    if (cwd / "Cargo.toml").exists():
        return DeployPlan(
            platform="crates.io",
            steps=["cargo publish"],
            project_name=cwd.name,
            detected_from="Cargo.toml",
        )
    return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def detect_all(cwd: Optional[Path] = None) -> List[DeployPlan]:
    """
    Return *all* matching deploy plans for the directory, ordered by
    priority (platform-specific first, then generic project types).
    """
    cwd = cwd or Path.cwd()
    plans: List[DeployPlan] = []

    for detector in _PLATFORM_DETECTORS:
        plan = detector(cwd)
        if plan is not None:
            plans.append(plan)

    for detector in _PROJECT_DETECTORS:
        plan = detector(cwd)
        if plan is not None:
            plans.append(plan)

    return plans


def detect(cwd: Optional[Path] = None) -> Optional[DeployPlan]:
    """
    Return the single best deploy plan, or None if nothing was detected.
    Prefers platform-specific matches over generic project types.
    """
    plans = detect_all(cwd)
    return plans[0] if plans else None

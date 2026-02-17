"""
Persistent per-project deploy configuration for Cliara.

Stores saved deploy plans in ``~/.cliara/deploys.json`` so that the
second time a user types ``deploy`` in the same project the previously
confirmed plan is reused without re-detection.
"""

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict


@dataclass
class SavedDeploy:
    """A previously confirmed deploy configuration for one project."""

    platform: str
    steps: List[str]
    project_name: str = ""
    framework: str = ""
    last_deployed: str = ""           # ISO timestamp
    deploy_count: int = 0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "steps": self.steps,
            "project_name": self.project_name,
            "framework": self.framework,
            "last_deployed": self.last_deployed,
            "deploy_count": self.deploy_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SavedDeploy":
        return cls(
            platform=data.get("platform", "unknown"),
            steps=data.get("steps", []),
            project_name=data.get("project_name", ""),
            framework=data.get("framework", ""),
            last_deployed=data.get("last_deployed", ""),
            deploy_count=data.get("deploy_count", 0),
        )


class DeployStore:
    """
    Read/write ``~/.cliara/deploys.json``.

    The file maps absolute directory paths to ``SavedDeploy`` dicts.
    """

    def __init__(self, store_path: Optional[Path] = None):
        self._path = store_path or (Path.home() / ".cliara" / "deploys.json")
        self._data: Dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, project_dir: Path) -> Optional[SavedDeploy]:
        """Return the saved deploy config for *project_dir*, or None."""
        key = str(project_dir.resolve())
        entry = self._data.get(key)
        if entry is None:
            return None
        return SavedDeploy.from_dict(entry)

    def save(
        self,
        project_dir: Path,
        platform: str,
        steps: List[str],
        project_name: str = "",
        framework: str = "",
    ):
        """Save or update deploy config for a project directory."""
        key = str(project_dir.resolve())
        existing = self._data.get(key, {})
        entry = SavedDeploy(
            platform=platform,
            steps=steps,
            project_name=project_name,
            framework=framework,
            last_deployed=existing.get("last_deployed", ""),
            deploy_count=existing.get("deploy_count", 0),
        )
        self._data[key] = entry.to_dict()
        self._save()

    def record_deploy(self, project_dir: Path):
        """Bump deploy count and update the last-deployed timestamp."""
        key = str(project_dir.resolve())
        entry = self._data.get(key)
        if entry is None:
            return
        entry["deploy_count"] = entry.get("deploy_count", 0) + 1
        entry["last_deployed"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def remove(self, project_dir: Path):
        """Delete saved config for a project."""
        key = str(project_dir.resolve())
        if key in self._data:
            del self._data[key]
            self._save()

    def list_all(self) -> Dict[str, SavedDeploy]:
        """Return all saved configs keyed by directory path."""
        return {k: SavedDeploy.from_dict(v) for k, v in self._data.items()}

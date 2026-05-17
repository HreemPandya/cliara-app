"""Causal command graph (A1).

Best-effort, cross-platform causal DAG for commands run through Cliara.

We can't rely on kernel audit (eBPF/auditd) everywhere, so this module builds a
useful graph via:
- git porcelain diff (files touched)
- optional psutil sampling (process tree + listening ports)
- simple heuristics to infer causal edges: if command B touches a file last
  touched by A, we add an edge A -> B labelled with that file.

The DAG is recorded silently in the background and only shown on demand via
`cliara graph`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from cliara.file_lock import with_file_lock


# -----------------------------
# Data model
# -----------------------------


@dataclass
class GraphEdge:
    src: str
    dst: str
    kind: str  # currently: "file"
    detail: str  # e.g. path


@dataclass
class GraphNode:
    id: str
    command: str
    cwd: str
    started_ts: float
    ended_ts: float
    exit_code: int
    # Best-effort
    touched_files: List[str] = field(default_factory=list)
    spawned_pids: List[int] = field(default_factory=list)
    listening_ports: List[int] = field(default_factory=list)
    env_vars_changed: List[str] = field(default_factory=list)


@dataclass
class CausalGraph:
    version: int
    project_root: str
    created_ts: float
    updated_ts: float
    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)

    # Internal indexing (persisted to keep edge inference stable)
    last_writer_by_file: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "project_root": self.project_root,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "nodes": [n.__dict__ for n in self.nodes],
            "edges": [e.__dict__ for e in self.edges],
            "last_writer_by_file": dict(self.last_writer_by_file),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CausalGraph":
        g = cls(
            version=int(data.get("version", 1)),
            project_root=str(data.get("project_root", "")),
            created_ts=float(data.get("created_ts", time.time())),
            updated_ts=float(data.get("updated_ts", time.time())),
            nodes=[],
            edges=[],
            last_writer_by_file=dict(data.get("last_writer_by_file", {}) or {}),
        )
        for n in data.get("nodes", []) or []:
            if not isinstance(n, dict):
                continue
            g.nodes.append(
                GraphNode(
                    id=str(n.get("id", "")) or str(uuid.uuid4()),
                    command=str(n.get("command", "")),
                    cwd=str(n.get("cwd", "")),
                    started_ts=float(n.get("started_ts", 0.0) or 0.0),
                    ended_ts=float(n.get("ended_ts", 0.0) or 0.0),
                    exit_code=int(n.get("exit_code", 0) or 0),
                    touched_files=[str(x) for x in (n.get("touched_files") or [])],
                    spawned_pids=[int(x) for x in (n.get("spawned_pids") or []) if str(x).isdigit()],
                    listening_ports=[int(x) for x in (n.get("listening_ports") or []) if str(x).isdigit()],
                    env_vars_changed=[str(x) for x in (n.get("env_vars_changed") or [])],
                )
            )
        for e in data.get("edges", []) or []:
            if not isinstance(e, dict):
                continue
            g.edges.append(
                GraphEdge(
                    src=str(e.get("src", "")),
                    dst=str(e.get("dst", "")),
                    kind=str(e.get("kind", "")),
                    detail=str(e.get("detail", "")),
                )
            )
        return g


# -----------------------------
# Storage
# -----------------------------


def _project_key(project_root: str) -> str:
    root = (project_root or "").strip() or "cwd"
    h = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()
    return h[:16]


def graph_path(config_dir: Path, project_root: str) -> Path:
    base = Path(config_dir) / "causal_graphs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{_project_key(project_root)}.json"


def load_graph(config_dir: Path, project_root: str) -> CausalGraph:
    path = graph_path(config_dir, project_root)
    if not path.exists():
        now = time.time()
        return CausalGraph(
            version=1,
            project_root=str(project_root),
            created_ts=now,
            updated_ts=now,
            nodes=[],
            edges=[],
            last_writer_by_file={},
        )
    try:
        with with_file_lock(path):
            raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        g = CausalGraph.from_dict(data if isinstance(data, dict) else {})
        if not g.project_root:
            g.project_root = str(project_root)
        return g
    except Exception:
        now = time.time()
        return CausalGraph(
            version=1,
            project_root=str(project_root),
            created_ts=now,
            updated_ts=now,
            nodes=[],
            edges=[],
            last_writer_by_file={},
        )


def save_graph(config_dir: Path, graph: CausalGraph) -> None:
    path = graph_path(config_dir, graph.project_root)
    graph.updated_ts = time.time()
    payload = json.dumps(graph.to_dict(), indent=2, sort_keys=False)
    with with_file_lock(path):
        path.write_text(payload, encoding="utf-8")


# -----------------------------
# Capture helpers
# -----------------------------


def _git_status_porcelain_z(repo_root: Path) -> List[Tuple[str, str]]:
    """Return list of (xy, path) records from `git status --porcelain -z`.

    xy is the two-character status. Path is repo-relative.
    Best-effort; returns [] on failure.
    """
    try:
        r = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "-z",
            ],
            cwd=repo_root,
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
        out = r.stdout
        if not out:
            return []
        parts = out.split(b"\x00")
        records: List[Tuple[str, str]] = []
        i = 0
        while i < len(parts):
            item = parts[i]
            i += 1
            if not item:
                continue
            # Format: b"XY path" (space after XY)
            if len(item) < 4:
                continue
            xy = item[:2].decode("utf-8", errors="replace")
            # Skip the space at index 2
            path1 = item[3:].decode("utf-8", errors="replace")
            if not path1:
                continue
            # Renames/copies have a second NUL path
            if xy and xy[0] in ("R", "C"):
                if i < len(parts) and parts[i]:
                    path2 = parts[i].decode("utf-8", errors="replace")
                    i += 1
                    # Record both ends so we can show the causal chain.
                    records.append((xy, path1))
                    records.append((xy, path2))
                    continue
            records.append((xy, path1))
        return records
    except Exception:
        return []


def git_status_map(repo_root: Optional[str]) -> Dict[str, str]:
    """Return a map of path -> XY status for the repo root."""
    if not repo_root:
        return {}
    root = Path(repo_root)
    recs = _git_status_porcelain_z(root)
    out: Dict[str, str] = {}
    for xy, p in recs:
        out[p] = xy
    return out


def touched_files_from_status(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Heuristic: return files whose git status record changed across the command."""
    touched: Set[str] = set()
    all_paths = set(before.keys()) | set(after.keys())
    for p in all_paths:
        b = before.get(p)
        a = after.get(p)
        if b != a:
            touched.add(p)
    return sorted(touched)


def env_vars_changed(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Return env var names whose values changed.

    Values are NOT stored (to avoid persisting secrets).
    """
    changed: Set[str] = set()
    keys = set(before.keys()) | set(after.keys())
    for k in keys:
        if before.get(k) != after.get(k):
            changed.add(k)
    return sorted(changed)


# -----------------------------
# Graph recording
# -----------------------------


def append_node(
    *,
    config_dir: Path,
    project_root: str,
    node: GraphNode,
    max_nodes: int = 500,
) -> None:
    """Append a node and infer causal edges, then persist."""
    graph = load_graph(config_dir, project_root)

    # Infer edges based on last writer per file.
    for p in node.touched_files:
        prev = graph.last_writer_by_file.get(p)
        if prev and prev != node.id:
            graph.edges.append(GraphEdge(src=prev, dst=node.id, kind="file", detail=p))
        graph.last_writer_by_file[p] = node.id

    graph.nodes.append(node)

    # Keep size bounded.
    if len(graph.nodes) > max_nodes:
        drop = len(graph.nodes) - max_nodes
        dropped_ids = {n.id for n in graph.nodes[:drop]}
        graph.nodes = graph.nodes[drop:]
        graph.edges = [e for e in graph.edges if e.src not in dropped_ids and e.dst not in dropped_ids]
        # Clean last_writer mapping if it points to dropped nodes.
        graph.last_writer_by_file = {
            k: v for k, v in graph.last_writer_by_file.items() if v not in dropped_ids
        }

    save_graph(config_dir, graph)


# -----------------------------
# Optional psutil sampling
# -----------------------------


def sample_process_tree_and_ports(pid: int, stop_event, sample_interval_s: float = 0.5) -> Tuple[Set[int], Set[int]]:
    """Best-effort sampler: returns (pids_seen, listening_ports).

    Requires psutil; if unavailable, returns ({pid}, set()).
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return {int(pid)}, set()

    pids: Set[int] = {int(pid)}
    ports: Set[int] = set()

    try:
        root = psutil.Process(int(pid))
    except Exception:
        return pids, ports

    while not stop_event.is_set():
        try:
            # Process tree
            kids = root.children(recursive=True)
            for k in kids:
                try:
                    pids.add(int(k.pid))
                except Exception:
                    pass
        except Exception:
            pass

        # Ports: listening only (best-effort, may require privileges)
        try:
            for p in list(pids):
                try:
                    proc = psutil.Process(int(p))
                except Exception:
                    continue
                try:
                    conns = proc.net_connections(kind="inet")
                except Exception:
                    continue
                for c in conns:
                    try:
                        if getattr(c, "status", "") != psutil.CONN_LISTEN:
                            continue
                        laddr = getattr(c, "laddr", None)
                        if not laddr:
                            continue
                        port = int(getattr(laddr, "port", 0) or 0)
                        if port:
                            ports.add(port)
                    except Exception:
                        continue
        except Exception:
            pass

        try:
            time.sleep(max(0.05, float(sample_interval_s)))
        except Exception:
            time.sleep(0.5)

    return pids, ports

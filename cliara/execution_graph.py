"""
Execution graph for Cliara task sessions.

Builds a tree from flat CommandEntry lists (using parent_id), renders as ASCII,
and exports to JSON or text.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from cliara.session_store import CommandEntry


ROOT_LABEL = "Start"
MAX_CMD_DISPLAY = 55


@dataclass
class TreeNode:
    """A node in the execution tree (virtual root or a command)."""

    label: str
    entry: Optional["CommandEntry"] = None  # None for root
    children: List["TreeNode"] = field(default_factory=list)


def build_execution_tree(commands: List["CommandEntry"]) -> TreeNode:
    """Build a tree from a flat list of CommandEntry using parent_id.
    Commands with parent_id None (or missing) are children of Start.
    Preserves chronological order of siblings. Orphan parent_id -> attach to Start."""
    root = TreeNode(label=ROOT_LABEL, entry=None)
    if not commands:
        return root

    # Map id -> node for every command; ensure every entry has a stable id
    id_to_node: dict = {}
    for i, c in enumerate(commands):
        nid = (c.id or "").strip() or f"_{i}"
        if not (c.id or "").strip():
            # Legacy entry: use synthetic id for lookup only; we don't store it back
            nid = f"_{i}"
        status = " (failed)" if c.exit_code != 0 else " (pass)"
        short = c.command[:MAX_CMD_DISPLAY] + "..." if len(c.command) > MAX_CMD_DISPLAY else c.command
        label = short + status
        node = TreeNode(label=label, entry=c)
        id_to_node[nid] = node
        # If the entry has a real id, also key by it so parent_id lookups work
        if (c.id or "").strip():
            id_to_node[c.id] = node

    # Attach each command to its parent in list order (so sibling order is preserved)
    for c in commands:
        nid = (c.id or "").strip() or f"_{commands.index(c)}"
        node = id_to_node.get(nid)
        if node is None:
            continue
        parent_id = (c.parent_id or "").strip() or None
        if not parent_id or parent_id not in id_to_node:
            root.children.append(node)
        else:
            id_to_node[parent_id].children.append(node)

    return root


def render_execution_tree(node: TreeNode, prefix: str = "", is_last: bool = True) -> str:
    """Render the tree as ASCII with ├ and L. Returns full string."""
    if node.entry is None:
        # Root node: just the label, no connector
        line = node.label
        child_prefix = ""
    else:
        # ASCII tree so it works in all terminals (├/└/│ would need UTF-8)
        connector = r"\- " if is_last else "+- "
        vert = "|"
        line = prefix + connector + node.label
        child_prefix = prefix + ("   " if is_last else vert + "  ")

    parts = [line]
    for i, child in enumerate(node.children):
        is_last_child = i == len(node.children) - 1
        parts.append(render_execution_tree(child, child_prefix, is_last_child))
    return "\n".join(parts)


def export_tree_json(commands: List["CommandEntry"], path: Path) -> None:
    """Write a JSON array of nodes (id, command, exit_code, parent_id, timestamp) to path."""
    nodes = []
    for c in commands:
        nodes.append({
            "id": c.id or "",
            "command": c.command,
            "exit_code": c.exit_code,
            "parent_id": c.parent_id,
            "timestamp": c.timestamp,
        })
    path.write_text(json.dumps(nodes, indent=2), encoding="utf-8")

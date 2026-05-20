"""Prompt-toolkit TUI for the causal command graph."""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.window import Window
from prompt_toolkit.styles import Style

from cliara.causal_graph import CausalGraph, GraphNode

def _fmt_ts(ts: float) -> str:
    try:
        dt = _dt.datetime.fromtimestamp(float(ts))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"

def _fmt_duration_s(node: GraphNode) -> str:
    try:
        d = float(node.ended_ts) - float(node.started_ts)
        if d < 0:
            d = 0.0
        if d < 60:
            return f"{d:.1f}s"
        m = int(d // 60)
        s = int(d % 60)
        return f"{m}m{s:02d}s"
    except Exception:
        return "?"


def _node_line(i: int, node: GraphNode, selected: bool) -> FormattedText:
    status = "ok" if node.exit_code == 0 else "fail"
    cmd = (node.command or "").strip()
    if len(cmd) > 70:
        cmd = cmd[:67] + "..."
    prefix = "> " if selected else "  "
    return FormattedText(
        [
            ("class:list-prefix", prefix),
            ("class:list-index", f"{i+1:>3} "),
            ("class:list-status-" + status, ("✓ " if status == "ok" else "✗ ")),
            ("class:list-cmd", cmd),
        ]
    )


def _detail_text(graph: CausalGraph, node: Optional[GraphNode]) -> FormattedText:
    if node is None:
        return FormattedText([( "class:detail", "No commands recorded yet." )])

    incoming = [e for e in graph.edges if e.dst == node.id]
    outgoing = [e for e in graph.edges if e.src == node.id]

    lines = []
    lines.append(("class:detail-title", "Command"))
    lines.append(("", "\n"))
    lines.append(("class:detail", f"{node.command}\n\n"))

    lines.append(("class:detail-title", "Meta"))
    lines.append(("", "\n"))
    lines.append(("class:detail", f"cwd: {node.cwd}\n"))
    lines.append(("class:detail", f"started: {_fmt_ts(node.started_ts)}\n"))
    lines.append(("class:detail", f"duration: {_fmt_duration_s(node)}\n"))
    lines.append(("class:detail", f"exit: {node.exit_code}\n\n"))

    if node.touched_files:
        lines.append(("class:detail-title", f"Files touched ({len(node.touched_files)})"))
        lines.append(("", "\n"))
        for p in node.touched_files[:200]:
            lines.append(("class:detail", f"- {p}\n"))
        if len(node.touched_files) > 200:
            lines.append(("class:detail-dim", f"… {len(node.touched_files)-200} more\n"))
        lines.append(("", "\n"))

    if node.spawned_pids:
        lines.append(("class:detail-title", f"Processes ({len(node.spawned_pids)})"))
        lines.append(("", "\n"))
        lines.append(("class:detail", ", ".join(str(x) for x in node.spawned_pids[:80]) + "\n\n"))

    if node.listening_ports:
        lines.append(("class:detail-title", f"Listening ports ({len(node.listening_ports)})"))
        lines.append(("", "\n"))
        lines.append(("class:detail", ", ".join(str(x) for x in node.listening_ports[:80]) + "\n\n"))

    if node.env_vars_changed:
        lines.append(("class:detail-title", f"Env vars changed ({len(node.env_vars_changed)})"))
        lines.append(("", "\n"))
        for k in node.env_vars_changed[:200]:
            lines.append(("class:detail", f"- {k}\n"))
        if len(node.env_vars_changed) > 200:
            lines.append(("class:detail-dim", f"… {len(node.env_vars_changed)-200} more\n"))
        lines.append(("", "\n"))

    if incoming:
        lines.append(("class:detail-title", f"Incoming edges ({len(incoming)})"))
        lines.append(("", "\n"))
        for e in incoming[:200]:
            lines.append(("class:detail", f"<- {e.kind}: {e.detail}\n"))
        if len(incoming) > 200:
            lines.append(("class:detail-dim", f"… {len(incoming)-200} more\n"))
        lines.append(("", "\n"))

    if outgoing:
        lines.append(("class:detail-title", f"Outgoing edges ({len(outgoing)})"))
        lines.append(("", "\n"))
        for e in outgoing[:200]:
            lines.append(("class:detail", f"-> {e.kind}: {e.detail}\n"))
        if len(outgoing) > 200:
            lines.append(("class:detail-dim", f"… {len(outgoing)-200} more\n"))

    return FormattedText(lines)


def run_graph_tui(graph: CausalGraph) -> None:
    nodes = list(graph.nodes)
    selected_idx = 0 if nodes else -1

    def _get_selected() -> Optional[GraphNode]:
        nonlocal selected_idx
        if selected_idx < 0 or selected_idx >= len(nodes):
            return None
        return nodes[selected_idx]

    def _list_fragments() -> FormattedText:
        frags = []
        if not nodes:
            frags.append(("class:list-dim", "  (no commands yet)"))
            return FormattedText(frags)
        for i, n in enumerate(nodes):
            frags.extend(_node_line(i, n, i == selected_idx))
            frags.append(("", "\n"))
        return FormattedText(frags)

    list_control = FormattedTextControl(text=_list_fragments, focusable=True)
    detail_control = FormattedTextControl(text=lambda: _detail_text(graph, _get_selected()))

    list_window = Window(
        content=list_control,
        width=Dimension(weight=2),
        right_margins=[ScrollbarMargin(display_arrows=True)],
        dont_extend_height=True,
    )
    detail_window = Window(
        content=detail_control,
        width=Dimension(weight=5),
        right_margins=[ScrollbarMargin(display_arrows=True)],
        dont_extend_height=True,
    )

    kb = KeyBindings()

    @kb.add("q")
    @kb.add("escape")
    def _quit(event):
        event.app.exit()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        nonlocal selected_idx
        if not nodes:
            return
        selected_idx = max(0, selected_idx - 1)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        nonlocal selected_idx
        if not nodes:
            return
        selected_idx = min(len(nodes) - 1, selected_idx + 1)

    title = f"Cliara Graph — {graph.project_root}"
    header = Window(
        content=FormattedTextControl(
            text=FormattedText(
                [
                    ("class:header", title),
                    ("class:header-dim", "    (q/esc to quit, j/k or arrows to navigate)"),
                ]
            )
        ),
        height=1,
    )

    root = HSplit([
        header,
        Window(height=1, char="─", style="class:rule"),
        VSplit([list_window, Window(width=1, char="│", style="class:rule"), detail_window]),
    ])

    style = Style.from_dict(
        {
            "header": "bold",
            "header-dim": "ansibrightblack",
            "rule": "ansibrightblack",
            "list-prefix": "ansibrightblack",
            "list-index": "ansibrightblack",
            "list-cmd": "",
            "list-dim": "ansibrightblack",
            "list-status-ok": "ansigreen",
            "list-status-fail": "ansired",
            "detail": "",
            "detail-dim": "ansibrightblack",
            "detail-title": "bold",
        }
    )

    app = Application(layout=Layout(root, focused_element=list_window), key_bindings=kb, full_screen=True, style=style)
    app.run()

"""IDE bridge: silent, bidirectional context exchange over a local Unix socket.

This module intentionally has **no** rich/console UX. It is safe to import from
core runtime code.

Protocol:
  - Transport: newline-delimited JSON (NDJSON) over a local Unix domain socket.
  - Request:  {"id":"...","type":"request","method":"...","params":{...}}
  - Reply:    {"id":"...","type":"response","ok":true|false,"result":{...},"error":"..."}
  - Event:    {"type":"event","event":"...","data":{...}}

Methods:
  - ping
  - ide.setState / ide.getState
  - cliara.getLastRun

State is stored in-process (Cliara writes last-run; IDE writes active file).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set


_PROTOCOL_VERSION = 1


def supports_unix_sockets() -> bool:
    """Return True if this Python build supports AF_UNIX sockets."""
    try:
        import socket

        return hasattr(socket, "AF_UNIX")
    except Exception:
        return False


def _default_socket_path() -> Path:
    """Choose a stable per-user socket path.

    On Windows, AF_UNIX path length can be restrictive; prefer a temp directory.
    """
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "cliara-ide-bridge.sock"
    return Path.home() / ".cliara" / "ide-bridge.sock"


def _sockpath_record_file(config_dir: Path) -> Path:
    return config_dir / "ide_bridge.sockpath"


@dataclass
class IdeState:
    active_file: Optional[str] = None
    workspace_root: Optional[str] = None
    editor: Optional[str] = None  # vscode|cursor|zed|unknown
    updated_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_file": self.active_file,
            "workspace_root": self.workspace_root,
            "editor": self.editor,
            "updated_ts": self.updated_ts,
        }


@dataclass
class LastRunBlock:
    command: str
    cwd: str
    shell: str
    os_name: str
    exit_code: int
    started_ts: float
    elapsed_s: float
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "shell": self.shell,
            "os": self.os_name,
            "exit_code": self.exit_code,
            "started_ts": self.started_ts,
            "elapsed_s": self.elapsed_s,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class IdeBridge:
    """In-process state + background Unix-socket server."""

    def __init__(self, *, config_dir: Path, socket_path: Optional[Path] = None, enabled: bool = True):
        self._config_dir = Path(config_dir)
        self._enabled = bool(enabled)
        self._socket_path = Path(socket_path) if socket_path else _default_socket_path()

        self._lock = threading.RLock()
        self._ide_state = IdeState()
        self._last_run: Optional[LastRunBlock] = None

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._started = False

    # ------------------------------------------------------------------
    # Public state API (used by Cliara core)
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def get_ide_state(self) -> IdeState:
        with self._lock:
            return IdeState(**self._ide_state.to_dict())

    def set_ide_state(self, *, active_file: Optional[str], workspace_root: Optional[str] = None, editor: Optional[str] = None) -> None:
        with self._lock:
            self._ide_state.active_file = active_file
            self._ide_state.workspace_root = workspace_root
            self._ide_state.editor = editor
            self._ide_state.updated_ts = time.time()

    def get_last_run(self) -> Optional[LastRunBlock]:
        with self._lock:
            return self._last_run

    def set_last_run(self, block: LastRunBlock) -> None:
        with self._lock:
            self._last_run = block

        # Broadcast event to any connected IDE clients (best-effort, silent).
        self._broadcast_event("cliara.lastRun", block.to_dict())

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            return
        if not supports_unix_sockets():
            # Spec asks for a Unix socket; if unsupported, remain silent.
            return
        if self._started:
            return

        self._config_dir.mkdir(parents=True, exist_ok=True)

        # Record the chosen sockpath for IDE clients (VS Code extension reads this).
        try:
            p = _sockpath_record_file(self._config_dir)
            p.write_text(str(self._socket_path), encoding="utf-8")
        except Exception:
            pass

        self._thread = threading.Thread(target=self._run_server_thread, name="cliara-ide-bridge", daemon=True)
        self._thread.start()
        self._started = True

    def _run_server_thread(self) -> None:
        try:
            asyncio.run(self._run_server())
        except Exception:
            # Silent by design.
            return

    async def _run_server(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Ensure socket dir exists.
        try:
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Remove stale socket file if present.
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except Exception:
            pass

        try:
            self._server = await asyncio.start_unix_server(self._handle_client, path=str(self._socket_path))
        except Exception:
            # Binding failed; remain silent.
            return

        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            try:
                if self._socket_path.exists():
                    self._socket_path.unlink()
            except Exception:
                pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="replace").strip()
                if not msg:
                    continue
                resp = self._handle_message_text(msg)
                if resp is None:
                    continue
                try:
                    writer.write((json.dumps(resp, separators=(",", ":")) + "\n").encode("utf-8"))
                    await writer.drain()
                except Exception:
                    break
        finally:
            try:
                self._clients.discard(writer)
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _handle_message_text(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(text)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        msg_type = str(data.get("type") or "").strip().lower()
        msg_id = data.get("id")
        method = str(data.get("method") or "").strip()
        params = data.get("params")

        if msg_type != "request" or not method:
            return None

        ok = True
        result: Any = None
        error: Optional[str] = None

        try:
            result = self._dispatch_method(method, params)
        except Exception as e:
            ok = False
            error = str(e) or "error"

        resp: Dict[str, Any] = {
            "id": msg_id,
            "type": "response",
            "ok": ok,
            "version": _PROTOCOL_VERSION,
        }
        if ok:
            resp["result"] = result
        else:
            resp["error"] = error
        return resp

    def _dispatch_method(self, method: str, params: Any) -> Any:
        if method == "ping":
            return {"ok": True, "version": _PROTOCOL_VERSION}

        if method == "ide.getState":
            return self.get_ide_state().to_dict()

        if method == "ide.setState":
            if not isinstance(params, dict):
                params = {}
            active_file = params.get("active_file")
            workspace_root = params.get("workspace_root")
            editor = params.get("editor")
            self.set_ide_state(
                active_file=str(active_file) if active_file else None,
                workspace_root=str(workspace_root) if workspace_root else None,
                editor=str(editor) if editor else None,
            )
            return {"ok": True}

        if method == "cliara.getLastRun":
            block = self.get_last_run()
            return block.to_dict() if block else None

        raise ValueError(f"Unknown method: {method}")

    def _broadcast_event(self, event: str, data: Dict[str, Any]) -> None:
        # Called from non-async context; schedule onto loop.
        if not self._loop:
            return
        try:
            payload = {"type": "event", "event": event, "data": data, "version": _PROTOCOL_VERSION}
            line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

            async def _send_all() -> None:
                dead: Set[asyncio.StreamWriter] = set()
                for w in list(self._clients):
                    try:
                        w.write(line)
                        await w.drain()
                    except Exception:
                        dead.add(w)
                for w in dead:
                    try:
                        self._clients.discard(w)
                    except Exception:
                        pass

            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(_send_all()))
        except Exception:
            return


_GLOBAL: Optional[IdeBridge] = None
_GLOBAL_LOCK = threading.Lock()


def get_bridge(*, config_dir: Path, enabled: bool = True, socket_path: Optional[Path] = None) -> IdeBridge:
    """Return a process-global IdeBridge instance."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = IdeBridge(config_dir=config_dir, enabled=enabled, socket_path=socket_path)
        return _GLOBAL


def peek_bridge() -> Optional[IdeBridge]:
    """Return the global bridge if created, else None (no side effects)."""
    with _GLOBAL_LOCK:
        return _GLOBAL

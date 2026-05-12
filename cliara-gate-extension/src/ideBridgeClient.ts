import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as net from "net";

type JsonValue = any;

interface Pending {
  resolve: (v: JsonValue) => void;
  reject: (e: Error) => void;
  timer?: NodeJS.Timeout;
}

export class IdeBridgeClient {
  private sockPath: string | null = null;
  private socket: net.Socket | null = null;
  private buffer = "";
  private nextId = 1;
  private pending = new Map<string, Pending>();
  private reconnectTimer: NodeJS.Timeout | null = null;

  constructor() {
    this.sockPath = this.resolveSockPath();
    this.connect();
  }

  dispose(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    for (const [, p] of this.pending) {
      try {
        p.reject(new Error("disposed"));
      } catch {
        // ignore
      }
    }
    this.pending.clear();
    if (this.socket) {
      try {
        this.socket.destroy();
      } catch {
        // ignore
      }
      this.socket = null;
    }
  }

  private resolveSockPath(): string {
    const home = os.homedir();
    const recordFile = path.join(home, ".cliara", "ide_bridge.sockpath");
    try {
      const txt = fs.readFileSync(recordFile, "utf-8").trim();
      if (txt) return txt;
    } catch {
      // ignore
    }

    // Fallbacks
    if (process.platform === "win32") {
      return path.join(os.tmpdir(), "cliara-ide-bridge.sock");
    }
    return path.join(home, ".cliara", "ide-bridge.sock");
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.sockPath = this.resolveSockPath();
      this.connect();
    }, 1200);
  }

  private connect(): void {
    if (!this.sockPath) return;
    if (this.socket) return;

    try {
      const s = net.createConnection({ path: this.sockPath });
      this.socket = s;

      s.on("data", (chunk: Buffer) => {
        this.buffer += chunk.toString("utf-8");
        this.drainBuffer();
      });

      s.on("error", () => {
        // silent
      });

      s.on("close", () => {
        this.socket = null;
        this.scheduleReconnect();
      });
    } catch {
      this.socket = null;
      this.scheduleReconnect();
    }
  }

  private drainBuffer(): void {
    while (true) {
      const idx = this.buffer.indexOf("\n");
      if (idx < 0) return;
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      if (msg && msg.type === "response" && msg.id) {
        const p = this.pending.get(String(msg.id));
        if (!p) continue;
        this.pending.delete(String(msg.id));
        if (p.timer) clearTimeout(p.timer);
        if (msg.ok) p.resolve(msg.result);
        else p.reject(new Error(msg.error || "error"));
      }
      // events are currently ignored (silent)
    }
  }

  private sendRaw(obj: any): void {
    this.connect();
    if (!this.socket) throw new Error("not connected");
    this.socket.write(JSON.stringify(obj) + "\n");
  }

  request(method: string, params?: any, timeoutMs = 800): Promise<JsonValue> {
    const id = String(this.nextId++);
    return new Promise((resolve, reject) => {
      const p: Pending = { resolve, reject };
      p.timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("timeout"));
      }, timeoutMs);
      this.pending.set(id, p);
      try {
        this.sendRaw({ id, type: "request", method, params: params ?? {} });
      } catch (e: any) {
        if (p.timer) clearTimeout(p.timer);
        this.pending.delete(id);
        reject(e instanceof Error ? e : new Error(String(e)));
      }
    });
  }

  publishIdeState(activeFile: string | null, workspaceRoot: string | null): void {
    // Fire-and-forget. No throws.
    try {
      this.sendRaw({
        id: String(this.nextId++),
        type: "request",
        method: "ide.setState",
        params: {
          active_file: activeFile,
          workspace_root: workspaceRoot,
          editor: "vscode",
        },
      });
    } catch {
      // ignore
    }
  }
}

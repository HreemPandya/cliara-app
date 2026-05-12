import * as vscode from "vscode";
import { CliaraTerminal } from "./cliaraTerminal";
import { IdeBridgeClient } from "./ideBridgeClient";

export function activate(context: vscode.ExtensionContext) {
  const ideBridge = new IdeBridgeClient();
  context.subscriptions.push({ dispose: () => ideBridge.dispose() });

  const publishActive = () => {
    const editor = vscode.window.activeTextEditor;
    const activeFile = editor?.document?.uri?.fsPath ?? null;
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath ?? null;
    ideBridge.publishIdeState(activeFile, workspaceRoot);
  };

  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(() => publishActive()));
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => publishActive()));
  publishActive();

  const provider: vscode.TerminalProfileProvider = {
    provideTerminalProfile(): vscode.ProviderResult<vscode.TerminalProfile> {
      return new vscode.TerminalProfile({
        name: "Cliara Gate",
        pty: new CliaraTerminal(),
        iconPath: new vscode.ThemeIcon("shield"),
      });
    },
  };

  context.subscriptions.push(
    vscode.window.registerTerminalProfileProvider("cliara-gate", provider),
  );

  const openCmd = vscode.commands.registerCommand("cliaraGate.openTerminal", () => {
    const pty = new CliaraTerminal();
    const terminal = vscode.window.createTerminal({
      name: "Cliara Gate",
      pty,
      iconPath: new vscode.ThemeIcon("shield"),
    });
    terminal.show();
  });
  context.subscriptions.push(openCmd);

  // Silent helper for IDE chat: copy the last Cliara run block to clipboard.
  const copyLastRun = vscode.commands.registerCommand("cliaraGate.copyLastRunBlock", async () => {
    try {
      const block = await ideBridge.request("cliara.getLastRun", {}, 1200);
      const text = block ? JSON.stringify(block, null, 2) : "(no last run available)";
      await vscode.env.clipboard.writeText(text);
    } catch {
      // silent
    }
  });
  context.subscriptions.push(copyLastRun);
}

export function deactivate() {}

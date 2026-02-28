import * as vscode from "vscode";
import { CliaraTerminal } from "./cliaraTerminal";

export function activate(context: vscode.ExtensionContext) {
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
}

export function deactivate() {}

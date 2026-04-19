"""Deploy command mixin for Cliara shell."""

import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

from cliara.deploy_detector import DeployPlan, detect_all as detect_deploy_targets
from cliara.shell_app.runtime import (
    print_dim,
    print_error,
    print_info,
    print_success,
    print_warning,
)


class DeployCommandMixin:
    """Deploy command handlers and helpers mixed into CliaraShell."""

    def handle_deploy(self, subcommand: str = ""):
        """
        Built-in smart deploy: detect the project's deployment target,
        show the plan, confirm, and execute step-by-step.

        Supports subcommands:
            deploy              Run the deploy flow
            deploy config       Show / edit saved deploy config
            deploy history      Show past deploys for this project
            deploy reset        Forget saved config and re-detect
        """
        sub = subcommand.strip().lower()
        if sub == "config":
            self._deploy_show_config()
            return
        if sub == "history":
            self._deploy_show_history()
            return
        if sub == "reset":
            self._deploy_reset()
            return
        if sub == "help":
            self._deploy_help()
            return
        if sub:
            print_error(f"[Cliara] Unknown deploy subcommand: '{sub}'")
            print_dim("  Available: deploy, deploy config, deploy history, deploy reset, deploy help")
            return

        cwd = Path.cwd()

        # "?"? 1. Check for saved config first "?"?
        saved = self.deploy_store.get(cwd)
        if saved is not None:
            self._deploy_from_saved(cwd, saved)
            return

        # "?"? 2. Auto-detect deploy targets "?"?
        plans = detect_deploy_targets(cwd)

        if not plans:
            # Nothing detected  -  fall back to NL
            self._deploy_nl_fallback(cwd)
            return

        if len(plans) == 1:
            plan = plans[0]
        else:
            plan = self._deploy_choose_target(plans)
            if plan is None:
                return

        # "?"? 3. Pre-deploy checks "?"?
        if not self._deploy_pre_checks(cwd, plan):
            return

        # "?"? 4. Show plan and confirm "?"?
        self._deploy_show_plan(plan, cwd)
        action = self._deploy_confirm()
        if action is None:
            return

        if action == "edit":
            steps = self._deploy_edit_steps(plan.steps)
            if steps is None:
                return
            plan.steps = steps

        # "?"? 5. Save config for next time "?"?
        self.deploy_store.save(
            cwd,
            platform=plan.platform,
            steps=plan.steps,
            project_name=plan.project_name,
            framework=plan.framework,
        )

        # "?"? 6. Execute "?"?
        self._deploy_execute(cwd, plan.steps, plan.platform)

    # -- Saved config flow ---------------------------------------------------

    def _deploy_from_saved(self, cwd: Path, saved):
        """Run a previously saved deploy config."""
        # Time-since-last-deploy hint
        age_hint = ""
        if saved.last_deployed:
            try:
                from datetime import datetime, timezone
                last = datetime.fromisoformat(saved.last_deployed)
                delta = datetime.now(timezone.utc) - last
                if delta.days > 0:
                    age_hint = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    age_hint = f"{delta.seconds // 3600}h ago"
                else:
                    age_hint = f"{delta.seconds // 60}m ago"
            except Exception:
                pass

        platform_label = saved.platform.title()
        if saved.framework:
            platform_label += f" ({saved.framework})"

        count_label = f"deployed {saved.deploy_count} time(s)" if saved.deploy_count else "never deployed"
        time_label = f"last: {age_hint}" if age_hint else ""
        meta = ", ".join(filter(None, [count_label, time_label]))

        print_info(f"\n[Cliara] Deploy to {platform_label}  ({meta})")
        print()
        for i, step in enumerate(saved.steps, 1):
            print(f"  {i}. {step}")
        print()

        try:
            response = input(
                "  Continue? (y)es / (e)dit / (r)edetect / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if response in ("r", "redetect"):
            self.deploy_store.remove(cwd)
            print_dim("  Saved config cleared  -  re-detecting...\n")
            self.handle_deploy()
            return

        if response in ("e", "edit"):
            steps = self._deploy_edit_steps(saved.steps)
            if steps is None:
                return
            self.deploy_store.save(
                cwd,
                platform=saved.platform,
                steps=steps,
                project_name=saved.project_name,
                framework=saved.framework,
            )
            self._deploy_execute(cwd, steps, saved.platform)
            return

        if response not in ("y", "yes"):
            print_warning("  [Cancelled]")
            return

        # Pre-deploy checks
        plan = DeployPlan(
            platform=saved.platform,
            steps=saved.steps,
            project_name=saved.project_name,
            framework=saved.framework,
        )
        if not self._deploy_pre_checks(cwd, plan):
            return

        self._deploy_execute(cwd, saved.steps, saved.platform)

    # -- Multiple targets ----------------------------------------------------

    def _deploy_choose_target(self, plans: list) -> "Optional[DeployPlan]":
        """Let the user pick from multiple detected deploy targets."""
        print_info("\n[Cliara] Multiple deploy targets detected:\n")
        for i, plan in enumerate(plans, 1):
            print(f"  {i}. {plan.summary_line}")
        print()

        try:
            choice = input("  Which target? (number, or 'n' to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if choice.lower() in ("n", "no", ""):
            print_warning("  [Cancelled]")
            return None

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(plans):
                return plans[idx]
        except ValueError:
            pass

        print_error("  Invalid choice.")
        return None

    # -- NL fallback ---------------------------------------------------------

    def _deploy_nl_fallback(self, cwd: Path):
        """
        When auto-detection finds nothing, ask the user to describe
        their deploy process in natural language and generate a plan.
        """
        print_warning("\n[Cliara] No deployment platform detected.\n")

        if not self.nl_handler.llm_enabled:
            print_dim(
                "  No deploy config files found (Vercel, Fly.io, Netlify, "
                "Dockerfile, etc.).\n"
                "  Set OPENAI_API_KEY in your .env to describe your deploy "
                "process in plain English.\n"
            )
            return

        print(
            "  Describe how you deploy this project (or press Enter to cancel):"
        )
        try:
            description = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not description:
            return

        print_dim("\n  Generating deploy steps...\n")
        context = {
            "cwd": str(cwd),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }
        # deploy agent returns JSON  -  do not stream raw JSON to the console
        commands = self.nl_handler.generate_deploy_steps(description, context, stream_callback=None)

        if not commands or (len(commands) == 1 and commands[0].startswith("#")):
            print_error("  Could not generate deploy steps.")
            return

        print_info("  Generated steps:")
        for i, cmd in enumerate(commands, 1):
            print(f"    {i}. {cmd}")
        print()

        try:
            response = input(
                "  Run these steps? (y)es / (e)dit / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if response in ("e", "edit"):
            commands = self._deploy_edit_steps(commands)
            if commands is None:
                return

        if response not in ("y", "yes", "e", "edit"):
            print_warning("  [Cancelled]")
            return

        # Offer to save
        try:
            save_resp = input(
                "  Save as default deploy for this project? (y/n): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            save_resp = "n"

        if save_resp in ("y", "yes"):
            self.deploy_store.save(
                cwd,
                platform="custom",
                steps=commands,
                project_name=cwd.name,
            )
            print_dim("  Saved!\n")

        self._deploy_execute(cwd, commands, "custom")

    # -- Pre-deploy checks ---------------------------------------------------

    def _deploy_pre_checks(self, cwd: Path, plan: DeployPlan) -> bool:
        """
        Run sanity checks before deploying.
        Returns True if OK to proceed, False to abort.
        """
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
            cwd=str(cwd),
        )
        if result.returncode == 0 and result.stdout.strip():
            print_warning(
                "\n  [Warning] You have uncommitted changes."
            )
            try:
                resp = input(
                    "  Run 'push' first to commit & push? (y/n): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if resp in ("y", "yes"):
                self.handle_push()
                print()

        # Check branch (warn if not main/master)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch not in ("main", "master"):
                print_warning(
                    f"\n  [Warning] You're on branch '{branch}', not main/master."
                )
                try:
                    resp = input(
                        "  Deploy from this branch? (y/n): "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return False
                if resp not in ("y", "yes"):
                    print_warning("  [Cancelled]")
                    return False

        return True

    # -- Plan display & confirmation -----------------------------------------

    def _deploy_show_plan(self, plan: DeployPlan, cwd: Path):
        """Print the detected deploy plan."""
        print_info(f"\n[Cliara] Deploy detected for this project:\n")
        print(f"  Platform:  {plan.platform.title()}")
        if plan.project_name:
            print(f"  Project:   {plan.project_name}")
        if plan.framework:
            print(f"  Framework: {plan.framework}")
        if plan.detected_from:
            print(f"  Detected:  {plan.detected_from}")
        print()
        print_dim("  Steps:")
        for i, step in enumerate(plan.steps, 1):
            print(f"    {i}. {step}")
        print()

    def _deploy_confirm(self) -> Optional[str]:
        """
        Prompt the user: (y)es / (e)dit / (n)o.
        Returns 'yes', 'edit', or None for cancel.
        """
        try:
            response = input(
                "  Continue? (y)es / (e)dit / (n)o: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if response in ("y", "yes"):
            return "yes"
        if response in ("e", "edit"):
            return "edit"

        print_warning("  [Cancelled]")
        return None

    def _deploy_edit_steps(self, steps: list) -> Optional[list]:
        """Let the user edit the deploy steps interactively."""
        print_dim(
            "\n  Edit steps (one command per line, empty line to finish):"
        )
        new_steps = []
        for i, step in enumerate(steps, 1):
            try:
                edited = input(f"  [{i}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            # If user just presses Enter, keep the original
            if not edited:
                # But if they entered nothing on a *new* slot, stop
                if i > len(steps):
                    break
                new_steps.append(step)
            else:
                new_steps.append(edited)

        # Allow adding extra steps
        extra_idx = len(steps) + 1
        while True:
            try:
                extra = input(f"  [{extra_idx}] (Enter to finish): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not extra:
                break
            new_steps.append(extra)
            extra_idx += 1

        if not new_steps:
            print_warning("  No steps  -  cancelled.")
            return None

        print()
        return new_steps

    # -- Execution -----------------------------------------------------------

    def _deploy_execute(self, cwd: Path, steps: list, platform_name: str):
        """Execute each deploy step sequentially with progress feedback."""
        total = len(steps)
        print()
        all_ok = True

        for i, step in enumerate(steps, 1):
            print_info(f"  [{i}/{total}] {step}")
            success = self.execute_shell_command(step)

            if success:
                print_success(f"  [{i}/{total}] Done")
            else:
                print_error(f"\n  [{i}/{total}] Failed: {step}")
                if i < total:
                    try:
                        resp = input(
                            "\n  Continue with remaining steps? (y/n): "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        all_ok = False
                        break
                    if resp not in ("y", "yes"):
                        all_ok = False
                        break
                else:
                    all_ok = False

        if all_ok:
            self.deploy_store.record_deploy(cwd)
            print_success(
                f"\n[Cliara] Deploy complete! ({platform_name.title()})"
            )
        else:
            print_warning(
                "\n[Cliara] Deploy did not complete successfully."
            )

    # -- Subcommands ---------------------------------------------------------

    def _deploy_show_config(self):
        """Show saved deploy config for the current project."""
        saved = self.deploy_store.get(Path.cwd())
        if saved is None:
            print_info("[Cliara] No saved deploy config for this project.")
            print_dim("  Run 'deploy' to auto-detect and configure.")
            return

        print_info(f"\n[Cliara] Deploy config for {Path.cwd().name}:\n")
        print(f"  Platform:  {saved.platform}")
        if saved.project_name:
            print(f"  Project:   {saved.project_name}")
        if saved.framework:
            print(f"  Framework: {saved.framework}")
        print(f"  Deploys:   {saved.deploy_count}")
        if saved.last_deployed:
            print(f"  Last:      {saved.last_deployed}")
        print()
        print_dim("  Steps:")
        for i, step in enumerate(saved.steps, 1):
            print(f"    {i}. {step}")
        print()

    def _deploy_show_history(self):
        """Show all saved deploy configs across projects."""
        all_configs = self.deploy_store.list_all()
        if not all_configs:
            print_info("[Cliara] No deploy history yet.")
            return

        print_info(f"\n[Cliara] Deploy history ({len(all_configs)} project(s)):\n")
        for path, saved in all_configs.items():
            deploys = f"{saved.deploy_count} deploy(s)" if saved.deploy_count else "never deployed"
            print(f"  {path}")
            print_dim(f"    {saved.platform.title()}  -  {deploys}")
            if saved.last_deployed:
                print_dim(f"    Last: {saved.last_deployed}")
            print()

    def _deploy_reset(self):
        """Forget saved deploy config for the current project."""
        cwd = Path.cwd()
        saved = self.deploy_store.get(cwd)
        if saved is None:
            print_info("[Cliara] No saved deploy config for this project.")
            return

        self.deploy_store.remove(cwd)
        print_success(
            f"[Cliara] Deploy config for '{cwd.name}' cleared. "
            "Next 'deploy' will re-detect."
        )

    def _deploy_help(self):
        """Show deploy subcommand help."""
        print_info("\n[Cliara] Deploy Commands\n")
        print("  deploy               Auto-detect and deploy this project")
        print("  deploy config        Show saved deploy config")
        print("  deploy history       Show deploy history across all projects")
        print("  deploy reset         Forget saved config and re-detect")
        print("  deploy help          Show this help")
        print()
        print_dim("  First run: Cliara detects your project type and proposes a plan.")
        print_dim("  After confirming, the plan is saved  -  next time it's instant.")
        print_dim("  PyPI: upload step uses twine --username __token__; paste your full pypi-... API token at the password prompt.")
        print()

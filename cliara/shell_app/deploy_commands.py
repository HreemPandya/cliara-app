"""Deploy command mixin for Cliara shell."""

import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional

from cliara.deploy_detector import DeployPlan, detect_all as detect_deploy_targets
from cliara import deploy_publish
from cliara.deploy_prereqs import (
    docker_daemon_running,
    get_requirements,
    is_authenticated,
)
from cliara.safety import DangerLevel
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

        # Preflight may rewrite steps (e.g. resolving a Docker registry), so
        # run what the plan holds now, not the original saved list.
        if plan.steps != saved.steps:
            self.deploy_store.save(
                cwd,
                platform=saved.platform,
                steps=plan.steps,
                project_name=saved.project_name,
                framework=saved.framework,
            )
        self._deploy_execute(cwd, plan.steps, saved.platform)

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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
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

        # Prerequisite preflight: tooling installed, authenticated, and
        # (for package publishing) the version isn't already on the registry.
        if not self._deploy_preflight(cwd, plan):
            return False

        return True

    # -- Prerequisite preflight ----------------------------------------------

    def _deploy_yn(self, prompt: str) -> bool:
        """Yes/No prompt that treats Ctrl+C / EOF / anything-but-yes as No."""
        try:
            resp = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return resp in ("y", "yes")

    def _deploy_preflight(self, cwd: Path, plan: DeployPlan) -> bool:
        """
        Make sure the user can actually deploy: resolve any unqualified Docker
        image target, ensure the required CLI(s) are installed and the user is
        logged in, and (for publishing) that the version isn't already taken.

        Returns True to proceed, False to abort.
        """
        if not self._deploy_resolve_docker_target(cwd, plan):
            return False
        if not self._deploy_check_clis(plan):
            return False
        if not self._deploy_check_auth(cwd, plan):
            return False
        if not self._deploy_publish_preflight(cwd, plan):
            return False
        return True

    # -- CLI install checks --------------------------------------------------

    def _deploy_check_clis(self, plan: DeployPlan) -> bool:
        """Ensure each required CLI is installed; offer to install if missing."""
        for req in get_requirements(plan.platform):
            if req.is_installed():
                continue

            print_warning(
                f"\n  [Missing] {req.display_name} ('{req.binary}') isn't installed."
            )
            install_cmd = req.install_command()

            if install_cmd and not req.install_is_url():
                print_dim(f"  Cliara can install it with:  {install_cmd}")
                if self._deploy_yn(f"  Install {req.display_name} now? (y/n): "):
                    self.execute_shell_command(install_cmd)
                    if not req.is_installed():
                        print_warning(
                            f"  '{req.binary}' still isn't on PATH. You may need to "
                            "restart your terminal so the new install is picked up."
                        )
                        if not self._deploy_yn("  Continue anyway? (y/n): "):
                            return False
                elif not self._deploy_yn("  Continue without installing? (y/n): "):
                    return False
            else:
                # Manual install (URL) or no automatic option.
                if install_cmd:
                    print_dim(f"  Install it from: {install_cmd}")
                if req.docs_url and req.docs_url != install_cmd:
                    print_dim(f"  Docs: {req.docs_url}")
                if not self._deploy_yn("  Continue anyway? (y/n): "):
                    return False
        return True

    # -- Authentication checks -----------------------------------------------

    def _deploy_check_auth(self, cwd: Path, plan: DeployPlan) -> bool:
        """Probe auth for each required CLI; offer to log in if needed."""
        # Docker has no whoami; the meaningful pre-check is the daemon.
        if plan.platform in ("docker", "docker-compose"):
            if not docker_daemon_running():
                print_warning(
                    "\n  [Docker] The Docker daemon doesn't appear to be running."
                )
                print_dim("  Start Docker Desktop (or the docker service), then retry.")
                if not self._deploy_yn("  Continue anyway? (y/n): "):
                    return False

        for req in get_requirements(plan.platform):
            if not req.is_installed():
                continue  # already surfaced (and accepted) in the CLI step
            authed = is_authenticated(req, cwd=str(cwd))
            if authed is not False:
                continue  # True (logged in) or None (couldn't tell) -> don't block

            print_warning(
                f"\n  [Auth] You don't appear to be logged in to {req.display_name}."
            )
            if req.login_cmd:
                if self._deploy_yn(f"  Run '{req.login_cmd}' now? (y/n): "):
                    self.execute_shell_command(req.login_cmd)
                    if is_authenticated(req, cwd=str(cwd)) is False:
                        if not self._deploy_yn(
                            "  Still not logged in. Continue anyway? (y/n): "
                        ):
                            return False
                elif not self._deploy_yn("  Continue without logging in? (y/n): "):
                    return False
            else:
                print_dim("  Authenticate with this platform, then re-run 'deploy'.")
                if not self._deploy_yn("  Continue anyway? (y/n): "):
                    return False
        return True

    # -- Publish version preflight -------------------------------------------

    def _deploy_publish_preflight(self, cwd: Path, plan: DeployPlan) -> bool:
        """
        For npm / PyPI / crates.io: if the current version is already on the
        registry, offer to bump it so the publish doesn't fail.
        """
        if plan.platform not in ("npm", "pypi", "crates.io"):
            return True

        info = deploy_publish.read_publish_info(cwd, plan.platform)
        if info is None:
            return True

        published = deploy_publish.is_version_published(info)
        if published is None:
            print_dim(
                f"  Couldn't verify whether {info.package_name} "
                f"{info.current_version} is already published — continuing."
            )
            return True
        if not published:
            print_dim(
                f"  {info.package_name} {info.current_version} is not yet on "
                f"{plan.platform} — good to publish."
            )
            return True

        # Already published: publishing this version will fail.
        print_warning(
            f"\n  [Version] {info.package_name} {info.current_version} is already "
            f"published on {plan.platform}."
        )
        patch = deploy_publish.bump_semver(info.current_version, "patch")
        minor = deploy_publish.bump_semver(info.current_version, "minor")
        major = deploy_publish.bump_semver(info.current_version, "major")
        if patch is None:
            print_dim(
                f"  Couldn't auto-bump '{info.current_version}'. Update the version "
                f"in {info.manifest_path.name} manually."
            )
            if not self._deploy_yn("  Continue anyway? (y/n): "):
                return False
            return True

        print_dim(
            f"  Bump to: (p)atch {patch}  /  (m)inor {minor}  /  "
            f"(M)ajor {major}  /  (k)eep / (c)ancel"
        )
        try:
            choice = input("  Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        low = choice.lower()
        if low in ("c", "cancel", "n", "no", ""):
            print_warning("  [Cancelled]")
            return False
        if low in ("k", "keep"):
            print_dim("  Keeping current version (publish will likely fail).")
            return True

        if choice == "M" or low == "major":
            new_version = major
        elif low in ("m", "minor"):
            new_version = minor
        else:  # patch / default
            new_version = patch

        if deploy_publish.write_new_version(info, new_version):
            print_success(
                f"  Bumped {info.package_name}: {info.current_version} -> {new_version} "
                f"({info.manifest_path.name})"
            )
        else:
            print_error(
                f"  Couldn't write the new version to {info.manifest_path.name}; "
                "update it manually."
            )
            if not self._deploy_yn("  Continue anyway? (y/n): "):
                return False
        return True

    # -- Docker image target resolution --------------------------------------

    def _deploy_resolve_docker_target(self, cwd: Path, plan: DeployPlan) -> bool:
        """
        A bare ``docker push <name>`` resolves to docker.io/library/<name>, which
        a normal user can't push to. Qualify the image with a real registry /
        namespace (guessed from the git remote, or asked for), or drop the push.
        """
        if plan.platform != "docker":
            return True

        push_ref = None
        for step in plan.steps:
            toks = step.split()
            if len(toks) >= 3 and toks[0] == "docker" and toks[1] == "push":
                push_ref = toks[2]
                break
        if push_ref is None or "/" in push_ref:
            return True  # nothing to push, or already qualified

        guess = self._deploy_guess_docker_namespace(cwd)
        print_info("\n  [Docker] The image needs a registry/namespace to push.")
        print_dim("  e.g. ghcr.io/you, docker.io/you, registry.example.com/team")
        prompt = (
            f"  Registry/namespace [{guess}]: " if guess
            else "  Registry/namespace (blank to skip push): "
        )
        try:
            entered = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        namespace = (entered or guess or "").rstrip("/")

        if not namespace:
            plan.steps = [
                s for s in plan.steps
                if not (s.split()[:2] == ["docker", "push"])
            ]
            print_dim("  Skipping push — building the image locally only.")
            return True

        new_ref = f"{namespace}/{push_ref}"
        new_steps = []
        for step in plan.steps:
            toks = step.split()
            if toks[:2] == ["docker", "build"]:
                new_steps.append(step.replace(f"-t {push_ref}", f"-t {new_ref}"))
            elif toks[:2] == ["docker", "push"]:
                new_steps.append(f"docker push {new_ref}")
            else:
                new_steps.append(step)
        plan.steps = new_steps
        print_dim(f"  Image: {new_ref}")
        return True

    def _deploy_guess_docker_namespace(self, cwd: Path) -> Optional[str]:
        """Guess ghcr.io/<owner> from a GitHub origin remote, else None."""
        try:
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(cwd), timeout=5,
            )
        except Exception:
            return None
        url = (result.stdout or "").strip()
        if not url or "github" not in url.lower():
            return None
        m = re.search(r"[:/]([^/:]+)/[^/]+?(?:\.git)?/?$", url)
        if not m:
            return None
        return f"ghcr.io/{m.group(1).lower()}"

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
                continue

            print_error(f"\n  [{i}/{total}] Failed: {step}")

            # Explain the failure in plain English and, if a fix is available,
            # offer to run it and retry the step before giving up.
            if self._deploy_diagnose_failure(cwd, step):
                print_success(f"  [{i}/{total}] Done (recovered)")
                continue

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
                all_ok = False
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

    def _deploy_diagnose_failure(self, cwd: Path, step: str) -> bool:
        """
        Translate a failed deploy step's stderr into a plain-English explanation
        and, when a fix is available, offer to run it and retry the step.

        Returns True only if a fix was applied and the step then succeeded.
        """
        if not getattr(self.nl_handler, "llm_enabled", False):
            return False
        stderr = (self.last_stderr or "").strip()
        if not stderr:
            return False

        context = {
            "cwd": str(cwd),
            "os": platform.system(),
            "shell": self.shell_path or os.environ.get("SHELL", "bash"),
        }

        print_dim("  Analyzing the failure...")
        try:
            result = self.nl_handler.translate_error(
                step,
                self.last_exit_code,
                stderr,
                context,
                stream_callback=None,
            )
        except Exception:
            return False

        explanation = (result.get("explanation") or "").strip()
        fix_commands = result.get("fix_commands") or []
        fix_explanation = (result.get("fix_explanation") or "").strip()

        if explanation:
            print_info(f"  [Cliara] {explanation}")

        if not fix_commands:
            return False

        fix_display = " && ".join(fix_commands)
        print_info(f"  Suggested fix: {fix_display}")
        if fix_explanation:
            print_dim(f"  ({fix_explanation})")

        if not self._deploy_yn("  Run fix and retry this step? (y/n): "):
            return False

        if not self._deploy_run_fix(fix_commands):
            return False

        # Retry the original step now that the fix has run.
        print_info(f"  Retrying: {step}")
        return self.execute_shell_command(step)

    def _deploy_run_fix(self, fix_commands: list) -> bool:
        """Run LLM-suggested fix commands, gated by the safety checker."""
        safety = getattr(self, "safety", None)
        if safety is not None:
            level, dangerous = safety.check_commands(fix_commands)
            if level != DangerLevel.SAFE:
                print_warning(
                    "  [Caution] The suggested fix includes commands that change "
                    "your system:"
                )
                for cmd, reason in dangerous:
                    print_dim(f"    {cmd}  ({reason})")
                prompt = safety.get_confirmation_prompt(level)
                try:
                    confirm = input(f"  {prompt}").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return False
                if not safety.validate_confirmation(confirm, level):
                    print_warning("  [Cancelled]")
                    return False

        for fix_cmd in fix_commands:
            print_info(f"  > {fix_cmd}")
            if not self.execute_shell_command(fix_cmd):
                print_error(f"  Fix command failed: {fix_cmd}")
                return False
        return True

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
        print()
        print_dim("  Before each deploy Cliara also checks prerequisites for you:")
        print_dim("    - the platform CLI is installed (offers to install it if not)")
        print_dim("    - you're logged in (offers to run the login command)")
        print_dim("    - for npm/PyPI/crates.io: the version isn't already published (offers to bump)")
        print_dim("    - Docker images get a real registry/namespace before pushing")
        print_dim("  If a step fails, Cliara explains why in plain English and can run a fix + retry.")
        print_dim("  PyPI: upload step uses twine --username __token__; paste your full pypi-... API token at the password prompt.")
        print()

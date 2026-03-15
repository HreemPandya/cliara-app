"""
Cliara - Main entry point.
Starts the Cliara shell.
"""

import sys
import argparse
from pathlib import Path

from cliara import __version__
from cliara.config import Config
from cliara.shell import CliaraShell


def _run_login():
    """Run OAuth login flow (standalone, no REPL)."""
    from cliara.auth import login
    from cliara.shell import print_success, print_error, print_warning, print_dim

    print()
    print_dim("  Cliara Login — Zero-Friction Cloud Access")
    print_dim("  ─────────────────────────────────────────")
    print_dim("  Free tier: 150 queries/month · no credit card · GPT-4o-mini")
    print()

    try:
        token, email = login()
        print()
        user_label = f" ({email})" if email else ""
        print_success(f"  Logged in to Cliara Cloud{user_label}")
        print_success("  Free tier · 150 queries/month · resets monthly")
        print_dim("  Token saved to ~/.cliara/token.json — auto-loaded on every startup.")
        print_dim("  Run 'cliara' to start the shell, or 'cliara logout' to sign out.")
    except KeyboardInterrupt:
        print()
        print_warning("  Login cancelled.")
    except RuntimeError as exc:
        print()
        print_error(f"  [Error] {exc}")
        print_dim("  Try 'setup-llm' inside cliara for BYOK options (Groq/Gemini are free).")
        sys.exit(1)


def _run_logout():
    """Clear stored token (standalone, no REPL)."""
    from cliara.auth import logout, load_token
    from cliara.icons import print_success, print_warning, print_dim

    token_data = load_token()
    if token_data is None:
        print()
        print_warning("  Not currently logged in to Cliara Cloud.")
        print_dim("  Run 'cliara login' to sign in.")
        return

    email = token_data.get("email", "")
    logout()
    print()
    label = f" ({email})" if email else ""
    print_success(f"  Logged out of Cliara Cloud{label}.")
    print_dim("  Token deleted from ~/.cliara/token.json.")
    print_dim("  Run 'cliara login' to sign in again.")


def _run_status(config_dir=None):
    """Show auth and LLM status (standalone, no REPL)."""
    from cliara.auth import load_token, get_valid_token
    from cliara.shell import print_success, print_warning, print_dim

    print()
    print_dim("  Cliara Status")
    print_dim("  ------------")
    print()

    token_data = load_token()
    if token_data and get_valid_token():
        email = token_data.get("email", "unknown")
        print_success(f"  Cliara Cloud: logged in ({email})")
        print_dim("  Free tier · 150 queries/month · resets monthly")
    else:
        # Check if BYOK is configured via config
        config = Config(config_dir=config_dir)
        provider = config.get("llm_provider")
        if provider:
            print_success(f"  BYOK: {provider}")
            print_dim("  Using your own API key")
        else:
            print_warning("  Not configured")
            print_dim("  Run 'cliara login' for Cloud, or 'setup-llm' inside cliara for BYOK")
    print()


def main():
    """Main entry point for Cliara."""
    parser = argparse.ArgumentParser(
        description="Cliara - AI-powered shell with natural language and macros",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cliara                    Start interactive Cliara shell
  cliara login              Log in to Cliara Cloud (GitHub OAuth, no API key needed)
  cliara logout             Sign out and clear stored token
  cliara status             Show auth and LLM status
  cliara -c "git status"    Run a single command through Cliara's gate
  cliara -c "rm -rf dist"   Risky commands still require approval
  cliara --config-dir ~/my-config  Use custom config directory
  cliara --version          Show version
  
Once in the shell:
  ls -la                    Run normal commands
  ? kill port 3000          Use natural language (Phase 2)
  macro add mycommand       Create a macro
  mycommand                 Run a macro
        """
    )
    
    parser.add_argument(
        '--version',
        action='version',
        version=f'Cliara {__version__}'
    )
    
    parser.add_argument(
        '-c',
        type=str,
        metavar='COMMAND',
        help='Run a single command through Cliara\'s risk gate, then exit'
    )
    
    parser.add_argument(
        '--config-dir',
        type=str,
        help='Custom configuration directory (default: ~/.cliara)'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode'
    )
    
    parser.add_argument(
        '--shell',
        type=str,
        help='Override shell path'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Always show full startup banner (quick tips)'
    )

    subparsers = parser.add_subparsers(dest='command', help='Subcommands')
    subparsers.add_parser('login', help='Log in to Cliara Cloud (GitHub OAuth)')
    subparsers.add_parser('logout', help='Sign out and clear stored Cliara Cloud token')
    subparsers.add_parser('status', help='Show auth and LLM status')
    
    args = parser.parse_args()

    # Standalone login/logout/status — run OAuth, clear token, or show status, then exit (no REPL)
    if args.command == 'login':
        _run_login()
        sys.exit(0)
    if args.command == 'logout':
        _run_logout()
        sys.exit(0)
    if args.command == 'status':
        _run_status(config_dir=args.config_dir)
        sys.exit(0)
    
    # Set debug mode
    if args.debug:
        import os
        os.environ['DEBUG'] = '1'
    
    try:
        # Initialize config
        config = Config(config_dir=args.config_dir)
        
        # Override shell if specified
        if args.shell:
            config.set('shell', args.shell)
        
        # Single-command mode: gate → execute → exit
        if args.c:
            shell = CliaraShell(config)
            exit_code = shell.run_single_command(args.c)
            sys.exit(exit_code)
        
        # Interactive mode
        shell = CliaraShell(config)
        shell.run(verbose_banner=args.verbose)
    
    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n[Fatal Error] {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

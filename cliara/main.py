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


def main():
    """Main entry point for Cliara."""
    parser = argparse.ArgumentParser(
        description="Cliara - AI-powered shell with natural language and macros",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cliara                    Start interactive Cliara shell
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
    
    args = parser.parse_args()
    
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

"""AdLoop — MCP server connecting Google Ads + GA4 + codebase."""

import sys

__version__ = "0.6.5"


def main() -> None:
    """Entry point for `adloop` console script.

    Subcommands:
      adloop                 Start the MCP server (default).
      adloop init            Run the interactive setup wizard.
      adloop install-rules   Install Claude orchestration rules globally.
      adloop update-rules    Refresh the installed rules block.
      adloop uninstall-rules Remove the installed rules block + commands.
      adloop --version, -V   Print version and exit.
    """
    args = sys.argv[1:]

    if args and args[0] in ("--version", "-V"):
        print(f"adloop {__version__}")
        return

    if args and args[0] == "init":
        from adloop.cli import run_init_wizard

        try:
            run_init_wizard()
        except KeyboardInterrupt:
            print("\n\n  Setup cancelled.\n")
            sys.exit(130)
        return

    if args and args[0] in ("install-rules", "update-rules", "uninstall-rules"):
        from adloop.cli import run_rules_command

        try:
            sys.exit(run_rules_command(args[0], args[1:]))
        except KeyboardInterrupt:
            print("\n\n  Cancelled.\n")
            sys.exit(130)
        return

    from adloop.server import mcp

    mcp.run()

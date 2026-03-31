"""AdLoop — MCP server connecting Google Ads + GA4 + codebase."""

import sys

__version__ = "0.3.0"


def main() -> None:
    """Entry point for `adloop` console script.

    Routes to the setup wizard when called as ``adloop init``,
    otherwise starts the MCP server.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        from adloop.cli import run_init_wizard

        run_init_wizard()
    else:
        from adloop.server import mcp

        mcp.run()

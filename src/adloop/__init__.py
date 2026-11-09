"""AdLoop — MCP server connecting Google Ads + GA4 + codebase."""

import sys
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("adloop")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0.dev0"


def install_runtime_patches() -> None:
    """Arm the upstream-bug workarounds for a long-running host process.

    Importing ``adloop.server`` is deliberately free of process-global side
    effects so the server can be embedded in an ASGI app. That leaves the
    workarounds in ``_mcp_patches`` unarmed for any embedder, including a
    hosted deployment, which is exactly where they matter most: the
    cancellation race they guard against (python-sdk#2416) fires when a
    host cancels a slow tool call, and it takes the transport down with it.

    Embedders should call this once during application startup. It is
    idempotent, never raises, and each patch inside self-disarms once the
    upstream bug it tracks is fixed.
    """
    from adloop import _mcp_patches

    _mcp_patches.install()


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

    # Process-global side effects (signal handlers, heartbeat thread, and
    # the stdio cancellation-race monkeypatch) are deliberately installed
    # here — in the stdio entry point — rather than at adloop.server import
    # time, so embedding the server in an ASGI app stays side-effect-free.
    from adloop import diagnostics

    diagnostics.install()
    install_runtime_patches()

    from adloop.server import hide_unconfigured_platforms, mcp

    # Also local-only: a platform that isn't set up (Reddit Ads without a
    # Reddit app, say) keeps its tools out of tools/list. A config that fails
    # to load hides nothing; every tool call reports the problem.
    try:
        from adloop.runtime import current_config

        hide_unconfigured_platforms(current_config())
    except Exception as e:  # noqa: BLE001
        print(f"adloop: could not check which platforms are set up: {e}", file=sys.stderr)

    mcp.run()

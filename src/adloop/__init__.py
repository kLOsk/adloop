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
      adloop microsoft-login Sign in to Microsoft Advertising in the browser
                             (--user <email> preselects the account; --device-code
                             for the code-on-another-device flow).
      adloop roku-login      Sign in to Roku Ads Manager (authorization code).
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

    if args and args[0] == "microsoft-login":
        from adloop.config import load_config
        from adloop.microsoft_ads.auth import MicrosoftAdsAuthError, browser_login, device_code_login

        options = args[1:]
        hint = next((options[k + 1] for k, a in enumerate(options[:-1]) if a == "--user"), "")
        try:
            if "--device-code" in options:
                path = device_code_login(load_config())
            else:
                path = browser_login(load_config(), login_hint=hint)
        except MicrosoftAdsAuthError as exc:
            print(f"\n  {exc}\n")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n\n  Cancelled.\n")
            sys.exit(130)
        print(f"\n  Signed in. Token saved to {path}. Restart the MCP server to use it.\n")
        return

    if args and args[0] == "roku-login":
        from adloop.config import load_config
        from adloop.roku_ads.auth import RokuAdsAuthError, authorization_login

        try:
            path = authorization_login(load_config())
        except RokuAdsAuthError as exc:
            print(f"\n  {exc}\n")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n\n  Cancelled.\n")
            sys.exit(130)
        print(f"\n  Signed in. Token saved to {path}. Restart the MCP server to use it.\n")
        return

    # Process-global side effects (signal handlers, heartbeat thread, and
    # the stdio cancellation-race monkeypatch) are deliberately installed
    # here — in the stdio entry point — rather than at adloop.server import
    # time, so embedding the server in an ASGI app stays side-effect-free.
    from adloop import diagnostics

    diagnostics.install()
    install_runtime_patches()

    from adloop.server import mcp

    mcp.run()

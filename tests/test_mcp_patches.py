"""The cancellation-race workaround has to be reachable by embedders.

python-sdk#2416 takes the transport down when a host cancels a slow tool
call. The workaround lived behind the stdio entry point, so anything that
embedded the server in an ASGI app ran unprotected: importing
adloop.server is deliberately side-effect-free, and the patch went with
the side effects. install_runtime_patches() is the supported way for an
embedder to arm it.
"""

import inspect

import pytest

# mcp 2.x removed mcp.shared.session, and install() already treats that as
# "nothing to patch" (it try/excepts the same import and returns). Mirror
# that rather than failing collection — but skip only the two tests that
# need a RequestResponder to talk about. The side-effect-free invariant
# below is about our own module and must hold on every mcp version.
try:
    from mcp.shared import session as mcp_session
except ImportError:  # mcp >= 2
    mcp_session = None

needs_request_responder = pytest.mark.skipif(
    mcp_session is None,
    reason="mcp >= 2 has no mcp.shared.session; the pysdk-2416 patch no-ops",
)


def test_importing_the_server_stays_side_effect_free():
    """The property that made the gap possible is intentional: keep it."""
    import adloop.server

    # This does not assert the patch is absent, because another test in the
    # same process may legitimately have armed it. What matters is that the
    # import itself does not reach for it.
    src = inspect.getsource(adloop.server)
    assert "_mcp_patches" not in src, (
        "adloop.server must not install process-global patches on import; "
        "embedders call install_runtime_patches() instead"
    )


@needs_request_responder
def test_install_runtime_patches_arms_the_cancellation_guard():
    import adloop

    responder = mcp_session.RequestResponder

    # Sample upstream BEFORE arming. Once patched, respond() is our own
    # function and no longer carries upstream's assert, so inspecting it
    # afterwards always reads as "upstream fixed it". A responder that is
    # already flagged can only have been patched because the bug was there.
    if getattr(responder, "_adloop_2416_patched", False):
        upstream_still_racey = True
    else:
        upstream_still_racey = "assert not self._completed" in inspect.getsource(
            responder.respond
        )

    adloop.install_runtime_patches()

    if upstream_still_racey:
        assert getattr(responder, "_adloop_2416_patched", False), (
            "upstream still has the racey assert but the guard did not arm"
        )
    else:
        # Upstream landed a fix, so the patch self-disarms by design and
        # this whole module can go.
        assert not getattr(responder, "_adloop_2416_patched", False)


@needs_request_responder
def test_install_runtime_patches_is_idempotent():
    import adloop

    adloop.install_runtime_patches()
    first = mcp_session.RequestResponder.respond
    adloop.install_runtime_patches()

    assert mcp_session.RequestResponder.respond is first

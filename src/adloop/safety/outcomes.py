"""Write outcomes that must not be replayed.

An applier raises :class:`PartialApplyError` when part of a plan already
changed the account (a multi-step create whose later step failed, a batch
that created some items and rejected others) or when it cannot know whether
a create went through (the response was lost). ``confirm_and_apply`` then
retires the plan instead of keeping it pending: confirming it again would
repeat what already happened. The error says what was done, so the person
can check the account and draft only what is still missing.
"""

from __future__ import annotations

from typing import Any


class PartialApplyError(Exception):
    """Part of the plan was applied, or its outcome is unknown; never replay it."""

    def __init__(self, message: str, *, created: dict[str, Any] | None = None, unknown: bool = False):
        super().__init__(message)
        self.created = created or {}
        self.unknown = unknown

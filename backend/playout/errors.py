"""Errors the playout adapter raises to its callers (spec: Engine, carried item C5)."""

from __future__ import annotations


class EngineStartError(Exception):
    """The session engine did not start or did not become ready.

    The message says which and why.
    """


class AnalyserError(Exception):
    """The cue analyser could not run a batch, or broke its output protocol.

    The message says which, with the exit code or the offending line and field.
    """

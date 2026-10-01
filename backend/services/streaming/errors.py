"""Failures of the stream service (spec: Errors and edge cases; the internal contract)."""

from __future__ import annotations

from backend.domain.streaming import StreamingError


class StationNotFoundError(StreamingError):
    """No station has these call letters, in any case (D72)."""


class StationBusyError(StreamingError):
    """Every listener slot is taken (D10)."""


class StreamUnavailableError(StreamingError):
    """The engine could not be started (D1 already retried once)."""


class InvalidStreamSettingError(StreamingError):
    """A streaming user setting holds a value the service cannot use (D27)."""


class UnknownSessionError(StreamingError):
    """No open session has this id."""


class SessionTokenError(StreamingError):
    """The session token is missing or wrong."""


class UnknownItemError(StreamingError):
    """The seq is not assigned and is not the next one: the engine must retry later."""

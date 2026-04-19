"""Unit tests for the pure lock-key helper in ingest_transaction.

``station_advisory_lock_key`` must produce a stable signed-bigint key for a
given station regardless of how the UUID string is formatted, so two uploads
against the same station cannot bypass the advisory lock.
"""
from __future__ import annotations

from uuid import uuid4

from backend.db.ingest_transaction import station_advisory_lock_key

_SIGNED_BIGINT_MAX = (1 << 63) - 1


def test_same_uuid_different_casing_produces_same_key() -> None:
    station = uuid4()
    lower = station_advisory_lock_key(str(station).lower())
    upper = station_advisory_lock_key(str(station).upper())
    assert lower == upper


def test_uuid_with_and_without_hyphens_produces_same_key() -> None:
    station = uuid4()
    hyphenated = station_advisory_lock_key(str(station))
    hex_only = station_advisory_lock_key(station.hex)
    assert hyphenated == hex_only


def test_different_stations_produce_different_keys() -> None:
    assert station_advisory_lock_key(str(uuid4())) != station_advisory_lock_key(
        str(uuid4())
    )


def test_empty_station_id_uses_reserved_key() -> None:
    # Empty and None should both land on the shared no-station key so
    # station-less uploads still serialise among themselves.
    assert station_advisory_lock_key("") == station_advisory_lock_key(None)


def test_non_uuid_station_id_is_still_deterministic() -> None:
    first = station_advisory_lock_key("station-abc")
    second = station_advisory_lock_key("station-abc")
    assert first == second
    # And different non-UUID strings produce different keys.
    assert first != station_advisory_lock_key("station-xyz")


def test_key_fits_in_signed_bigint_range() -> None:
    for station_id in (str(uuid4()), "not-a-uuid", "", None):
        key = station_advisory_lock_key(station_id)
        assert -_SIGNED_BIGINT_MAX <= key <= _SIGNED_BIGINT_MAX

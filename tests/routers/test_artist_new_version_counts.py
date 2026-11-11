"""The artist list's and the artist page's new-version counts, as they read before the artist-list
follow-up to versions phase 2b PR 3 (SPEC S8, D2, D5): characterization tests.

A work's count is how many of its texts are new: unmapped, read as a version, and with no state
row, or with a copy of that version on the work (a present file of the work, of the recording of
that version) indexed after the state's ``seen_at``. The list counts a group's works on its
primary; the artist page answers the group's count and each work's own.

``test_unmapped_versions.py`` pins the counts for texts with no state row, and for a seen text
with no copy. These pin the counts that read the work's files: a seen text that a later copy may
answer, on the primary's work and on a merged member's; an artist whose works hold files but no
version text; and texts whose lines are matched to the copies themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from tests.routers import version_pages_seed as seed
from tests.routers.version_pages_seed import AFTER, BEFORE, T0

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.routers.version_pages_seed import Conn

pytestmark = pytest.mark.integration

ARTISTS = "/api/v1/library/artists"


def played(conn: Conn, title: str, file_id: UUID, count: int) -> None:
    """A line titled *title* matched to *file_id*, with *count* plays at one station."""
    line = seed.line(conn, title, file_id)
    times = [datetime(2001, 1, 1 + n, 8, tzinfo=UTC) for n in range(count)]
    seed.plays(conn, line, seed.station(conn, "KISW"), *times)


def list_counts(client: TestClient) -> dict[str, int]:
    response = client.get(ARTISTS, params={"limit": 50})
    assert response.status_code == 200, response.text
    return {i["id"]: i["new_version_count"] for i in response.json()["items"]}


def page_counts(client: TestClient, artist_id: str) -> tuple[int, dict[str, int]]:
    """The artist page's count, and each of its works' own."""
    response = client.get(f"{ARTISTS}/{artist_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body["new_version_count"], {w["id"]: w["new_version_count"] for w in body["works"]}


def test_a_later_copy_may_answer_a_seen_version(client: TestClient, db_conn: Conn) -> None:
    """S8, D2, D5: a seen text counts again when a copy of its version on the work was indexed
    after its ``seen_at``, on the primary's work and on a merged member's; a copy indexed before
    doesn't. An artist whose works hold files, a version's copy among them, but no version text
    counts 0, and so does an artist with no works."""
    heart = seed.artist(db_conn, "Heart")
    ann = seed.artist(db_conn, "Ann Wilson")
    seed.merge(db_conn, ann, heart)
    barracuda = seed.work(db_conn, "Barracuda", heart)
    barracuda_master = seed.file(db_conn, barracuda)
    played(db_conn, "Barracuda (Live)", barracuda_master, 2)
    seed.state(db_conn, barracuda, "live", None, T0)
    seed.file(db_conn, barracuda, version_type="live", indexed_at=AFTER)
    played(db_conn, "Barracuda (Acoustic)", barracuda_master, 1)
    seed.state(db_conn, barracuda, "acoustic", None, T0)
    seed.file(db_conn, barracuda, version_type="acoustic", indexed_at=BEFORE)
    alone = seed.work(db_conn, "Alone", ann)
    played(db_conn, "Alone (Demo)", seed.file(db_conn, alone), 1)
    seed.state(db_conn, alone, "demo", None, T0)
    seed.file(db_conn, alone, version_type="demo", indexed_at=AFTER)
    quiet = seed.artist(db_conn, "Quiet Artist")
    silence = seed.work(db_conn, "Silence", quiet)
    seed.file(db_conn, silence, indexed_at=AFTER)
    seed.file(db_conn, silence, version_type="live", indexed_at=AFTER)
    hush = seed.work(db_conn, "Hush", quiet)
    seed.file(db_conn, hush, version_type="demo", indexed_at=AFTER)
    empty = seed.artist(db_conn, "No Works")
    db_conn.commit()

    assert list_counts(client) == {heart: 2, quiet: 0, empty: 0}
    assert page_counts(client, heart) == (2, {barracuda: 1, alone: 1})
    assert page_counts(client, ann) == (2, {barracuda: 1, alone: 1})
    assert page_counts(client, quiet) == (0, {silence: 0, hush: 0})


def test_lines_matched_to_the_copies(client: TestClient, db_conn: Conn) -> None:
    """D5: a text's lines may be matched to a copy of its version; the count is the work page's
    new markers all the same. Live is seen, and its copy (its line's file) was indexed after;
    demo is seen, and its copy (its line's file) before; acoustic has no state row."""
    rush = seed.artist(db_conn, "Rush")
    limelight = seed.work(db_conn, "Limelight", rush)
    plain = seed.file(db_conn, limelight)
    live = seed.file(db_conn, limelight, version_type="live", indexed_at=AFTER)
    played(db_conn, "Limelight (Live)", live, 3)
    seed.state(db_conn, limelight, "live", None, T0)
    demo = seed.file(db_conn, limelight, version_type="demo", indexed_at=BEFORE)
    played(db_conn, "Limelight (Demo)", demo, 1)
    seed.state(db_conn, limelight, "demo", None, T0)
    played(db_conn, "Limelight (Acoustic)", plain, 1)
    db_conn.commit()

    versions = client.get(f"/api/v1/library/works/{limelight}/versions").json()["versions"]

    assert {v["version_text"]: v["new"] for v in versions} == {
        "live": True,
        "demo": False,
        "acoustic": True,
    }
    assert list_counts(client) == {rush: 2}
    assert page_counts(client, rush) == (2, {limelight: 2})

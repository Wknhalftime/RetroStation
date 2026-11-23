"""The merge list's planner reads the cache fill's fetched work links (MusicBrainz cache fill,
phase 4.1 PR 3; SPEC S5; D1; R1, R3-g; AUD-R092, AUD-R080, AUD-R089), on PostgreSQL.

R1: the planner reads a work link from a work's own MBID (a MusicBrainz work) or from the
``recording:`` cache row of a performance's recording, and never from a ``release:`` row. PR 3's
fill stores each track's link from a release lookup under ``recording-work:{rid}``, as
``{"work": {"id", "title"}}`` or ``{"work": null}``, and ``_LINKED`` reads those rows beside the
``recording:`` rows, joined on ``performances.recording_mbid``. Like the ``recording:`` arm, it
reads a row whatever its ``expires_at``. A row naming no work joins nothing, two works kept apart
are never joined (AUD-R089), and the planner only plans: nothing merges until Lance's Apply.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from backend.domain.work_merge_planner import MergeJoin
from backend.services import merge_list_service
from backend.services.merge_plan_service import plan
from backend.services.repository_factory import RepositoryFactory, merge_list_repos
from tests.integration.cache_fill_rig import Conn, cached
from tests.integration.credit_picks_seed import set_marker
from tests.integration.work_link_seed import (
    UNLINKED,
    Song,
    linked,
    performs,
    recording_mbid,
    release,
    release_mbid,
    song,
    track,
    work_key,
    work_mbid,
)

pytestmark = pytest.mark.integration

LINK = frozenset({MergeJoin.WORK_LINK})


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    """The test database, its credits marked done: the planner waits for them (Q22)."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        set_marker(connection, WORK_ARTISTS_CREDITED_SETTING)
        yield connection


def fetched(conn: Conn, sung: Song, work: str, title: str, *, expired: bool = False) -> None:
    """The fill's row for *sung*'s recording, naming the work *work*."""
    cached(conn, work_key(sung.recording), expired=expired, data=linked(work, title))


def known_mbids(conn: Conn) -> dict[str, frozenset[str]]:
    """Each work's linked work MBIDs, as the planner reads them."""
    return {w.work_id: w.linked_mbids for w in RepositoryFactory(conn).merge_list.plan_works()}


def groups(conn: Conn) -> list[tuple[str, frozenset[str], frozenset[MergeJoin]]]:
    """The planned groups: each one's target, its works, and why they join."""
    repos = RepositoryFactory(conn)
    return [
        (g.target_id, frozenset({g.target_id, *g.source_ids}), g.joins)
        for g in plan(repos.merge_list, repos.user_settings).groups
    ]


def test_a_fetched_link_is_one_of_the_works_linked_mbids(conn: Conn) -> None:
    sung = song(conn, "Battery", recording_mbid(1))
    fetched(conn, sung, work_mbid(1), "Battery")

    assert known_mbids(conn)[sung.work_id] == frozenset({work_mbid(1)})


def test_it_joins_a_work_to_the_musicbrainz_work_it_names(conn: Conn) -> None:
    """The MusicBrainz work is the group's target (AUD-R080)."""
    labelle = song(conn, "Lady Marmalade", recording_mbid(1), musicbrainz=work_mbid(1))
    aguilera = song(conn, "Lady Marmalade", recording_mbid(2))
    fetched(conn, aguilera, work_mbid(1), "Lady Marmalade")

    assert groups(conn) == [(labelle.work_id, frozenset({labelle.work_id, aguilera.work_id}), LINK)]


def test_two_fetched_links_naming_one_work_join_their_works(conn: Conn) -> None:
    """Neither is a MusicBrainz work, and their titles differ: the links alone join them."""
    english = song(conn, "Lady Marmalade", recording_mbid(1))
    french = song(conn, "Voulez-Vous Coucher Avec Moi", recording_mbid(2))
    fetched(conn, english, work_mbid(1), "Lady Marmalade")
    fetched(conn, french, work_mbid(1), "Lady Marmalade")

    [(_target, works, joins)] = groups(conn)

    assert (works, joins) == (frozenset({english.work_id, french.work_id}), LINK)


def test_it_reads_them_beside_recording_lookups(conn: Conn) -> None:
    """One work's link is a cached recording lookup's, the other's a fetched one."""
    looked_up = song(conn, "Lady Marmalade", recording_mbid(1))
    cached(
        conn,
        f"recording:{recording_mbid(1)}",
        data={"id": recording_mbid(1), "relations": [performs(work_mbid(1), "Lady Marmalade")]},
    )
    fetch = song(conn, "Lady Marmalade", recording_mbid(2))
    fetched(conn, fetch, work_mbid(1), "Lady Marmalade")

    [(_target, works, joins)] = groups(conn)

    assert (works, joins) == (frozenset({looked_up.work_id, fetch.work_id}), LINK)


def test_a_row_naming_no_work_joins_nothing(conn: Conn) -> None:
    first = song(conn, "Lady Marmalade", recording_mbid(1))
    second = song(conn, "Lady Marmalade", recording_mbid(2))
    cached(conn, work_key(first.recording), data=UNLINKED)
    cached(conn, work_key(second.recording), data=UNLINKED)

    assert groups(conn) == []
    mbids = known_mbids(conn)
    assert (mbids[first.work_id], mbids[second.work_id]) == (frozenset(), frozenset())


def test_an_expired_row_still_links(conn: Conn) -> None:
    """R3-g: as the ``recording:`` arm reads a row whatever its ``expires_at``."""
    first = song(conn, "Lady Marmalade", recording_mbid(1))
    second = song(conn, "Lady Marmalade", recording_mbid(2))
    fetched(conn, first, work_mbid(1), "Lady Marmalade", expired=True)
    fetched(conn, second, work_mbid(1), "Lady Marmalade")

    [(_target, works, joins)] = groups(conn)

    assert (works, joins) == (frozenset({first.work_id, second.work_id}), LINK)


def test_a_release_row_is_still_not_read(conn: Conn) -> None:
    """R1: a cached release naming the work for both recordings links neither; only the rows
    the fill writes from it do."""
    first = song(conn, "Lady Marmalade", recording_mbid(1), release_mbid(1))
    second = song(conn, "Lady Marmalade", recording_mbid(2), release_mbid(1))
    cached(
        conn,
        f"release:{release_mbid(1)}",
        data=release(
            release_mbid(1),
            [
                track(recording_mbid(1), performs(work_mbid(1), "Lady Marmalade")),
                track(recording_mbid(2), performs(work_mbid(1), "Lady Marmalade")),
            ],
        ),
    )

    assert groups(conn) == []
    assert known_mbids(conn)[first.work_id] == known_mbids(conn)[second.work_id] == frozenset()


def test_two_works_kept_apart_stay_apart(conn: Conn) -> None:
    """AUD-R089: Lance's Keep apart holds against a fetched link as against any other: the pair
    is never joined, and the list counts it kept."""
    first = song(conn, "Lady Marmalade", recording_mbid(1))
    second = song(conn, "Lady Marmalade", recording_mbid(2))
    fetched(conn, first, work_mbid(1), "Lady Marmalade")
    fetched(conn, second, work_mbid(1), "Lady Marmalade")
    repos = merge_list_repos(RepositoryFactory(conn))
    assert merge_list_service.count(repos).total == 1

    merge_list_service.keep_apart([first.work_id, second.work_id], repos)

    counted = merge_list_service.count(repos)
    assert (counted.total, counted.kept_apart) == (0, 1)

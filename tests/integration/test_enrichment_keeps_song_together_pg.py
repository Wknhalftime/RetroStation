"""Integration: enrichment keeps a song's copies on one work (AUD-R103, "Relabel + leaks").

Grouping puts every copy of a song on one local work. When enrichment learns the MusicBrainz
work a copy's recording performs and no work holds that MBID yet, the copy's local work takes
the MBID (R1), so the copies enrichment cannot link (an untagged one, an ambiguous one) stay
with it. Before this, a new MusicBrainz work was made and only the linked copy moved onto it:
the 1,055 MusicBrainz + local pairs of the 2026-11-19 gate copy.

A work qualifies when it is local, has the artist the step gives the work, and has a title
equal by grouping's exact path (version stripped, normalized, same part number, equal once
strict-normalized; phase 3b's D29). It takes MusicBrainz's spelling of the title. Where a work
already holds the MBID, nothing is merged: today's per-file move stays and the pair waits for
the merge list.

C1: the recording is written without its work, then put on the work through the guarded
``link_work``; a version slot another recording holds leaves it off the work instead of
raising. Each test runs the pipeline's own steps in its order: the scan's grouping, enrichment
pass 1's batch, then the step under test (#227's recording-lookup apply, pass 2, 3 or 4).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.catalog import Recording
from backend.domain.enums import EnrichmentStatus
from backend.services.library_enrichment_service import (
    apply_recording_lookup,
    enrich_by_recording,
    enrich_by_recording_batch,
    enrich_by_release,
)
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.services.untagged_recording_service import (
    PickOutcome,
    enrich_untagged_pair,
    untagged_pairs,
)
from tests.fakes.mb_client import FakeMbClient
from tests.integration.song_together_seed import (
    add_file,
    credit,
    enrichment_repos,
    group_like_the_scan,
    insert_local_work,
    recording_payload,
    recordings_on,
    stored,
    work_row,
    works_of_artist,
)

pytestmark = pytest.mark.integration

_U2 = "a3cb23fc-acd3-4ce0-8f36-1e5aa6a18432"
_GREEN_DAY = "084308bd-1654-436f-ba03-df6697104e19"
_OTHER_ARTIST = "00000000-0000-4000-8000-0000000e0009"
_WORK = "00000000-0000-4000-8000-0000000e0001"
_RECORDING = "00000000-0000-4000-8000-0000000e0002"
_RELEASE = "00000000-0000-4000-8000-0000000e0003"
_OTHER_RECORDING = "00000000-0000-4000-8000-0000000e0004"

_MB_TITLE = "Pride (In the Name of Love)"
_TAG_TITLE = "Pride (In The Name Of Love)"  # equal by the rule, spelled differently


def _pride_payload(*, work_title: str = _MB_TITLE, artist: str = _U2) -> dict[str, Any]:
    return recording_payload(
        _RECORDING,
        title=work_title,
        length=228_000,
        artist_credit=credit(artist, "U2" if artist == _U2 else "Joe Cocker"),
        work_mbid=_WORK,
        work_title=work_title,
        release_mbid=_RELEASE,
    )


def _link_and_apply(repos: RepositoryFactory, file_id: UUID, payload: dict[str, Any]) -> str:
    """Pass 1 links the tagged copy, then the recordings phase applies the lookup (#227)."""
    client = FakeMbClient(recordings={_RECORDING: payload})
    outcome = enrich_by_recording_batch([stored(repos, file_id)], enrichment_repos(repos), client)
    assert outcome.enriched == 1
    recording = repos.recordings.get_by_id(_RECORDING)
    assert recording is not None
    applied = apply_recording_lookup(recording, payload, enrichment_repos(repos))
    assert applied.work_id is not None
    return applied.work_id


def test_recording_lookup_gives_the_local_work_the_mbid_and_keeps_both_copies(
    migrated_db: str,
) -> None:
    """R1 through #227's apply step (and so its backfill script): T1."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        tagged = add_file(
            repos,
            "/music/U2/The Unforgettable Fire/03 Pride.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
            release_mbid=_RELEASE,
        )
        untagged = add_file(
            repos,
            "/music/U2/Best Of 1980-1990/02 Pride.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=229_000,
        )
        local = group_like_the_scan(repos, tagged)
        assert group_like_the_scan(repos, untagged) == local, "grouping: one song, one work"
        untagged_recording = stored(repos, untagged.id).recording_id
        before = work_row(conn, local)
        assert before is not None and before["mbid"] is None
        conn.commit()

        work_id = _link_and_apply(repos, tagged.id, _pride_payload())
        conn.commit()

        assert work_id == local, "the local work takes the MBID; no new work is made"
        assert stored(repos, tagged.id).work_id == local
        assert stored(repos, untagged.id).work_id == local, "the untagged copy stays with it"
        after = work_row(conn, local)
        assert after is not None
        assert after["mbid"] == _WORK
        assert after["origin"] == "musicbrainz"
        assert after["title"] == _MB_TITLE, "MusicBrainz's spelling of the title"
        assert after["needs_enhancement"] is True, "stamped by the works phase like a new one"
        assert after["artist_id"] == before["artist_id"]
        assert [w["id"] for w in works_of_artist(conn, before["artist_id"])] == [local]
        # Grouping's recording still holds the untagged copy and the original-version slot,
        # so the guarded link leaves the MusicBrainz recording off the work (C1).
        recording = repos.recordings.get_by_id(_RECORDING)
        assert recording is not None and recording.work_id is None
        assert stored(repos, untagged.id).recording_id == untagged_recording
        assert untagged_recording in recordings_on(conn, local)


def test_untagged_pass_gives_the_local_work_the_mbid_and_keeps_the_ambiguous_copy(
    migrated_db: str,
) -> None:
    """R1 through pass 4 (the untagged-file search): T2."""
    title = "Geek Stink Breath"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        # The local-artist linker gave the artist its MBID, which is what pass 4 needs.
        repos.artists.upsert_musicbrainz_artist(
            mbid=_GREEN_DAY,
            name="Green Day",
            sort_name="Green Day",
            normalized_name=normalize_artist("Green Day"),
        )
        album = add_file(
            repos,
            "/music/Green Day/Insomniac/05.flac",
            artist="Green Day",
            title=title,
            duration_ms=135_500,
        )
        other = add_file(
            repos,
            "/music/Green Day/Mixtape/11.flac",
            artist="Green Day",
            title=title,
            duration_ms=160_000,
        )
        local = group_like_the_scan(repos, album)
        assert group_like_the_scan(repos, other) == local
        other_recording = stored(repos, other.id).recording_id
        conn.commit()

        lookup = recording_payload(
            _RECORDING,
            title=title,
            length=135_000,
            artist_credit=credit(_GREEN_DAY, "Green Day"),
            work_mbid=_WORK,
            work_title=title,
        )
        client = FakeMbClient(
            recording_searches={(_GREEN_DAY, title): [lookup]},
            recordings={_RECORDING: lookup},
        )
        (pair,) = untagged_pairs(repos.library_files.list_untagged_with_linked_artist())
        outcome = enrich_untagged_pair(pair, enrichment_repos(repos), client)
        conn.commit()

        assert outcome.enriched == 1
        assert [s.outcome for s in outcome.skipped] == [PickOutcome.AMBIGUOUS]
        assert stored(repos, album.id).work_id == local
        assert stored(repos, album.id).recording_id == _RECORDING
        left = stored(repos, other.id)
        assert left.work_id == local, "the ambiguous copy stays with the song"
        assert left.enrichment_status is EnrichmentStatus.PENDING
        assert left.recording_id == other_recording
        row = work_row(conn, local)
        assert row is not None and row["mbid"] == _WORK and row["origin"] == "musicbrainz"
        # The ambiguous copy's grouping recording holds the slot: the MB recording stays off.
        recording = repos.recordings.get_by_id(_RECORDING)
        assert recording is not None and recording.work_id is None


def test_release_pass_gives_the_local_work_the_mbid(migrated_db: str) -> None:
    """R1 through pass 2 (one lookup per release): T3."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        tagged = add_file(
            repos,
            "/music/U2/UF/03.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
            release_mbid=_RELEASE,
        )
        untagged = add_file(
            repos, "/music/U2/Live/07.flac", artist="U2", title=_TAG_TITLE, duration_ms=229_000
        )
        local = group_like_the_scan(repos, tagged)
        assert group_like_the_scan(repos, untagged) == local
        conn.commit()

        track = recording_payload(
            _RECORDING,
            title=_MB_TITLE,
            length=228_000,
            artist_credit=credit(_U2, "U2"),
            work_mbid=_WORK,
            work_title=_MB_TITLE,
        )
        release = {
            "id": _RELEASE,
            "title": "The Unforgettable Fire",
            "artist-credit": credit(_U2, "U2"),
            "media": [{"tracks": [{"recording": track}]}],
        }
        enriched = enrich_by_release(
            _RELEASE, enrichment_repos(repos), FakeMbClient(releases={_RELEASE: release})
        )
        conn.commit()

        assert enriched == 1
        assert stored(repos, tagged.id).work_id == local
        assert stored(repos, untagged.id).work_id == local
        row = work_row(conn, local)
        assert row is not None and row["mbid"] == _WORK and row["title"] == _MB_TITLE
        assert [w["id"] for w in works_of_artist(conn, row["artist_id"])] == [local]


def test_a_lone_copy_frees_the_version_slot_and_its_recording_joins_the_work(
    migrated_db: str,
) -> None:
    """C1 through pass 3 (a file tagged with a recording but no release): T3b.

    With no other copy, grouping's recording is left without files once the copy links to
    its MusicBrainz recording; it goes, and the MusicBrainz recording takes the slot."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        lone = add_file(
            repos,
            "/music/U2/Single/01.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
        )
        local = group_like_the_scan(repos, lone)
        conn.commit()

        payload = recording_payload(
            _RECORDING,
            title=_MB_TITLE,
            length=228_000,
            artist_credit=credit(_U2, "U2"),
            work_mbid=_WORK,
            work_title=_MB_TITLE,
        )
        enriched = enrich_by_recording(
            _RECORDING, enrichment_repos(repos), FakeMbClient(recordings={_RECORDING: payload})
        )
        conn.commit()

        assert enriched == 1
        linked = stored(repos, lone.id)
        assert linked.work_id == local and linked.recording_id == _RECORDING
        row = work_row(conn, local)
        assert row is not None and row["mbid"] == _WORK
        assert recordings_on(conn, local) == [_RECORDING]


def test_an_existing_musicbrainz_work_takes_only_the_linked_copy(migrated_db: str) -> None:
    """No automatic merge: where a work already holds the MBID, today's per-file move stays
    and the local work keeps the other copy, for the merge list: T4."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        tagged = add_file(
            repos,
            "/music/U2/UF/03.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
            release_mbid=_RELEASE,
        )
        untagged = add_file(
            repos, "/music/U2/Best/02.flac", artist="U2", title=_TAG_TITLE, duration_ms=229_000
        )
        local = group_like_the_scan(repos, tagged)
        assert group_like_the_scan(repos, untagged) == local
        local_row = work_row(conn, local)
        assert local_row is not None
        # Another album's copy already made the MusicBrainz work.
        existing = repos.works.upsert_from_mb(
            mbid=_WORK, title=_MB_TITLE, artist_id=local_row["artist_id"]
        )
        conn.commit()

        work_id = _link_and_apply(repos, tagged.id, _pride_payload())
        conn.commit()

        assert work_id == existing
        assert stored(repos, tagged.id).work_id == existing
        assert stored(repos, untagged.id).work_id == local, "nothing is merged"
        after_local = work_row(conn, local)
        assert after_local is not None
        assert after_local["mbid"] is None and after_local["origin"] == "local"
        assert work_row(conn, existing) is not None


@pytest.mark.parametrize(
    ("tag_title", "mb_title"),
    [
        ("Pride", "Pride (In the Name of Love)"),
        ("Another Brick in the Wall, Part 1", "Another Brick in the Wall, Part 2"),
    ],
    ids=["near-title", "other-part"],
)
def test_no_claim_when_the_titles_differ_by_groupings_exact_rule(
    migrated_db: str, tag_title: str, mb_title: str
) -> None:
    """A near title or another part keeps today's path: a new MusicBrainz work: T5."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        lone = add_file(
            repos,
            "/music/a.flac",
            artist="U2",
            title=tag_title,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
            release_mbid=_RELEASE,
        )
        local = group_like_the_scan(repos, lone)
        conn.commit()

        work_id = _link_and_apply(repos, lone.id, _pride_payload(work_title=mb_title))
        conn.commit()

        assert work_id != local
        assert stored(repos, lone.id).work_id == work_id
        made = work_row(conn, work_id)
        assert made is not None and made["mbid"] == _WORK and made["title"] == mb_title
        kept = work_row(conn, local)
        assert kept is None or kept["mbid"] is None


@pytest.mark.parametrize(
    ("tag_title", "mb_title"),
    [
        ("Another Brick in the Wall, Pt. II", "Another Brick in the Wall (Part 2)"),
        ("dont stop believin", "Don’t Stop Believin’"),
        ("PRIDE (in the name of love)", "Pride (In the Name of Love)"),
    ],
    ids=["part-spelling", "punctuation", "case"],
)
def test_claim_when_the_titles_are_equal_by_groupings_exact_rule(
    migrated_db: str, tag_title: str, mb_title: str
) -> None:
    """Equal by grouping's exact path, spelled differently: the claim takes MB's title: T6."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        lone = add_file(
            repos,
            "/music/a.flac",
            artist="U2",
            title=tag_title,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
            release_mbid=_RELEASE,
        )
        local = group_like_the_scan(repos, lone)
        conn.commit()

        work_id = _link_and_apply(repos, lone.id, _pride_payload(work_title=mb_title))
        conn.commit()

        assert work_id == local
        row = work_row(conn, local)
        assert row is not None
        assert row["mbid"] == _WORK and row["origin"] == "musicbrainz"
        assert row["title"] == mb_title
        # A lone copy leaves grouping's recording without files; it goes, and the
        # MusicBrainz recording takes the version slot (C1, on the apply path too).
        assert recordings_on(conn, local) == [_RECORDING]


def test_no_claim_for_a_local_work_of_another_artist(migrated_db: str) -> None:
    """The work's artist must be the one the step gives the work (here the track's): T7."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        lone = add_file(
            repos,
            "/music/a.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
            release_mbid=_RELEASE,
        )
        local = group_like_the_scan(repos, lone)
        conn.commit()

        work_id = _link_and_apply(repos, lone.id, _pride_payload(artist=_OTHER_ARTIST))
        conn.commit()

        assert work_id != local
        made = work_row(conn, work_id)
        assert made is not None and made["mbid"] == _WORK
        kept = work_row(conn, local)
        assert kept is None or kept["mbid"] is None


def test_the_first_copy_by_id_claims_and_the_other_copy_joins_it(migrated_db: str) -> None:
    """Copies of one recording on two local works: the first file by id has its work claim
    the MBID; the other file then finds the MBID taken and moves onto it: T8.

    The claiming work has the higher work id, so the rule is the file's order, not the
    work's."""
    first_file = UUID("00000000-0000-4000-8000-0000000f0001")
    second_file = UUID("00000000-0000-4000-8000-0000000f0002")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("U2", normalize_artist("U2"))
        claiming = insert_local_work(
            conn, "00000000-0000-4000-8000-0000000f0019", _TAG_TITLE, artist_id
        )
        other = insert_local_work(
            conn, "00000000-0000-4000-8000-0000000f0018", _TAG_TITLE, artist_id
        )
        repos.recordings.upsert(Recording(id=_RECORDING, title=_MB_TITLE))
        for file_id, work_id, path in (
            (first_file, claiming, "/music/a.flac"),
            (second_file, other, "/music/b.flac"),
        ):
            add_file(
                repos,
                path,
                artist="U2",
                title=_TAG_TITLE,
                recording_mbid=_RECORDING,
                file_id=file_id,
            )
            repos.library_files.update_work_id(file_id, work_id)
            repos.library_files.update_recording_link(
                file_id, _RECORDING, EnrichmentStatus.ENRICHED
            )
        conn.commit()

        recording = repos.recordings.get_by_id(_RECORDING)
        assert recording is not None
        applied = apply_recording_lookup(recording, _pride_payload(), enrichment_repos(repos))
        conn.commit()

        assert applied.work_id == claiming
        assert stored(repos, first_file).work_id == claiming
        assert stored(repos, second_file).work_id == claiming
        row = work_row(conn, claiming)
        assert row is not None and row["mbid"] == _WORK
        assert work_row(conn, other) is None, "the emptied local work is deleted"


def test_a_held_version_slot_on_an_existing_work_does_not_stop_the_link(
    migrated_db: str,
) -> None:
    """C1 through pass 3: the work already holds another recording of the version, so the
    file links and joins the work, its recording stays off it, and nothing raises: T10."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_musicbrainz_artist(
            mbid=_U2, name="U2", sort_name="U2", normalized_name=normalize_artist("U2")
        )
        existing = repos.works.upsert_from_mb(mbid=_WORK, title=_MB_TITLE, artist_id=artist_id)
        repos.recordings.upsert(Recording(id=_OTHER_RECORDING, title=_MB_TITLE, work_id=existing))
        tagged = add_file(
            repos,
            "/music/U2/Single/01.flac",
            artist="U2",
            title=_TAG_TITLE,
            duration_ms=228_000,
            recording_mbid=_RECORDING,
        )
        conn.commit()

        payload = recording_payload(
            _RECORDING,
            title=_MB_TITLE,
            length=228_000,
            artist_credit=credit(_U2, "U2"),
            work_mbid=_WORK,
            work_title=_MB_TITLE,
        )
        enriched = enrich_by_recording(
            _RECORDING, enrichment_repos(repos), FakeMbClient(recordings={_RECORDING: payload})
        )
        conn.commit()

        assert enriched == 1
        linked = stored(repos, tagged.id)
        assert linked.enrichment_status is EnrichmentStatus.ENRICHED
        assert linked.recording_id == _RECORDING
        assert linked.work_id == existing
        recording = repos.recordings.get_by_id(_RECORDING)
        assert recording is not None and recording.work_id is None
        assert recordings_on(conn, existing) == [_OTHER_RECORDING]

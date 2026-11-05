"""Artist names, phase 2a PR 1 (SPEC S5, S6, S7, S13, S14's names half, S9 on the real tables; C41,
M18, M33, D2, D5, D6, D7; AUD-R041, AUD-R072), on PostgreSQL with real scans.

S5: names are unique only without an MBID. 0047 makes ``idx_artists_norm_name`` partial, adds a
plain index for the readers of a name, and creates ``library_suggestions``.
``get_by_normalized_name`` answers for the name-only artist.

S6: ``upsert_musicbrainz_artist``. Found by MBID, that artist. Otherwise the name's MBIDs decide
(the lowercased, well-formed, non-placeholder values of present files whose ``artist_mbid`` holds
exactly one MBID): none or only this one promotes the name-only artist in place, anything else
inserts. It never returns another MBID's artist, and ``_resolve`` reads ahead by the same rule.

D7: a match that would promote a rejected artist stays refused. An automatic match to a different
artist with the normalized name of an artist the log name rejected, name-only or holding an MBID,
becomes a suggestion in ``plan_answer``, and the name stays in review.

S7: ``upsert_local_artist`` counts the artists of the name first. None, a new name-only artist.
One on a name holding at most one MBID, that artist. Several, or a name holding several MBIDs,
the single name-only artist, created if absent. A fuzzy match runs first and still joins its work.

S13: a stub MusicBrainz names takes its normalized name whoever holds it. A name-only holder
stays as it is, and the name gets one ``different_acts`` row. No link moves.

S14: the sweep, last in every scan. Each MBID of a name holding several gets an artist, with the
spelling most of its files carry, and the name gets one ``shared_name`` row holding the MBIDs and
the shared titles. It reads ``library_files``, so a re-tag, a tag in a later scan and a fuzzy join
all reach it. Works stay where grouping put them until 3b. A repeat updates a row and never its
``state``. Each name is its own transaction. Any other single-MBID file gets no artist (D6).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg.rows import DictRow, dict_row
from structlog.testing import capture_logs

from backend.config import get_settings
from backend.db.repositories.artist_tag_files import PgArtistTagFilesRepository
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.library_suggestions import PgLibrarySuggestionRepository
from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import (
    ArtistLinkOutcome,
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    ReasonCode,
)
from backend.domain.library import PURGE_MISSING_SETTING
from backend.domain.log_name_status import NameState, suggestion_detail
from backend.domain.matching import LogNameLink
from backend.domain.system import UserSetting
from backend.services.artist_linking_service import link_artist
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    ArtistMatchResult,
    hold_back_refused_link,
    match_artists_for_playlist,
    matcher_answer,
)
from backend.services.log_name_link_service import (
    AnswerPlan,
    LinkTarget,
    LogNameLinkService,
    MatcherAnswer,
)
from backend.services.matching_constants import MAX_ARTIST_SUGGESTIONS
from backend.services.mb_types import MbArtistResult
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from backend.services.repository_factory import RepositoryFactory
from backend.tasks import huey_app, library_scan_tasks
from backend.tasks.artist_identity_tasks import artist_identity_sweep_task, sweep_artist_identities
from backend.tasks.library_watcher_tasks import library_scan_files_task
from backend.tasks.mb_enrichment_tasks import ArtistEnhanceOutcome, _enhance_artist
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.mb_client import FakeMbClient
from tests.integration.missing_file_seed import full_scan, put
from tests.matching_seed import seed_pg_link

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

_MIGRATIONS = Path(__file__).resolve().parents[2] / "backend" / "db" / "migrations"

BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"
SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"
THIRD = "0a000000-0000-4000-8000-0000000000c3"
OTHER = "0a000000-0000-4000-8000-0000000000c4"
SOLO = "0a000000-0000-4000-8000-0000000000c5"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
ALICE = "alice cooper"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def reader(migrated_db: str) -> Iterator[Conn]:
    """A second connection, which sees only what was committed."""
    with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as connection:
        yield connection


@pytest.fixture
def task_database(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The settings name the test database, so a task's own connections use it; whatever it
    hands off stays off the queue."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def tagged(dest: Path, artist: str, title: str, artist_mbid: str | None = None) -> None:
    """A copy of the fixture MP3 tagged with *artist*, *title* and one MBID tag value."""
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    if artist_mbid:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=[artist_mbid]))
    tags.save()


def _file(
    conn: Conn,
    artist_name: str,
    *,
    tag: str | None = None,
    title: str | None = "Some Song",
    present: bool = True,
) -> None:
    """A ``library_files`` row with no work, for the rules a real tag can't reach."""
    conn.execute(
        """INSERT INTO library_files
               (file_path, format, track_title, artist_name, normalized_artist_name,
                artist_mbid, file_status, missing_since)
           VALUES (%s, 'flac', %s, %s, %s, %s, %s, %s)""",
        (
            f"/music/{uuid4().hex}.flac",
            title,
            artist_name,
            normalize_artist(artist_name),
            tag,
            "present" if present else "missing",
            None if present else "2026-10-01T00:00:00Z",
        ),
    )


def _artists(conn: Conn, normalized_name: str = ALICE) -> list[tuple[str | None, str, int]]:
    """The artists of the name, name-only first: (MBID, origin, works)."""
    rows = conn.execute(
        """SELECT a.mbid, a.origin, count(w.id) AS works
             FROM artists a LEFT JOIN works w ON w.artist_id = a.id
            WHERE a.normalized_name = %s
            GROUP BY a.id
            ORDER BY a.mbid NULLS FIRST""",
        (normalized_name,),
    ).fetchall()
    return [(r["mbid"], r["origin"], r["works"]) for r in rows]


def _suggestions(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT normalized_name, reason, state, candidates, evidence, library_file_id
             FROM library_suggestions
            ORDER BY normalized_name, reason"""
    ).fetchall()
    return [dict(r) for r in rows]


def _shared_name_row(conn: Conn, normalized_name: str = ALICE) -> dict[str, Any]:
    [row] = [
        r
        for r in _suggestions(conn)
        if (r["normalized_name"], r["reason"]) == (normalized_name, "shared_name")
    ]
    return row


def _works_by_file(conn: Conn) -> dict[str, str]:
    rows = conn.execute("SELECT file_path, work_id FROM library_files").fetchall()
    return {PureWindowsPath(r["file_path"]).name: str(r["work_id"]) for r in rows}


def _artist_of_work(conn: Conn, work_id: str) -> str:
    row = conn.execute("SELECT artist_id FROM works WHERE id = %s", (work_id,)).fetchone()
    assert row is not None
    return str(row["artist_id"])


def _insert_artist(conn: Conn, name: str, mbid: str | None) -> str:
    row = conn.execute(
        """INSERT INTO artists
               (id, name, sort_name, normalized_name, mbid, origin, needs_enhancement)
           VALUES (gen_random_uuid()::text, %(name)s, %(name)s, %(norm)s, %(mbid)s, %(origin)s,
                   FALSE)
           RETURNING id""",
        {
            "name": name,
            "norm": normalize_artist(name),
            "mbid": mbid,
            "origin": "musicbrainz" if mbid else "local",
        },
    ).fetchone()
    assert row is not None
    return str(row["id"])


# --- S5: the indexes ------------------------------------------------------------------------------


def test_the_schema_after_0047(conn: Conn) -> None:
    """One partial unique index for name-only artists, and one plain index, which ``works.py``'s
    candidate join and S7's count need because a partial index can't serve them. The swap sets
    its lock timeout first and analyzes the table (C45)."""
    applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version LIKE '0047\\_%'"
    ).fetchone()
    assert applied is not None
    [migration] = _MIGRATIONS.glob("0047_*.sql")
    statements = [
        line
        for line in migration.read_text(encoding="utf-8").upper().splitlines()
        if not line.lstrip().startswith("--")
    ]
    assert statements[0].startswith("SET LOCAL LOCK_TIMEOUT")
    assert any(line.startswith("ANALYZE ARTISTS") for line in statements)
    rows = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'artists'"
        " AND indexdef LIKE '%(normalized_name)%'"
    ).fetchall()
    definitions = [r["indexdef"] for r in rows]
    [unique] = [d for d in definitions if "UNIQUE" in d]
    [plain] = [d for d in definitions if "UNIQUE" not in d]
    assert "idx_artists_norm_name" in unique
    assert unique.endswith("WHERE (mbid IS NULL)")
    assert "WHERE" not in plain


def test_names_are_unique_only_without_an_mbid(conn: Conn) -> None:
    """Two MBID artists and one name-only artist share a name. A second name-only one is
    refused, and so is a second artist of one MBID."""
    _insert_artist(conn, "Alice Cooper", BAND)
    _insert_artist(conn, "Alice Cooper", SINGER)
    _insert_artist(conn, "Alice Cooper", None)

    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert_artist(conn, "Alice Cooper", None)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert_artist(conn, "Alice Cooper", BAND)


def test_the_local_upsert_finds_its_own_artist(conn: Conn) -> None:
    repo = PgArtistRepository(conn)

    first = repo.upsert_local_artist("Alice Cooper", ALICE)
    again = repo.upsert_local_artist("ALICE COOPER", ALICE)

    assert first == again
    assert _artists(conn) == [(None, "local", 0)]


def test_get_by_normalized_name_answers_for_the_name_only_artist(conn: Conn) -> None:
    repo = PgArtistRepository(conn)
    _insert_artist(conn, "Alice Cooper", BAND)
    assert repo.get_by_normalized_name(ALICE) is None

    name_only = _insert_artist(conn, "Alice Cooper", None)
    _insert_artist(conn, "Alice Cooper", SINGER)

    found = repo.get_by_normalized_name(ALICE)
    assert found is not None
    assert (found.id, found.mbid) == (name_only, None)


def test_the_candidate_join_sees_every_artist_of_the_name(conn: Conn) -> None:
    """``get_candidates_by_normalized_artist`` joins on the name, so a fuzzy match finds a work
    whichever artist of the name holds it (M18)."""
    works = PgWorkRepository(conn)
    expected = {
        works.create_local("Poison", _insert_artist(conn, "Alice Cooper", BAND)),
        works.create_local("Elected", _insert_artist(conn, "Alice Cooper", SINGER)),
        works.create_local("Hey Stoopid", _insert_artist(conn, "Alice Cooper", None)),
    }

    candidates = works.get_candidates_by_normalized_artist(ALICE)

    assert {work_id for work_id, _ in candidates} == expected


# --- S6: the MusicBrainz upsert -------------------------------------------------------------------


def _upsert(conn: Conn, mbid: str, name: str = "Alice Cooper") -> str:
    return PgArtistRepository(conn).upsert_musicbrainz_artist(
        mbid=mbid, name=name, sort_name=name, normalized_name=normalize_artist(name)
    )


def test_two_alice_coopers_stay_two(conn: Conn) -> None:
    """The name holds both MBIDs. Neither upsert promotes the name-only artist or returns the
    other MBID's artist, and a repeat finds its own by MBID."""
    name_only = PgArtistRepository(conn).upsert_local_artist("Alice Cooper", ALICE)
    _file(conn, "Alice Cooper", tag=BAND)
    _file(conn, "Alice Cooper", tag=SINGER)

    band = _upsert(conn, BAND)
    singer = _upsert(conn, SINGER)

    assert len({name_only, band, singer}) == 3
    assert (_upsert(conn, BAND), _upsert(conn, SINGER)) == (band, singer)
    assert [mbid for mbid, _, _ in _artists(conn)] == [None, BAND, SINGER]


def test_a_writer_that_gets_there_first_is_returned(
    conn: Conn, reader: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5: ``INSERT ... ON CONFLICT (mbid) DO NOTHING``, then the artist that holds it. Another
    connection commits the same MBID after this upsert has looked for it."""
    won: list[str] = []

    def another_writer_wins(_self: PgArtistRepository, _name: str) -> set[str]:
        won.append(_insert_artist(reader, "Alice Cooper", BAND))
        return {OTHER}

    monkeypatch.setattr(PgArtistRepository, "name_mbids", another_writer_wins)

    assert _upsert(conn, BAND) == won[0]


def test_no_tag_promotes_though_another_mbid_artist_holds_the_name(conn: Conn) -> None:
    """S6 and D5 as written: only the name's MBIDs decide, and an artist of the name is no tag.
    So the name-only artist beside an MBID artist takes the hit's MBID."""
    name_only = _insert_artist(conn, "Alice Cooper", None)
    _insert_artist(conn, "Alice Cooper", BAND)

    assert _upsert(conn, SINGER) == name_only
    assert [mbid for mbid, _, _ in _artists(conn)] == [BAND, SINGER]


def test_the_names_mbids_are_the_single_values_of_present_files(conn: Conn) -> None:
    """S6's reading, the count behind M33's 17: lowercased and well-formed. A joined value, a
    placeholder, a malformed value, a missing file and an untagged file don't count."""
    _file(conn, "Mixed", tag=BAND)
    _file(conn, "Mixed", tag=BAND.upper())
    _file(conn, "Mixed", tag=f"{BAND}, {SINGER}")
    _file(conn, "Mixed", tag=UNKNOWN)
    _file(conn, "Mixed", tag="artist-mbid-0001")
    _file(conn, "Mixed", tag=THIRD, present=False)
    _file(conn, "Mixed")
    _file(conn, "Someone Else", tag=OTHER)

    assert PgArtistRepository(conn).name_mbids("mixed") == {BAND}
    assert PgArtistRepository(conn).name_mbids("nobody") == set()


@pytest.mark.parametrize(
    ("tags", "promotes"),
    [((), True), ((SOLO,), True), ((OTHER,), False), ((SOLO, OTHER), False)],
    ids=["no tags", "only this MBID", "another MBID", "several"],
)
def test_resolve_reads_ahead_as_the_upsert_will(
    conn: Conn, tags: tuple[str, ...], promotes: bool
) -> None:
    """A MusicBrainz hit for a name-only artist's name, with the pair rejected. While the upsert
    would promote that artist the pair is refused. When the name's MBIDs say the upsert will
    insert, the hit is a new artist and no rejected pair (S6). Then the upsert does what was
    read."""
    catalog = PgArtistRepository(conn)
    holder = catalog.upsert_local_artist("Ozzy Osbourne", "ozzy osbourne")
    names = RepositoryFactory(conn).broadcast_artists
    name = names.upsert(
        BroadcastArtist(
            id=uuid4(),
            original_name="OZZY OSBOURNE THE METAL LEGEND",
            normalized_name="ozzy osbourne the metal legend",
        )
    )
    seed_pg_link(
        conn,
        name.id,
        holder,
        origin=LogNameLinkOrigin.MANUAL,
        state=LogNameLinkState.REJECTED,
    )
    service = LogNameLinkService(PgLogNameLinkRepository(conn), catalog, names)
    answer = MatcherAnswer(
        proposed=NameState(MatchStatus.AUTO_MATCHED),
        link=LinkTarget(
            target=SOLO,
            tier=MatchTier.MUSICBRAINZ_API,
            score=100.0,
            mb_candidate={"id": SOLO, "name": "Ozzy Osbourne", "score": 100},
        ),
    )

    for tag in tags:
        _file(conn, "Ozzy Osbourne", tag=tag)

    assert set(service.rejected_in(name.id, answer)) == ({SOLO} if promotes else set())
    assert (_upsert(conn, SOLO, "Ozzy Osbourne") == holder) is promotes


# --- S6, D7: a match on a name the log name rejected ----------------------------------------------

_TRUNCATED = "ALICE COOPER AND HIS FAMOUS BA"  # cut off at the field's width, so it reaches MB
_REVIEW_100 = NameState(
    MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, suggestion_detail([100])
)


def _log_name(conn: Conn, spelling: str = _TRUNCATED) -> BroadcastArtist:
    return RepositoryFactory(conn).broadcast_artists.upsert(
        BroadcastArtist(
            id=uuid4(), original_name=spelling, normalized_name=normalize_artist(spelling)
        )
    )


def _reject(conn: Conn, name: BroadcastArtist, artist_id: str) -> None:
    seed_pg_link(
        conn,
        name.id,
        artist_id,
        origin=LogNameLinkOrigin.MANUAL,
        state=LogNameLinkState.REJECTED,
    )


def _suggest(conn: Conn, name: BroadcastArtist, artist_id: str, strength: int = 60) -> None:
    seed_pg_link(
        conn,
        name.id,
        artist_id,
        origin=LogNameLinkOrigin.AUTOMATIC,
        state=LogNameLinkState.SUGGESTED,
        strength=strength,
    )


def _service(conn: Conn) -> LogNameLinkService:
    return LogNameLinkService(
        PgLogNameLinkRepository(conn),
        PgArtistRepository(conn),
        RepositoryFactory(conn).broadcast_artists,
    )


def _hit(name: str = "Alice Cooper", mbid: str = SOLO) -> MbArtistResult:
    return {"id": mbid, "name": name, "score": 100}


def _hit_answer(name: str = "Alice Cooper", mbid: str = SOLO) -> MatcherAnswer:
    return MatcherAnswer(
        proposed=NameState(MatchStatus.AUTO_MATCHED),
        link=LinkTarget(
            target=mbid,
            tier=MatchTier.MUSICBRAINZ_API,
            score=100.0,
            mb_candidate=_hit(name, mbid),
        ),
    )


def _catalog_answer(target: str, tier: MatchTier = MatchTier.NORMALIZATION) -> MatcherAnswer:
    return MatcherAnswer(
        proposed=NameState(MatchStatus.AUTO_MATCHED),
        link=LinkTarget(target=target, tier=tier, score=100.0),
    )


def _status(conn: Conn, name: BroadcastArtist) -> NameState:
    found = RepositoryFactory(conn).broadcast_artists.get_by_id(name.id)
    assert found is not None
    return NameState(found.match_status, found.reason_code, found.reason_detail)


def _link_states(conn: Conn, name: BroadcastArtist) -> dict[str | None, str]:
    """Each link of the log name: its artist's MBID (None for a name-only artist), its state."""
    rows = conn.execute(
        """SELECT a.mbid, l.state
             FROM log_name_links l JOIN artists a ON a.id = l.artist_id
            WHERE l.broadcast_artist_id = %s""",
        (name.id,),
    ).fetchall()
    return {r["mbid"]: r["state"] for r in rows}


def _rejected_links(conn: Conn, name: BroadcastArtist) -> list[LogNameLink]:
    return [
        link
        for link in PgLogNameLinkRepository(conn).list_by_name(name.id)
        if link.state == LogNameLinkState.REJECTED
    ]


def _artist_row(conn: Conn, artist_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM artists WHERE id = %s", (artist_id,)).fetchone()
    assert row is not None
    return dict(row)


def _writes(plan: AnswerPlan) -> list[tuple[str, LogNameLinkState]]:
    return [(target.target, state) for target, state in plan.writes]


@pytest.mark.parametrize(
    ("rejected_mbid", "name_only_beside_it", "tags"),
    [
        (None, False, (OTHER,)),
        (None, False, (SOLO, OTHER)),
        (BAND, False, ()),
        (BAND, False, (BAND,)),
        (BAND, False, (BAND, SOLO)),
        (BAND, True, ()),
    ],
    ids=[
        "name-only, another MBID",
        "name-only, several",
        "holds an MBID, no tags",
        "holds an MBID, its own tag",
        "holds an MBID, several",
        "holds an MBID, beside the name-only artist the upsert would promote",
    ],
)
def test_match_on_a_rejected_name_is_a_suggestion(
    conn: Conn, rejected_mbid: str | None, name_only_beside_it: bool, tags: tuple[str, ...]
) -> None:
    """D7. The log name rejected an artist of this name, and a 95+ MusicBrainz hit names a
    different one. Nothing refuses it, because the pair isn't rejected. To Lance an auto-link
    would read as the name linked again after he rejected it, so the plan writes a suggestion and
    the name stays in review. The rejected pair and every artist of the name stay as they were:
    an unconfirmed suggestion never gives a local artist an MBID."""
    rejected = _insert_artist(conn, "Alice Cooper", rejected_mbid)
    others = [_insert_artist(conn, "Alice Cooper", None)] if name_only_beside_it else []
    for tag in tags:
        _file(conn, "Alice Cooper", tag=tag)
    name = _log_name(conn)
    _reject(conn, name, rejected)
    conn.commit()
    service = _service(conn)
    answer = _hit_answer()
    pairs = _rejected_links(conn, name)
    artists = [_artist_row(conn, artist) for artist in (rejected, *others)]

    assert service.rejected_in(name.id, answer) == {}
    plan = service.plan_answer(name.id, answer)

    assert plan.state == _REVIEW_100
    assert _writes(plan) == [(SOLO, LogNameLinkState.SUGGESTED)]

    assert service.write_plan(name.id, plan)

    assert _status(conn, name) == _REVIEW_100
    assert _link_states(conn, name) == {rejected_mbid: "rejected", SOLO: "suggested"}
    assert _rejected_links(conn, name) == pairs
    assert [_artist_row(conn, artist) for artist in (rejected, *others)] == artists
    [suggested] = [
        link
        for link in PgLogNameLinkRepository(conn).list_by_name(name.id)
        if link.state == LogNameLinkState.SUGGESTED
    ]
    assert (suggested.origin, suggested.strength) == (LogNameLinkOrigin.AUTOMATIC, 100)
    assert suggested.evidence["target_name"] == "Alice Cooper"


@pytest.mark.parametrize(
    ("reject", "spelling", "seed_mbid", "rejected_mbid", "tags", "score"),
    [
        (True, "ALICE COOPER", SOLO, None, (), 100),
        (True, "ALICE COOPERS", SOLO, BAND, (), 96),
        (True, _TRUNCATED, None, None, (OTHER,), 100),
        (False, "ALICE COOPER", SOLO, None, (), 100),
        (False, "ALICE COOPERS", SOLO, BAND, (), 96),
        (False, _TRUNCATED, None, None, (OTHER,), 100),
    ],
    ids=[
        "exact name, one MBID artist beside the name-only one",
        "fuzzy, two MBID artists tied",
        "MusicBrainz hit",
        "no rejected pair: exact name",
        "no rejected pair: fuzzy",
        "no rejected pair: MusicBrainz hit",
    ],
)
def test_a_matcher_run_suggests_on_a_rejected_name(
    conn: Conn,
    reject: bool,
    spelling: str,
    seed_mbid: str | None,
    rejected_mbid: str | None,
    tags: tuple[str, ...],
    score: int,
) -> None:
    """The whole run. S5 makes a catalog target with no MusicBrainz hit reachable: two artists of
    a name, where the exact pass links the one MBID artist, and a fuzzy tie goes to the lowest
    MBID (SOLO's here). Pass 2 can't reach the rule, because its one name-only artist would be
    the rejected artist itself. Without a rejected pair each of these links, as it does today."""
    holder = _insert_artist(conn, "Alice Cooper", rejected_mbid)
    if seed_mbid is not None:
        _insert_artist(conn, "Alice Cooper", seed_mbid)
    for tag in tags:
        _file(conn, "Alice Cooper", tag=tag)
    factory = RepositoryFactory(conn)
    station = factory.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KRSC"))
    playlist = factory.broadcast_playlists.create(
        BroadcastPlaylist(
            id=uuid4(), name="show.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )
    name = _log_name(conn, spelling)
    song = factory.broadcast_identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=name.id,
            original_title="Song",
            normalized_title="song",
            normalized_signature=compute_normalized_signature(name.normalized_name, "song"),
        )
    )
    factory.broadcast_events.create(
        BroadcastPlayEvent(
            id=uuid4(),
            identity_id=song.id,
            playlist_id=playlist.id,
            played_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    )
    if reject:
        _reject(conn, name, holder)
    holder_before = _artist_row(conn, holder)

    match_artists_for_playlist(
        playlist_id=playlist.id,
        repos=ArtistMatchingRepos(
            broadcast_artist_repo=factory.broadcast_artists,
            track_identity_repo=factory.broadcast_identities,
            artist_repo=factory.artists,
            match_repo=factory.matches,
            rules_repo=FakeMappingRuleRepository(),
            link_repo=PgLogNameLinkRepository(conn),
        ),
        mb_client=FakeMbClient(responses={_TRUNCATED: [dict(_hit())]}),
    )

    assert _artist_row(conn, holder) == holder_before
    if reject:
        assert _status(conn, name) == NameState(
            MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, suggestion_detail([score])
        )
        assert _link_states(conn, name) == {rejected_mbid: "rejected", SOLO: "suggested"}
    else:
        assert _status(conn, name) == NameState(MatchStatus.AUTO_MATCHED)
        assert _link_states(conn, name) == {SOLO: "active"}


@pytest.mark.parametrize(
    "target_seed",
    ["its artist", "its stub", "another spelling"],
)
def test_a_hit_is_compared_by_its_normalized_name(conn: Conn, target_seed: str) -> None:
    """The hit's MBID may already have an artist, or a stub with no name yet, which only the hit
    can name. A hit spelled otherwise normalizes to the rejected name."""
    holder = _insert_artist(conn, "Alice Cooper", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    if target_seed == "its artist":
        _insert_artist(conn, "Alice Cooper", SOLO)
    if target_seed == "its stub":
        PgArtistRepository(conn).create_stub(SOLO)
    name = _log_name(conn)
    _reject(conn, name, holder)
    service = _service(conn)
    spelling = "The Alice Cooper" if target_seed == "another spelling" else "Alice Cooper"

    plan = service.plan_answer(name.id, _hit_answer(spelling))

    assert plan.state == _REVIEW_100
    assert _writes(plan) == [(SOLO, LogNameLinkState.SUGGESTED)]
    assert service.write_plan(name.id, plan)
    assert _link_states(conn, name) == {None: "rejected", SOLO: "suggested"}


@pytest.mark.parametrize("target_by", ["MBID", "artist id"])
def test_a_catalog_target_is_compared_by_its_name(conn: Conn, target_by: str) -> None:
    """No hit, so the catalog artist's own name decides, whichever way the answer names it."""
    rejected = _insert_artist(conn, "Alice Cooper", None)
    target = _insert_artist(conn, "Alice Cooper", SOLO)
    name = _log_name(conn)
    _reject(conn, name, rejected)
    service = _service(conn)
    answer = _catalog_answer(SOLO if target_by == "MBID" else target)

    plan = service.plan_answer(name.id, answer)

    assert plan.state == _REVIEW_100
    assert [state for _, state in plan.writes] == [LogNameLinkState.SUGGESTED]
    assert service.write_plan(name.id, plan)
    assert _link_states(conn, name) == {None: "rejected", SOLO: "suggested"}


def test_a_mapping_rule_on_a_rejected_name_is_a_suggestion_too(conn: Conn) -> None:
    """A rule is an automatic answer in the plan's eyes (origin automatic, tier manual). The name
    goes to review, which is the side Lance's rule allows: it widens his review window."""
    rejected = _insert_artist(conn, "Alice Cooper", None)
    target = _insert_artist(conn, "Alice Cooper", SOLO)
    name = _log_name(conn)
    _reject(conn, name, rejected)

    plan = _service(conn).plan_answer(name.id, _catalog_answer(target, MatchTier.MANUAL))

    assert plan.state == _REVIEW_100
    assert [state for _, state in plan.writes] == [LogNameLinkState.SUGGESTED]


def test_any_rejected_pair_protects_its_name(conn: Conn) -> None:
    """The name's second rejected pair counts as its first does."""
    someone = _insert_artist(conn, "Someone Else", None)
    alice = _insert_artist(conn, "Alice Cooper", None)
    zappa = _insert_artist(conn, "Zappa", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    name = _log_name(conn)
    for artist in (someone, alice, zappa):
        _reject(conn, name, artist)

    plan = _service(conn).plan_answer(name.id, _hit_answer())

    assert plan.state == _REVIEW_100
    assert _writes(plan) == [(SOLO, LogNameLinkState.SUGGESTED)]


def test_a_pair_already_suggested_stays_suggested(conn: Conn) -> None:
    """The hit would lift a suggestion to active. On a rejected name it writes nothing, the
    suggestion keeps its strength, and the name stays in review."""
    rejected = _insert_artist(conn, "Alice Cooper", None)
    target = _insert_artist(conn, "Alice Cooper", SOLO)
    name = _log_name(conn)
    _reject(conn, name, rejected)
    _suggest(conn, name, target, strength=60)

    plan = _service(conn).plan_answer(name.id, _hit_answer())

    assert plan.writes == ()
    assert plan.state == NameState(
        MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, suggestion_detail([60])
    )


def test_a_full_list_of_suggestions_still_gets_no_active_link(conn: Conn) -> None:
    """AUD-R038's cap holds: with no room the hit is dropped, never linked, and the name stays in
    review on the suggestions it holds."""
    rejected = _insert_artist(conn, "Alice Cooper", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    name = _log_name(conn)
    _reject(conn, name, rejected)
    for number in range(MAX_ARTIST_SUGGESTIONS):
        _suggest(conn, name, _insert_artist(conn, f"Act {number}", None))

    plan = _service(conn).plan_answer(name.id, _hit_answer())

    assert plan.writes == ()
    assert plan.state == NameState(
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LINK_SUGGESTED,
        suggestion_detail([60] * MAX_ARTIST_SUGGESTIONS),
    )


@pytest.mark.parametrize(
    ("rejected_mbid", "tags", "with_hit"),
    [(None, (), True), (None, (SOLO,), True), (SOLO, (), True), (SOLO, (), False)],
    ids=[
        "the upsert would promote it, no tags",
        "the upsert would promote it, only this MBID",
        "it holds the hit's MBID",
        "it is the catalog target",
    ],
)
def test_a_match_to_the_rejected_artist_itself_stays_refused(
    conn: Conn, rejected_mbid: str | None, tags: tuple[str, ...], with_hit: bool
) -> None:
    """D7 leaves this as it was: the rejected artist is never suggested back. The matcher's
    answer is held back with LINK_REJECTED, no plan writes a link, nothing is promoted and no
    artist or stub is made for the MBID."""
    rejected = _insert_artist(conn, "Alice Cooper", rejected_mbid)
    for tag in tags:
        _file(conn, "Alice Cooper", tag=tag)
    name = _log_name(conn)
    _reject(conn, name, rejected)
    conn.commit()
    service = _service(conn)
    result = ArtistMatchResult(
        status=MatchStatus.AUTO_MATCHED,
        tier=MatchTier.MUSICBRAINZ_API if with_hit else MatchTier.NORMALIZATION,
        confidence_score=100.0,
        target_id=SOLO,
        mb_candidate=_hit() if with_hit else None,
    )
    artist = _artist_row(conn, rejected)

    assert service.plan_answer(name.id, matcher_answer(result)).writes == ()

    held = hold_back_refused_link(name, result, service)
    assert (held.status, held.reason_code) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_REJECTED)
    plan = service.plan_answer(name.id, matcher_answer(held))
    assert plan.writes == ()
    assert service.write_plan(name.id, plan)

    assert _status(conn, name).reason_code == ReasonCode.LINK_REJECTED
    assert _link_states(conn, name) == {rejected_mbid: "rejected"}
    assert _artist_row(conn, rejected) == artist
    assert len(_artists(conn)) == 1


# The controls: each of these is a hit no rejected pair of its name stands against.


def _assert_links_automatically(conn: Conn, name: BroadcastArtist, answer: MatcherAnswer) -> None:
    service = _service(conn)

    plan = service.plan_answer(name.id, answer)

    assert plan.state == NameState(MatchStatus.AUTO_MATCHED)
    assert _writes(plan) == [(SOLO, LogNameLinkState.ACTIVE)]
    assert service.write_plan(name.id, plan)
    assert _status(conn, name) == NameState(MatchStatus.AUTO_MATCHED)
    assert _link_states(conn, name)[SOLO] == "active"


@pytest.mark.parametrize(
    ("tags", "promotes"),
    [((), True), ((SOLO,), True), ((OTHER,), False), ((SOLO, OTHER), False)],
    ids=["no tags", "only this MBID", "another MBID", "several"],
)
def test_with_no_rejected_pair_a_hit_links_as_s6_reads(
    conn: Conn, tags: tuple[str, ...], promotes: bool
) -> None:
    holder = _insert_artist(conn, "Alice Cooper", None)
    for tag in tags:
        _file(conn, "Alice Cooper", tag=tag)
    name = _log_name(conn)

    _assert_links_automatically(conn, name, _hit_answer())

    assert _link_states(conn, name) == {SOLO: "active"}
    assert (_artist_row(conn, holder)["mbid"] == SOLO) is promotes


def test_a_rejected_artist_of_another_name_protects_nothing(conn: Conn) -> None:
    holder = _insert_artist(conn, "Alice Cooper", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    name = _log_name(conn)
    _reject(conn, name, _insert_artist(conn, "Someone Else", None))

    _assert_links_automatically(conn, name, _hit_answer())

    assert _artist_row(conn, holder)["mbid"] is None


def test_another_log_names_rejection_protects_nothing(conn: Conn) -> None:
    holder = _insert_artist(conn, "Alice Cooper", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    name = _log_name(conn)
    _reject(conn, _log_name(conn, "ALICE COOPER BAND"), holder)

    _assert_links_automatically(conn, name, _hit_answer())


def test_a_suggested_pair_with_the_holder_is_no_rejection(conn: Conn) -> None:
    holder = _insert_artist(conn, "Alice Cooper", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    name = _log_name(conn)
    _suggest(conn, name, holder)

    _assert_links_automatically(conn, name, _hit_answer())

    assert _link_states(conn, name) == {None: "suggested", SOLO: "active"}


def test_a_name_that_normalizes_to_nothing_protects_nothing(conn: Conn) -> None:
    """Two symbol-only names both normalize to nothing, which is no name they share."""
    name = _log_name(conn)
    _reject(conn, name, _insert_artist(conn, "???", BAND))

    _assert_links_automatically(conn, name, _hit_answer("!!!"))


def test_stubs_with_no_name_yet_protect_nothing(conn: Conn) -> None:
    """A rejected stub has no normalized name until enrichment names it, and neither has the
    target: no name is no name they share."""
    catalog = PgArtistRepository(conn)
    name = _log_name(conn)
    _reject(conn, name, catalog.create_stub(BAND).id)
    catalog.create_stub(SOLO)

    _assert_links_automatically(conn, name, _catalog_answer(SOLO))


def test_a_suggestion_of_the_matchers_stays_one(conn: Conn) -> None:
    """Below the auto-link score the answer was a suggestion before D7, on any name."""
    rejected = _insert_artist(conn, "Alice Cooper", None)
    _file(conn, "Alice Cooper", tag=OTHER)
    name = _log_name(conn)
    _reject(conn, name, rejected)
    answer = MatcherAnswer(
        proposed=NameState(MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, "from the matcher"),
        suggestions=(
            LinkTarget(
                target=SOLO, tier=MatchTier.MUSICBRAINZ_API, score=80.0, mb_candidate=_hit()
            ),
        ),
    )

    plan = _service(conn).plan_answer(name.id, answer)

    assert _writes(plan) == [(SOLO, LogNameLinkState.SUGGESTED)]
    assert plan.state == NameState(
        MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, suggestion_detail([80])
    )


# --- S7: the local upsert -------------------------------------------------------------------------


def _alice_tags(conn: Conn, *mbids: str) -> None:
    for mbid in mbids:
        _file(conn, "Alice Cooper", tag=mbid)


@pytest.mark.parametrize(
    "tags",
    [(), (BAND,), (OTHER,), (BAND, f"{BAND}, {SINGER}", UNKNOWN, BAND.upper())],
    ids=["no tags", "its own MBID", "another MBID", "S6's reading of the tags"],
)
def test_one_artist_on_a_name_holding_at_most_one_mbid_takes_the_file(
    conn: Conn, tags: tuple[str, ...]
) -> None:
    held = _insert_artist(conn, "Alice Cooper", BAND)
    _alice_tags(conn, *tags)
    _file(conn, "Alice Cooper", tag=SINGER, present=False)

    assert PgArtistRepository(conn).upsert_local_artist("Alice Cooper", ALICE) == held
    assert [mbid for mbid, _, _ in _artists(conn)] == [BAND]


@pytest.mark.parametrize("tags", [(), (BAND,)], ids=["no tags", "its MBID"])
def test_a_name_only_artist_beside_an_mbid_artist_takes_the_file(
    conn: Conn, tags: tuple[str, ...]
) -> None:
    """Two artists of the name, as S13's holder and named stub are, count as several whatever
    the tags say, and an MBID-only count would put the file on the stub."""
    name_only = _insert_artist(conn, "Alice Cooper", None)
    _insert_artist(conn, "Alice Cooper", BAND)
    _alice_tags(conn, *tags)

    assert PgArtistRepository(conn).upsert_local_artist("Alice Cooper", ALICE) == name_only


@pytest.mark.parametrize(
    ("mbid_artists", "tags"),
    [
        ((BAND,), (BAND, SINGER)),
        ((BAND, SINGER), ()),
        ((BAND, SINGER), (BAND,)),
    ],
    ids=["one artist, a name holding two", "two artists, no tags", "two artists, one tag"],
)
def test_several_go_to_the_single_name_only_artist_created_if_absent(
    conn: Conn, mbid_artists: tuple[str, ...], tags: tuple[str, ...]
) -> None:
    """Whichever file the scan reaches first, and the suggestion row is the sweep's, not this
    upsert's (S14)."""
    for mbid in mbid_artists:
        _insert_artist(conn, "Alice Cooper", mbid)
    _alice_tags(conn, *tags)
    repo = PgArtistRepository(conn)

    name_only = repo.upsert_local_artist("Alice Cooper", ALICE)

    assert repo.upsert_local_artist("Alice Cooper", ALICE) == name_only
    created = repo.get_by_id(name_only)
    assert created is not None and created.mbid is None
    assert _suggestions(conn) == []


def test_a_name_only_artist_is_kept_on_a_name_holding_several(conn: Conn) -> None:
    """S5: the upsert's ``ON CONFLICT`` meets the partial index here, and finds this artist."""
    name_only = _insert_artist(conn, "Alice Cooper", None)
    _alice_tags(conn, BAND, SINGER)

    assert PgArtistRepository(conn).upsert_local_artist("Alice Cooper", ALICE) == name_only


def test_untagged_shared_name(conn: Conn, tmp_path: Path) -> None:
    """A name holding two MBIDs meets untagged files. A new title goes to a work of the
    name-only artist. A title already held joins that work, whichever artist of the name holds
    it (M18), so no second work is made."""
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)
    band = PgArtistRepository(conn).get_by_mbid(BAND)
    assert band is not None
    held_by_an_mbid_artist = PgWorkRepository(conn).create_local("Under My Wheels", band.id)
    conn.commit()

    tagged(root / "new-title.mp3", "Alice Cooper", "Feed My Frankenstein")
    tagged(root / "held-by-name-only.mp3", "Alice Cooper", "Poison")
    tagged(root / "held-by-mbid-artist.mp3", "Alice Cooper", "Under My Wheels")
    full_scan(conn, root)

    work = _works_by_file(conn)
    name_only = PgArtistRepository(conn).get_by_normalized_name(ALICE)
    assert name_only is not None
    assert _artist_of_work(conn, work["new-title.mp3"]) == name_only.id
    assert work["held-by-name-only.mp3"] == work["singer.mp3"]
    assert work["held-by-mbid-artist.mp3"] == held_by_an_mbid_artist
    assert _artists(conn) == [
        (None, "local", 3),
        (BAND, "musicbrainz", 1),
        (SINGER, "musicbrainz", 0),
    ]


@pytest.mark.parametrize(
    "names",
    [
        ("a-untagged.mp3", "m-band.mp3", "z-singer.mp3"),
        ("z-untagged.mp3", "m-band.mp3", "a-singer.mp3"),
    ],
    ids=["untagged first", "untagged last"],
)
def test_the_result_does_not_depend_on_which_file_the_scan_reaches_first(
    conn: Conn, tmp_path: Path, names: tuple[str, str, str]
) -> None:
    root = tmp_path / "library"
    untagged, with_band, with_singer = names
    tagged(root / untagged, "Alice Cooper", "Elected")
    tagged(root / with_band, "Alice Cooper", "School's Out", BAND)
    tagged(root / with_singer, "Alice Cooper", "Poison", SINGER)

    full_scan(conn, root)

    assert _artists(conn) == [
        (None, "local", 3),
        (BAND, "musicbrainz", 0),
        (SINGER, "musicbrainz", 0),
    ]


@pytest.mark.parametrize(
    "names",
    [("a-untagged.mp3", "z-singer.mp3"), ("z-untagged.mp3", "a-singer.mp3")],
    ids=["untagged first", "untagged last"],
)
def test_after_a_promotion_the_new_files_go_name_only_in_either_order(
    conn: Conn, tmp_path: Path, names: tuple[str, str]
) -> None:
    """An upsert promoted the name while it held one MBID. Once a scan brings a second, neither
    new file joins the promoted artist, whichever comes first."""
    root = tmp_path / "library"
    tagged(root / "m-band.mp3", "Alice Cooper", "School's Out", BAND)
    full_scan(conn, root)
    _upsert(conn, BAND)
    conn.commit()
    untagged, with_singer = names
    tagged(root / untagged, "Alice Cooper", "Elected")
    tagged(root / with_singer, "Alice Cooper", "Poison", SINGER)

    full_scan(conn, root)

    assert _artists(conn) == [
        (None, "local", 2),
        (BAND, "musicbrainz", 1),
        (SINGER, "musicbrainz", 0),
    ]


# --- S13: a stub MusicBrainz names ----------------------------------------------------------------


def _name_a_stub(conn: Conn, mbid: str, mb_name: str) -> str:
    repo = PgArtistRepository(conn)
    stub = repo.create_stub(mbid)
    client = FakeMbClient(artists={mbid: {"id": mbid, "name": mb_name, "sort-name": mb_name}})

    outcome = _enhance_artist(stub, client, conn, RepositoryFactory(conn))

    assert outcome is ArtistEnhanceOutcome.ENHANCED
    return stub.id


def _named_row(conn: Conn, artist_id: str) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT name, normalized_name FROM artists WHERE id = %s", (artist_id,)
    ).fetchone()
    assert row is not None
    return row["name"], row["normalized_name"]


def test_stub_takes_a_held_name(conn: Conn) -> None:
    """D2: both stay, each with the name. The name gets one ``different_acts`` row, which a
    second stub of the name joins. The holder is untouched, no link moves whichever artist it
    points at, and nothing warns: two acts may share a name."""
    repo = PgArtistRepository(conn)
    holder = repo.upsert_local_artist("Metallica", "metallica")
    log_name = RepositoryFactory(conn).broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="METALLICA", normalized_name="metallica")
    )
    seed_pg_link(conn, log_name.id, holder, origin=LogNameLinkOrigin.MANUAL)
    repo.create_stub(BAND)
    seed_pg_link(conn, log_name.id, BAND, origin=LogNameLinkOrigin.AUTOMATIC)
    before = repo.get_by_id(holder)
    links_before = PgLogNameLinkRepository(conn).list_by_name(log_name.id)
    assert len(links_before) == 2

    with capture_logs() as events:
        first = _name_a_stub(conn, BAND, "Metallica")
        second = _name_a_stub(conn, SINGER, "METALLICA")

    assert [e for e in events if e["log_level"] in ("warning", "error")] == []
    assert _named_row(conn, first) == ("Metallica", "metallica")
    assert _named_row(conn, second) == ("METALLICA", "metallica")
    assert repo.get_by_id(holder) == before
    found = repo.get_by_normalized_name("metallica")
    assert found is not None and found.id == holder
    assert PgLogNameLinkRepository(conn).list_by_name(log_name.id) == links_before
    [row] = _suggestions(conn)
    assert (row["normalized_name"], row["reason"], row["state"], row["library_file_id"]) == (
        "metallica",
        "different_acts",
        "open",
        None,
    )
    assert sorted(row["candidates"]) == sorted([BAND, SINGER])


def test_a_name_that_normalizes_to_nothing_keeps_null(conn: Conn) -> None:
    """A symbol-only name is taken with the normalized name left NULL, not '', and asks nobody."""
    PgArtistRepository(conn).upsert_local_artist("???", "")

    stub = _name_a_stub(conn, BAND, "!!!")

    assert _named_row(conn, stub) == ("!!!", None)
    assert _suggestions(conn) == []


def test_a_stub_gets_no_row_when_only_mbid_artists_hold_the_name(conn: Conn) -> None:
    _insert_artist(conn, "Nirvana", OTHER)

    stub = _name_a_stub(conn, BAND, "Nirvana")

    assert _named_row(conn, stub) == ("Nirvana", "nirvana")
    assert _suggestions(conn) == []


# --- S14: the sweep -------------------------------------------------------------------------------


def test_shared_name_makes_each_artist(conn: Conn, tmp_path: Path) -> None:
    """Every MBID of the name gets an artist with the spelling most of its files carry, and the
    works stay on the name-only artist (until 3b). One row holds the MBIDs and the titles files
    under two or more of them share, compared by ``normalize_title``."""
    root = tmp_path / "library"
    tagged(root / "1.mp3", "ALICE COOPER", "Elected", BAND)
    tagged(root / "2.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "3.mp3", "Alice Cooper", "Under My Wheels", BAND)
    tagged(root / "4.mp3", "Alice Cooper", "Poison", SINGER)
    tagged(root / "5.mp3", "ALICE COOPER", "UNDER MY WHEELS", SINGER)
    tagged(root / "6.mp3", "ALICE COOPER", "Bed Of Nails", SINGER)
    tagged(root / "7.mp3", "Alice Cooper", "Hey Stoopid")

    full_scan(conn, root)

    assert _artists(conn) == [
        (None, "local", 6),
        (BAND, "musicbrainz", 0),
        (SINGER, "musicbrainz", 0),
    ]
    repo = PgArtistRepository(conn)
    band, singer = repo.get_by_mbid(BAND), repo.get_by_mbid(SINGER)
    assert band is not None and singer is not None
    assert (band.name, band.needs_enhancement) == ("Alice Cooper", True)
    assert (singer.name, singer.needs_enhancement) == ("ALICE COOPER", True)
    row = _shared_name_row(conn)
    assert (row["state"], row["library_file_id"]) == ("open", None)
    assert sorted(row["candidates"]) == sorted([BAND, SINGER])
    titles = row["evidence"]["shared_titles"]
    assert [normalize_title(title) for title in titles] == ["under my wheels"]


def test_only_the_real_mbids_of_a_shared_name_get_an_artist(conn: Conn) -> None:
    """S6's reading decides what gets an artist and what the row lists, not only whether a name
    is shared: no artist for a placeholder (C7), a second case of an MBID, a joined value or a
    missing file. A title is shared only between real MBIDs, and a file with no title holds
    none."""
    _file(conn, "Alice Cooper", tag=BAND, title="Poison")
    _file(conn, "Alice Cooper", tag=BAND.upper(), title="Poison")
    _file(conn, "Alice Cooper", tag=SINGER, title="Elected")
    _file(conn, "Alice Cooper", tag=SINGER, title=None)
    _file(conn, "Alice Cooper", tag=UNKNOWN, title="Elected")
    _file(conn, "Alice Cooper", tag=f"{THIRD}, {OTHER}", title="Elected")
    _file(conn, "Alice Cooper", tag=SOLO, title="Elected", present=False)
    conn.commit()

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert [mbid for mbid, _, _ in _artists(conn)] == [BAND, SINGER]
    for mbid in (UNKNOWN, THIRD, OTHER, SOLO, BAND.upper()):
        assert PgArtistRepository(conn).get_by_mbid(mbid) is None
    row = _shared_name_row(conn)
    assert sorted(row["candidates"]) == sorted([BAND, SINGER])
    assert row["evidence"]["shared_titles"] == []


def test_files_with_no_title_share_nothing(conn: Conn) -> None:
    """And they don't fail the sweep."""
    _file(conn, "Alice Cooper", tag=BAND, title=None)
    _file(conn, "Alice Cooper", tag=SINGER, title=None)
    conn.commit()

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert _shared_name_row(conn)["evidence"]["shared_titles"] == []


def test_tag_in_a_later_scan(conn: Conn, tmp_path: Path) -> None:
    """D6: one MBID makes no artist. A second one arriving later gives the earlier MBID its
    artist too, though the earlier file is never grouped again."""
    root = tmp_path / "library"
    tagged(root / "first.mp3", "Alice Cooper", "School's Out", BAND)
    full_scan(conn, root)
    assert _artists(conn) == [(None, "local", 1)]
    assert _suggestions(conn) == []

    tagged(root / "later.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)

    assert _artists(conn) == [
        (None, "local", 2),
        (BAND, "musicbrainz", 0),
        (SINGER, "musicbrainz", 0),
    ]
    assert sorted(_shared_name_row(conn)["candidates"]) == sorted([BAND, SINGER])


def test_retag_of_a_grouped_file(conn: Conn, tmp_path: Path) -> None:
    """A grouped file re-tagged with another MBID keeps its work and is never grouped again, yet
    the sweep sees it."""
    root = tmp_path / "library"
    tagged(root / "one.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "two.mp3", "Alice Cooper", "Poison", BAND)
    full_scan(conn, root)
    works_before = _works_by_file(conn)

    tagged(root / "two.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)

    assert _works_by_file(conn) == works_before
    assert _artists(conn) == [
        (None, "local", 2),
        (BAND, "musicbrainz", 0),
        (SINGER, "musicbrainz", 0),
    ]
    assert sorted(_shared_name_row(conn)["candidates"]) == sorted([BAND, SINGER])


def test_fuzzy_join_still_writes_the_row(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both tagged files join the existing work by title, so grouping never reaches
    ``upsert_local_artist``, and the name still gets its artists and its row."""
    root = tmp_path / "library"
    tagged(root / "first.mp3", "Joined", "Some Song")
    full_scan(conn, root)
    local_upserts: list[str] = []
    upsert_local = PgArtistRepository.upsert_local_artist

    def counting(self: PgArtistRepository, name: str, normalized_name: str) -> str:
        local_upserts.append(normalized_name)
        return upsert_local(self, name, normalized_name)

    monkeypatch.setattr(PgArtistRepository, "upsert_local_artist", counting)
    tagged(root / "a.mp3", "Joined", "Some Song", BAND)
    tagged(root / "b.mp3", "Joined", "Some Song", SINGER)

    full_scan(conn, root)

    assert local_upserts == []
    assert _artists(conn, "joined") == [
        (None, "local", 1),
        (BAND, "musicbrainz", 0),
        (SINGER, "musicbrainz", 0),
    ]
    row = _shared_name_row(conn, "joined")
    assert sorted(row["candidates"]) == sorted([BAND, SINGER])
    assert row["evidence"]["shared_titles"] == ["Some Song"]


def test_repeat_keeps_state(conn: Conn, tmp_path: Path) -> None:
    """A name Lance kept separate never reopens: a repeat updates the candidates and the evidence
    and leaves ``state`` alone (phase 5's E5), even for an MBID new since. It stays one row."""
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)
    assert _shared_name_row(conn)["evidence"]["shared_titles"] == []
    conn.execute("UPDATE library_suggestions SET state = 'dismissed'")
    conn.commit()

    tagged(root / "third.mp3", "Alice Cooper", "Poison", THIRD)
    full_scan(conn, root)

    row = _shared_name_row(conn)
    assert row["state"] == "dismissed"
    assert sorted(row["candidates"]) == sorted([BAND, SINGER, THIRD])
    assert row["evidence"]["shared_titles"] == ["Poison"]
    assert len(_suggestions(conn)) == 1


def test_a_name_that_stops_holding_several_keeps_its_row(conn: Conn, tmp_path: Path) -> None:
    """Its row stays as it was, and its artists stay. The sweep only adds."""
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)
    before = _shared_name_row(conn)

    (root / "singer.mp3").unlink()
    full_scan(conn, root)

    assert PgArtistRepository(conn).name_mbids(ALICE) == {BAND}
    assert _shared_name_row(conn) == before
    assert [mbid for mbid, _, _ in _artists(conn)] == [None, BAND, SINGER]


def test_a_single_mbid_makes_no_artist(conn: Conn, tmp_path: Path) -> None:
    """D6: the linker and enrichment promote these as today. Joined values and placeholders don't
    make a name shared (S6's reading)."""
    root = tmp_path / "library"
    tagged(root / "one.mp3", "Solo", "One", SOLO)
    tagged(root / "two.mp3", "Solo", "Two")
    tagged(root / "joined.mp3", "Solo", "Three", f"{SOLO}, {OTHER}")
    tagged(root / "placeholder.mp3", "Solo", "Four", UNKNOWN)
    tagged(root / "upper.mp3", "Solo", "Five", SOLO.upper())

    full_scan(conn, root)

    assert _artists(conn, "solo") == [(None, "local", 5)]
    assert _suggestions(conn) == []


def test_a_known_mbid_is_left_as_it_is(conn: Conn, tmp_path: Path) -> None:
    """The sweep writes only what is missing: an artist holding the MBID under another name
    keeps that name, and a stub stays unnamed for enrichment."""
    known = _insert_artist(conn, "Alice Cooper Band", BAND)
    stub = PgArtistRepository(conn).create_stub(SINGER)
    conn.commit()
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)

    full_scan(conn, root)

    assert _named_row(conn, known) == ("Alice Cooper Band", "alice cooper band")
    assert _named_row(conn, stub.id) == (SINGER, None)
    assert _artists(conn) == [(None, "local", 2)]
    assert sorted(_shared_name_row(conn)["candidates"]) == sorted([BAND, SINGER])


def test_a_later_mbid_leaves_a_promoted_artist_its_works(conn: Conn, tmp_path: Path) -> None:
    """An upsert promoted the name while it held one MBID. A second MBID gets its own artist
    beside it, the promoted artist keeps its works, and the new file goes name-only (S7)."""
    root = tmp_path / "library"
    tagged(root / "first.mp3", "Alice Cooper", "School's Out", BAND)
    full_scan(conn, root)
    promoted = _upsert(conn, BAND)
    conn.commit()
    assert _artist_of_work(conn, _works_by_file(conn)["first.mp3"]) == promoted

    tagged(root / "later.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)

    assert _artists(conn) == [
        (None, "local", 1),
        (BAND, "musicbrainz", 1),
        (SINGER, "musicbrainz", 0),
    ]


def test_a_blank_name_is_skipped(conn: Conn) -> None:
    _file(conn, "!!!", tag=BAND)
    _file(conn, "???", tag=SINGER)
    conn.commit()

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert PgArtistRepository(conn).get_by_mbid(BAND) is None
    assert PgArtistRepository(conn).get_by_mbid(SINGER) is None
    assert _suggestions(conn) == []


def test_the_linker_leaves_a_shared_name(conn: Conn, tmp_path: Path) -> None:
    """S9 on the real tables: the linker's answer would be LINKED, and the name-only artist stays
    name-only, marked ambiguous."""
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)
    full_scan(conn, root)
    name_only = PgArtistRepository(conn).get_by_normalized_name(ALICE)
    assert name_only is not None
    client = FakeMbClient(
        artists={
            BAND: {"id": BAND, "name": "Alice Cooper", "sort-name": "Cooper, Alice"},
            SINGER: {"id": SINGER, "name": "Vincent Furnier", "sort-name": "Furnier, Vincent"},
        }
    )

    outcome = link_artist(name_only, RepositoryFactory(conn).artist_linking, client)

    assert outcome == ArtistLinkOutcome.AMBIGUOUS
    after = PgArtistRepository(conn).get_by_id(name_only.id)
    assert after is not None
    assert (after.mbid, after.mb_lookup_outcome) == (None, ArtistLinkOutcome.AMBIGUOUS)


# --- S14: how the sweep runs ----------------------------------------------------------------------


def _fail_the_row_of(monkeypatch: pytest.MonkeyPatch, normalized_name: str) -> None:
    """The name's last write fails, once its artists may already be written."""
    write = PgLibrarySuggestionRepository.upsert_for_name

    def failing(self: PgLibrarySuggestionRepository, name: str, *args: Any, **kwargs: Any) -> bool:
        if name == normalized_name:
            raise psycopg.errors.DeadlockDetected("the database refused this name")
        return write(self, name, *args, **kwargs)

    monkeypatch.setattr(PgLibrarySuggestionRepository, "upsert_for_name", failing)


def _two_shared_names(conn: Conn) -> None:
    for name, first, second in (("Alpha", BAND, SINGER), ("Beta", THIRD, OTHER)):
        _file(conn, name, tag=first)
        _file(conn, name, tag=second)


@pytest.mark.parametrize(
    ("failing", "landing"), [("alpha", "beta"), ("beta", "alpha")], ids=["first", "last"]
)
def test_a_failure_on_one_name_leaves_the_others_and_the_next_sweep_retries(
    conn: Conn, reader: Conn, monkeypatch: pytest.MonkeyPatch, failing: str, landing: str
) -> None:
    """Each name is its own transaction: a ``psycopg.Error`` rolls back that name whole, is
    logged with the name, and every other name lands, committed. The next sweep writes it."""
    _two_shared_names(conn)
    conn.commit()
    with monkeypatch.context() as patch:
        _fail_the_row_of(patch, failing)
        with capture_logs() as events:
            sweep_artist_identities(conn, RepositoryFactory(conn))

    assert _artists(reader, failing) == []
    assert [r["normalized_name"] for r in _suggestions(reader)] == [landing]
    assert len(_artists(reader, landing)) == 2
    assert any(e["log_level"] == "warning" and e.get("normalized_name") == failing for e in events)

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert [r["normalized_name"] for r in _suggestions(reader)] == ["alpha", "beta"]
    assert len(_artists(reader, failing)) == 2


def test_what_is_pending_is_committed_first(
    conn: Conn, reader: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing name rolls back only its own writes, never the caller's."""
    _two_shared_names(conn)
    conn.commit()
    pending = PgArtistRepository(conn).upsert_local_artist("Pending", "pending")  # not committed
    _fail_the_row_of(monkeypatch, "alpha")

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert PgArtistRepository(reader).get_by_id(pending) is not None


def _refused_read(_self: PgArtistTagFilesRepository) -> list[Any]:
    raise psycopg.OperationalError("the read was refused")


def test_what_is_pending_survives_a_failed_read(
    conn: Conn, reader: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = PgArtistRepository(conn).upsert_local_artist("Pending", "pending")  # not committed
    monkeypatch.setattr(PgArtistTagFilesRepository, "list_tag_groups", _refused_read)

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert PgArtistRepository(reader).get_by_id(pending) is not None


def test_a_failed_read_does_not_fail_the_scan(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan has committed its files by then: the failure is logged, and the next scan's sweep
    does the work."""
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)

    with monkeypatch.context() as patch:
        patch.setattr(PgArtistTagFilesRepository, "list_tag_groups", _refused_read)
        with capture_logs() as events:
            full_scan(conn, root)

    assert any(e["log_level"] == "warning" for e in events)
    assert _artists(conn) == [(None, "local", 2)]

    full_scan(conn, root)

    assert [mbid for mbid, _, _ in _artists(conn)] == [None, BAND, SINGER]


def test_the_sweep_runs_last_in_a_full_scan(conn: Conn, tmp_path: Path) -> None:
    """After the reconciliation and the purge."""
    RepositoryFactory(conn).user_settings.upsert(
        UserSetting(key=PURGE_MISSING_SETTING, value="after_scan")
    )
    conn.commit()
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)

    with capture_logs() as events:
        full_scan(conn, root)

    seen = [e["event"] for e in events]
    assert (
        seen.index("missing_files_reconciled")
        < seen.index("missing_files_purged")
        < seen.index("artist_identity_sweep_complete")
    )


def test_the_sweep_runs_after_a_failed_reconciliation(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library_scan_tasks, "reconcile_missing_after_scan", lambda *_: None)
    root = tmp_path / "library"
    tagged(root / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(root / "singer.mp3", "Alice Cooper", "Poison", SINGER)

    full_scan(conn, root)

    assert [mbid for mbid, _, _ in _artists(conn)] == [None, BAND, SINGER]


@pytest.mark.usefixtures("task_database")
def test_the_watcher_sweeps_after_its_reconciliation(conn: Conn, tmp_path: Path) -> None:
    folder = tmp_path / "library"
    tagged(folder / "band.mp3", "Alice Cooper", "School's Out", BAND)
    tagged(folder / "singer.mp3", "Alice Cooper", "Poison", SINGER)

    with capture_logs() as events:
        library_scan_files_task.call_local([str(folder)], uuid4().hex)

    seen = [e["event"] for e in events]
    assert seen.index("missing_files_reconciled") < seen.index("artist_identity_sweep_complete")
    assert [mbid for mbid, _, _ in _artists(conn)] == [None, BAND, SINGER]
    assert sorted(_shared_name_row(conn)["candidates"]) == sorted([BAND, SINGER])


@pytest.mark.usefixtures("task_database")
def test_the_task_runs_with_no_arguments_and_reports_its_counts(conn: Conn) -> None:
    """The gate's run: names held by several MBIDs, artists made, rows written, names failed. A
    repeat writes nothing, because it writes only what is missing."""
    _two_shared_names(conn)
    conn.commit()

    first = artist_identity_sweep_task.call_local()
    again = artist_identity_sweep_task.call_local()

    assert first == {"names": 2, "artists_made": 4, "rows_written": 2, "names_failed": 0}
    assert again == {"names": 2, "artists_made": 0, "rows_written": 0, "names_failed": 0}


@pytest.mark.usefixtures("task_database")
def test_the_task_on_an_empty_library() -> None:
    assert artist_identity_sweep_task.call_local() == {
        "names": 0,
        "artists_made": 0,
        "rows_written": 0,
        "names_failed": 0,
    }


# --- 0047's table (S14, C21, section 6.10) --------------------------------------------------------


def _insert_row(conn: Conn, name: str = "alpha", **columns: Any) -> None:
    row: dict[str, Any] = {"normalized_name": name, "reason": "shared_name", **columns}
    names = ", ".join(row)
    values = ", ".join(f"%({column})s" for column in row)
    conn.execute(  # type: ignore[call-overload]
        f"INSERT INTO library_suggestions ({names}) VALUES ({values})", row
    )


def test_the_table_holds_one_row_per_name_and_reason_where_no_file_is_named(conn: Conn) -> None:
    _insert_row(conn)
    _insert_row(conn, reason="different_acts")
    _insert_row(conn, "beta")

    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert_row(conn)
    [alpha, *_] = _suggestions(conn)
    assert (alpha["state"], alpha["candidates"], alpha["evidence"]) == ("open", [], {})


def _library_file_id(conn: Conn) -> str:
    row = conn.execute(
        "INSERT INTO library_files (file_path, format) VALUES (%s, 'flac') RETURNING id",
        (f"/music/{uuid4().hex}.flac",),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def test_rows_naming_a_file_can_repeat_and_go_with_the_file(conn: Conn) -> None:
    """Phases 4.4 and 6 write a row per file. Only rows naming no file are one per name."""
    first, second = _library_file_id(conn), _library_file_id(conn)
    _insert_row(conn, library_file_id=first)
    _insert_row(conn, library_file_id=second)

    conn.execute("DELETE FROM library_files WHERE id = %s", (first,))

    assert [str(r["library_file_id"]) for r in _suggestions(conn)] == [second]


@pytest.mark.parametrize(
    "columns", [{"state": "pending"}, {"reason": "looks odd"}], ids=["state", "reason"]
)
def test_the_table_refuses_an_unknown_state_or_reason(conn: Conn, columns: dict[str, str]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _insert_row(conn, **columns)


@pytest.mark.parametrize("state", ["open", "accepted", "dismissed"])
def test_the_table_holds_each_state(conn: Conn, state: str) -> None:
    _insert_row(conn, state=state)

    assert [r["state"] for r in _suggestions(conn)] == [state]

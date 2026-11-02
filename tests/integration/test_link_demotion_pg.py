"""Migration 0045 demotes the fuzzy links and removes the placeholder links (artist model phase
1c, SPEC S1, S2, S5, S6; E1, E2, E5; M22, C7, C35, C36, AUD-R038, AUD-R039).

S1, in this order, after copying every link and name it will change or delete into
``link_demotion_backup`` (one row per link and per name: ``kind``, ``id``, the row ``before``
and ``after`` it, ``after`` NULL for a deleted link):

1. the safety net: any active link of a ``manual_rejected`` or ``auto_rejected`` name becomes
   ``rejected`` (0044 already copied them so, and 1b's Reject rejects them);
2. C7: every link, in any state, to a ``SPECIAL_PURPOSE_MBIDS`` artist is deleted, and so is
   every link of a name on the placeholder list (``log_name_placeholders``, created and seeded
   here with the five station names, exact cleaned names only);
3. M22: every active ``legacy`` link whose v0 tier is ``normalization`` and whose strength is
   under 95 becomes ``suggested`` when its score is 56 or more, and is deleted otherwise. Exact
   names (100), MusicBrainz links, manual links, mapping rules (tier ``manual``) and the
   matcher's own links since 1a (origin ``automatic``) stay;
4. each name whose links changed takes its status from its links (S3's rule, no user action):
   an active manual link ``manual_matched``, another active link ``auto_matched``, a suggestion
   ``needs_review`` / ``LINK_SUGGESTED``, a matched name left with none ``needs_review`` /
   ``LINK_REMOVED``; ``reason_detail`` says why. A rejected name, and a name on the list (the
   list hides it), keep their status.

Lines and song matches are untouched (E2): the demoted names' lines keep playing, and Tier 1
skips the name until it is linked again. The migration sets ``lock_timeout`` and locks the
tables it writes in ingest's order (E5).

S2: ``backend/db/rollback_0045_link_demotion.sql`` puts back, from the backup, every link and
name the migration changed or deleted, unless it moved since (a link or a status that no longer
holds what the migration wrote keeps its new state: the curator's later decision wins), then
drops ``link_demotion_backup`` and ``log_name_placeholders`` and deletes 0045's
``schema_migrations`` row.

Each test runs on the database as it was before 0045: the rollback undoes 0045 inside one
transaction, the test seeds rows as 0044 and 1a's writers left them and runs the migration's
SQL, and the transaction is rolled back at the end, so the test database keeps 0045.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.services.artist_linking_service import SPECIAL_PURPOSE_MBIDS
from backend.services.log_name_link_service import LogNameLinkService
from tests.integration.demotion_seed import (
    MIGRATION,
    T0,
    VERSION,
    Conn,
    before_the_demotion,
    catalog_artist,
    link,
    log_name,
    migrate,
    roll_back,
    strength,
)

HOUR = timedelta(hours=1)
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
UNKNOWN_ARTIST = "125ec42a-7229-4250-afc5-e057484327fe"
STATION_PLACEHOLDERS = {
    "spot break",
    "special programming",
    "syndicated program",
    "various",
    "various artists",
}


# --- reads ---------------------------------------------------------------------------------------


def _links_of(conn: Conn, name_id: UUID) -> dict[str, tuple[str, str, int]]:
    rows = conn.execute(
        "SELECT artist_id, state, origin, strength FROM log_name_links"
        " WHERE broadcast_artist_id = %s",
        (name_id,),
    ).fetchall()
    return {row["artist_id"]: (row["state"], row["origin"], row["strength"]) for row in rows}


def _state(conn: Conn, name_id: UUID) -> tuple[str, str | None, str | None]:
    row = conn.execute(
        "SELECT match_status, reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
        (name_id,),
    ).fetchone()
    assert row is not None
    return row["match_status"], row["reason_code"], row["reason_detail"]


def _status(conn: Conn, name_id: UUID) -> tuple[str, str | None]:
    return _state(conn, name_id)[:2]


def _backed_up(conn: Conn, kind: str) -> set[UUID]:
    rows = conn.execute("SELECT id FROM link_demotion_backup WHERE kind = %s", (kind,)).fetchall()
    return {row["id"] for row in rows}


def _all_links(conn: Conn) -> dict[UUID, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM log_name_links").fetchall()
    return {row["id"]: {k: v for k, v in row.items() if k != "updated_at"} for row in rows}


def _all_names(conn: Conn) -> dict[UUID, tuple[str, str | None, str | None]]:
    rows = conn.execute(
        "SELECT id, match_status, reason_code, reason_detail FROM broadcast_artists"
    ).fetchall()
    return {r["id"]: (r["match_status"], r["reason_code"], r["reason_detail"]) for r in rows}


def _table_exists(conn: Conn, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()
    return row is not None and row["t"] is not None


MATCHED = ("auto_matched", None)
SUGGESTED = ("needs_review", "LINK_SUGGESTED")
REMOVED = ("needs_review", "LINK_REMOVED")


# --- S1 -------------------------------------------------------------------------------------------


@pytest.mark.integration
def test_demotion_rules(migrated_db: str) -> None:
    with before_the_demotion(migrated_db) as conn:
        art = {name: catalog_artist(conn, name, str(uuid4())) for name in ("Archies", "Architects")}
        local = catalog_artist(conn, "Van Halen")
        names: dict[str, UUID] = {}
        changed: dict[str, UUID] = {}  # links the migration changes or deletes
        kept: dict[str, UUID] = {}

        def seed(key: str, status: str = "auto_matched", **fields: Any) -> UUID:
            reason, detail = fields.pop("reason", (None, None))
            names[key] = log_name(conn, key.upper(), status, reason, detail)
            target = fields.pop("artist", art["Architects"])
            bucket = changed if fields.pop("changes", False) else kept
            bucket[key] = link(conn, names[key], target, **fields)
            return names[key]

        # M22: active legacy normalization links under 95, by score.
        low = ("LOW_CONFIDENCE", "Score 70%")
        seed("demoted", score=72.4, changes=True)
        seed("edge 56", score=56.0, changes=True)
        seed("under 56", score=55.99, changes=True)  # strength 56, score under: deleted
        seed("strength 94", score=94.4, changes=True)
        seed("strength 95", score=94.5)  # strength 95: stays
        seed("in review", "needs_review", score=70.0, reason=low, changes=True)
        seed("pending", "pending", score=60.0, changes=True)
        # What stays: exact names, MusicBrainz links, mapping rules, the matcher's own, manual.
        seed("exact", score=100.0)
        seed("exact local", artist=local, score=100.0)
        seed("musicbrainz", tier="musicbrainz_api", score=80.0)
        seed("mapping rule", tier="manual", score=100.0)
        seed("automatic", origin="automatic", score=60.0)
        seed("manual", "manual_matched", origin="manual")
        already = seed("already rejected", score=100.0)
        kept["already rejected fuzzy"] = link(
            conn, already, art["Archies"], score=60.0, state="rejected"
        )
        # A name with an exact link and a fuzzy one: the fuzzy one is demoted, the name stays.
        mixed = seed("mixed", score=100.0)
        changed["mixed fuzzy"] = link(conn, mixed, art["Archies"], score=61.0)
        # The safety net: a rejected name's active links become rejected, first.
        seed("manual rejected", "manual_rejected", score=100.0, changes=True)
        seed("auto rejected", "auto_rejected", score=60.0, changes=True)
        untouched = log_name(conn, "NO LINKS", "pending")
        before = _all_names(conn)

        migrate(conn)

        links = {key: _links_of(conn, name_id) for key, name_id in names.items()}
        states = {key: _state(conn, name_id) for key, name_id in names.items()}
        backed_up_links, backed_up_names = _backed_up(conn, "link"), _backed_up(conn, "name")
        after = _all_names(conn)

    architects = art["Architects"]
    assert links["demoted"] == {architects: ("suggested", "legacy", 72)}
    assert links["edge 56"] == {architects: ("suggested", "legacy", 56)}
    assert links["under 56"] == {}
    assert links["strength 94"] == {architects: ("suggested", "legacy", 94)}
    assert links["strength 95"] == {architects: ("active", "legacy", 95)}
    assert links["in review"] == {architects: ("suggested", "legacy", 70)}
    assert links["pending"] == {architects: ("suggested", "legacy", 60)}
    for key in ("exact", "musicbrainz", "mapping rule"):
        assert links[key] == {architects: ("active", "legacy", 100 if key != "musicbrainz" else 80)}
    assert links["exact local"] == {local: ("active", "legacy", 100)}
    assert links["automatic"] == {architects: ("active", "automatic", 60)}
    assert links["manual"] == {architects: ("active", "manual", 100)}
    assert links["already rejected"] == {
        architects: ("active", "legacy", 100),
        art["Archies"]: ("rejected", "legacy", 60),  # a rejected link stays as it is
    }
    assert links["mixed"] == {
        architects: ("active", "legacy", 100),
        art["Archies"]: ("suggested", "legacy", 61),
    }
    assert links["manual rejected"] == {architects: ("rejected", "legacy", 100)}
    assert links["auto rejected"] == {architects: ("rejected", "legacy", 60)}

    statuses = {key: state[:2] for key, state in states.items()}
    for key in ("demoted", "edge 56", "strength 94", "in review", "pending"):
        assert statuses[key] == SUGGESTED, key
        detail = states[key][2]
        assert detail is not None and "M22" in detail and "1 suggested artist, best" in detail
    assert statuses["under 56"] == REMOVED
    detail = states["under 56"][2]
    assert detail is not None and "removed" in detail.lower()
    for key in (
        "strength 95",
        "exact",
        "exact local",
        "musicbrainz",
        "mapping rule",
        "automatic",
        "already rejected",
        "mixed",
    ):
        assert states[key] == ("auto_matched", None, None), key
    assert statuses["manual"] == ("manual_matched", None)
    assert statuses["manual rejected"] == ("manual_rejected", None)
    assert statuses["auto rejected"] == ("auto_rejected", None)
    assert after[untouched] == before[untouched]

    # The backup holds exactly what changed: every changed or deleted link, every changed name.
    assert backed_up_links == set(changed.values())
    changed_names = ("demoted", "edge 56", "under 56", "strength 94", "in review", "pending")
    assert backed_up_names == {names[key] for key in changed_names}


@pytest.mark.integration
def test_manual_names_keep_status(migrated_db: str) -> None:
    """E1: Beth Hart 58, After the Burial 59 and John Mellencamp 94 hold a manual link and a
    fuzzy one. The fuzzy one becomes a suggestion; the name stays manual_matched."""
    with before_the_demotion(migrated_db) as conn:
        cases = []
        for spelling, fuzzy_name, score in (
            ("BETH HART", "Beth Harte", 58.0),
            ("AFTER THE BURIAL", "After Burial", 59.0),
            ("JOHN MELLENCAMP", "John Cougar Mellencamp", 94.0),
        ):
            name_id = log_name(conn, spelling, "manual_matched")
            manual = catalog_artist(conn, spelling.title(), str(uuid4()))
            fuzzy = catalog_artist(conn, fuzzy_name, str(uuid4()))
            link(conn, name_id, manual, origin="manual", at=T0 + HOUR)
            link(conn, name_id, fuzzy, score=score)
            cases.append((name_id, manual, fuzzy, strength(score)))

        migrate(conn)

        found = [(_state(conn, name_id), _links_of(conn, name_id)) for name_id, *_ in cases]
        names_backed_up = _backed_up(conn, "name")

    for (state, links), (name_id, manual, fuzzy, held) in zip(found, cases, strict=True):
        assert state == ("manual_matched", None, None)
        assert links == {
            manual: ("active", "manual", 100),
            fuzzy: ("suggested", "legacy", held),
        }
        assert name_id not in names_backed_up


@pytest.mark.integration
def test_lines_untouched(migrated_db: str) -> None:
    """E2: a demoted name's lines and their song matches are not touched, so they keep playing
    (AUD-R022 D2 holds); Tier 1 finds no link for the name until it is linked again."""
    with before_the_demotion(migrated_db) as conn:
        name_id = log_name(conn, "ARCHIES")
        link(conn, name_id, catalog_artist(conn, "Architects", str(uuid4())), score=72.4)
        for status, score in (
            ("auto_matched", 91.0),
            ("manual_matched", 1.0),
            ("needs_review", 60.0),
            ("pending", None),
        ):
            song_id = uuid4()
            conn.execute(
                """INSERT INTO track_identities
                       (id, broadcast_artist_id, original_title, normalized_title,
                        normalized_signature, match_status, match_tier)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    song_id,
                    name_id,
                    f"Song {status}",
                    f"song {status}",
                    f"archies:song {status}:{song_id.hex}",
                    status,
                    None if score is None else "local_file_fuzzy",
                ),
            )
            if score is not None:
                conn.execute(
                    "INSERT INTO matches (id, identity_id, confidence_score, match_tier)"
                    " VALUES (%s, %s, %s, 'local_file_fuzzy')",
                    (uuid4(), song_id, score),
                )

        def lines() -> list[dict[str, Any]]:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT ti.*, m.id AS match_id, m.confidence_score, m.match_tier AS m_tier
                         FROM track_identities ti
                         LEFT JOIN matches m ON m.identity_id = ti.id
                        WHERE ti.broadcast_artist_id = %s
                        ORDER BY ti.id""",
                    (name_id,),
                ).fetchall()
            ]

        before = lines()
        migrate(conn)
        after = lines()
        tier1 = LogNameLinkService(PgLogNameLinkRepository(conn), PgArtistRepository(conn))

        assert _status(conn, name_id) == SUGGESTED
        assert tier1.tier1_link(name_id) is None

    assert after == before
    assert len(after) == 4


# --- S5 (its migration half) and S6 (the list's seed) ---------------------------------------------


@pytest.mark.integration
def test_placeholder_links_removed(migrated_db: str) -> None:
    """C7: every link to a MusicBrainz placeholder artist goes, in any state and of any origin,
    and every link of a listed name; a listed name keeps its status (the list hides it)."""
    with before_the_demotion(migrated_db) as conn:
        various = catalog_artist(conn, "Various Artists", VARIOUS_ARTISTS)
        unknown = catalog_artist(conn, "[unknown]", UNKNOWN_ARTIST)
        real = catalog_artist(conn, "38 Special", str(uuid4()))
        to_various = [log_name(conn, f"VARIOUS {n}") for n in range(3)]
        for name_id in to_various:
            link(conn, name_id, various, tier="musicbrainz_api")
        manual = log_name(conn, "UNKNOWN ARTIST", "manual_matched")
        link(conn, manual, unknown, origin="manual")
        rejected = log_name(conn, "VA", "needs_review", "USER_UNMATCHED")
        link(conn, rejected, various, state="rejected")
        kept_name = log_name(conn, "38 SPECIAL")  # a real act with a placeholder-like word
        link(conn, kept_name, real)
        spot = log_name(conn, "SPOT BREAK", "needs_review", "DEFERRED_RETRY", "deferred")
        link(conn, spot, real, score=60.0)
        special = log_name(conn, "SPECIAL PROGRAMMING", "manual_rejected")
        link(conn, special, real, state="rejected")
        # The safety net would reject it first; a listed name's links go whatever their state.
        link(conn, special, unknown, state="active")

        migrate(conn)

        links = {
            name_id: _links_of(conn, name_id)
            for name_id in (*to_various, manual, rejected, kept_name, spot, special)
        }
        states = {name_id: _state(conn, name_id) for name_id in (*to_various, manual, rejected)}
        listed = (_state(conn, spot), _state(conn, special))
        placeholder_links_left = conn.execute(
            "SELECT count(*) AS n FROM log_name_links l JOIN artists a ON a.id = l.artist_id"
            " WHERE a.mbid = ANY(%s)",
            (list(SPECIAL_PURPOSE_MBIDS),),
        ).fetchone()
        backed_up = _backed_up(conn, "link")

    for name_id in (*to_various, manual, rejected, spot, special):
        assert links[name_id] == {}
    assert links[kept_name] == {real: ("active", "legacy", 100)}
    for name_id in (*to_various, manual):
        status, reason, detail = states[name_id]
        assert (status, reason) == REMOVED
        assert detail is not None and "placeholder" in detail.lower()
    assert states[rejected] == ("needs_review", "USER_UNMATCHED", None)
    assert listed == (
        ("needs_review", "DEFERRED_RETRY", "deferred"),
        ("manual_rejected", None, None),
    )
    assert placeholder_links_left is not None and placeholder_links_left["n"] == 0
    assert len(backed_up) == 3 + 1 + 1 + 1 + 2


@pytest.mark.integration
def test_station_placeholders_are_seeded(migrated_db: str) -> None:
    """C7: the list holds the five station names, as exact cleaned (normalized) names."""
    with before_the_demotion(migrated_db) as conn:
        migrate(conn)
        rows = conn.execute(
            "SELECT normalized_name, added_at, added_by FROM log_name_placeholders"
        ).fetchall()

    assert {row["normalized_name"] for row in rows} == STATION_PLACEHOLDERS
    assert {row["added_by"] for row in rows} == {"migration"}
    assert all(row["added_at"] is not None for row in rows)


@pytest.mark.integration
def test_empty_install(migrated_db: str) -> None:
    """A fresh install: no links, so an empty demotion and an empty backup; the list is still
    seeded."""
    with before_the_demotion(migrated_db) as conn:
        name_id = log_name(conn, "METALLICA", "pending")

        migrate(conn)

        backup = conn.execute("SELECT count(*) AS n FROM link_demotion_backup").fetchone()
        listed = conn.execute("SELECT count(*) AS n FROM log_name_placeholders").fetchone()
        state = _state(conn, name_id)

    assert backup is not None and backup["n"] == 0
    assert listed is not None and listed["n"] == len(STATION_PLACEHOLDERS)
    assert state == ("pending", None, None)


# --- S2 -------------------------------------------------------------------------------------------


def _demotion_world(conn: Conn) -> dict[str, UUID]:
    architects = catalog_artist(conn, "Architects", str(uuid4()))
    archies = catalog_artist(conn, "Archies", str(uuid4()))
    various = catalog_artist(conn, "Various Artists", VARIOUS_ARTISTS)
    names = {
        "demoted": log_name(conn, "ARCHIES"),
        "removed": log_name(conn, "ARCHIE"),
        "placeholder": log_name(conn, "VARIOUS HITS"),
        "rejected": log_name(conn, "REJECTED ACT", "manual_rejected"),
        "listed": log_name(conn, "SPOT BREAK", "needs_review", "DEFERRED_RETRY", "deferred"),
        "accepted": log_name(conn, "ARCHITECTS UK", "needs_review", "LOW_CONFIDENCE", "Score 70%"),
        "relinked": log_name(conn, "ARCHIE BELL"),
    }
    link(conn, names["demoted"], archies, score=72.4, at=T0)
    link(conn, names["demoted"], architects, score=100.0, state="rejected", at=T0 + HOUR)
    link(conn, names["removed"], archies, score=50.0)
    link(conn, names["placeholder"], various, tier="musicbrainz_api")
    link(conn, names["rejected"], architects)
    link(conn, names["listed"], architects, score=60.0)
    link(conn, names["accepted"], architects, score=70.0)
    link(conn, names["relinked"], archies, score=50.0)
    return names


@pytest.mark.integration
def test_rollback_restores(migrated_db: str) -> None:
    with before_the_demotion(migrated_db) as conn:
        _demotion_world(conn)
        links_before, names_before = _all_links(conn), _all_names(conn)
        migrate(conn)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (VERSION,))
        assert _all_links(conn) != links_before

        roll_back(conn)

        links_after, names_after = _all_links(conn), _all_names(conn)
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()
        tables = (
            _table_exists(conn, "link_demotion_backup"),
            _table_exists(conn, "log_name_placeholders"),
        )

    assert links_after == links_before  # every link, deleted ones with their id and created_at
    assert names_after == names_before
    assert applied is None
    assert tables == (False, False)


@pytest.mark.integration
def test_rollback_keeps_what_moved_since(migrated_db: str) -> None:
    """The curator accepted a suggestion, and the matcher linked a removed pair again, after the
    migration: both keep their new state; everything else is restored."""
    with before_the_demotion(migrated_db) as conn:
        names = _demotion_world(conn)
        links_before, names_before = _all_links(conn), _all_names(conn)
        migrate(conn)
        conn.execute(
            "UPDATE log_name_links SET state = 'active', origin = 'manual', strength = 100,"
            ' evidence = \'{"version": 0, "tier": "manual", "score": 1.0}\''
            " WHERE broadcast_artist_id = %s",
            (names["accepted"],),
        )
        conn.execute(
            "UPDATE broadcast_artists SET match_status = 'manual_matched', reason_code = NULL,"
            " reason_detail = NULL WHERE id = %s",
            (names["accepted"],),
        )
        archies = conn.execute("SELECT id FROM artists WHERE name = 'Archies'").fetchone()
        assert archies is not None
        link(conn, names["relinked"], archies["id"], origin="automatic", score=100.0)
        conn.execute(
            "UPDATE broadcast_artists SET match_status = 'auto_matched', reason_code = NULL,"
            " reason_detail = NULL WHERE id = %s",
            (names["relinked"],),
        )
        moved = {names["accepted"], names["relinked"]}

        def links_of_moved(moved_too: bool) -> dict[UUID, dict[str, Any]]:
            return {
                link_id: row
                for link_id, row in _all_links(conn).items()
                if (row["broadcast_artist_id"] in moved) is moved_too
            }

        links_moved = links_of_moved(True)
        names_moved = {k: v for k, v in _all_names(conn).items() if k in moved}

        roll_back(conn)

        links_after, links_moved_after = links_of_moved(False), links_of_moved(True)
        names_after = _all_names(conn)

    assert links_after == {
        k: v for k, v in links_before.items() if v["broadcast_artist_id"] not in moved
    }
    assert {k: v for k, v in names_after.items() if k not in moved} == {
        k: v for k, v in names_before.items() if k not in moved
    }
    assert links_moved_after == links_moved  # the manual link and the new automatic one
    assert len(links_moved_after) == 2
    assert {k: v for k, v in names_after.items() if k in moved} == names_moved


# --- E5 -------------------------------------------------------------------------------------------

INGEST_ORDER = ["playlists", "broadcast_artists", "track_identities", "play_events"]


def _statements(sql: str) -> list[str]:
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [" ".join(s.split()) for s in without_comments.split(";") if s.strip()]


def test_lock_timeout_set() -> None:
    """E5: the migration sets ``lock_timeout`` before it locks anything, and locks the tables it
    writes up front, ingest's tables in ingest's order (``ingestion_service``: playlists,
    broadcast_artists, track_identities, play_events), and the names before their links,
    as the matcher and Resolve write them, so it never deadlocks with either."""
    statements = _statements(MIGRATION.read_text(encoding="utf-8"))
    timeout = next(i for i, s in enumerate(statements) if s.startswith("SET LOCAL lock_timeout"))
    locks = [i for i, s in enumerate(statements) if s.upper().startswith("LOCK TABLE")]
    writes = [
        i
        for i, s in enumerate(statements)
        if s.split()[0].upper() in {"UPDATE", "DELETE", "INSERT", "WITH"}
    ]

    assert locks and timeout < locks[0] < writes[0]
    locked = [
        table.strip().lower()
        for i in locks
        for table in re.sub(r"(?i)^LOCK TABLE\s+|\s+IN\s+.*$", "", statements[i]).split(",")
    ]
    assert "broadcast_artists" in locked and "log_name_links" in locked
    assert locked.index("broadcast_artists") < locked.index("log_name_links")
    ingest_tables = [t for t in locked if t in INGEST_ORDER]
    assert ingest_tables == sorted(ingest_tables, key=INGEST_ORDER.index)


@pytest.mark.integration
@pytest.mark.slow
def test_a_lock_timeout_fails_the_migration(migrated_db: str) -> None:
    """E5: while an ingest transaction writes log names, the migration gives up after its own
    ``lock_timeout`` with an error, so the deploy fails loudly instead of waiting. The test
    database's default lock_timeout is turned off first, so only the migration's counts: without
    it the wait would end at the statement timeout instead (QueryCanceled, not this error).
    The ingest lock is taken before 0045 is rolled back for the test, so the rollback script (run
    by hand with the app stopped) must take no lock on broadcast_artists above ROW EXCLUSIVE."""
    with psycopg.connect(migrated_db) as ingest:
        ingest.execute("LOCK TABLE broadcast_artists IN ROW EXCLUSIVE MODE")
        with before_the_demotion(migrated_db) as conn:
            conn.execute("SET LOCAL lock_timeout = 0")
            conn.execute("SET LOCAL statement_timeout = '20s'")

            with pytest.raises(psycopg.errors.LockNotAvailable):
                migrate(conn)

        ingest.rollback()

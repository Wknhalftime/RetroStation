"""Dry run of song title-scoring trial mode (AUD-R100): what it would change, written nowhere.

Before trial mode ships, Lance reads which songs it moves. Through a read-only connection, this
resolves every song not matched by hand with today's strategy chain and in trial mode, and every
song Lance matched by hand as if it were pending, using the same strategies as the song-matching
task (build_song_engines). A song rejected by hand is undecided: every re-check re-scores it
without its rejected files (AUD-R029), and so does this. It writes three CSV reports into --out:

- frozen_check.csv: the product's V11.1 against the frozen v11.py, for every title pair this run
  scored (with --frozen, once FROZEN.sha256 checks out; without it, the header only);
- changed_songs.csv: every song not matched by hand whose trial outcome or file differs from
  today's, and whether a re-check would apply it (existing auto-matches are not rewound);
- accepted_songs.csv: where each hand-matched song lands, today and in trial mode.

It prints the counts and V11.1's average time per pair (SPEC E6: at most 200 us).

MusicBrainz is never called. Step B (the recording search) is skipped, as if no artist had a
file only it could add (AUD-R031): its answer does not depend on the title scorer, and every
file it can find is one of Step C's candidates too. The output says for how many artists it was
skipped.

Run: uv run python scripts/dry_run_title_scoring.py --out results/title_trial
         [--database-url URL] [--frozen <the frozen V11.1 folder>]

Targets --database-url if given, else the app's configured database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.db.repositories.artists import PgArtistRepository  # noqa: E402
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository  # noqa: E402
from backend.db.repositories.broadcast_track_identities import (  # noqa: E402
    PgBroadcastTrackIdentityRepository,
)
from backend.db.repositories.library_files import PgLibraryFileRepository  # noqa: E402
from backend.db.repositories.mapping_rules import PgMappingRuleRepository  # noqa: E402
from backend.db.repositories.matches import PgMatchRepository  # noqa: E402
from backend.db.sync_conn import connect_sync  # noqa: E402
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity  # noqa: E402
from backend.domain.enums import MatchStatus  # noqa: E402
from backend.repositories.recording_search_reach import (  # noqa: E402
    RecordingSearchReachRepository,
)
from backend.services.identity_matching_service import (  # noqa: E402
    IdentityMatchingRepos,
    IdentityMatchResult,
    build_song_engines,
    trial_result,
)
from backend.services.title_scoring import v11_title_score  # noqa: E402

# Every song: hand-matched ones are scored for accepted_songs.csv, the rest for changed_songs.csv.
_SONGS_SQL = """
    SELECT id, broadcast_artist_id, original_title, normalized_title, normalized_signature,
           match_status, rejected_file_ids
      FROM track_identities
     ORDER BY broadcast_artist_id, id
"""
_CHOSEN_FILES_SQL = """
    SELECT m.identity_id, m.library_file_id
      FROM matches m JOIN track_identities ti ON ti.id = m.identity_id
     WHERE ti.match_status = 'manual_matched' AND m.library_file_id IS NOT NULL
"""
_FILE_TITLES_SQL = """
    SELECT id, coalesce(track_title, normalized_title, '') AS title
      FROM library_files WHERE id = ANY(%s)
"""
# A re-check rewinds these, so a change to them takes effect then (AUD-R029); an auto-match is
# never rewound (Lance, 2026-11-01: no rewind).
_RESCORED = {
    MatchStatus.PENDING,
    MatchStatus.NEEDS_REVIEW,
    MatchStatus.AUTO_REJECTED,
    MatchStatus.MANUAL_REJECTED,
}
_SAME_SCORE = 0.001

FROZEN_COLUMNS = ["log_title", "file_title", "v11_score", "frozen_score", "differs"]
CHANGED_COLUMNS = [
    "artist",
    "log_title",
    "db_status",
    "takes_effect",
    "today_status",
    "today_score",
    "today_file",
    "v11_status",
    "v11_score",
    "v11_file",
    "trial_status",
    "trial_score",
    "trial_file",
    "trial_reason",
]
ACCEPTED_COLUMNS = [
    "artist",
    "log_title",
    "chosen_file",
    "today_lands",
    "today_score",
    "today_file",
    "trial_lands",
    "trial_score",
    "trial_file",
    "trial_reason",
]


def connect_read_only(database_url: str) -> psycopg.Connection[Any]:
    """A connection on which every transaction is read-only: the dry run cannot write, so it
    cannot store MusicBrainz answers either."""
    return connect_sync(database_url, options="-c default_transaction_read_only=on")


class SkipStepB(RecordingSearchReachRepository):
    """Step B's pre-check, answering no for every artist so that no search is made; it notes
    the artists for which the real answer is yes."""

    def __init__(self, reach: RecordingSearchReachRepository) -> None:
        self._reach = reach
        self.skipped: set[str] = set()

    def has_file_beyond_artist_mbid(self, normalized_artist_name: str, artist_mbid: str) -> bool:
        if self._reach.has_file_beyond_artist_mbid(normalized_artist_name, artist_mbid):
            self.skipped.add(normalized_artist_name)
        return False


class NoMusicBrainz:
    """The dry run's MusicBrainz client. Never called: Step B is skipped."""

    def search_recording(self, artist_mbid: str, title: str, limit: int = 10) -> list[Any]:
        raise RuntimeError("the dry run never calls MusicBrainz")


class TimedV11:
    """v11_title_score, timed, keeping every pair it scored for the frozen check."""

    def __init__(self) -> None:
        self.calls = 0
        self.seconds = 0.0
        self.pairs: dict[tuple[str, str], float] = {}

    def __call__(self, log_title: str, file_title: str) -> float:
        start = time.perf_counter()
        score = v11_title_score(log_title, file_title)
        self.seconds += time.perf_counter() - start
        self.calls += 1
        self.pairs[(log_title, file_title)] = score
        return score


@dataclass(frozen=True)
class SongResults:
    song: BroadcastTrackIdentity
    artist: BroadcastArtist
    today: IdentityMatchResult
    v11: IdentityMatchResult
    trial: IdentityMatchResult


@dataclass(frozen=True)
class Timings:
    today_seconds: float
    trial_seconds: float


def load_songs(conn: psycopg.Connection[Any]) -> list[BroadcastTrackIdentity]:
    return [
        BroadcastTrackIdentity(
            id=row["id"],
            broadcast_artist_id=row["broadcast_artist_id"],
            original_title=row["original_title"],
            normalized_title=row["normalized_title"],
            normalized_signature=row["normalized_signature"],
            match_status=MatchStatus(row["match_status"]),
            rejected_file_ids=tuple(row["rejected_file_ids"] or ()),
        )
        for row in conn.execute(_SONGS_SQL).fetchall()
    ]


def resolve_all(
    songs: list[BroadcastTrackIdentity],
    artists: dict[UUID, BroadcastArtist],
    repos: IdentityMatchingRepos,
    v11: Callable[[str, str], float],
) -> tuple[list[SongResults], Timings]:
    """Each song's result from today's chain, V11.1's chain and trial mode. The songs come
    grouped by artist, so each artist's files are read once, as in a run."""
    engines = build_song_engines(
        songs,
        repos,
        NoMusicBrainz(),  # type: ignore[arg-type]
        get_settings().strong_match_threshold,
        trial_title_score=v11,
    )
    if engines.trial is None:
        raise RuntimeError("build_song_engines built no trial chain")
    results: list[SongResults] = []
    today_seconds = trial_seconds = 0.0
    for song in songs:
        artist = artists.get(song.broadcast_artist_id)
        if artist is None:
            continue  # an orphan: matching marks it for review, whatever the scorer
        start = time.perf_counter()
        today = engines.today.resolve(song, artist)
        middle = time.perf_counter()
        v11_result = engines.trial.resolve(song, artist)
        trial_seconds += time.perf_counter() - middle
        today_seconds += middle - start
        if today is None or v11_result is None:
            continue  # every chain ends in a result; matching rejects a song if not
        results.append(
            SongResults(song, artist, today, v11_result, trial_result(today, v11_result))
        )
    return results, Timings(today_seconds, trial_seconds)


def changed(result: SongResults) -> bool:
    today, trial = result.today, result.trial
    return (today.status, today.library_file_id) != (trial.status, trial.library_file_id)


def lands(result: IdentityMatchResult, chosen: set[UUID]) -> str:
    """Where a hand-matched song lands under one scorer: its outcome, and whether the file it
    shows is the one Lance chose."""
    if result.status == MatchStatus.AUTO_REJECTED:
        return "hidden"
    outcome = "auto-matched" if result.status == MatchStatus.AUTO_MATCHED else "review"
    if result.library_file_id is None:
        return f"{outcome}, no file"
    return f"{outcome}, {'chosen file' if result.library_file_id in chosen else 'another file'}"


def _score(result: IdentityMatchResult) -> str:
    return f"{result.confidence_score:.1f}"


def changed_row(result: SongResults, titles: dict[UUID, str]) -> list[str]:
    song, today, v11, trial = result.song, result.today, result.v11, result.trial
    rescored = song.match_status in _RESCORED
    return [
        result.artist.original_name,
        song.original_title,
        song.match_status.value,
        "next re-check" if rescored else "never (no rewind)",
        today.status.value,
        _score(today),
        _title(today, titles),
        v11.status.value,
        _score(v11),
        _title(v11, titles),
        trial.status.value,
        _score(trial),
        _title(trial, titles),
        trial.reason_detail or (trial.reason_code.value if trial.reason_code else ""),
    ]


def accepted_row(result: SongResults, titles: dict[UUID, str], chosen: set[UUID]) -> list[str]:
    today, trial = result.today, result.trial
    return [
        result.artist.original_name,
        result.song.original_title,
        "; ".join(sorted(titles.get(f, str(f)) for f in chosen)),
        lands(today, chosen),
        _score(today),
        _title(today, titles),
        lands(trial, chosen),
        _score(trial),
        _title(trial, titles),
        trial.reason_detail or (trial.reason_code.value if trial.reason_code else ""),
    ]


def _title(result: IdentityMatchResult, titles: dict[UUID, str]) -> str:
    if result.library_file_id is None:
        return ""
    return titles.get(result.library_file_id, str(result.library_file_id))


def load_frozen_v11(frozen: Path) -> Callable[[str, str], float]:
    """The frozen v11.py's scorer, once every file listed in FROZEN.sha256 checks out."""
    for line in (frozen / "FROZEN.sha256").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(" *", 1)
        if hashlib.sha256((frozen / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{name} changed since it was frozen: the frozen check is void")
    # Nothing may be written into the frozen folder, __pycache__ included, and its modules
    # (variants, patterns, ...) must not shadow later imports.
    writes_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(frozen))
    try:
        module = importlib.import_module("v11")
    finally:
        sys.path.remove(str(frozen))
        sys.dont_write_bytecode = writes_bytecode
    return lambda log_title, file_title: float(module.v11(log_title, file_title, None))


def frozen_rows(
    pairs: dict[tuple[str, str], float], frozen_v11: Callable[[str, str], float]
) -> list[list[str]]:
    rows = []
    for (log_title, file_title), score in sorted(pairs.items()):
        frozen = frozen_v11(log_title, file_title)
        differs = abs(score - frozen) > _SAME_SCORE
        rows.append([log_title, file_title, repr(score), repr(frozen), "yes" if differs else ""])
    return rows


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    # utf-8-sig, so a spreadsheet shows curly quotes and accents as they are.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


def _tally(outcomes: Iterable[str]) -> str:
    return ", ".join(f"{name} {count:,}" for name, count in Counter(outcomes).most_common())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--database-url", help="default: the app's configured database")
    parser.add_argument("--out", default="results/title_trial", help="folder for the reports")
    parser.add_argument(
        "--frozen", type=Path, help="the frozen V11.1 folder, to check the port against"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frozen_v11 = load_frozen_v11(args.frozen) if args.frozen else None
    v11 = TimedV11()

    with connect_read_only(args.database_url or get_settings().database_url) as conn:
        songs = load_songs(conn)
        artists = {
            a.id: a
            for a in PgBroadcastArtistRepository(conn).get_by_ids(
                list({s.broadcast_artist_id for s in songs})
            )
        }
        library_files = PgLibraryFileRepository(conn)
        skip_step_b = SkipStepB(library_files)
        repos = IdentityMatchingRepos(
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=library_files,
            rules_repo=PgMappingRuleRepository(conn),
            catalog_repo=PgArtistRepository(conn),
            recording_search_reach=skip_step_b,
        )
        results, timings = resolve_all(songs, artists, repos, v11)
        chosen: dict[UUID, set[UUID]] = {}
        for row in conn.execute(_CHOSEN_FILES_SQL).fetchall():
            chosen.setdefault(row["identity_id"], set()).add(row["library_file_id"])
        file_ids = {f for files in chosen.values() for f in files} | {
            r.library_file_id
            for result in results
            for r in (result.today, result.v11, result.trial)
            if r.library_file_id is not None
        }
        titles = {
            row["id"]: row["title"]
            for row in conn.execute(_FILE_TITLES_SQL, (list(file_ids),)).fetchall()
        }

    by_hand = [r for r in results if r.song.match_status == MatchStatus.MANUAL_MATCHED]
    undecided = [r for r in results if r.song.match_status != MatchStatus.MANUAL_MATCHED]
    moved = sorted(
        (r for r in undecided if changed(r)),
        key=lambda r: (r.today.status, r.trial.status, r.artist.original_name, r.song.id),
    )
    write_csv(out / "changed_songs.csv", CHANGED_COLUMNS, (changed_row(r, titles) for r in moved))
    write_csv(
        out / "accepted_songs.csv",
        ACCEPTED_COLUMNS,
        (accepted_row(r, titles, chosen.get(r.song.id, set())) for r in by_hand),
    )
    checked = frozen_rows(v11.pairs, frozen_v11) if frozen_v11 is not None else []
    write_csv(out / "frozen_check.csv", FROZEN_COLUMNS, checked)

    differing = sum(1 for row in checked if row[-1])
    print(f"Title-scoring trial dry run, read-only; reports in {out}")
    print(
        f"MusicBrainz not called: Step B skipped for {len(skip_step_b.skipped):,} artists "
        "that have a file only it could add"
    )
    rejected = sum(1 for r in undecided if r.song.match_status == MatchStatus.MANUAL_REJECTED)
    print(
        f"songs: {len(undecided):,} not matched by hand ({rejected:,} of them rejected by hand, "
        f"re-scored without their rejected files), {len(by_hand):,} matched by hand"
    )
    print(
        f"changed_songs.csv: {len(moved):,} songs differ in trial mode "
        f"({_tally(f'{r.today.status.value} -> {r.trial.status.value}' for r in moved)})"
    )
    takes_effect = sum(1 for r in moved if r.song.match_status in _RESCORED)
    print(
        f"  {takes_effect:,} take effect at the next re-check, "
        f"{len(moved) - takes_effect:,} never (existing auto-matches are not rewound)"
    )
    today_lands = _tally(lands(r.today, chosen.get(r.song.id, set())) for r in by_hand)
    trial_lands = _tally(lands(r.trial, chosen.get(r.song.id, set())) for r in by_hand)
    print(f"accepted_songs.csv: today: {today_lands}")
    print(f"  trial mode: {trial_lands}")
    per_pair = v11.seconds / v11.calls * 1e6 if v11.calls else 0.0
    print(
        f"V11.1: {v11.calls:,} pairs scored in {v11.seconds:.1f} s: {per_pair:.1f} µs per pair "
        "(E6: at most 200)"
    )
    print(
        f"trial chain: {timings.trial_seconds:.1f} s on top of today's chain "
        f"({timings.today_seconds:.1f} s, its reads included)"
    )
    if frozen_v11 is None:
        print("frozen_check.csv: not run (no --frozen), header only")
    else:
        print(
            f"frozen_check.csv: {len(checked):,} pairs against the frozen v11.py, "
            f"{differing:,} differ"
        )
    return 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())

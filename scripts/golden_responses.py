"""Golden responses of the app's routes on a gate copy, and their sample (phase 0b SPEC
2026-10-28, S7).

    python scripts/golden_responses.py --dsn <bench db> --out <file> [--sample <file>]
    python scripts/golden_responses.py --build-sample --dsn <bench db> --out <file>
    python scripts/golden_responses.py --compare <old> <new> [--report <file>] [--allow <file>]
        [--work-map <file>] [--mode <section>=<mode> ...]

It records what the routes return, through the app itself: the lifespan runs as
``tests/lifespan_client.py`` runs it (copied, not imported), less the boot migrations, the
leftover meter row's end (a write) and the ``system_logs`` sink, with streaming off and every
pooled connection READ ONLY. Sections:

- ``artist-list``: the artist list's first 3 pages: ``total``, ``order`` and one key per artist;
- ``artist-detail`` and ``work-detail``: one key per sample artist or work;
- ``queue``: the Resolution Center queue's first 5 pages: ``total``, ``unlikely_total``,
  ``order`` and one key per log name; ``queue-counts``: per bucket, its two totals;
- ``queue-search``: the queue searched for each sample log name's text, keyed by its id.

Volatile fields (keys ending ``_at`` or holding ``duration``) are dropped, and a list of titled
items has its ties (runs of one title) sorted by id. A sample id the route does not find is
recorded as ``{"status": <code>}``.

The sample (``audit/golden_sample.json``, tracked so it can be locked) lists ids by kind and
category: 50 random artists and 50 random works (picked by a hash of the id, so the same
database gives the same file), every artist of a name two or more MBIDs share among the files'
tags (M33), every work in a planned merge group (a query standing in for 3b's planner), every
log name with several links, and the placeholders.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import quote

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

TOOL = "golden_responses"
FORMAT_VERSION = 1
DEFAULT_SAMPLE = gate_common.ROOT / "audit" / "golden_sample.json"
COMPARE = gate_common.CompareSpec(tool=TOOL, work_keyed=frozenset({"work-detail"}))
PAGE = 50
ARTIST_PAGES = 3
QUEUE_PAGES = 5
BUCKETS = ("all", "quick_review", "needs_attention", "blocked")
READ_ONLY = "-c default_transaction_read_only=on"


def read_only_dsn(dsn: str) -> str:
    """*dsn* with every transaction READ ONLY: the app's pool, and so every route, can't write."""
    return psycopg.conninfo.make_conninfo(dsn, options=READ_ONLY)


# --- the app -------------------------------------------------------------------------------------


def _skipped(*_args: object, **_kwargs: object) -> None:
    return None


@contextmanager
def app_session() -> Iterator[Any]:
    """The app on the settings' database, sending the settings' token.

    The ``tests/lifespan_client.py`` recipe (S7: copied, not imported): a TestClient that runs
    the lifespan, with structlog's configuration put back on the way out. Three start-up steps
    are left out: the boot migrations (the copy is migrated by ``gate.py migrate``), the
    leftover meter row's end (it writes) and the ``system_logs`` sink (it writes).
    """
    import structlog
    from fastapi.testclient import TestClient

    import backend.main as main_module
    from backend.config import get_settings

    snapshot = structlog.get_config()
    # psycopg's async pool needs the selector event loop on Windows (as backend/run_server.py
    # sets it); the TestClient's loop comes from the policy.
    policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        with (
            mock.patch.object(main_module, "run_migrations", _skipped),
            mock.patch.object(main_module, "end_leftover_meter_row", _skipped),
            mock.patch.object(main_module, "configure_logging", _skipped),
            TestClient(main_module.app, raise_server_exceptions=False) as client,
        ):
            client.headers["X-Airwave-Token"] = get_settings().airwave_token
            yield client
    finally:
        structlog.configure(**snapshot)
        asyncio.set_event_loop_policy(policy)


def _volatile(key: str) -> bool:
    return key.endswith("_at") or "duration" in key


def _ties_by_id(items: list[Any]) -> list[Any]:
    """A list whose items all have a title and an id: each run of one title sorted by id (a
    route that orders only by title returns its ties in any order)."""
    if not items or not all(isinstance(i, dict) and {"title", "id"} <= set(i) for i in items):
        return items
    out: list[Any] = []
    for _title, run in itertools.groupby(items, key=lambda item: item["title"]):
        out.extend(sorted(run, key=lambda item: str(item["id"])))
    return out


def clean(value: Any) -> Any:
    """A response without its volatile fields, its titled lists' ties sorted by id."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not _volatile(k)}
    if isinstance(value, list):
        return _ties_by_id([clean(v) for v in value])
    return value


def _ok(response: Any) -> Any:
    if response.status_code != 200:
        raise SystemExit(f"{response.request.url} answered {response.status_code}")
    return response.json()


def _pages(client: Any, path: str, pages: int, totals: Sequence[str]) -> dict[str, Any]:
    """A paged list's first *pages* pages of PAGE: its totals, the ids in order, each item."""
    out: dict[str, Any] = {name: 0 for name in totals}
    order: list[str] = []
    for page in range(pages):
        body = _ok(client.get(path, params={"limit": PAGE, "offset": page * PAGE}))
        if page == 0:
            out.update({name: body[name] for name in totals})
        for item in body["items"]:
            order.append(str(item["id"]))
            out[str(item["id"])] = clean(item)
        if len(body["items"]) < PAGE:
            break
    out["order"] = order
    return out


def _answer(response: Any) -> Any:
    """The route's answer, or ``{"status": code}`` when it found nothing to answer with."""
    if response.status_code != 200:
        return {"status": response.status_code}
    return clean(response.json())


def _detail(client: Any, path: str) -> Any:
    return _answer(client.get(path))


def _queue_counts(client: Any) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for bucket in BUCKETS:
        params: dict[str, Any] = {"limit": 1}
        if bucket != "all":
            params["bucket"] = bucket
        body = _ok(client.get("/api/v1/matching/queue", params=params))
        counts[bucket] = {"total": body["total"], "unlikely_total": body["unlikely_total"]}
    return counts


def _queue_search(client: Any, names: Mapping[str, str | None]) -> dict[str, Any]:
    searched: dict[str, Any] = {}
    for name_id, text in sorted(names.items()):
        if text is None:
            searched[name_id] = {"status": 404}
            continue
        response = client.get("/api/v1/matching/queue", params={"limit": PAGE, "search": text})
        searched[name_id] = _answer(response)
    return searched


def _ids(sample: Mapping[str, Any], kind: str) -> list[str]:
    """Every id of one kind in the sample, whatever its category."""
    return sorted({str(i) for ids in sample.get(kind, {}).values() for i in ids})


def _sections(
    client: Any, sample: Mapping[str, Any], names: Mapping[str, str | None]
) -> dict[str, Any]:
    artists, works = _ids(sample, "artists"), _ids(sample, "works")
    return {
        "artist-list": _pages(client, "/api/v1/library/artists", ARTIST_PAGES, ("total",)),
        "artist-detail": {
            a: _detail(client, f"/api/v1/library/artists/{quote(a, safe='')}") for a in artists
        },
        "work-detail": {
            w: _detail(client, f"/api/v1/library/works/{quote(w, safe='')}") for w in works
        },
        "queue": _pages(client, "/api/v1/matching/queue", QUEUE_PAGES, ("total", "unlikely_total")),
        "queue-counts": _queue_counts(client),
        "queue-search": _queue_search(client, names),
    }


def _log_name_texts(conn: psycopg.Connection[Any], ids: Sequence[str]) -> dict[str, str | None]:
    """Each sample log name id -> the text the queue is searched for (None: no such name)."""
    found = {
        str(row["id"]): row["original_name"]
        for row in conn.execute(
            "SELECT id::text AS id, original_name FROM broadcast_artists WHERE id::text = ANY(%s)",
            (list(ids),),
        ).fetchall()
    }
    return {name_id: found.get(name_id) for name_id in ids}


def record(dsn: str, sample: Mapping[str, Any]) -> dict[str, Any]:
    """The golden responses for *sample*, the app on the settings' (read-only) database."""
    with gate_common.snapshot_read(dsn) as conn:
        head = gate_common.header(TOOL, FORMAT_VERSION, conn)
        names = _log_name_texts(conn, _ids(sample, "log_names"))
    with gate_common.quiet_logging(), app_session() as client:
        sections = _sections(client, sample, names)
    return {"header": head, "sections": sections}


# --- the sample ----------------------------------------------------------------------------------

SAMPLE_SEED = ":golden-0b"
STATION_PLACEHOLDERS = (
    "SPOT BREAK",
    "SPECIAL PROGRAMMING",
    "SYNDICATED PROGRAM",
    "VARIOUS",
    "VARIOUS ARTISTS",
)
_RANDOM_ARTISTS_SQL = """
SELECT id FROM artists WHERE name IS DISTINCT FROM mbid
ORDER BY md5(id || %(seed)s), id LIMIT 50
"""
_RANDOM_WORKS_SQL = "SELECT id FROM works ORDER BY md5(id || %(seed)s), id LIMIT 50"
# M33: a normalized artist name two or more artist MBIDs share among the files' tags; every
# catalog artist of that name or of those MBIDs.
_SHARED_NAME_SQL = """
WITH shared AS (
    SELECT normalized_artist_name AS name FROM library_files
    WHERE artist_mbid IS NOT NULL AND normalized_artist_name IS NOT NULL
    GROUP BY 1 HAVING count(DISTINCT artist_mbid) > 1
)
SELECT a.id FROM artists a
WHERE a.normalized_name IN (SELECT name FROM shared)
   OR a.mbid IN (
        SELECT lf.artist_mbid FROM library_files lf
        JOIN shared s ON s.name = lf.normalized_artist_name)
"""
# A planned merge group, standing in for 3b's planner: works whose files' recordings share a
# cached MusicBrainz work link (a recording lookup, or a release lookup's tracks), or works
# with one base title (before a bracket) under one normalized artist name.
_MERGE_GROUP_SQL = r"""
WITH rec_work AS (
    SELECT substr(c.cache_key, 11) AS rec, r #>> '{work,id}' AS mb_work
    FROM mb_cache c,
         jsonb_path_query(c.response_data, '$.relations[*] ? (@.type == "performance")') r
    WHERE starts_with(c.cache_key, 'recording:')
    UNION
    SELECT t #>> '{recording,id}', r #>> '{work,id}'
    FROM mb_cache c,
         jsonb_path_query(c.response_data, '$.media[*].tracks[*]') t,
         jsonb_path_query(t, '$.recording.relations[*] ? (@.type == "performance")') r
    WHERE starts_with(c.cache_key, 'release:')
),
linked AS (
    SELECT DISTINCT rw.mb_work, lf.work_id
    FROM rec_work rw JOIN library_files lf ON lf.recording_mbid = rw.rec
    WHERE lf.work_id IS NOT NULL AND rw.mb_work IS NOT NULL
    UNION
    SELECT w.mbid, w.id FROM works w WHERE w.mbid IS NOT NULL
),
titled AS (
    SELECT DISTINCT w.id,
           regexp_replace(lower(w.title), '\s*[\(\[].*$', '') AS base,
           lf.normalized_artist_name AS artist
    FROM works w JOIN library_files lf ON lf.work_id = w.id
    WHERE lf.normalized_artist_name IS NOT NULL
)
SELECT work_id AS id FROM linked
WHERE mb_work IN (SELECT mb_work FROM linked GROUP BY 1 HAVING count(DISTINCT work_id) > 1)
UNION
SELECT id FROM titled
WHERE (base, artist) IN (
    SELECT base, artist FROM titled GROUP BY 1, 2 HAVING count(DISTINCT id) > 1)
"""


def _column(
    conn: psycopg.Connection[Any], sql: str, params: Mapping[str, Any] | None = None
) -> list[str]:
    return sorted({str(row["id"]) for row in conn.execute(sql, params).fetchall()})


def _placeholder_names(
    conn: psycopg.Connection[Any], links: Mapping[str, Mapping[str, str]]
) -> list[str]:
    """The five station placeholders, and every log name linked to a special-purpose artist."""
    from backend.services.artist_linking_service import SPECIAL_PURPOSE_MBIDS
    from backend.services.normalization import normalize_artist

    by_name = _column(
        conn,
        "SELECT id::text AS id FROM broadcast_artists WHERE normalized_name = ANY(%(names)s)",
        {"names": sorted({normalize_artist(n) for n in STATION_PLACEHOLDERS})},
    )
    linked = [name for name, keys in links.items() if set(keys) & SPECIAL_PURPOSE_MBIDS]
    return sorted(set(by_name) | set(linked))


def build_sample(dsn: str) -> dict[str, Any]:
    """The sample for *dsn* (see the module docstring); the same database, the same file."""
    from backend.services.artist_linking_service import SPECIAL_PURPOSE_MBIDS

    seed = {"seed": SAMPLE_SEED}
    with gate_common.snapshot_read(dsn) as conn:
        links = gate_common.active_links(conn)
        return {
            "artists": {
                "random": _column(conn, _RANDOM_ARTISTS_SQL, seed),
                "shared_name": _column(conn, _SHARED_NAME_SQL),
                "placeholder": _column(
                    conn,
                    "SELECT id FROM artists WHERE mbid = ANY(%(mbids)s)",
                    {"mbids": sorted(SPECIAL_PURPOSE_MBIDS)},
                ),
            },
            "works": {
                "random": _column(conn, _RANDOM_WORKS_SQL, seed),
                "merge_group": _column(conn, _MERGE_GROUP_SQL),
            },
            "log_names": {
                "several_links": sorted(n for n, keys in links.items() if len(keys) > 1),
                "placeholder": _placeholder_names(conn, links),
            },
        }


# --- the command line ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", help="the gate copy (a bench database)")
    parser.add_argument("--out", type=Path, help="the file to write")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--build-sample", action="store_true", help="write the sample instead")
    gate_common.add_compare_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.compare:
        return gate_common.run_compare(args, COMPARE)
    if not args.dsn or args.out is None:
        parser.error("--dsn and --out are required (or --compare)")
    started = time.perf_counter()
    if args.build_sample:
        gate_common.point_at(args.dsn)
        sample = build_sample(args.dsn)
        gate_common.write_doc(args.out, sample)
        sizes = {kind: {c: len(ids) for c, ids in cats.items()} for kind, cats in sample.items()}
        print(f"{TOOL} sample: {sizes} -> {args.out}")
        return 0
    dsn = read_only_dsn(args.dsn)
    gate_common.point_at(dsn)
    doc = record(dsn, gate_common.read_doc(args.sample))
    gate_common.write_doc(args.out, doc)
    counts = ", ".join(f"{name} {len(rows)}" for name, rows in sorted(doc["sections"].items()))
    print(f"{TOOL}: {counts}; {time.perf_counter() - started:.1f} s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

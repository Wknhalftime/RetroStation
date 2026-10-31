"""Phase 0b gate (SPEC 2026-10-28): golden responses and their sample (S7).

``scripts/golden_responses.py --dsn <db> --out <file> [--sample <file>]`` records what the app's
routes return, through the app itself (its lifespan with no boot migrations and streaming off)
on a READ ONLY connection. The sample (``audit/golden_sample.json`` by default) lists ids by
kind: ``artists``, ``works`` and ``log_names``, each a dict of category -> ids. Sections:

- ``artist-list``: the artist list's first 3 pages (50 a page): ``total``, ``order`` (the ids in
  the route's order) and one key per artist id;
- ``artist-detail`` and ``work-detail``: one key per sample artist or work;
- ``queue``: the Resolution Center queue's first 5 pages: ``total``, ``unlikely_total``,
  ``order`` and one key per log name id; ``queue-counts``: ``total`` and ``unlikely_total`` per
  bucket (``all``, ``quick_review``, ``needs_attention``, ``blocked``);
- ``queue-search``: the queue searched for each sample log name's text, keyed by its id.

Volatile fields (timestamps, durations) are dropped and ties in a list the route leaves
unordered are sorted by id. A sample id the route does not find is recorded as
``{"status": <code>}``. ``--compare`` works as the snapshot's (S2).

``--build-sample --dsn <db> --out <file>`` writes the sample: 50 random artists and 50 random
works (seeded by id, so the same database gives the same file), every artist of a name two or
more MBIDs share among the files' tags (M33), every work in a planned merge group (works sharing
a cached MusicBrainz work link, or a base title under one normalized artist name), every log
name with several links, and the placeholders (the five station names and the MusicBrainz
special-purpose artists, with the log names linked to them).

Seeds go through the scan, the ingestion task, the routes and the locked shared seed. One is an
exception, stated where it is made: no route or task can give an artist a special-purpose MBID
that lasts through phase 1c, so the sample test inserts one into ``artists`` (stable columns).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.services.streaming.resource_meter import METER_ID
from backend.services.streaming.resource_meter import SCOPE as METER_SCOPE
from tests.integration import stream_seed as seed
from tests.scripts.gate_world import (
    ROOT,
    Conn,
    app_client,
    build_world,
    catalog_artist,
    file_named,
    ingest,
    load_tool,
    name_id,
    production_client,
    read_json,
    resolve_name,
    run_guarded,
    scan,
    tagged,
    work_of,
    write_json,
)

pytestmark = pytest.mark.usefixtures("queue_in_memory")

SECTIONS = {"artist-list", "artist-detail", "work-detail", "queue", "queue-counts", "queue-search"}
BUCKETS = ("all", "quick_review", "needs_attention", "blocked")
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"  # a MusicBrainz special-purpose artist
DAY = "1995-03-14"


@pytest.fixture(scope="module")
def golden() -> Any:
    return load_tool("golden_responses")


def without_volatile(value: Any) -> Any:
    """The route's JSON without timestamps and durations, as the recorder keeps it."""
    if isinstance(value, dict):
        return {
            k: without_volatile(v)
            for k, v in value.items()
            if not k.endswith("_at") and "duration" not in k
        }
    if isinstance(value, list):
        return [without_volatile(v) for v in value]
    return value


def keys_anywhere(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys_anywhere(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys_anywhere(v)


def record(golden: Any, dsn: str, sample: Path, out: Path) -> dict[str, Any]:
    assert golden.main(["--dsn", dsn, "--sample", str(sample), "--out", str(out)]) == 0
    return read_json(out)


def compare(tool: Any, old: Path, new: Path, tmp_path: Path, *extra: str) -> tuple[int, Any]:
    report = tmp_path / f"report-{uuid4().hex}.json"  # a fresh file: never a stale report
    code = tool.main(["--compare", str(old), str(new), "--report", str(report), *extra])
    return code, read_json(report)


def titled_lists(value: Any) -> Iterator[list[dict[str, Any]]]:
    """Every list in a response whose items all carry a title and an id."""
    if isinstance(value, dict):
        for v in value.values():
            yield from titled_lists(v)
    elif isinstance(value, list):
        if value and all(isinstance(v, dict) and {"title", "id"} <= set(v) for v in value):
            yield value
        for v in value:
            yield from titled_lists(v)


def sample_file(tmp_path: Path, artists: list[str], works: list[str], names: list[str]) -> Path:
    return write_json(
        tmp_path / "sample.json",
        {"artists": {"random": artists}, "works": {"random": works}, "log_names": {"x": names}},
    )


# --- S7 ------------------------------------------------------------------------------------------


@pytest.mark.integration
def test_records_the_routes_for_the_sample(
    golden: Any,
    conn: Conn,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: None,
) -> None:
    """S7: the artist list, artist and work detail for the sample, the queue's pages and counts,
    and the queue searched for the sample's log names, each as the route returns it."""
    with app_client(bench) as client:
        world = build_world(conn, client, tmp_path, monkeypatch)
    gamma_text = "GAMMA"
    sample = sample_file(
        tmp_path,
        [world.alpha_artist, world.beta_artist, "no-such-artist"],
        [world.first_work],
        [str(world.delta), str(world.gamma)],
    )

    doc = record(golden, bench, sample, tmp_path / "golden.json")

    assert doc["header"]["format_version"] == golden.FORMAT_VERSION
    s = doc["sections"]
    assert set(s) == SECTIONS
    with app_client(bench) as client:
        listed = client.get("/api/v1/library/artists", params={"limit": 50}).json()
        assert s["artist-list"]["total"] == listed["total"]
        assert s["artist-list"]["order"] == [a["id"] for a in listed["items"]]
        for item in listed["items"]:
            assert s["artist-list"][item["id"]] == without_volatile(item)
        for artist in (world.alpha_artist, world.beta_artist):
            detail = client.get(f"/api/v1/library/artists/{artist}").json()
            assert s["artist-detail"][artist] == without_volatile(detail)
        assert s["artist-detail"]["no-such-artist"] == {"status": 404}
        work = client.get(f"/api/v1/library/works/{world.first_work}").json()
        assert s["work-detail"] == {world.first_work: without_volatile(work)}

        queue = client.get("/api/v1/matching/queue", params={"limit": 50}).json()
        assert queue["items"]  # GAMMA and DELTA's pending line at least
        assert s["queue"]["total"] == queue["total"]
        assert s["queue"]["unlikely_total"] == queue["unlikely_total"]
        assert s["queue"]["order"] == [item["id"] for item in queue["items"]]
        for item in queue["items"]:
            assert s["queue"][item["id"]] == without_volatile(item)
        for bucket in BUCKETS:
            params: dict[str, Any] = {"limit": 1}
            if bucket != "all":
                params["bucket"] = bucket
            counts = client.get("/api/v1/matching/queue", params=params).json()
            assert s["queue-counts"][bucket] == {
                "total": counts["total"],
                "unlikely_total": counts["unlikely_total"],
            }
        searched = client.get(
            "/api/v1/matching/queue", params={"limit": 50, "search": gamma_text}
        ).json()
        assert s["queue-search"][str(world.gamma)] == without_volatile(searched)
        assert set(s["queue-search"]) == {str(world.delta), str(world.gamma)}


@pytest.mark.integration
@pytest.mark.slow
def test_no_boot_migrations_and_no_writes(
    golden: Any, conn: Conn, bench: str, tmp_path: Path, tool_env: None
) -> None:
    """S7, S10: run as its own process, where the app's test-only flag that skips the boot
    migrations is ignored: the tool runs no boot migrations, leaves a RUNNING meter row (as a
    copy of the streaming dev DB holds) as it is, opens only the bench database and imports no
    test code (the lifespan recipe is copied)."""
    progress = PgTaskProgressRepository(conn)
    now = datetime.now(UTC)
    progress.upsert(
        TaskProgress(
            task_id=METER_ID,
            task_type=TaskType.STREAM_RESOURCES,
            status=TaskStatus.RUNNING,
            progress_data={"open_streams": 0, "scope": METER_SCOPE},
            started_at=now,
            updated_at=now,
        )
    )
    [last] = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchall()
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (last["version"],))
    conn.commit()
    sample = sample_file(tmp_path, ["no-such-artist"], [], [])
    try:
        done = run_guarded(
            bench,
            "golden_responses.py",
            "--dsn",
            bench,
            "--sample",
            str(sample),
            "--out",
            str(tmp_path / "golden.json"),
        )
        still_missing = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (last["version"],)
        ).fetchall()
        meter = progress.get_by_id(METER_ID)
    finally:
        conn.rollback()
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (last["version"],),
        )
        conn.commit()

    assert done.returncode == 0, done.stderr[-3000:]
    assert not still_missing  # the boot migrations did not run
    assert meter is not None and meter.status == TaskStatus.RUNNING


@pytest.mark.integration
def test_read_only_and_streaming_off(
    golden: Any,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: None,
) -> None:
    """S7: the app's pool hands out read-only connections, and streaming is off whatever the
    environment says; the tool leaves the app as it found it."""
    import backend.main as main_module
    from backend.db import pool as pool_module
    from backend.main import app

    monkeypatch.setenv("STREAM_ENABLED", "true")
    read_only: list[object] = []
    streaming: list[bool] = []
    real_init_pool = pool_module.init_pool

    def probing_init_pool(database_url: str) -> Any:
        app_pool = real_init_pool(database_url)
        real_open = app_pool.open

        async def open_and_probe(*args: Any, **kwargs: Any) -> None:
            await real_open(*args, **kwargs)
            async with app_pool.connection() as pooled:
                cur = await pooled.execute("SHOW transaction_read_only")
                row = await cur.fetchone()
                read_only.append(next(iter(row.values())) if isinstance(row, dict) else row)

        app_pool.open = open_and_probe  # type: ignore[method-assign]
        return app_pool

    async def no_streaming(settings: Any) -> None:
        streaming.append(settings.stream_enabled)

    monkeypatch.setattr(main_module, "init_pool", probing_init_pool)
    monkeypatch.setattr(main_module, "start_streaming", no_streaming)

    record(golden, bench, sample_file(tmp_path, [], [], []), tmp_path / "golden.json")

    assert read_only == ["on"]
    assert streaming == [False]
    assert app.dependency_overrides == {}


@pytest.mark.integration
def test_first_pages(golden: Any, conn: Conn, bench: str, tmp_path: Path, tool_env: None) -> None:
    """S7: the artist list's first 3 pages (150 of 160 artists) and the queue's first 5 pages
    (250 of 260 names), in each route's own order."""
    for _ in range(160):
        seed.work(conn)  # the locked shared seed: an act and a work
    conn.commit()
    rows = [
        (f"ACT {n:03d}", "SONG", f"{DAY} {8 + n // 60:02d}:{n % 60:02d}:00") for n in range(260)
    ]
    ingest(conn, seed.station(conn, "AC"), rows, "pages")

    s = record(golden, bench, sample_file(tmp_path, [], [], []), tmp_path / "golden.json")[
        "sections"
    ]

    with app_client(bench) as client:
        artists = [
            item
            for offset in (0, 50, 100)
            for item in client.get(
                "/api/v1/library/artists", params={"limit": 50, "offset": offset}
            ).json()["items"]
        ]
        queue = [
            item
            for offset in (0, 50, 100, 150, 200)
            for item in client.get(
                "/api/v1/matching/queue", params={"limit": 50, "offset": offset}
            ).json()["items"]
        ]
    assert s["artist-list"]["total"] == 160
    assert s["artist-list"]["order"] == [a["id"] for a in artists]
    assert len(s["artist-list"]["order"]) == 150
    assert set(s["artist-list"]) == {"total", "order", *(a["id"] for a in artists)}
    assert s["queue"]["total"] == 260
    assert s["queue"]["order"] == [item["id"] for item in queue]
    assert len(s["queue"]["order"]) == 250
    assert set(s["queue"]) == {"total", "unlikely_total", "order", *(i["id"] for i in queue)}


@pytest.mark.integration
def test_empty_db(golden: Any, bench: str, tmp_path: Path, tool_env: None) -> None:
    """E5: an empty database gives empty lists and zero counts; two runs compare equal."""
    sample = sample_file(tmp_path, [], [], [])
    old = tmp_path / "old.json"
    s = record(golden, bench, sample, old)["sections"]
    new = tmp_path / "new.json"
    record(golden, bench, sample, new)

    assert s["artist-list"] == {"total": 0, "order": []}
    assert s["queue"] == {"total": 0, "unlikely_total": 0, "order": []}
    assert s["queue-counts"] == {b: {"total": 0, "unlikely_total": 0} for b in BUCKETS}
    assert s["artist-detail"] == s["work-detail"] == s["queue-search"] == {}
    assert compare(golden, old, new, tmp_path) == (
        0,
        {"refused": None, "failed": [], "allowed": []},
    )


@pytest.mark.integration
def test_volatile_fields_dropped_and_ties_sorted_by_id(
    golden: Any,
    conn: Conn,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: None,
) -> None:
    """S7: no timestamp or duration survives, a list the route orders only by title has its ties
    sorted by id, and two runs over the same data give the same file."""
    with app_client(bench) as client:
        world = build_world(conn, client, tmp_path, monkeypatch)
        split = client.post(
            f"/api/v1/library/works/{world.first_work}/split", json={"file_id": str(world.single)}
        )
        assert split.status_code == 201, split.text
    sample = sample_file(tmp_path, [world.alpha_artist], [world.first_work], [])

    first = record(golden, bench, sample, tmp_path / "first.json")
    record(golden, bench, sample, tmp_path / "second.json")

    assert (tmp_path / "first.json").read_bytes() == (tmp_path / "second.json").read_bytes()
    assert not [k for k in keys_anywhere(first["sections"]) if k.endswith("_at")]
    assert not [k for k in keys_anywhere(first["sections"]) if "duration" in k]
    lists = list(titled_lists(first["sections"]["artist-detail"][world.alpha_artist]))
    assert lists
    tied = 0
    for items in lists:
        titled = [(item["title"], item["id"]) for item in items]
        assert titled == sorted(titled)
        tied += len(titled) - len({title for title, _id in titled})
    assert tied  # the split left two works titled First Song


@pytest.mark.integration
def test_compare_works_as_in_the_snapshot(
    golden: Any,
    conn: Conn,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: None,
) -> None:
    """S7: --compare finds a changed response by its key, passes it when allowlisted, and
    refuses files of another format version."""
    with app_client(bench) as client:
        world = build_world(conn, client, tmp_path, monkeypatch)
    sample = sample_file(tmp_path, [world.alpha_artist], [world.first_work], [])
    old = tmp_path / "old.json"
    data = record(golden, bench, sample, old)
    with app_client(bench) as client:
        master = client.put(
            f"/api/v1/library/works/{world.first_work}/master",
            json={"preferred_file_id": str(world.single)},
        )
        assert master.status_code == 200, master.text
    new = tmp_path / "new.json"
    record(golden, bench, sample, new)

    code, report = compare(golden, old, new, tmp_path)
    assert code == 1
    assert ("work-detail", world.first_work) in {(d["section"], d["key"]) for d in report["failed"]}
    allow = tmp_path / "allow.txt"
    lines = {f"{d['section']} {d['key']} Set master on purpose" for d in report["failed"]}
    allow.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    assert compare(golden, old, new, tmp_path, "--allow", str(allow))[0] == 0

    newer = write_json(
        tmp_path / "newer.json",
        {**data, "header": {**data["header"], "format_version": golden.FORMAT_VERSION + 1}},
    )
    assert compare(golden, old, newer, tmp_path)[0] == 2


# --- the sample ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_sample_builder_picks_every_kind(
    golden: Any,
    conn: Conn,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: None,
    no_rate_limit: None,
) -> None:
    """The sample holds the random artists and works (all of them, here: fewer than 50), every
    shared-name artist, every work of a planned merge group, every log name with several
    links, and the placeholders; the same database gives the same file."""
    root = tmp_path / "library"
    twin_mbids = ("0b000000-0000-4000-8000-0000000000c1", "0b000000-0000-4000-8000-0000000000c2")
    tagged(root / "twin-1.mp3", "Twin Act", "Twin One", artist_mbid=twin_mbids[0])
    tagged(root / "twin-2.mp3", "Twin Act", "Twin Two", artist_mbid=twin_mbids[1])
    tagged(root / "various.mp3", "Various Artists", "Medley", artist_mbid=VARIOUS_ARTISTS)
    rec_1, rec_2, rec_3, rec_4 = (f"0b000000-0000-4000-8000-0000000000d{n}" for n in range(4))
    tagged(root / "linked-1.mp3", "Cover One", "Same Song", recording_mbid=rec_1)
    tagged(root / "linked-2.mp3", "Cover Two", "Same Tune", recording_mbid=rec_2)
    tagged(root / "linked-3.mp3", "Cover Three", "Other Song", recording_mbid=rec_3)
    tagged(root / "linked-4.mp3", "Cover Four", "Other Tune", recording_mbid=rec_4)
    tagged(root / "split-a.mp3", "Split Act", "Split Song", release_type="album")
    tagged(root / "split-b.mp3", "Split Act", "Split Song", release_type="single")
    scan(conn, root)
    release = "0b000000-0000-4000-8000-0000000000c5"
    mb_work = {"type": "performance", "work": {"id": "0b000000-0000-4000-8000-0000000000c6"}}
    release_body = {
        "id": release,
        "title": "Covers",
        "media": [
            {
                "tracks": [
                    {"recording": {"id": rec, "title": "Same Song", "relations": [mb_work]}}
                    for rec in (rec_1, rec_2)
                ]
            }
        ],
    }

    other_work = {"type": "performance", "work": {"id": "0b000000-0000-4000-8000-0000000000c7"}}

    def lookup_answer(entity: str, query: str) -> dict[str, Any]:
        if entity == release:
            return release_body
        assert entity in (rec_3, rec_4), (entity, query)
        return {"id": entity, "title": "Other Song", "relations": [other_work]}

    mb = production_client(conn, lookup_answer, ttl_days=30)
    mb.lookup_release(release)
    mb.lookup_recording(rec_3)
    mb.lookup_recording(rec_4)
    # The seed rule's one exception here: an act holding a special-purpose MBID, which no route
    # keeps through phase 1c. artists keeps these columns through every phase.
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, mbid, origin)
           VALUES ('va-0b', 'Various Artists', 'Various Artists', %s, 'musicbrainz')""",
        (VARIOUS_ARTISTS,),
    )
    conn.commit()
    with app_client(bench) as client:
        split_b = file_named(conn, "split-b.mp3")
        split = client.post(
            f"/api/v1/library/works/{work_of(conn, split_b)}/split",
            json={"file_id": str(split_b)},
        )
        assert split.status_code == 201, split.text
        station = seed.station(conn, "AC")
        ingest(
            conn,
            station,
            [
                ("SPOT BREAK", "BREAK", f"{DAY} 08:00:00"),
                ("VARIOUS ARTISTS", "MEDLEY", f"{DAY} 08:04:00"),
                ("TWO LINKS", "A SONG", f"{DAY} 08:12:00"),
            ],
            "sample",
        )
        two = name_id(conn, "TWO LINKS")
        resolve_name(client, two, catalog_artist(client, "Split Act")["id"])
        resolve_name(client, two, catalog_artist(client, "Cover One")["id"])
        twins = [
            a["id"]
            for a in client.get("/api/v1/library/artists", params={"search": "Twin Act"}).json()[
                "items"
            ]
        ]
    conn.commit()

    out = tmp_path / "sample.json"
    assert golden.main(["--build-sample", "--dsn", bench, "--out", str(out)]) == 0
    again = tmp_path / "again.json"
    assert golden.main(["--build-sample", "--dsn", bench, "--out", str(again)]) == 0
    assert out.read_bytes() == again.read_bytes()

    sample = read_json(out)
    every_artist = {r["id"] for r in conn.execute("SELECT id FROM artists").fetchall()}
    every_work = {r["id"] for r in conn.execute("SELECT id FROM works").fetchall()}
    assert set(sample["artists"]["random"]) == every_artist
    assert set(sample["works"]["random"]) == every_work
    assert set(twins) <= set(sample["artists"]["shared_name"])
    split_works = {work_of(conn, file_named(conn, f"split-{x}.mp3")) for x in "ab"}
    linked_works = {work_of(conn, file_named(conn, f"linked-{x}.mp3")) for x in "1234"}
    assert len(split_works) == 2 and len(linked_works) == 4
    assert split_works | linked_works <= set(sample["works"]["merge_group"])
    assert set(sample["log_names"]["several_links"]) == {str(two)}
    placeholders = {str(name_id(conn, n)) for n in ("SPOT BREAK", "VARIOUS ARTISTS")}
    assert placeholders <= set(sample["log_names"]["placeholder"])
    assert "va-0b" in sample["artists"]["placeholder"]


def test_tracked_sample_is_well_formed(golden: Any) -> None:
    """``audit/golden_sample.json`` (built from a gate copy of the dev DB, locked with these
    tests): every kind and category, 50 random artists and 50 random works, ids only."""
    sample = read_json(ROOT / "audit" / "golden_sample.json")
    categories = {
        "artists": {"random", "shared_name", "placeholder"},
        "works": {"random", "merge_group"},
        "log_names": {"several_links", "placeholder"},
    }
    assert {kind: set(sample[kind]) for kind in categories} == categories
    assert len(set(sample["artists"]["random"])) == 50
    assert len(set(sample["works"]["random"])) == 50
    for kind, names in categories.items():
        for category in names:
            ids = sample[kind][category]
            assert ids, (kind, category)
            assert all(isinstance(i, str) and i for i in ids)
            assert ids == sorted(set(ids))

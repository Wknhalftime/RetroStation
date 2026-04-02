from __future__ import annotations

from uuid import uuid4

import psycopg

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.enums import EnrichmentStatus, SelectionMethod
from backend.domain.models import (
    Artist,
    FormatOverride,
    LibraryFile,
    LibraryQuarantine,
    Recording,
    SongMaster,
    Work,
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_artist(mbid: str, name: str = "Test Artist", sort_name: str | None = None) -> Artist:
    return Artist(
        id=mbid,
        name=name,
        sort_name=sort_name or name,
    )


def _make_work(mbid: str, title: str, artist_id: str) -> Work:
    return Work(id=mbid, title=title, artist_id=artist_id)


def _make_recording(mbid: str, title: str, work_id: str | None = None) -> Recording:
    return Recording(id=mbid, title=title, work_id=work_id, duration_ms=210_000)


def _make_file(
    file_path: str,
    format: str = "flac",
    recording_id: str | None = None,
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash="abc123",
        format=format,
        enrichment_status=enrichment_status,
        recording_id=recording_id,
        track_title="Track Title",
        release_title="Album Title",
        bitrate=320,
        duration_ms=210_000,
    )


def _seed_canonical_chain(
    conn: psycopg.Connection,
    artist_mbid: str = "artist-001",
    work_mbid: str = "work-001",
    recording_mbid: str = "rec-001",
    file_path: str = "/music/track.flac",
    format: str = "flac",
    artist_name: str = "Test Artist",
    sort_name: str | None = None,
) -> tuple[Artist, Work, Recording, LibraryFile]:
    """Insert artist → work → recording → library_file chain."""
    artist = PgArtistRepository(conn).upsert(
        _make_artist(artist_mbid, name=artist_name, sort_name=sort_name)
    )
    work = PgWorkRepository(conn).upsert(
        _make_work(work_mbid, "Test Work", artist_id=artist_mbid)
    )
    recording = PgRecordingRepository(conn).upsert(
        _make_recording(recording_mbid, "Test Recording", work_id=work_mbid)
    )
    lf = PgLibraryFileRepository(conn).upsert(
        _make_file(file_path, format=format, recording_id=recording_mbid)
    )
    conn.commit()
    return artist, work, recording, lf


# ---------------------------------------------------------------------------
# Tests — LibraryStatus
# ---------------------------------------------------------------------------


class TestLibraryStatus:
    def test_returns_zeros_when_empty(self, client) -> None:
        resp = client.get("/api/v1/library/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_files"] == 0
        assert data["quarantine_count"] == 0
        assert data["by_format"] == {}
        assert data["by_enrichment"] == {}

    def test_counts_files_and_formats(self, client, db_conn) -> None:
        repo = PgLibraryFileRepository(db_conn)
        repo.upsert(_make_file("/music/a.flac", format="flac"))
        repo.upsert(_make_file("/music/b.flac", format="flac"))
        repo.upsert(
            _make_file(
                "/music/c.mp3",
                format="mp3",
                enrichment_status=EnrichmentStatus.ENRICHED,
            )
        )
        db_conn.commit()

        resp = client.get("/api/v1/library/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_files"] == 3
        assert data["by_format"]["flac"] == 2
        assert data["by_format"]["mp3"] == 1
        assert data["by_enrichment"]["pending"] == 2
        assert data["by_enrichment"]["enriched"] == 1

    def test_counts_quarantine(self, client, db_conn) -> None:
        PgLibraryQuarantineRepository(db_conn).create(
            LibraryQuarantine(
                id=uuid4(),
                file_path="/bad/corrupt.mp3",
                error_message="cannot decode",
            )
        )
        db_conn.commit()

        resp = client.get("/api/v1/library/status")
        assert resp.status_code == 200
        assert resp.json()["quarantine_count"] == 1


# ---------------------------------------------------------------------------
# Tests — PaginatedArtists
# ---------------------------------------------------------------------------


class TestLibraryArtists:
    def test_empty_returns_zero_total(self, client) -> None:
        resp = client.get("/api/v1/library/artists")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_paginated(self, client, db_conn) -> None:
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
            artist_name="Alpha Artist",
            sort_name="Alpha Artist",
        )
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-002",
            work_mbid="w-002",
            recording_mbid="r-002",
            file_path="/m/b.flac",
            artist_name="Beta Artist",
            sort_name="Beta Artist",
        )

        resp = client.get("/api/v1/library/artists?limit=50&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        # Ordered by sort_name
        assert data["items"][0]["name"] == "Alpha Artist"
        assert data["items"][1]["name"] == "Beta Artist"

    def test_work_count_and_file_count(self, client, db_conn) -> None:
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )

        resp = client.get("/api/v1/library/artists")
        data = resp.json()
        assert data["items"][0]["work_count"] == 1
        assert data["items"][0]["file_count"] == 1

    def test_search_filters_by_name(self, client, db_conn) -> None:
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
            artist_name="Beatles",
        )
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-002",
            work_mbid="w-002",
            recording_mbid="r-002",
            file_path="/m/b.flac",
            artist_name="Rolling Stones",
        )

        resp = client.get("/api/v1/library/artists?search=beatl")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Beatles"

    def test_search_case_insensitive(self, client, db_conn) -> None:
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
            artist_name="Beatles",
        )

        resp = client.get("/api/v1/library/artists?search=BEATL")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_pagination_offset(self, client, db_conn) -> None:
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
            artist_name="Alpha Artist",
            sort_name="Alpha Artist",
        )
        _seed_canonical_chain(
            db_conn,
            artist_mbid="a-002",
            work_mbid="w-002",
            recording_mbid="r-002",
            file_path="/m/b.flac",
            artist_name="Beta Artist",
            sort_name="Beta Artist",
        )

        resp = client.get("/api/v1/library/artists?limit=1&offset=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2  # total is unaffected by offset
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Beta Artist"


# ---------------------------------------------------------------------------
# Tests — ArtistDetail
# ---------------------------------------------------------------------------


class TestArtistDetail:
    def test_not_found(self, client) -> None:
        resp = client.get("/api/v1/library/artists/nonexistent-artist-id")
        assert resp.status_code == 404

    def test_found_with_works(self, client, db_conn) -> None:
        artist, work, _, _ = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
            artist_name="Test Artist",
        )

        resp = client.get(f"/api/v1/library/artists/{artist.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == artist.id
        assert data["name"] == "Test Artist"
        assert len(data["works"]) == 1
        assert data["works"][0]["id"] == work.id
        assert data["works"][0]["recording_count"] == 1
        assert data["works"][0]["has_master"] is False

    def test_has_master_true_when_master_set(self, client, db_conn) -> None:
        artist, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )
        PgSongMasterRepository(db_conn).upsert(
            SongMaster(
                id=uuid4(),
                work_id=work.id,
                preferred_file_id=lf.id,
                selection_method=SelectionMethod.AUTO,
            )
        )
        db_conn.commit()

        resp = client.get(f"/api/v1/library/artists/{artist.id}")
        data = resp.json()
        assert data["works"][0]["has_master"] is True

    def test_artist_with_no_works(self, client, db_conn) -> None:
        PgArtistRepository(db_conn).upsert(_make_artist("a-001", name="Solo Artist"))
        db_conn.commit()

        resp = client.get("/api/v1/library/artists/a-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["works"] == []


# ---------------------------------------------------------------------------
# Tests — WorkDetail
# ---------------------------------------------------------------------------


class TestWorkDetail:
    def test_not_found(self, client) -> None:
        resp = client.get("/api/v1/library/works/nonexistent-work-id")
        assert resp.status_code == 404

    def test_found_with_recordings_and_files(self, client, db_conn) -> None:
        _, work, recording, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )

        resp = client.get(f"/api/v1/library/works/{work.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == work.id
        assert data["artist_id"] == "a-001"
        assert len(data["recordings"]) == 1
        rec = data["recordings"][0]
        assert rec["id"] == recording.id
        assert len(rec["files"]) == 1
        assert rec["files"][0]["format"] == "flac"
        assert rec["files"][0]["enrichment_status"] == "pending"
        assert data["song_master"] is None
        assert data["format_overrides"] == []

    def test_includes_song_master(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )
        PgSongMasterRepository(db_conn).upsert(
            SongMaster(
                id=uuid4(),
                work_id=work.id,
                preferred_file_id=lf.id,
                selection_method=SelectionMethod.AUTO,
            )
        )
        db_conn.commit()

        resp = client.get(f"/api/v1/library/works/{work.id}")
        data = resp.json()
        assert data["song_master"] is not None
        assert data["song_master"]["preferred_file_id"] == str(lf.id)
        assert data["song_master"]["selection_method"] == "auto"

    def test_includes_format_overrides(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )
        PgFormatOverrideRepository(db_conn).create(
            FormatOverride(
                id=uuid4(),
                work_id=work.id,
                format_name="flac",
                preferred_file_id=lf.id,
                notes="lossless preferred",
            )
        )
        db_conn.commit()

        resp = client.get(f"/api/v1/library/works/{work.id}")
        data = resp.json()
        assert len(data["format_overrides"]) == 1
        fo = data["format_overrides"][0]
        assert fo["format_name"] == "flac"
        assert fo["notes"] == "lossless preferred"

    def test_recording_with_no_files(self, client, db_conn) -> None:
        PgArtistRepository(db_conn).upsert(_make_artist("a-001"))
        PgWorkRepository(db_conn).upsert(_make_work("w-001", "A Work", artist_id="a-001"))
        PgRecordingRepository(db_conn).upsert(_make_recording("r-001", "A Recording", work_id="w-001"))
        db_conn.commit()

        resp = client.get("/api/v1/library/works/w-001")
        data = resp.json()
        assert len(data["recordings"]) == 1
        assert data["recordings"][0]["files"] == []


# ---------------------------------------------------------------------------
# Tests — SetMaster / DeleteMaster
# ---------------------------------------------------------------------------


class TestSetMaster:
    def test_set_manual_master(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )

        resp = client.put(
            f"/api/v1/library/works/{work.id}/master",
            json={"preferred_file_id": str(lf.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["work_id"] == work.id
        assert data["preferred_file_id"] == str(lf.id)
        assert data["selection_method"] == "manual"
        assert data["score"] is None

    def test_set_master_work_not_found(self, client, db_conn) -> None:
        # Need at least one file to reference
        lf = PgLibraryFileRepository(db_conn).upsert(_make_file("/m/a.flac"))
        db_conn.commit()

        resp = client.put(
            "/api/v1/library/works/nonexistent-work/master",
            json={"preferred_file_id": str(lf.id)},
        )
        assert resp.status_code == 404

    def test_revert_to_auto_by_upsert(self, client, db_conn) -> None:
        """Setting master twice with different files uses the second file."""
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )
        lf2 = PgLibraryFileRepository(db_conn).upsert(_make_file("/m/b.flac", format="mp3"))
        db_conn.commit()

        client.put(
            f"/api/v1/library/works/{work.id}/master",
            json={"preferred_file_id": str(lf.id)},
        )
        resp = client.put(
            f"/api/v1/library/works/{work.id}/master",
            json={"preferred_file_id": str(lf2.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["preferred_file_id"] == str(lf2.id)
        assert data["selection_method"] == "manual"

    def test_delete_master(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )
        client.put(
            f"/api/v1/library/works/{work.id}/master",
            json={"preferred_file_id": str(lf.id)},
        )

        resp = client.delete(f"/api/v1/library/works/{work.id}/master")
        assert resp.status_code == 204

        # Verify gone
        detail_resp = client.get(f"/api/v1/library/works/{work.id}")
        assert detail_resp.json()["song_master"] is None

    def test_delete_master_not_found(self, client, db_conn) -> None:
        PgArtistRepository(db_conn).upsert(_make_artist("a-001"))
        PgWorkRepository(db_conn).upsert(_make_work("w-001", "A Work", artist_id="a-001"))
        db_conn.commit()

        resp = client.delete("/api/v1/library/works/w-001/master")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — FormatOverrides
# ---------------------------------------------------------------------------


class TestFormatOverrides:
    def test_create_format_override(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )

        resp = client.post(
            f"/api/v1/library/works/{work.id}/format-overrides",
            json={
                "format_name": "flac",
                "preferred_file_id": str(lf.id),
                "notes": "prefer lossless",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["work_id"] == work.id
        assert data["format_name"] == "flac"
        assert data["preferred_file_id"] == str(lf.id)
        assert data["notes"] == "prefer lossless"
        assert "id" in data
        assert "created_at" in data

    def test_create_format_override_work_not_found(self, client, db_conn) -> None:
        lf = PgLibraryFileRepository(db_conn).upsert(_make_file("/m/a.flac"))
        db_conn.commit()

        resp = client.post(
            "/api/v1/library/works/nonexistent-work/format-overrides",
            json={"format_name": "flac", "preferred_file_id": str(lf.id)},
        )
        assert resp.status_code == 404

    def test_delete_format_override(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )

        create_resp = client.post(
            f"/api/v1/library/works/{work.id}/format-overrides",
            json={"format_name": "flac", "preferred_file_id": str(lf.id)},
        )
        assert create_resp.status_code == 201
        override_id = create_resp.json()["id"]

        del_resp = client.delete(
            f"/api/v1/library/works/{work.id}/format-overrides/{override_id}"
        )
        assert del_resp.status_code == 204

        # Verify gone from work detail
        detail_resp = client.get(f"/api/v1/library/works/{work.id}")
        assert detail_resp.json()["format_overrides"] == []

    def test_delete_format_override_not_found(self, client, db_conn) -> None:
        PgArtistRepository(db_conn).upsert(_make_artist("a-001"))
        PgWorkRepository(db_conn).upsert(_make_work("w-001", "A Work", artist_id="a-001"))
        db_conn.commit()

        resp = client.delete(
            f"/api/v1/library/works/w-001/format-overrides/{uuid4()}"
        )
        assert resp.status_code == 404

    def test_create_format_override_without_notes(self, client, db_conn) -> None:
        _, work, _, lf = _seed_canonical_chain(
            db_conn,
            artist_mbid="a-001",
            work_mbid="w-001",
            recording_mbid="r-001",
            file_path="/m/a.flac",
        )

        resp = client.post(
            f"/api/v1/library/works/{work.id}/format-overrides",
            json={"format_name": "flac", "preferred_file_id": str(lf.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["notes"] is None

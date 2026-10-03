"""The sign-off clip store: ``save_sign_off`` and ``remove_sign_off`` (PR G1, Task 3;
traceability G: T3.4-T3.16).

Requirements: D26 (the user's own sign-off clip, set in PR G); PG3 (FLAC, MP3 or WAV by
content; at most 25 MiB; 1 s to 5 min; content-named; the old file deleted after the commit;
unique partials; a sweep); review rulings I1 (delete only after the settings commit), I2
(the detected format decides the stored extension; the upload's name never decides the
format or the path), I3 (storage failures are ``ClipStorageError``; unique partials; a
locked file is kept, logged and swept later); M4 (the size limit is exactly 25 x 2^20 bytes).

The probe is a port (``ClipProbe``): these tests give the format and length it detects, so
no encoder is needed (the real probe is pinned in ``tests/services/test_clip_probe.py``).
Disk failures are injected by patching ``Path.write_bytes``, ``os.replace`` and
``Path.unlink`` (the plan's Achievability table). No test sleeps: file ages are set with
``os.utime``.
"""

from __future__ import annotations

import errno
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.domain.streaming import (
    MAX_CLIP_BYTES,
    ClipFormat,
    ClipLengthError,
    ClipStorageError,
    ClipTooLargeError,
    ProbedClip,
    SignOff,
    SignOffError,
    UnreadableClipError,
    UnsupportedClipError,
)
from backend.domain.system import UserSetting
from backend.services.streaming.sign_off import (
    PARTIAL_MAX_AGE,
    ClipUpload,
    SignOffPorts,
    remove_sign_off,
    save_sign_off,
    stored_sign_off,
)
from tests.fakes.user_settings import FakeUserSettingRepository

KEY = "stream_sign_off"
CONTENT_NAME = re.compile(r"^[0-9a-f]{16}\.(flac|mp3|wav)$")
ORIGINAL_UNLINK = Path.unlink
ORIGINAL_WRITE_BYTES = Path.write_bytes


class RecordingSettings(FakeUserSettingRepository):
    """The settings fake, recording writes into the shared event list."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def upsert(self, setting: UserSetting) -> UserSetting:
        self._events.append(f"upsert {setting.key}")
        return super().upsert(setting)

    def delete(self, key: str) -> None:
        self._events.append(f"delete {key}")
        super().delete(key)


@dataclass
class Store:
    """One sign-off folder with recording ports: the probe answers ``probed``."""

    folder: Path
    events: list[str] = field(default_factory=list)
    probed: ProbedClip | SignOffError = field(
        default_factory=lambda: ProbedClip(ClipFormat.MP3, 10_000)
    )
    probe_paths: list[Path] = field(default_factory=list)
    commit_error: Exception | None = None
    settings: RecordingSettings = field(init=False)

    def __post_init__(self) -> None:
        self.settings = RecordingSettings(self.events)

    def probe(self, path: Path) -> ProbedClip:
        self.probe_paths.append(path)
        if isinstance(self.probed, SignOffError):
            raise self.probed
        return self.probed

    def commit(self) -> None:
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    @property
    def ports(self) -> SignOffPorts:
        return SignOffPorts(
            settings=self.settings, folder=self.folder, probe=self.probe, commit=self.commit
        )

    def save(self, name: str, data: bytes) -> SignOff:
        return save_sign_off(self.ports, ClipUpload(name=name, data=data))

    def setting(self) -> str | None:
        found = self.settings.get(KEY)
        return None if found is None else found.value

    def files(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir()) if self.folder.exists() else []

    def file_events(self) -> list[str]:
        """The events, without the clean-up of this upload's own partial."""
        return [e for e in self.events if not e.endswith(".partial")]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "sign-off")  # not created: the first save makes it


@pytest.fixture
def spy_unlink(store: Store, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Record every ``Path.unlink`` as an event; names in the returned set refuse to go."""
    locked: set[str] = set()

    def unlink(self: Path, missing_ok: bool = False) -> None:
        store.events.append(f"unlink {self.name}")
        if self.name in locked:
            raise PermissionError(errno.EACCES, "in use by another process", str(self))
        ORIGINAL_UNLINK(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    return locked


def age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


@pytest.mark.parametrize(
    ("name", "detected"),
    [("a.mp3", ClipFormat.FLAC), ("b.wav", ClipFormat.MP3), ("c", ClipFormat.WAV)],
)
def test_the_stored_extension_comes_from_the_detected_format(
    store: Store, name: str, detected: ClipFormat
) -> None:
    # T3.4 (PG3, I2): the stored file is <16 hex>.<detected format>; the setting says the
    # detected format; the upload's name is kept only as the name; the probe never sees it.
    store.probed = ProbedClip(detected, 4_000)
    saved = store.save(name, b"clip bytes")
    assert CONTENT_NAME.match(saved.file_name)
    assert saved.file_name.endswith(f".{detected}")
    assert (saved.format, saved.span_ms, saved.name) == (detected, 4_000, name)
    assert store.files() == [saved.file_name]
    assert json.loads(store.setting() or "{}")["format"] == str(detected)
    assert stored_sign_off(store.settings) == saved
    [probed_path] = store.probe_paths
    assert probed_path.suffix == ".partial"


@pytest.mark.parametrize(
    "name", ["ogg-content.mp3", "m4a-content.flac"], ids=["ogg-as-mp3", "m4a-as-flac"]
)
def test_a_detected_format_other_than_flac_mp3_or_wav_is_refused(store: Store, name: str) -> None:
    # T3.5 (I2): the probe detects OGG or M4A whatever the name says.
    store.probed = UnsupportedClipError("the clip is Ogg Vorbis; use FLAC, MP3 or WAV")
    with pytest.raises(UnsupportedClipError):
        store.save(name, b"OggS....")
    assert store.files() == []
    assert store.setting() is None


@pytest.mark.parametrize(
    ("size", "kept"), [(26_214_400, True), (26_214_401, False)], ids=["25MiB", "25MiB+1"]
)
def test_the_size_limit_is_exactly_25_mib(store: Store, size: int, kept: bool) -> None:
    # T3.6 (PG3, M4): 25 x 2^20 bytes is allowed; one byte more is refused before anything is
    # written or probed.
    assert MAX_CLIP_BYTES == 25 * 2**20 == 26_214_400
    data = bytes(size)
    if kept:
        saved = store.save("big.mp3", data)
        assert (store.folder / saved.file_name).stat().st_size == size
        return
    with pytest.raises(ClipTooLargeError):
        store.save("big.mp3", data)
    assert store.files() == []
    assert store.probe_paths == []
    assert store.setting() is None


@pytest.mark.parametrize(
    ("span_ms", "kept"), [(999, False), (1_000, True), (300_000, True), (300_001, False)]
)
def test_a_clip_lasts_1_second_to_5_minutes(store: Store, span_ms: int, kept: bool) -> None:
    # T3.7 (PG3): a refused length leaves no file and no setting.
    store.probed = ProbedClip(ClipFormat.FLAC, span_ms)
    if kept:
        assert store.save("id.flac", b"flac").span_ms == span_ms
        return
    with pytest.raises(ClipLengthError):
        store.save("id.flac", b"flac")
    assert store.files() == []
    assert store.setting() is None


def test_an_unreadable_clip_is_refused_and_leaves_no_file(store: Store) -> None:
    # T3.8 (error handling).
    store.probed = UnreadableClipError("the file is not audio that can be read")
    with pytest.raises(UnreadableClipError):
        store.save("noise.mp3", b"\x00\x01\x02")
    assert store.files() == []
    assert store.setting() is None


def test_a_new_clip_replaces_the_old_and_the_old_file_goes_after_the_commit(
    store: Store, spy_unlink: set[str]
) -> None:
    # T3.9 (PG3, I1): upsert, commit, then delete the old file.
    old = store.save("old.mp3", b"old clip")
    store.events.clear()
    new = store.save("new.mp3", b"new clip")
    assert store.file_events() == [f"upsert {KEY}", "commit", f"unlink {old.file_name}"]
    assert store.files() == [new.file_name]
    assert stored_sign_off(store.settings) == new


def test_an_old_clip_still_in_use_is_kept_and_logged(store: Store, spy_unlink: set[str]) -> None:
    # T3.10 (I3): a live engine holds the old file open; the new clip is stored anyway, the
    # old file stays for a later sweep, and the failure is logged.
    old = store.save("old.mp3", b"old clip")
    spy_unlink.add(old.file_name)
    with capture_logs() as logs:
        new = store.save("new.mp3", b"new clip")
    assert stored_sign_off(store.settings) == new
    assert store.files() == sorted([old.file_name, new.file_name])
    failures = [e for e in logs if e["event"] == "stream_sign_off_cleanup_failed"]
    assert failures and failures[0]["log_level"] == "warning"
    assert old.file_name in json.dumps(failures[0], default=str)


def test_removing_the_clip_clears_commits_then_deletes(store: Store, spy_unlink: set[str]) -> None:
    # T3.11 (D26, I1): delete the setting, commit, then delete the file; a second remove is
    # fine.
    clip = store.save("bye.mp3", b"bye")
    store.events.clear()
    remove_sign_off(store.ports)
    assert store.file_events()[:3] == [f"delete {KEY}", "commit", f"unlink {clip.file_name}"]
    assert store.files() == []
    assert store.setting() is None
    remove_sign_off(store.ports)
    assert store.setting() is None


def test_the_same_clip_twice_is_one_file_and_nothing_is_deleted(
    store: Store, spy_unlink: set[str]
) -> None:
    # T3.12 (I3; guard on the shim): the same content is the same file; it is never deleted
    # as "the previous clip", and the second upload's partial is discarded.
    first = store.save("one.mp3", b"same bytes")
    store.events.clear()
    second = store.save("again.mp3", b"same bytes")
    assert second.file_name == first.file_name
    assert f"unlink {first.file_name}" not in store.events
    assert store.files() == [first.file_name]
    assert stored_sign_off(store.settings) == second


def test_a_failed_commit_keeps_the_old_file(store: Store) -> None:
    # T3.13 (I1): the committed setting still names the old file, so it must still exist.
    old = store.save("old.mp3", b"old clip")
    store.commit_error = ClipStorageError("the clip could not be stored: connection lost")
    with pytest.raises(ClipStorageError):
        store.save("new.mp3", b"new clip")
    assert (store.folder / old.file_name).exists()


def _fail_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing a partial runs out of space half-way."""

    def write_bytes(self: Path, data: bytes) -> int:
        ORIGINAL_WRITE_BYTES(self, data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device", str(self))

    monkeypatch.setattr(Path, "write_bytes", write_bytes)


def _fail_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving the partial onto its content name is refused (a file held open)."""

    def replace(src: object, dst: object) -> None:
        raise PermissionError(errno.EACCES, "in use by another process", str(dst))

    monkeypatch.setattr(os, "replace", replace)


@pytest.mark.parametrize(
    "fail", [_fail_write, _fail_replace], ids=["ENOSPC-on-write", "PermissionError-on-replace"]
)
def test_a_write_that_fails_is_a_storage_error_and_leaves_no_partial(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    fail: Callable[[pytest.MonkeyPatch], None],
) -> None:
    # T3.14 (I3): a domain error (503 at the route), never an OSError (a 500); no partial
    # left; the stored clip unchanged.
    old = store.save("old.mp3", b"old clip")
    before = store.setting()
    fail(monkeypatch)
    with pytest.raises(ClipStorageError):
        store.save("new.mp3", b"new clip")
    assert [n for n in store.files() if n.endswith(".partial")] == []
    assert store.setting() == before
    assert (store.folder / old.file_name).exists()


def test_each_upload_stages_under_its_own_partial_name(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T3.15 (I3): two uploads of the same bytes never share a partial.
    written: list[Path] = []

    def write_bytes(self: Path, data: bytes) -> int:
        written.append(self)
        return ORIGINAL_WRITE_BYTES(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    store.save("one.mp3", b"same bytes")
    store.save("two.mp3", b"same bytes")
    partials = [p for p in written if p.name.endswith(".partial")]
    assert len(partials) == 2
    assert partials[0] != partials[1]
    assert [n for n in store.files() if n.endswith(".partial")] == []


def test_a_save_sweeps_leftovers_but_not_fresh_partials(store: Store, spy_unlink: set[str]) -> None:
    # T3.16 (I3): an old clip that is not current and a partial older than an hour are
    # removed; another upload's fresh partial is kept; a locked leftover is left and logged.
    assert PARTIAL_MAX_AGE.total_seconds() == 3600
    store.folder.mkdir(parents=True)
    leftover = store.folder / "aaaaaaaaaaaaaaaa.mp3"
    stale = store.folder / "11111111-1111-4111-8111-111111111111.partial"
    fresh = store.folder / "22222222-2222-4222-8222-222222222222.partial"
    locked = store.folder / "bbbbbbbbbbbbbbbb.wav"
    for path in (leftover, stale, fresh, locked):
        path.write_bytes(b"x")
    age(stale, 2 * 3600)
    age(fresh, 60)
    spy_unlink.add(locked.name)
    with capture_logs() as logs:
        clip = store.save("new.mp3", b"new clip")
    assert store.files() == sorted([clip.file_name, fresh.name, locked.name])
    failures = [e for e in logs if e["event"] == "stream_sign_off_cleanup_failed"]
    assert any(locked.name in json.dumps(e, default=str) for e in failures)

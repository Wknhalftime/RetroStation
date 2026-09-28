"""Pairing each missing row with the present row holding the same track."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

from backend.domain.enums import AudioHashKind, FileStatus
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile, MissingFilePlan
from backend.services.missing_file_reconciliation_service import (
    choose_successor,
    is_same_track,
    plan_missing_file_moves,
    release_key,
    successor_candidates,
)
from tests.fakes.library_files import FakeLibraryFileRepository


def _row(
    path: str,
    *,
    missing: bool = False,
    work_id: str | None = "w1",
    recording_mbid: str | None = "rec-1",
    release_mbid: str | None = "rel-1",
    duration_ms: int | None = 54_040,
    release_title: str | None = "Pulp Fiction",
    disc_number: int | None = 1,
    track_number: int | None = 16,
    normalized_title: str | None = "ezekiel 25 17",
    normalized_artist_name: str | None = "samuel l jackson",
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        format="flac",
        file_status=FileStatus.MISSING if missing else FileStatus.PRESENT,
        work_id=work_id,
        audio=AudioMetadata(
            recording_mbid=recording_mbid,
            release_mbid=release_mbid,
            duration_ms=duration_ms,
            release_title=release_title,
            disc_number=disc_number,
            track_number=track_number,
            normalized_title=normalized_title,
            normalized_artist_name=normalized_artist_name,
        ),
    )


def _plan(missing: list[LibraryFile], present: list[LibraryFile]) -> MissingFilePlan:
    return plan_missing_file_moves(missing, lambda _m: present)


OLD = r"D:\Music\Pulp Fiction\Samuel L. Jackson - Ezekiel 25;17.flac"
NEW = r"D:\Music\Pulp Fiction_ Music From the Motion Picture\[dialogue] - Ezekiel 25_17.flac"


def test_the_single_present_copy_of_the_recording_is_the_successor() -> None:
    old, new = _row(OLD, missing=True), _row(NEW, duration_ms=54_900)

    plan = _plan([old], [new])

    assert [(m.missing_id, m.successor_id) for m in plan.moves] == [(old.id, new.id)]
    assert plan.ambiguous == () and plan.unmatched == ()


def test_a_retag_without_mbids_matches_on_the_release_track() -> None:
    old = _row(OLD, missing=True, recording_mbid=None, release_mbid=None)
    new = _row(NEW, recording_mbid="rec-9", release_mbid="rel-9")

    assert is_same_track(old, new)


def test_durations_more_than_two_seconds_apart_are_not_the_same_track() -> None:
    old, new = _row(OLD, missing=True), _row(NEW, duration_ms=54_040 + 2_001)

    assert not is_same_track(old, new)


def test_a_row_without_a_duration_is_not_the_same_track() -> None:
    assert not is_same_track(_row(OLD, missing=True, duration_ms=None), _row(NEW))


def test_same_recording_on_another_release_is_not_the_same_track() -> None:
    old = _row(OLD, missing=True)
    new = _row(NEW, release_mbid="rel-2", release_title="Greatest Hits", track_number=4)

    assert not is_same_track(old, new)


def test_no_disc_number_on_both_sides_counts_as_equal() -> None:
    old = _row(OLD, missing=True, recording_mbid=None, disc_number=None)
    new = _row(NEW, recording_mbid=None, disc_number=None)

    assert is_same_track(old, new)


def test_several_copies_are_settled_by_an_identical_file_name() -> None:
    old = _row(r"D:\old\16 Ezekiel.flac", missing=True)
    same_name = _row(r"D:\new\16 Ezekiel.flac")
    other = _row(r"D:\compilation\Ezekiel 25_17.flac")

    plan = _plan([old], [other, same_name])

    assert [m.successor_id for m in plan.moves] == [same_name.id]


def test_several_copies_without_one_name_match_are_ambiguous() -> None:
    old = _row(OLD, missing=True)

    plan = _plan([old], [_row(r"D:\a\x.flac"), _row(r"D:\b\y.flac")])

    assert plan.moves == ()
    assert plan.ambiguous == (OLD,)


def test_a_present_row_succeeds_only_one_missing_row() -> None:
    first = _row(r"D:\a\one.flac", missing=True)
    second = _row(r"D:\b\two.flac", missing=True)
    new = _row(NEW)

    plan = _plan([second, first], [new])

    assert [m.missing_id for m in plan.moves] == [first.id]
    assert plan.ambiguous == (second.file_path,)


def test_a_claimed_pick_leaves_the_later_claimant_ambiguous() -> None:
    first = _row(r"D:\a\16 Ezekiel.flac", missing=True)
    second = _row(r"D:\b\16 Ezekiel.flac", missing=True)
    same_name = _row(r"D:\new\16 Ezekiel.flac")
    other = _row(r"D:\compilation\Ezekiel 25_17.flac")

    plan = _plan([second, first], [other, same_name])

    assert [(m.missing_id, m.successor_id) for m in plan.moves] == [(first.id, same_name.id)]
    assert plan.ambiguous == (second.file_path,)


def test_a_successor_without_a_work_waits_for_the_next_run() -> None:
    old, new = _row(OLD, missing=True), _row(NEW, work_id=None)

    plan = _plan([old], [new])

    assert plan.moves == ()
    assert plan.unmatched == (OLD,)


def test_a_missing_row_is_never_a_successor() -> None:
    old = _row(OLD, missing=True)
    also_gone = _row(NEW, missing=True)

    plan = _plan([old], [also_gone])

    assert plan.moves == ()
    assert plan.unmatched == (OLD,)


def test_a_move_between_works_is_flagged() -> None:
    old, new = _row(OLD, missing=True, work_id="w1"), _row(NEW, work_id="w2")

    (move,) = _plan([old], [new]).moves

    assert move.crosses_work
    assert (move.missing_work_id, move.successor_work_id) == ("w1", "w2")


def test_candidates_come_from_both_lookups_once_each() -> None:
    repo = FakeLibraryFileRepository()
    old = repo.upsert(_row(OLD))
    repo.mark_missing(OLD)
    new = repo.upsert(_row(NEW))

    assert [c.id for c in successor_candidates(old, repo)] == [new.id]


_AUDIO = AudioHash(AudioHashKind.FLAC_MD5, "c" * 32)


def _with_audio(row: LibraryFile, audio_hash: AudioHash | None = _AUDIO) -> LibraryFile:
    return dataclasses.replace(row, audio_hash=audio_hash)


def test_an_audio_match_needs_no_matching_tags_or_duration() -> None:
    old = _with_audio(_row(OLD, missing=True))
    retagged = _with_audio(
        _row(
            NEW,
            recording_mbid=None,
            release_mbid=None,
            release_title="Pulp Fiction (Collector's Edition)",
            normalized_title="ezekiel 25 17 dialogue",
            duration_ms=60_000,
        )
    )

    assert is_same_track(old, retagged)
    assert _plan([old], [retagged]).moves[0].successor_id == retagged.id


def test_an_audio_match_wins_over_a_tag_match() -> None:
    old = _with_audio(_row(OLD, missing=True))
    tag_twin = _with_audio(
        _row(r"D:\Music\Other\Samuel L. Jackson - Ezekiel 25;17.flac"),
        AudioHash(AudioHashKind.FLAC_MD5, "d" * 32),
    )
    audio_twin = _with_audio(_row(NEW, recording_mbid=None, normalized_title="dialogue"))

    plan = _plan([old], [tag_twin, audio_twin])

    assert [m.successor_id for m in plan.moves] == [audio_twin.id]


def test_several_audio_matches_settle_by_file_name() -> None:
    old = _with_audio(_row(OLD, missing=True))
    same_name = _with_audio(_row(r"D:\Music\Moved\Samuel L. Jackson - Ezekiel 25;17.flac"))
    other_name = _with_audio(_row(NEW, recording_mbid=None, normalized_title="dialogue"))

    plan = _plan([old], [other_name, same_name])

    assert [m.successor_id for m in plan.moves] == [same_name.id]


def test_an_ungrouped_audio_match_is_not_used_yet() -> None:
    old = _with_audio(_row(OLD, missing=True, recording_mbid=None, release_title=None))
    ungrouped = _with_audio(_row(NEW, work_id=None))

    plan = _plan([old], [ungrouped])

    assert plan.moves == ()
    assert plan.unmatched == (OLD,)


def test_different_audio_falls_back_to_the_tag_rules() -> None:
    old = _with_audio(_row(OLD, missing=True))
    re_encoded = _with_audio(_row(NEW), AudioHash(AudioHashKind.FLAC_MD5, "d" * 32))

    assert is_same_track(old, re_encoded)  # same recording, same release, duration within 2 s


def test_successor_candidates_include_rows_with_the_same_audio() -> None:
    repo = FakeLibraryFileRepository()
    old = repo.upsert(_with_audio(_row(OLD, missing=True, recording_mbid=None, release_title=None)))
    twin = repo.upsert(_with_audio(_row(NEW, recording_mbid=None, release_title=None)))

    assert twin.id in {c.id for c in successor_candidates(old, repo)}


def test_an_audio_match_waits_while_a_hashable_candidate_has_no_fingerprint() -> None:
    old = _with_audio(_row(OLD, missing=True))
    compilation_twin = _with_audio(_row(r"D:\Music\Hits\16 Ezekiel.flac"))
    retagged = dataclasses.replace(_row(NEW), format="mp3")

    assert choose_successor(old, [compilation_twin, retagged]) is None
    assert _plan([old], [compilation_twin, retagged]).ambiguous == (OLD,)


def test_a_never_hashable_candidate_does_not_hold_an_audio_match_back() -> None:
    old = _with_audio(_row(OLD, missing=True))
    audio_twin = _with_audio(_row(r"D:\Music\Hits\16 Ezekiel.flac"))
    ogg = dataclasses.replace(_row(NEW), format="ogg")

    assert choose_successor(old, [audio_twin, ogg]) == audio_twin


def test_several_audio_matches_without_a_name_match_do_not_fall_back_to_tags() -> None:
    old = _with_audio(_row(OLD, missing=True))
    twin_a = _with_audio(_row(r"D:\a\x.flac"))
    twin_b = _with_audio(_row(r"D:\b\y.flac"))
    tag_match = _with_audio(_row(NEW), AudioHash(AudioHashKind.FLAC_MD5, "d" * 32))

    assert choose_successor(old, [twin_a, twin_b, tag_match]) is None


PROMO_OLD_TITLE = "Promo Only: Mainstream Radio (March 1999)"
PROMO_NEW_TITLE = "Promo Only: Mainstream Radio, March 1999"
PROMO_OLD = r"D:\Music\Promo Only\Mainstream Radio 1999-03\14 Mariah Carey - Heartbreaker.flac"
PROMO_NEW = r"D:\Music\Promo Only\Mainstream Radio, March 1999\14 Heartbreaker.flac"


def test_release_key_ignores_punctuation_and_spacing() -> None:
    assert release_key(PROMO_OLD_TITLE) == release_key(PROMO_NEW_TITLE)
    assert release_key(PROMO_NEW_TITLE) == "promoonlymainstreamradiomarch1999"


def test_release_key_ignores_case() -> None:
    assert release_key("PULP FICTION") == release_key("Pulp Fiction")


def test_release_key_does_not_equate_an_ampersand_with_and() -> None:
    assert release_key("Rock & Roll") != release_key("Rock and Roll")


def test_release_key_of_a_title_without_letters_or_digits_is_none() -> None:
    assert release_key("...!?") is None
    assert release_key("") is None
    assert release_key(None) is None


def test_release_key_keeps_non_latin_letters() -> None:
    # "Kimi no Na wa." in Japanese, and "Serdtse - 2" in Cyrillic.
    assert release_key("\u541b\u306e\u540d\u306f\u3002") == "\u541b\u306e\u540d\u306f"
    assert release_key("\u0421\u0435\u0440\u0434\u0446\u0435 \u2014 2") == (
        "\u0441\u0435\u0440\u0434\u0446\u0435" + "2"
    )


def _promo(
    path: str,
    release_title: str,
    *,
    missing: bool = False,
    mbid: str | None = None,
    disc_number: int | None = 1,
    track_number: int = 14,
    duration_ms: int = 266_000,
) -> LibraryFile:
    return _row(
        path,
        missing=missing,
        recording_mbid=mbid,
        release_mbid=None if mbid is None else "rel-promo-1999-03",
        duration_ms=duration_ms,
        release_title=release_title,
        disc_number=disc_number,
        track_number=track_number,
        normalized_title="heartbreaker",
        normalized_artist_name="mariah carey",
    )


def _old_promo() -> LibraryFile:
    return _promo(PROMO_OLD, PROMO_OLD_TITLE, missing=True)


def test_a_retag_that_only_repunctuates_the_release_title_pairs_the_moved_file() -> None:
    repo = FakeLibraryFileRepository()
    old = repo.upsert(_old_promo())
    repo.mark_missing(PROMO_OLD)
    new = repo.upsert(_promo(PROMO_NEW, PROMO_NEW_TITLE, mbid="rec-1", duration_ms=267_500))

    plan = plan_missing_file_moves(repo.get_missing(), lambda m: successor_candidates(m, repo))

    assert [(m.missing_id, m.successor_id) for m in plan.moves] == [(old.id, new.id)]


def test_a_repunctuated_release_on_another_disc_or_track_is_not_the_same_track() -> None:
    old = _old_promo()

    assert not is_same_track(old, _promo(PROMO_NEW, PROMO_NEW_TITLE, disc_number=2))
    assert not is_same_track(old, _promo(PROMO_NEW, PROMO_NEW_TITLE, track_number=15))


def test_a_repunctuated_release_more_than_two_seconds_off_is_not_the_same_track() -> None:
    new = _promo(PROMO_NEW, PROMO_NEW_TITLE, duration_ms=266_000 + 2_001)

    assert not is_same_track(_old_promo(), new)


def test_release_titles_without_letters_or_digits_are_not_the_same_release() -> None:
    old = _promo(PROMO_OLD, "?!", missing=True)

    assert not is_same_track(old, _promo(PROMO_NEW, "?!"))


def test_two_releases_whose_titles_normalize_equal_are_ambiguous() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_old_promo())
    repo.mark_missing(PROMO_OLD)
    repo.upsert(_promo(PROMO_NEW, PROMO_NEW_TITLE))
    repo.upsert(
        _promo(r"D:\Music\Copy\Heartbreaker.flac", "PROMO ONLY - Mainstream Radio: March, 1999")
    )

    plan = plan_missing_file_moves(repo.get_missing(), lambda m: successor_candidates(m, repo))

    assert plan.moves == ()
    assert plan.ambiguous == (PROMO_OLD,)

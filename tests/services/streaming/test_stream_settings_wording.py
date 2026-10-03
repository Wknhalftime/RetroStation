"""The sign-off problems the Streaming page shows are written for the user (G1 review, M3).

Requirements: design note 8 (a missing clip file reads "the clip's file is missing; upload it
again"); H9 (a bad stored value is reported, never raised, and tells the user what to do).
The locked T3.17 and T3.27 check the state; this checks the wording.
"""

from __future__ import annotations

from pathlib import Path

from backend.domain.streaming import ClipFormat, SignOff
from backend.domain.system import UserSetting
from backend.services.streaming.stream_settings import StreamingState, read_stream_settings
from tests.fakes.user_settings import FakeUserSettingRepository

KEY = "stream_sign_off"
CLIP = SignOff(
    file_name="0123456789abcdef.mp3", format=ClipFormat.MP3, span_ms=12_500, name="Good night.mp3"
)


def test_a_missing_clip_file_tells_the_user_to_upload_it_again(tmp_path: Path) -> None:
    settings = FakeUserSettingRepository()
    settings.upsert(UserSetting(key=KEY, value=CLIP.to_setting()))
    read = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert read.sign_off == CLIP
    assert read.sign_off_problem == "the clip's file is missing; upload it again"


def test_a_bad_stored_sign_off_tells_the_user_to_upload_the_clip_again(tmp_path: Path) -> None:
    settings = FakeUserSettingRepository({KEY: "not json"})
    read = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert read.sign_off is None
    assert read.sign_off_problem == "the stored sign-off could not be read; upload the clip again"

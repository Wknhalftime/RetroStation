"""FakeSongMasterRepository mirrors PgSongMasterRepository's upsert contract.

PgSongMasterRepository.upsert updates on conflict only WHERE the stored pick is 'auto',
so a MANUAL pick survives any upsert. A fake that overwrites it hides callers that rely
on upsert to replace a manual master (tests/integration/test_cross_work_fold_masters.py
has the Pg twin of these tests).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from tests.fakes.song_masters import FakeSongMasterRepository


def _pick(file_id: UUID, method: SelectionMethod) -> SongMaster:
    return SongMaster(id=uuid4(), work_id="w1", preferred_file_id=file_id, selection_method=method)


@pytest.mark.parametrize("incoming", [SelectionMethod.AUTO, SelectionMethod.MANUAL])
def test_upsert_never_overwrites_a_manual_pick(incoming: SelectionMethod) -> None:
    repo = FakeSongMasterRepository()
    chosen, other = uuid4(), uuid4()
    repo.upsert(_pick(chosen, SelectionMethod.MANUAL))

    repo.upsert(_pick(other, incoming))

    master = repo.get_by_work("w1")
    assert master is not None
    assert (master.preferred_file_id, master.selection_method) == (
        chosen,
        SelectionMethod.MANUAL,
    )


def test_upsert_replaces_an_auto_pick() -> None:
    repo = FakeSongMasterRepository()
    first, second = uuid4(), uuid4()
    repo.upsert(_pick(first, SelectionMethod.AUTO))

    repo.upsert(_pick(second, SelectionMethod.MANUAL))

    master = repo.get_by_work("w1")
    assert master is not None
    assert (master.preferred_file_id, master.selection_method) == (
        second,
        SelectionMethod.MANUAL,
    )

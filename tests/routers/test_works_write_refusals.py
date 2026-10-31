"""The works router's write refusals keep today's status codes and bodies (artist model phase
0c, SPEC S4; C75).

When the writes moved into ``work_curation_service`` (S3), each refusal became a domain error
the route maps back to its HTTP status. Each case here sends one refused write through the
app and expects today's status and ``{"detail": ...}`` body exactly. Choosing a master by hand
keeps its own error texts (``master_choice_service``), so only its status codes are pinned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.domain.curation import FormatOverride
from backend.domain.library import LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration

LIBRARY = "/api/v1/library"


@dataclass(frozen=True)
class _World:
    work: str
    other_work: str
    present: UUID
    missing: UUID
    foreign: UUID


def _seed(conn: psycopg.Connection[Any]) -> _World:
    factory = RepositoryFactory(conn)
    artist = factory.artists.upsert_local_artist("Nirvana", "nirvana")
    work = factory.works.create_local("Lithium", artist)
    other_work = factory.works.create_local("Breed", artist)

    def file(work_id: str, name: str) -> UUID:
        row = LibraryFile(id=uuid4(), file_path=rf"D:\m\{name}.mp3", format="mp3", work_id=work_id)
        return factory.library_files.upsert(row).id

    present, missing, foreign = file(work, "present"), file(work, "missing"), file(other_work, "x")
    conn.execute("UPDATE library_files SET file_status = 'missing' WHERE id = %s", (missing,))
    factory.format_overrides.create(
        FormatOverride(id=uuid4(), work_id=work, format_name="flac", preferred_file_id=present)
    )
    conn.commit()
    return _World(work, other_work, present, missing, foreign)


# (name, method, path, json, status, detail or None); fields are filled from the world.
_NOWHERE = "00000000-0000-0000-0000-000000000000"
_CASES: list[tuple[str, str, str, dict[str, Any] | None, int, str | None]] = [
    ("master-no-work", "PUT", "/works/no-such-work/master", {"preferred_file_id": "{present}"},
     404, None),
    ("master-file-elsewhere", "PUT", "/works/{work}/master", {"preferred_file_id": "{foreign}"},
     422, None),
    ("master-file-missing", "PUT", "/works/{work}/master", {"preferred_file_id": "{missing}"},
     422, None),
    ("master-delete-none", "DELETE", "/works/{work}/master", None,
     404, "No master found for work {work}"),
    ("override-no-work", "POST", "/works/no-such-work/format-overrides",
     {"format_name": "mp3", "preferred_file_id": "{present}"}, 404, "Work no-such-work not found"),
    ("override-file-elsewhere", "POST", "/works/{work}/format-overrides",
     {"format_name": "mp3", "preferred_file_id": "{foreign}"},
     422, "File {foreign} does not belong to work {work}"),
    ("override-file-missing", "POST", "/works/{work}/format-overrides",
     {"format_name": "mp3", "preferred_file_id": "{missing}"},
     422, "File {missing} is missing from disk"),
    ("override-exists", "POST", "/works/{work}/format-overrides",
     {"format_name": "flac", "preferred_file_id": "{present}"},
     409, "Format override for work {work} and format 'flac' already exists"),
    ("override-delete-none", "DELETE", f"/works/{{work}}/format-overrides/{_NOWHERE}", None,
     404, f"Format override {_NOWHERE} not found for work {{work}}"),
    ("merge-no-target", "POST", "/works/no-such-work/merge", {"source_work_ids": ["{work}"]},
     404, "Work no-such-work not found"),
    ("merge-target-among-sources", "POST", "/works/{work}/merge",
     {"source_work_ids": ["{other_work}", "{work}"]},
     422, "Target work ID must not appear in source_work_ids"),
    ("split-no-work", "POST", "/works/no-such-work/split", {"file_id": "{present}"},
     404, "Work no-such-work not found"),
    ("split-file-elsewhere", "POST", "/works/{work}/split", {"file_id": "{foreign}"},
     422, "File {foreign} does not belong to work {work}"),
    ("reassign-no-file", "PATCH", f"/files/{_NOWHERE}/work", {"work_id": "{work}"},
     404, f"File {_NOWHERE} not found"),
    ("reassign-no-work", "PATCH", "/files/{present}/work", {"work_id": "no-such-work"},
     404, "Work no-such-work not found"),
    ("reassign-already-there", "PATCH", "/files/{present}/work", {"work_id": "{work}"},
     422, "File {present} is already assigned to work {work}"),
]  # fmt: skip


def _filled(value: Any, world: _World) -> Any:
    if isinstance(value, str):
        return value.format(**{k: str(v) for k, v in vars(world).items()})
    if isinstance(value, list):
        return [_filled(v, world) for v in value]
    if isinstance(value, dict):
        return {k: _filled(v, world) for k, v in value.items()}
    return value


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "detail"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_a_refused_write_answers_as_today(
    client: TestClient,
    db_conn: psycopg.Connection[Any],
    method: str,
    path: str,
    body: dict[str, Any] | None,
    status: int,
    detail: str | None,
) -> None:
    world = _seed(db_conn)

    response = client.request(method, LIBRARY + _filled(path, world), json=_filled(body, world))

    assert response.status_code == status
    if detail is not None:
        assert response.json() == {"detail": _filled(detail, world)}

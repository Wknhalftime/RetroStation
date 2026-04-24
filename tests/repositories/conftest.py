from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row


@pytest.fixture
def pg_conn(migrated_db: str) -> Iterator[psycopg.Connection[dict[str, object]]]:
    """Per-test psycopg connection against a freshly truncated DB."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn

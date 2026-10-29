"""Migration lint (artist model phase 0a, SPEC S6, E1; C45, AUD-R044).

Every migration numbered after 0043, the last one before this lint, must:

- set ``SET LOCAL lock_timeout``. The runner (``backend/db/migrations.py``) wraps each file in
  a transaction, so ``SET LOCAL`` ends with it, while a plain ``SET`` would leak into the
  files after it;
- hold no statement-level ``BEGIN;`` or ``COMMIT;``, which would end the runner's transaction;
- ``ANALYZE`` when it copies or rewrites rows: an ``INSERT ... SELECT``, an ``UPDATE``, or a
  stored generated column added to a table.

Comments, string literals and ``$$`` bodies are stripped first: 0019's header comment says
"NO BEGIN/COMMIT", and 0040's plpgsql bodies hold ``BEGIN`` and ``UPDATE``.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "backend" / "db" / "migrations"
LAST_BEFORE_THE_LINT = 43

NO_LOCK_TIMEOUT = "no SET LOCAL lock_timeout"
TRANSACTION_CONTROL = "a statement-level BEGIN or COMMIT"
NO_ANALYZE = "rows copied or rewritten without ANALYZE"

_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")


def _quoted_end(sql: str, start: int) -> int:
    """The index just past the single-quoted literal opening at *start* ('' is a quote)."""
    i = start + 1
    while (close := sql.find("'", i)) >= 0:
        if not sql.startswith("''", close):
            return close + 1
        i = close + 2
    return len(sql)


def _skipped(sql: str, i: int) -> int | None:
    """Where the comment, literal or ``$$`` body opening at *i* ends; None if none opens."""
    if sql.startswith("--", i):
        end = sql.find("\n", i)
        return len(sql) if end < 0 else end
    if sql.startswith("/*", i):
        end = sql.find("*/", i + 2)
        return len(sql) if end < 0 else end + 2
    if sql[i] == "'":
        return _quoted_end(sql, i)
    if (tag := _DOLLAR_QUOTE.match(sql, i)) is not None:
        end = sql.find(tag.group(0), tag.end())
        return len(sql) if end < 0 else end + len(tag.group(0))
    return None


def strip_comments_and_bodies(sql: str) -> str:
    """*sql* with every comment, string literal and ``$$`` body replaced by a space."""
    kept: list[str] = []
    i = 0
    while i < len(sql):
        end = _skipped(sql, i)
        if end is None:
            kept.append(sql[i])
            i += 1
        else:
            kept.append(" ")
            i = end
    return "".join(kept)


def statements(sql: str) -> list[str]:
    """The statements of *sql*, stripped (see above), each on one line, upper-cased."""
    parts = strip_comments_and_bodies(sql).split(";")
    return [" ".join(part.split()).upper() for part in parts if part.strip()]


def _fills_rows(statement: str) -> bool:
    first = statement.split(" ", 1)[0]
    if first == "UPDATE":
        return True
    if first == "INSERT":
        return re.search(r"\bSELECT\b", statement) is not None
    if first == "WITH":
        return re.search(r"\b(?:INSERT|UPDATE)\b", statement) is not None
    if first == "ALTER":
        return re.search(r"\bGENERATED ALWAYS AS\b.*\bSTORED\b", statement) is not None
    return False


def lint(sql: str) -> list[str]:
    """What a migration numbered after 0043 does wrong; empty when it passes."""
    found = statements(sql)
    problems: list[str] = []
    if not any(re.match(r"SET LOCAL LOCK_TIMEOUT\b", s) for s in found):
        problems.append(NO_LOCK_TIMEOUT)
    if any(s.split(" ", 1)[0] in {"BEGIN", "COMMIT"} for s in found):
        problems.append(TRANSACTION_CONTROL)
    if any(_fills_rows(s) for s in found) and not any(s.startswith("ANALYZE") for s in found):
        problems.append(NO_ANALYZE)
    return problems


def _number(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def _migration(number: str) -> str:
    [path] = MIGRATIONS.glob(f"{number}_*.sql")
    return path.read_text(encoding="utf-8")


LOCK = "SET LOCAL lock_timeout = '5s';\n"


def test_migrations_after_0043_pass_the_lint() -> None:
    linted = sorted(p for p in MIGRATIONS.glob("*.sql") if _number(p) > LAST_BEFORE_THE_LINT)
    failures = {p.name: lint(p.read_text(encoding="utf-8")) for p in linted}

    assert (MIGRATIONS / "0043_station_liners.sql").is_file()
    assert {name: problems for name, problems in failures.items() if problems} == {}


def test_comments_and_bodies_ignored() -> None:
    """E1: BEGIN only in a comment or a $$ body passes; a statement-level BEGIN; fails."""
    header_comment = _migration("0019")  # "NO BEGIN/COMMIT" in its header comment
    plpgsql_bodies = _migration("0040")  # BEGIN ... END in $$ bodies, UPDATE in one
    passing = [
        header_comment,
        plpgsql_bodies,
        "/* BEGIN; */ SELECT 1;",
        "SELECT 'COMMIT;';",
        "DO $do$ BEGIN PERFORM 1; END $do$;",
    ]

    for sql in passing:
        assert TRANSACTION_CONTROL not in lint(sql), sql[:60]
    for sql in (header_comment, plpgsql_bodies):
        assert TRANSACTION_CONTROL in lint(f"{sql}\nBEGIN;\n")
        assert TRANSACTION_CONTROL in lint(f"{sql}\ncommit;\n")


def test_a_migration_sets_a_local_lock_timeout() -> None:
    """Only ``SET LOCAL``: a plain ``SET`` would outlive the runner's transaction."""
    added_columns = _migration("0019")

    assert lint(added_columns) == [NO_LOCK_TIMEOUT]
    assert lint(LOCK + added_columns) == []
    assert lint("SET lock_timeout = '5s';\n" + added_columns) == [NO_LOCK_TIMEOUT]


def test_rows_copied_or_rewritten_need_an_analyze() -> None:
    backfill = LOCK + _migration("0040")  # ends with an INSERT ... SELECT
    update = LOCK + "UPDATE works SET title = title;"
    generated = (
        LOCK + "ALTER TABLE track_identities ADD COLUMN version_text TEXT "
        "GENERATED ALWAYS AS (lower(original_title)) STORED;"
    )
    cte = LOCK + "WITH moved AS (UPDATE works SET title = title RETURNING id) SELECT 1;"

    for sql in (backfill, update, generated, cte):
        assert lint(sql) == [NO_ANALYZE], sql[-60:]
        assert lint(f"{sql}\nANALYZE works;") == [], sql[-60:]


def test_ddl_that_only_mentions_update_needs_no_analyze() -> None:
    """A trigger ON UPDATE, a foreign key ON UPDATE and a plain INSERT copy no rows."""
    ddl = (
        LOCK
        + "ALTER TABLE a ADD CONSTRAINT a_b FOREIGN KEY (b) REFERENCES b (id) ON UPDATE CASCADE;\n"
        + "CREATE TRIGGER t AFTER INSERT OR UPDATE ON a FOR EACH ROW EXECUTE FUNCTION f();\n"
        + "INSERT INTO settings (key, value) VALUES ('k', 'v');\n"
        + "CREATE TABLE c (n INT, m INT GENERATED ALWAYS AS (n + 1) STORED);"
    )

    assert lint(ddl) == []

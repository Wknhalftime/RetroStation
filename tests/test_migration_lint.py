"""Migration lint (artist model phase 0a, SPEC S6, E1; C45, AUD-R044).

Every migration numbered after 0043, the last one before this lint, must:

- set ``SET LOCAL lock_timeout``. The runner (``backend/db/migrations.py``) wraps each file in
  a transaction, so ``SET LOCAL`` ends with it, while a plain ``SET`` would leak into the
  files after it;
- hold no statement-level ``BEGIN;`` or ``COMMIT;`` (nor ``END``, ``ROLLBACK``, ``ABORT`` or
  ``START TRANSACTION``), which would end the runner's transaction;
- run ``ANALYZE`` (or ``ANALYSE``) after the last statement that copies or rewrites rows: an
  ``INSERT ... SELECT``, an ``UPDATE``, a stored generated column added to a table, a
  ``CREATE TABLE ... AS``, a materialized view filled at creation, a ``SELECT ... INTO``, or
  any of these inside a ``DO`` block, which runs with the migration.

Comments, string literals and ``$$`` bodies are stripped first: 0019's header comment says
"NO BEGIN/COMMIT", and 0040's plpgsql function bodies hold ``BEGIN`` and ``UPDATE``. A function
body runs only when it is called, so only a ``DO`` block's body is read, for its writes; a SQL
function's ``BEGIN ATOMIC ... END`` belongs to its ``CREATE``.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "backend" / "db" / "migrations"
LAST_BEFORE_THE_LINT = 43

NO_LOCK_TIMEOUT = "no SET LOCAL lock_timeout"
TRANSACTION_CONTROL = "a statement-level BEGIN, COMMIT or other transaction control"
NO_ANALYZE = "rows copied or rewritten without an ANALYZE after them"

_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")
_PLACEHOLDER = re.compile(r"\$(\d+)\$")
_CONTROL = re.compile(r"(?:BEGIN|COMMIT|END|ROLLBACK|ABORT)\b|START TRANSACTION\b")
_CREATE_TABLE_AS = re.compile(
    r"CREATE (?:(?:UNLOGGED|TEMP|TEMPORARY) )?TABLE\b.*(?<!ALWAYS )\bAS (?:\( ?)?"
    r"(?:SELECT|TABLE|VALUES|WITH)\b"
)
_DO_BODY_WRITE = re.compile(
    r"(?<!FOR )(?<!ON )\bUPDATE\s+[\w\"]|\bINSERT\s+INTO\b[^;]*\bSELECT\b"
    r"|\bCREATE\s+(?:UNLOGGED\s+)?TABLE\b[^;]*\bAS\b",
    re.IGNORECASE,
)


def _quoted_end(sql: str, start: int) -> int:
    """The index just past the single-quoted literal opening at *start* ('' is a quote)."""
    i = start + 1
    while (close := sql.find("'", i)) >= 0:
        if not sql.startswith("''", close):
            return close + 1
        i = close + 2
    return len(sql)


def _comment_or_literal_end(sql: str, i: int) -> int | None:
    """Where the comment or string literal opening at *i* ends; None if none opens there."""
    if sql.startswith("--", i):
        end = sql.find("\n", i)
        return len(sql) if end < 0 else end
    if sql.startswith("/*", i):
        end = sql.find("*/", i + 2)
        return len(sql) if end < 0 else end + 2
    if sql[i] == "'":
        return _quoted_end(sql, i)
    return None


def strip_comments_and_bodies(sql: str) -> tuple[str, list[str]]:
    """*sql* with each comment and string literal replaced by a space and each ``$$`` body by
    a numbered placeholder (``$0$``, ``$1$``, ...); and the bodies, in that order."""
    kept: list[str] = []
    bodies: list[str] = []
    i = 0
    while i < len(sql):
        if (tag := _DOLLAR_QUOTE.match(sql, i)) is not None:
            close = sql.find(tag.group(0), tag.end())
            bodies.append(sql[tag.end() : len(sql) if close < 0 else close])
            kept.append(f" ${len(bodies) - 1}$ ")
            i = len(sql) if close < 0 else close + len(tag.group(0))
        elif (end := _comment_or_literal_end(sql, i)) is not None:
            kept.append(" ")
            i = end
        else:
            kept.append(sql[i])
            i += 1
    return "".join(kept), bodies


def statements(stripped: str) -> list[str]:
    """The statements of stripped SQL, each on one line, upper-cased; a SQL function's
    ``BEGIN ATOMIC ... END`` body stays in its ``CREATE`` statement."""
    found: list[str] = []
    atomic: list[str] = []
    for part in stripped.split(";"):
        text = " ".join(part.split()).upper()
        if not text:
            continue
        if atomic:
            atomic.append(text)
            if text == "END":
                found.append("; ".join(atomic))
                atomic = []
        elif re.search(r"\bBEGIN ATOMIC\b", text) and not text.endswith(" END"):
            atomic = [text]
        else:
            found.append(text)
    if atomic:
        found.append("; ".join(atomic))
    return found


def _fills_rows(statement: str, bodies: list[str]) -> bool:
    """Whether *statement* copies or rewrites rows when the migration runs."""
    first, _, rest = statement.partition(" ")
    if first == "UPDATE":
        return True
    if first == "INSERT":
        return re.search(r"\bSELECT\b", rest) is not None
    if first == "WITH":
        return re.search(r"\b(?:INSERT|UPDATE)\b", rest) is not None
    if first == "SELECT":
        return re.search(r"\bINTO\b", rest) is not None
    if first == "ALTER":
        return re.search(r"\bGENERATED ALWAYS AS\b.*\bSTORED\b", rest) is not None
    if first == "CREATE":
        filled_view = rest.startswith("MATERIALIZED VIEW") and "WITH NO DATA" not in rest
        return filled_view or _CREATE_TABLE_AS.match(statement) is not None
    if first == "DO":
        return any(
            _DO_BODY_WRITE.search(strip_comments_and_bodies(bodies[int(n)])[0])
            for n in _PLACEHOLDER.findall(rest)
        )
    return False


def lint(sql: str) -> list[str]:
    """What a migration numbered after 0043 does wrong; empty when it passes."""
    stripped, bodies = strip_comments_and_bodies(sql)
    found = statements(stripped)
    problems: list[str] = []
    if not any(re.match(r"SET LOCAL LOCK_TIMEOUT\b", s) for s in found):
        problems.append(NO_LOCK_TIMEOUT)
    if any(_CONTROL.match(s) for s in found):
        problems.append(TRANSACTION_CONTROL)
    fills = [i for i, s in enumerate(found) if _fills_rows(s, bodies)]
    analyzed = [i for i, s in enumerate(found) if s.split(" ", 1)[0] in {"ANALYZE", "ANALYSE"}]
    if fills and not any(i > fills[-1] for i in analyzed):
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
        "CREATE FUNCTION one() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END;",
        "COMMENT ON TABLE works IS 'one';",
    ]

    for sql in passing:
        assert TRANSACTION_CONTROL not in lint(sql), sql[:60]
    for sql in (header_comment, plpgsql_bodies):
        assert TRANSACTION_CONTROL in lint(f"{sql}\nBEGIN;\n")
        assert TRANSACTION_CONTROL in lint(f"{sql}\ncommit;\n")
    for control in ("END;", "ROLLBACK;", "ABORT;", "START TRANSACTION;"):
        assert TRANSACTION_CONTROL in lint(f"{header_comment}\n{control}\n"), control


def test_a_migration_sets_a_local_lock_timeout() -> None:
    """Only ``SET LOCAL``: a plain ``SET`` would outlive the runner's transaction."""
    added_columns = _migration("0019")

    assert lint(added_columns) == [NO_LOCK_TIMEOUT]
    assert lint(LOCK + added_columns) == []
    assert lint("SET lock_timeout = '5s';\n" + added_columns) == [NO_LOCK_TIMEOUT]


def test_rows_copied_or_rewritten_need_an_analyze_after_them() -> None:
    copies = {
        "backfill": LOCK + _migration("0040"),  # ends with an INSERT ... SELECT
        "update": LOCK + "UPDATE works SET title = title;",
        "generated": LOCK + "ALTER TABLE track_identities ADD COLUMN version_text TEXT "
        "GENERATED ALWAYS AS (lower(original_title)) STORED;",
        "cte": LOCK + "WITH moved AS (UPDATE works SET title = title RETURNING id) SELECT 1;",
        "ctas": LOCK + "CREATE TABLE works_copy AS SELECT * FROM works;",
        "matview": LOCK + "CREATE MATERIALIZED VIEW plays AS SELECT 1 AS n;",
        "select_into": LOCK + "SELECT * INTO works_copy FROM works;",
        "do_block": LOCK + "DO $$ BEGIN UPDATE works SET title = trim(title); END $$;",
    }

    for name, sql in copies.items():
        assert lint(sql) == [NO_ANALYZE], name
        assert lint(f"{sql}\nANALYZE works;") == [], name
        assert lint(f"{sql}\nANALYSE works;") == [], name
        assert lint(f"{LOCK}ANALYZE works;\n{sql}") == [NO_ANALYZE], name


def test_ddl_that_only_mentions_a_write_needs_no_analyze() -> None:
    """A trigger or foreign key ON UPDATE, a plain INSERT, a generated column of a new table,
    a function body (it runs only when called) and a DO block that only reads copy no rows."""
    ddl = (
        LOCK
        + "ALTER TABLE a ADD CONSTRAINT a_b FOREIGN KEY (b) REFERENCES b (id) ON UPDATE CASCADE;\n"
        + "CREATE TRIGGER t AFTER INSERT OR UPDATE ON a FOR EACH ROW EXECUTE FUNCTION f();\n"
        + "INSERT INTO settings (key, value) VALUES ('k', 'v');\n"
        + "CREATE TABLE c (n INT, m INT GENERATED ALWAYS AS (n + 1) STORED);\n"
        + "CREATE MATERIALIZED VIEW v AS SELECT 1 AS n WITH NO DATA;\n"
        + "CREATE FUNCTION g() RETURNS void LANGUAGE sql AS $$ UPDATE works SET title = '' $$;\n"
        + "DO $$ DECLARE n int; BEGIN SELECT count(*) INTO n FROM works FOR UPDATE; END $$;"
    )

    assert lint(ddl) == []

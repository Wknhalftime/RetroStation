"""No code in ``backend/`` reads or writes the artist rows of ``matches`` (artist model phase 1a,
SPEC S12; M35, AUD-R035).

The old rows stay in ``matches``, with ``matches.artist_id``, ``xor_match_target``,
``matches_artist_id_target_id_key`` and ``MatchRepository.get_by_artist``, until phase 1c drops
them; from 1a log names link to artists through ``log_name_links`` only. An AST scan of every
``.py`` file in ``backend/`` fails on:

1. a ``get_by_artist`` call on a match repository: its receiver names a match repository
   (``self._match_repo``, ``repos.matches``, ``match_repo``). ``WorkRepository.get_by_artist``
   (``grouping_service``, ``work_curation_service``) passes;
2. a ``Match(...)`` built with an ``artist_id=`` keyword;
3. SQL text naming ``matches.artist_id``: written out, through an alias of ``matches``
   (``FROM matches m ... m.artist_id``), as a bare column of a statement on ``matches``
   (``INSERT INTO matches (..., artist_id, ...)``, ``DELETE FROM matches WHERE artist_id``),
   or picking artist rows by their target type (``matches ... target_type = 'artist'``);
4. an ``execute(...)`` call passing ``TargetType.ARTIST`` among its arguments (the same
   choice, as a parameter).

It skips the migrations and ``PgMatchRepository``'s own module
(``backend/db/repositories/matches.py``), which keeps ``get_by_artist`` until 1c.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED = {"backend/db/repositories/matches.py"}
SKIPPED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

# Words that can follow "matches" in SQL without being its alias.
_NOT_AN_ALIAS = {
    "where", "set", "join", "on", "using", "left", "right", "inner", "outer", "full", "cross",
    "natural", "group", "order", "limit", "offset", "returning", "values", "union", "except",
    "intersect", "having", "window", "for", "default", "select", "as",
}  # fmt: skip
_BARE_ARTIST_ID = re.compile(r"(?<![\w.])artist_id\b", re.IGNORECASE)
_ON_MATCHES = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+(?:ONLY\s+)?matches\b", re.IGNORECASE)
_SCOPE_ENDS = re.compile(r"\b(?:FROM|JOIN)\b|\(\s*SELECT\b", re.IGNORECASE)


def names_matches_artist_id(sql: str) -> bool:
    """Whether SQL *sql* reads or writes ``matches.artist_id``, or picks artist rows."""
    text = " ".join(sql.split())
    if re.search(r"\bmatches\s*\.\s*artist_id\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bmatches\b.*\btarget_type\s*=\s*'artist'", text, re.IGNORECASE):
        return True
    for alias in re.findall(r"\bmatches\s+(?:AS\s+)?([A-Za-z_]\w*)", text, re.IGNORECASE):
        if alias.lower() in _NOT_AN_ALIAS:
            continue
        if re.search(rf"(?<![\w.]){re.escape(alias)}\s*\.\s*artist_id\b", text):
            return True
    for statement in _ON_MATCHES.finditer(text):
        rest = text[statement.end() :]
        end = _SCOPE_ENDS.search(rest)
        if _BARE_ARTIST_ID.search(rest[: end.start()] if end else rest):
            return True
    return False


def _string_literals(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Every string literal, with its line; an f-string as its literal parts joined, once."""
    parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                p.value
                for p in node.values
                if isinstance(p, ast.Constant) and isinstance(p.value, str)
            )
            yield node.lineno, text
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in parts
        ):
            yield node.lineno, node.value


def _mentions_target_type_artist(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "ARTIST"
        and isinstance(n.value, ast.Name)
        and n.value.id == "TargetType"
        for n in ast.walk(node)
    )


def violations(source: str) -> list[str]:
    """What *source* does to the artist rows of matches, one line per finding."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get_by_artist":
            receiver = ast.unparse(func.value)
            if "match" in receiver.lower():
                found.append(f"line {node.lineno}: {receiver}.get_by_artist")
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "Match" and any(k.arg == "artist_id" for k in node.keywords):
            found.append(f"line {node.lineno}: Match(artist_id=...)")
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(_mentions_target_type_artist(a) for a in arguments):
                found.append(f"line {node.lineno}: execute(..., TargetType.ARTIST)")
    found += [
        f"line {line}: SQL names matches.artist_id"
        for line, text in _string_literals(tree)
        if names_matches_artist_id(text)
    ]
    return found


def _backend_modules() -> Iterator[tuple[str, Path]]:
    for path in sorted((ROOT / "backend").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if SKIPPED_DIRS & set(path.parts) or relative.startswith("backend/db/migrations/"):
            continue
        if relative not in SKIPPED:
            yield relative, path


def test_backend_leaves_artist_rows_alone() -> None:
    found = {
        relative: hits
        for relative, path in _backend_modules()
        if (hits := violations(path.read_text(encoding="utf-8")))
    }

    assert found == {}


def test_the_scan_finds_each_kind_of_use() -> None:
    caught = {
        "self._match_repo.get_by_artist(artist.id)": 1,
        "repos.matches.get_by_artist(name_id)": 1,
        "match_repo.get_by_artist(a)": 1,
        "Match(id=uuid4(), artist_id=name.id, target_id=t, confidence_score=1.0, "
        "match_tier=tier)": 1,
        'conn.execute("SELECT target_id FROM matches WHERE artist_id = %s", (x,))': 1,
        'conn.execute("""INSERT INTO matches\n (id, artist_id, target_id)'
        ' VALUES (%s, %s, %s)""")': 1,
        'conn.execute("DELETE FROM matches WHERE artist_id = %s OR identity_id IN '
        '(SELECT id FROM track_identities WHERE broadcast_artist_id = %s)")': 1,
        'sql = "SELECT 1 FROM artists a JOIN matches m ON m.target_id = a.id '
        'JOIN broadcast_artists ba ON ba.id = m.artist_id"': 1,
        'sql = "SELECT m.id FROM matches AS mm WHERE mm.artist_id IS NOT NULL"': 1,
        "sql = \"SELECT 1 FROM matches WHERE target_type = 'artist'\"": 1,
        'conn.execute("SELECT id FROM matches WHERE target_type = %s",'
        " (TargetType.ARTIST.value,))": 1,
        'sql = f"SELECT matches.artist_id FROM matches WHERE id IN ({marks})"': 1,
    }
    passed = [
        "work_repo.get_by_artist(artist_id)",
        "repos.works.get_by_artist(artist_id)",
        "Match(id=uuid4(), identity_id=i, library_file_id=f, confidence_score=1.0, "
        "match_tier=tier)",
        'conn.execute("SELECT * FROM matches WHERE identity_id = %s", (x,))',
        'conn.execute("UPDATE matches SET work_id = %s WHERE work_id IN '
        '(SELECT id FROM works WHERE artist_id = %s)")',
        'sql = "SELECT 1 FROM matches m JOIN works w ON w.id = m.work_id WHERE w.artist_id = %s"',
        'sql = "SELECT id FROM track_identities WHERE broadcast_artist_id = %s"',
        'sql = "SELECT 1 FROM works WHERE artist_id = %s"',
        "rule.target_type != TargetType.ARTIST",
        'conn.execute("SELECT 1 FROM log_name_links WHERE artist_id = %s", (artist_id,))',
    ]

    assert {source: len(violations(source)) for source in caught} == caught
    assert {source: violations(source) for source in passed} == dict.fromkeys(passed, [])

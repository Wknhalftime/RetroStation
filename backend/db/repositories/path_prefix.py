"""LIKE patterns that match files beneath a directory, whatever its characters."""

from __future__ import annotations


def like_literal(text: str) -> str:
    """Escape LIKE metacharacters (``\\``, ``%``, ``_``) so *text* matches only itself."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def dir_like_prefix(folder_path: str) -> tuple[str, str]:
    """(escaped ``folder + sep`` LIKE prefix, escaped separator) for *folder_path*.

    Handles both ``/`` and ``\\`` separators so queries work on Windows
    (where stored paths use backslashes) and POSIX alike. Escaped for LIKE:
    backslash is PostgreSQL's default escape character, so an unescaped
    Windows prefix ending in ``\\`` turned the trailing ``\\%`` into a
    literal percent sign and matched nothing at all. The separator comes
    from the path as given: a drive root ``X:\\`` strips to ``X:``, which
    alone would pick ``/`` and match nothing.
    """
    sep = "\\" if "\\" in folder_path else "/"
    stripped = folder_path.rstrip("/").rstrip("\\")
    return like_literal(stripped + sep), like_literal(sep)

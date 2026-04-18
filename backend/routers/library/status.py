from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


class LibraryStatus(BaseModel):
    total_files: int
    quarantine_count: int
    by_format: dict[str, int]
    by_enrichment: dict[str, int]


@router.get("/status", response_model=LibraryStatus)
async def get_library_status(conn: DbConn, _token: Token) -> LibraryStatus:
    """Return aggregate counts: total files, quarantined files, by format, by enrichment."""
    total_cur = await conn.execute("SELECT COUNT(*) AS cnt FROM library_files")
    total_row = await total_cur.fetchone()
    total_files: int = total_row["cnt"] if total_row else 0

    q_cur = await conn.execute("SELECT COUNT(*) AS cnt FROM library_quarantine")
    q_row = await q_cur.fetchone()
    quarantine_count: int = q_row["cnt"] if q_row else 0

    fmt_cur = await conn.execute(
        "SELECT format, COUNT(*) AS cnt FROM library_files GROUP BY format"
    )
    fmt_rows = await fmt_cur.fetchall()
    by_format: dict[str, int] = {r["format"]: r["cnt"] for r in fmt_rows}

    enr_cur = await conn.execute(
        "SELECT enrichment_status, COUNT(*) AS cnt FROM library_files GROUP BY enrichment_status"
    )
    enr_rows = await enr_cur.fetchall()
    by_enrichment: dict[str, int] = {r["enrichment_status"]: r["cnt"] for r in enr_rows}

    return LibraryStatus(
        total_files=total_files,
        quarantine_count=quarantine_count,
        by_format=by_format,
        by_enrichment=by_enrichment,
    )

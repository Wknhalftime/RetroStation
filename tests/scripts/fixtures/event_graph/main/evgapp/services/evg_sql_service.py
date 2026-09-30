"""Announces what chain_head_task finished."""


def announce_import(cur) -> None:
    cur.execute("NOTIFY imports_done")


def announce_scan(cur, batch_id: str) -> None:
    cur.execute("SELECT pg_notify('scans_done', %s)", (batch_id,))


def listen_imports(cur) -> None:
    cur.execute("LISTEN imports_done")


def read_notify_count(cur) -> None:
    cur.execute("SELECT notify_count FROM settings")

from __future__ import annotations

import base64


def encode(artist_id: str, track_title: str) -> str:
    """Return a URL-safe synthetic work ID encoding artist_id and track_title."""
    raw = f"{artist_id}:{track_title}"
    return "syn_" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(work_id: str) -> tuple[str, str] | None:
    """Decode a synthetic work ID. Returns (artist_id, track_title) or None."""
    if not work_id.startswith("syn_"):
        return None
    encoded = work_id[4:]
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    try:
        raw = base64.urlsafe_b64decode(encoded).decode()
    except Exception:
        return None
    colon_idx = raw.find(":")
    if colon_idx == -1:
        return None
    return raw[:colon_idx], raw[colon_idx + 1:]

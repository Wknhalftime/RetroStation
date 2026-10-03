"""Reading a sign-off clip upload from an HTTP request, bounded (D26; PG3, M3, M4).

The multipart body is parsed from ``request.stream()`` through a counter, so an upload is
bounded however it is sent: a ``Content-Length`` over the limit is refused before the body is
read, and a chunked upload (no ``Content-Length``) is refused once more than the limit has
been read, without reading the rest. Starlette's ``request.form(max_part_size=...)`` bounds
only the non-file fields, never a file part.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import PureWindowsPath

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from backend.domain.streaming import MAX_CLIP_BYTES, MAX_SIGN_OFF_NAME, ClipTooLargeError
from backend.services.streaming.sign_off import ClipUpload

MULTIPART_SLACK = 2**20
"""Room for the multipart framing around the clip: boundaries, part headers, a small field."""

MAX_UPLOAD_BYTES = MAX_CLIP_BYTES + MULTIPART_SLACK
"""The most of a request body read for one clip upload."""

_FIELD = "file"
_FALLBACK_NAME = "sign-off"
TOO_LARGE = f"the upload is larger than a {MAX_CLIP_BYTES // 2**20} MiB clip"
"""Why an upload over the limit is refused (413)."""


class _BodyTooLargeError(MultiPartException):
    """The body passed ``MAX_UPLOAD_BYTES``. A ``MultiPartException``, so the parser closes
    the temporary files it spooled before the error leaves it."""


def declared_too_large(request: Request) -> bool:
    """Whether the request's ``Content-Length`` already says the body is over the limit."""
    length = request.headers.get("content-length", "")
    return length.isdecimal() and int(length) > MAX_UPLOAD_BYTES


async def read_clip_upload(request: Request) -> ClipUpload:
    """The clip sent as the multipart ``file`` field, at most ``MAX_CLIP_BYTES + 1`` bytes of
    it (one byte over lets the store refuse it as too large).

    Raises:
        ClipTooLargeError: the body passed ``MAX_UPLOAD_BYTES``; the rest is not read.
        RequestValidationError: the body is not a multipart upload with a ``file`` part
            (422 in FastAPI's list shape).
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise _invalid("a multipart/form-data upload is required")
    parser = MultiPartParser(request.headers, _bounded(request.stream()), max_files=1, max_fields=1)
    try:
        form = await parser.parse()
    except _BodyTooLargeError as too_large:
        raise ClipTooLargeError(TOO_LARGE) from too_large
    except MultiPartException as malformed:
        raise _invalid(malformed.message) from malformed
    try:
        upload = form.get(_FIELD)
        if not isinstance(upload, UploadFile):
            raise _invalid("a clip file is required")
        data = await upload.read(MAX_CLIP_BYTES + 1)
        return ClipUpload(name=_display_name(upload.filename), data=data)
    finally:
        await form.close()


async def _bounded(stream: AsyncIterator[bytes]) -> AsyncGenerator[bytes]:
    read = 0
    async for chunk in stream:
        read += len(chunk)
        if read > MAX_UPLOAD_BYTES:
            raise _BodyTooLargeError(TOO_LARGE)
        yield chunk


def _display_name(filename: str | None) -> str:
    """The upload's own name, as the page shows it: no folder part a browser may send, cut
    to ``SignOff.name``'s length. It never names a stored file."""
    name = PureWindowsPath(filename or "").name.strip()
    return (name or _FALLBACK_NAME)[:MAX_SIGN_OFF_NAME]


def _invalid(message: str) -> RequestValidationError:
    return RequestValidationError(
        [{"loc": ("body", _FIELD), "msg": message, "type": "value_error"}]
    )

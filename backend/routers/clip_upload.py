"""Reading an audio upload from an HTTP request, bounded (D26; PG3, M3, M4; LR2).

One reader serves the sign-off clip and the station liners; ``UploadLimits`` holds what
differs between them: the most a file may be, the 413's reason, how the sent file name
becomes ``ClipUpload.name``, and the event logged when the client goes away.

The multipart body is parsed from ``request.stream()`` through a counter, so an upload is
bounded however it is sent: a ``Content-Length`` over the limit is refused before the body is
read, and a chunked upload (no ``Content-Length``) is refused once more than the limit has
been read, without reading the rest. Starlette's ``request.form(max_part_size=...)`` bounds
only the non-file fields, never a file part.

A body that is not valid multipart is a 422, and a client that goes away mid-upload is an
``UploadAbortedError``, logged, never a server error. Either way the temporary files the
parser spooled are closed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from backend.domain.streaming import MAX_CLIP_BYTES, MAX_SIGN_OFF_NAME, ClipTooLargeError
from backend.services.streaming.sign_off import ClipUpload

logger = structlog.get_logger(__name__)

MULTIPART_SLACK = 2**20
"""Room for the multipart framing around the clip: boundaries, part headers, a small field."""

MAX_UPLOAD_BYTES = MAX_CLIP_BYTES + MULTIPART_SLACK
"""The most of a request body read for one sign-off clip upload."""

_FIELD = "file"
_FALLBACK_NAME = "sign-off"
TOO_LARGE = f"the upload is larger than a {MAX_CLIP_BYTES // 2**20} MiB clip"
"""Why a sign-off upload over the limit is refused (413)."""


@dataclass(frozen=True)
class UploadLimits:
    """What bounds one kind of audio upload, and how its file name is kept.

    ``max_file_bytes`` is the most the stored file may be; the body may be ``MULTIPART_SLACK``
    more. ``too_large`` is the 413's reason. ``name_of`` turns the sent file name, its folder
    part already removed, into ``ClipUpload.name``. ``aborted_event`` is logged when the
    client goes away mid-upload.
    """

    max_file_bytes: int
    too_large: str
    name_of: Callable[[str], str]
    aborted_event: str

    @property
    def max_body_bytes(self) -> int:
        """The most of a request body read for one upload."""
        return self.max_file_bytes + MULTIPART_SLACK


class UploadAbortedError(Exception):
    """The client went away before its upload was read in full: nobody receives an answer."""


class _BodyTooLargeError(MultiPartException):
    """The body passed ``UploadLimits.max_body_bytes``. A ``MultiPartException``, so the
    parser closes the temporary files it spooled before the error leaves it."""


class _ClientGoneError(MultiPartException):
    """The client disconnected mid-body; a ``MultiPartException`` for the same reason."""


class _ClipFormParser(MultiPartParser):
    """Starlette's multipart parser, also turning python-multipart's own parse errors (a
    garbage body, a wrong boundary, a broken part header) into ``MultiPartException``.

    Starlette 1.0 catches only ``MultiPartException``: a ``MultipartParseError`` would escape
    as a 500 and leave the file part it had begun spooling open, so this closes the parser's
    spooled files (Starlette's own ``_files_to_close_on_error`` list) first.
    """

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except MultipartParseError as malformed:
            for spooled in self._files_to_close_on_error:
                spooled.close()
            raise MultiPartException(f"the multipart body is malformed: {malformed}") from malformed


def _sign_off_name(name: str) -> str:
    """The sign-off clip's name, as the page shows it: cut to ``SignOff.name``'s length. It
    never names a stored file."""
    return (name.strip() or _FALLBACK_NAME)[:MAX_SIGN_OFF_NAME]


SIGN_OFF_LIMITS = UploadLimits(
    max_file_bytes=MAX_CLIP_BYTES,
    too_large=TOO_LARGE,
    name_of=_sign_off_name,
    aborted_event="stream_sign_off_upload_aborted",
)
"""The sign-off clip upload's limits (D26)."""


def declared_over(request: Request, limits: UploadLimits) -> bool:
    """Whether the request's ``Content-Length`` already says the body is over ``limits``."""
    length = request.headers.get("content-length", "")
    return length.isdecimal() and int(length) > limits.max_body_bytes


def declared_too_large(request: Request) -> bool:
    """Whether the request's ``Content-Length`` already says the body is over the sign-off
    limit."""
    return declared_over(request, SIGN_OFF_LIMITS)


async def read_clip_upload(request: Request) -> ClipUpload:
    """The sign-off clip sent as the multipart ``file`` field (``read_audio_upload`` with
    ``SIGN_OFF_LIMITS``)."""
    return await read_audio_upload(request, SIGN_OFF_LIMITS)


async def read_audio_upload(request: Request, limits: UploadLimits) -> ClipUpload:
    """The audio sent as the multipart ``file`` field, at most ``limits.max_file_bytes + 1``
    bytes of it (one byte over lets the store refuse it as too large).

    Raises:
        ClipTooLargeError: the body passed ``limits.max_body_bytes``; the rest is not read.
        RequestValidationError: the body is not a multipart upload with a ``file`` part
            (422 in FastAPI's list shape).
        UploadAbortedError: the client disconnected before the body was read.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise _invalid("a multipart/form-data upload is required")
    body = _bounded(request.stream(), limits)
    parser = _ClipFormParser(request.headers, body, max_files=1, max_fields=1)
    try:
        form = await parser.parse()
    except _BodyTooLargeError as too_large:
        raise ClipTooLargeError(limits.too_large) from too_large
    except _ClientGoneError as gone:
        logger.info(limits.aborted_event, reason=gone.message)
        raise UploadAbortedError(gone.message) from gone
    except MultiPartException as malformed:
        raise _invalid(malformed.message) from malformed
    try:
        upload = form.get(_FIELD)
        if not isinstance(upload, UploadFile):
            raise _invalid("a clip file is required")
        data = await upload.read(limits.max_file_bytes + 1)
        name = PureWindowsPath(upload.filename or "").name
        return ClipUpload(name=limits.name_of(name), data=data)
    finally:
        await form.close()


async def _bounded(stream: AsyncIterator[bytes], limits: UploadLimits) -> AsyncGenerator[bytes]:
    read = 0
    try:
        async for chunk in stream:
            read += len(chunk)
            if read > limits.max_body_bytes:
                raise _BodyTooLargeError(limits.too_large)
            yield chunk
    except ClientDisconnect as gone:
        raise _ClientGoneError(f"the client disconnected after {read} bytes") from gone


def _invalid(message: str) -> RequestValidationError:
    return RequestValidationError(
        [{"loc": ("body", _FIELD), "msg": message, "type": "value_error"}]
    )

"""A Windows Job Object that kills every assigned process when it is closed.

The API process holds one job for its lifetime. When the API exits, even by a crash, the
kernel closes the handle and every Liquidsoap child dies with it (spec: Engine). A process
killed this way exits with code 0, so callers must check that it is gone, not its code.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from types import TracebackType

if sys.platform != "win32":  # pragma: no cover - CI (Linux) skips everything that imports this
    raise ImportError("backend.playout.windows_job requires Windows")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_EXTENDED_LIMIT_INFORMATION = 9
_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _last_error(action: str) -> OSError:
    """The calling thread's last Windows error, with the OS's own text for it."""
    code = ctypes.get_last_error()
    return ctypes.WinError(code, f"{action} failed: {ctypes.FormatError(code).strip()}")


def _check(ok: object, action: str) -> None:
    if not ok:
        raise _last_error(action)


class KillOnCloseJob:
    """Owns one job handle; processes assigned to it die when it is closed."""

    def __init__(self) -> None:
        handle = _kernel32.CreateJobObjectW(None, None)
        _check(handle, "CreateJobObjectW")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        if not ok:
            error = _last_error("SetInformationJobObject")
            _kernel32.CloseHandle(handle)
            raise error
        self._handle: int | None = handle

    def assign(self, pid: int) -> None:
        """Put process ``pid`` in the job; raises OSError if that is impossible."""
        if self._handle is None:
            raise OSError("KillOnCloseJob is closed")
        process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        _check(process, f"OpenProcess({pid})")
        try:
            _check(
                _kernel32.AssignProcessToJobObject(self._handle, process),
                f"AssignProcessToJobObject({pid})",
            )
        finally:
            _kernel32.CloseHandle(process)

    def terminate(self, exit_code: int) -> None:
        """End every assigned process now with ``exit_code``, the caller too if assigned.

        Unlike ``close``, this sets the code the processes exit with; the job stays open.
        """
        if self._handle is None:
            raise OSError("KillOnCloseJob is closed")
        _check(_kernel32.TerminateJobObject(self._handle, exit_code), "TerminateJobObject")

    def close(self) -> None:
        """Close the handle, killing every assigned process; closing twice is harmless."""
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> KillOnCloseJob:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

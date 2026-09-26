"""Child processes (media server, ffmpeg pushes) must not outlive the process that started them.

A crashed or killed backend otherwise leaves an orphan media server holding the ports: the next backend's
media server cannot bind them and exits, while the orphan keeps serving stale configuration.

Windows: put this process in a job object with KILL_ON_JOB_CLOSE (children inherit the job; when the last
handle closes, i.e. this process dies, Windows kills them). Linux: children get SIGTERM when the parent dies.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Any

_job: Any = None


def bind_children_to_this_process() -> None:
    global _job
    if os.name != "nt" or _job is not None:
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Limit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Limit), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    try:
        job = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
        if k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            _job = job  # the handle stays open for the life of this process
    except (OSError, ctypes.ArgumentError):  # never block startup over this safeguard
        pass


def preexec() -> Any:
    """preexec_fn for subprocess.Popen on Linux (None elsewhere)."""
    if not sys.platform.startswith("linux"):
        return None

    def _set() -> None:
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG

    return _set

"""Tie child processes to this process's lifetime.

On Windows a child keeps running when its parent dies. A headless kicad-cli left behind keeps
its project open (and its lock file), so KiCad then warns that the project is already open.
Putting the child in a Job Object with KILL_ON_JOB_CLOSE makes Windows end it as soon as this
process exits, even when it is killed.
"""

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


# Job handles must stay open for the life of this process; closing one kills its children.
_jobs: list[int] = []


def pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION

    if not handle:
        return False

    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


class LockRegistry:
    """Project lock files created by kicad-mcp servers, with the MCP process that owns each.

    A kicad-cli that is killed (e.g. together with its MCP) cannot remove its project lock, and
    KiCad then warns that the project is already open. Locks recorded here whose owner process
    is gone are known to be stale and can be removed safely; locks of other KiCads never are.
    """

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _store(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, indent=1), encoding="utf-8")

    @staticmethod
    def _token(lock: Path) -> str | None:
        try:
            return json.loads(lock.read_text(encoding="utf-8")).get("token")
        except (OSError, ValueError):
            return None

    def record(self, lock: Path) -> None:
        token = self._token(lock)

        if token:
            entries = [e for e in self._load() if e["lock"] != str(lock)]
            entries.append({"lock": str(lock), "token": token, "pid": os.getpid()})
            self._store(entries)

    def forget(self, lock: Path) -> None:
        self._store([e for e in self._load() if e["lock"] != str(lock)])

    def remove_if_stale(self, lock: Path) -> bool:
        """Delete `lock` if a dead kicad-mcp created it. True if the lock is gone."""
        if not lock.exists():
            return True

        token = self._token(lock)

        for entry in self._load():
            if entry["lock"] == str(lock) and entry["token"] == token and not pid_alive(entry["pid"]):
                lock.unlink(missing_ok=True)
                self.forget(lock)
                return True

        return False

    def remove_own(self, lock: Path) -> None:
        """Delete a lock this process recorded (its kicad-cli died and could not remove it)."""
        token = self._token(lock)

        for entry in self._load():
            if entry["lock"] == str(lock) and entry["token"] == token and entry["pid"] == os.getpid():
                lock.unlink(missing_ok=True)
                self.forget(lock)

    def sweep(self) -> None:
        """Remove every stale lock left by kicad-mcp servers that are no longer running."""
        for entry in self._load():
            if not pid_alive(entry["pid"]):
                self.remove_if_stale(Path(entry["lock"]))


def kill_with_parent(process: subprocess.Popen) -> bool:
    """End `process` automatically when this process exits. Returns False if not supported."""
    if sys.platform != "win32":
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)

    if not job:
        return False

    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(job)
        return False

    if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
        kernel32.CloseHandle(job)
        return False

    _jobs.append(job)
    return True

"""Lock liveness check: test whether the lock holder runs, never signal or stop it."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

import pytest

from porter import runner

PID = 4242
HANDLE = 77


class FakeKernel32:
    """Stands in for kernel32 and records the calls that `_alive` makes."""

    def __init__(self, *, handle=HANDLE, exit_code=259, query_ok=True):
        self.handle = handle
        self.exit_code = exit_code
        self.query_ok = query_ok
        self.opened: list[tuple] = []
        self.closed: list[int] = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle, code):
        assert handle == self.handle
        if not self.query_ok:
            return 0
        code.contents.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def alive_on(monkeypatch, os_name: str, pid: int = PID) -> bool:
    # Patch os.name only around the call: pathlib and others read it too.
    with monkeypatch.context() as m:
        m.setattr(os, "name", os_name)
        return runner._alive(pid)


def windows_alive(monkeypatch, kernel32: FakeKernel32, last_error: int = 0) -> bool:
    def no_kill(*_):
        raise AssertionError("os.kill must not run on Windows: it can stop the holder")

    monkeypatch.setattr(os, "kill", no_kill)
    monkeypatch.setattr(runner, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    return alive_on(monkeypatch, "nt")


@pytest.mark.parametrize(
    ("error", "expected"),
    [(None, True), (ProcessLookupError, False), (PermissionError, True)],
)
def test_posix_sends_signal_zero(monkeypatch, error, expected):
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if error:
            raise error

    monkeypatch.setattr(os, "kill", fake_kill)
    assert alive_on(monkeypatch, "posix") is expected
    assert calls == [(PID, 0)]


@pytest.mark.parametrize(
    ("kernel32", "expected"),
    [
        (FakeKernel32(exit_code=259), True),  # STILL_ACTIVE
        (FakeKernel32(exit_code=0), False),  # exited
        (FakeKernel32(query_ok=False), True),  # cannot tell: keep the lock
    ],
    ids=["running", "exited", "query-fails"],
)
def test_windows_queries_exit_code(monkeypatch, kernel32, expected):
    assert windows_alive(monkeypatch, kernel32) is expected
    assert kernel32.opened == [(0x1000, False, PID)]  # PROCESS_QUERY_LIMITED_INFORMATION
    assert kernel32.closed == [HANDLE]


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [(87, False), (5, True)],
    ids=["no-such-process", "access-denied"],
)
def test_windows_open_process_fails(monkeypatch, last_error, expected):
    kernel32 = FakeKernel32(handle=None)
    assert windows_alive(monkeypatch, kernel32, last_error) is expected
    assert kernel32.closed == []


@pytest.mark.skipif(os.name != "nt", reason="uses the real Windows API")
def test_windows_real_check_leaves_holder_running():
    assert runner._alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert runner._alive(child.pid)
        assert child.poll() is None  # the check did not stop it
    finally:
        child.kill()
        child.wait()
    assert not runner._alive(child.pid)

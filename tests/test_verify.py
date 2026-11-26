"""Verify commands: the ``{python}`` placeholder works in the platform's shell."""

from __future__ import annotations

import os
import sys

import pytest

from porter import verify
from porter.proc import Shell
from porter.verify import run_checks


def test_python_placeholder_runs_in_the_platform_shell(tmp_path):
    # The default `porter init` check. cmd.exe on Windows reads single quotes literally,
    # so a POSIX-quoted interpreter path made this fail there.
    report = run_checks(Shell(), ["{python} -c \"print('checks ok')\""], tmp_path, timeout=60)
    assert report.passed, report.feedback()
    assert report.results[0].output == "checks ok"


@pytest.mark.parametrize(
    ("os_name", "exe", "expected"),
    [
        ("posix", "/opt/my python/bin/python3", "'/opt/my python/bin/python3' -V"),
        ("nt", r"C:\Program Files\Python\python.exe", r'"C:\Program Files\Python\python.exe" -V'),
        ("nt", r"C:\py\python.exe", r'"C:\py\python.exe" -V'),
    ],
)
def test_python_placeholder_is_quoted_for_the_shell(monkeypatch, os_name, exe, expected):
    monkeypatch.setattr(sys, "executable", exe)
    # Patch os.name only around the call: pathlib and others read it too.
    with monkeypatch.context() as m:
        m.setattr(os, "name", os_name)
        assert verify.expand("{python} -V") == expected

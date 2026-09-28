"""Shared pytest set-up for the server tests."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

import pytest

from _serverlib import isolate_home, remove_temporary_homes

# The real data folder, where server.py and amd_setup.py put everything without SHISUKO_HOME, found
# before any test changes HOME or USERPROFILE. The tests never write to it: conftest.py is imported
# before any test module, and SHISUKO_HOME points at a temporary folder from here on, for server.py
# (its paths are fixed at import), amd_setup.py and every child process the tests start. The
# session check below fails the run when the folder changed anyway.
#
# What it watches: every name at the top level (a file by its mtime, a folder by its name only),
# and one level down the AMD engine's folders, which amd_setup.py installs, swaps (replace_dir())
# and removes, and its download folder, whose leftover would be a .part file: there a folder counts
# by its mtime too. models/ and cache/ themselves are not looked into, nor are server.log and the
# server-<port>.lock files (OWN_SERVER_FILES): the viewer's own server, which may well be running
# while the tests do, writes those, never a test.
REAL_HOME = Path.home() / ".shisu-ko"
WATCHED = ("rocm", "rocm.new", "rocm.old", "cache/rocm-download")
# Written by the viewer's own server while the tests run, never by a test: native_host.launch()
# appends the server's output to server.log (the Start button on Linux/macOS), and a server
# started meanwhile creates its server-<port>.lock.
OWN_SERVER_FILES = re.compile(r"server\.log|server-\d+\.lock")


def top_level(folder: Path, dirs_too: bool = False) -> Optional[dict]:
    """The names in `folder`, each with its mtime (ns) when it is a file (or, with `dirs_too`, a
    folder) and None for anything else; None when there is no such folder. OWN_SERVER_FILES are left
    out. Only a listing and stat(): no file is opened, read or changed.
    """
    try:
        with os.scandir(folder) as entries:
            found = {}
            for entry in entries:
                if OWN_SERVER_FILES.fullmatch(entry.name):
                    continue
                try:
                    dated = entry.is_file(follow_symlinks=False) or (dirs_too and entry.is_dir(follow_symlinks=False))
                    found[entry.name] = entry.stat(follow_symlinks=False).st_mtime_ns if dated else None
                except OSError:
                    found[entry.name] = None
            return found
    except (FileNotFoundError, NotADirectoryError):
        return None


def snapshot(home: Path) -> dict:
    """top_level() of `home` (under the key "") and of each WATCHED folder in it (under its path)."""
    return {"": top_level(home), **{rel: top_level(home / rel, dirs_too=True) for rel in WATCHED}}


def listing_changes(before: Optional[dict], after: Optional[dict], prefix: str = "") -> list[str]:
    """What differs between two top_level() listings of one folder, one line each."""
    if before is None:
        # Nothing to compare with; a folder that appears during the session was made by it.
        if after is None:
            return []
        what = f"created: {prefix.rstrip('/')}" if prefix else "the folder was created"
        return [what + (", holding: " + ", ".join(sorted(after)) if after else "")]
    if after is None:
        return [f"removed: {prefix.rstrip('/')}" if prefix else "the folder was removed"]
    return ([f"new: {prefix}{name}" for name in sorted(after.keys() - before.keys())]
            + [f"gone: {prefix}{name}" for name in sorted(before.keys() - after.keys())]
            + [f"modified: {prefix}{name}" for name in sorted(before.keys() & after.keys())
               if before[name] != after[name]])


def home_changes(before: dict, after: dict) -> list[str]:
    """What differs between two snapshot()s of the real folder, one line each, named by its path in it."""
    changes = []
    for rel in sorted(before.keys() | after.keys()):
        changes += listing_changes(before.get(rel), after.get(rel), f"{rel}/" if rel else "")
    return changes


_real_home_before = snapshot(REAL_HOME)
isolate_home()

# The native libraries that come with faster-whisper (CTranslate2, onnxruntime) keep threads of
# their own, and the C++ runtime can tear them down in the wrong order when the interpreter exits:
# on CI's Python 3.10 the process once aborted with "terminate called without an active exception"
# (exit code 134) after every test had passed, failing the job. On CI, once pytest has written its
# report, the process ends with the session's own exit status, before those destructors run.
# Nothing is lost: the report, the cache and any junit file are written before unconfigure.
_session_status: int | None = None


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """The real-folder check, after every test and fixture teardown (trylast: after the runner's own
    sessionfinish, which tears down the session fixtures); a change fails the run."""
    global _session_status
    changes = home_changes(_real_home_before, snapshot(REAL_HOME))
    if changes:
        lines = [f"The tests wrote to the real data folder {REAL_HOME}; nothing may reach it "
                 "(SHISUKO_HOME is a temporary folder for the tests):", *(f"  {line}" for line in changes)]
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            print("\n".join(lines), file=sys.stderr)
        else:
            reporter.write_sep("=", "real ~/.shisu-ko changed", red=True, bold=True)
            for line in lines:
                reporter.write_line(line, red=True)
        if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    _session_status = int(session.exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if _session_status is None or os.environ.get("CI") != "true":
        return
    remove_temporary_homes()  # os._exit() skips atexit
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_session_status)

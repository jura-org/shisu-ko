"""Shared pytest set-up for the server tests."""
from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional

import pytest

from _serverlib import isolate_home, loaded_server, remove_temporary_homes

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

# With pytest-xdist installed (server/requirements-test.txt) a run of the whole folder goes in
# parallel: one worker per core, at most XDIST_WORKERS, unless the command line names a count (-n 0
# runs it in this one process, -n auto on every core) or PYTEST_XDIST_AUTO_NUM_WORKERS does. A
# command line that names only files or tests runs them in one process, where starting the workers
# would cost more than it saves (one file: 0.3 s alone, 6.7 s on 8 workers). The process started
# here is then the controller, which hands out the tests and reports; every worker is a pytest
# process of its own that loads this file again, with a SHISUKO_HOME of its own (isolate_home()),
# and PYTEST_XDIST_WORKER set, since a worker must not start workers in turn.
#
# Why at most 8: xdist starts its workers one after another, about half a second each on Windows,
# and hands out no test before every one has collected (some 4 s). On 24 cores (Windows, Python
# 3.12, 2026-10-18) the suite took 74 s in one process, 27 s on 4 workers, 22-23 s on 8 to 12
# and 32-35 s on 24.
XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
XDIST_WORKERS = 8


def names_a_folder(config) -> bool:
    """Whether the command line runs a whole folder; naming nothing runs the current one."""
    return any(Path(config.invocation_params.dir, arg.split("::")[0]).is_dir() for arg in config.args)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """The default worker count. Runs before the plugin's own hook (tryfirst, registered after it),
    which sets the run up for that count; --pdb, which needs one process, keeps it at none."""
    if XDIST_WORKER or not config.pluginmanager.hasplugin("xdist"):
        return
    if config.option.numprocesses is None and not config.getoption("usepdb") and names_a_folder(config):
        cores = config.hook.pytest_xdist_auto_num_workers(config=config)  # what -n auto would start
        chosen = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
        config.option.numprocesses = cores if chosen else min(cores, XDIST_WORKERS)


class StoppedApp:
    """The App a Transcriber thread finds once its test is over: no model to switch, no work, no sweep.

    The thread's loop asks these three and sleeps, so from its next round on it touches nothing else;
    `seen` is set at the start of that round, once whatever it was doing for its own App is done.
    """

    def __init__(self):
        self.seen = threading.Event()
        self.sweep = iter(())

    def switch_model_if_wanted(self) -> bool:
        self.seen.set()
        return False

    def pick_work(self):
        return None


@pytest.fixture(autouse=True)
def stop_transcribers():
    """Takes every Transcriber thread a test started off its App when the test ends.

    App.__init__ starts one, and its loop never ends. Left alone it went on picking work from the
    finished test's sessions while the tests after it ran, with whatever those had patched into
    server.py (CACHE_DIR, detect_speech, ...): over a hundred tests left one, about a dozen with a
    session still to transcribe, and which later test it ran into depended on the order, which
    xdist changes from run to run (build-and-test.md, "Parallel runs"). Set up before any other
    fixture of the test, so torn down after them all, the test's monkeypatch included. A thread
    that takes longer than 10 s to come back from its round is left to find the StoppedApp then.
    """
    before = set(threading.enumerate())
    yield
    server = loaded_server()
    if server is None:
        return
    stopped = [thread for thread in threading.enumerate()
               if thread not in before and isinstance(thread, server.Transcriber)]
    for thread in stopped:
        thread.app = StoppedApp()
    for thread in stopped:
        thread.app.seen.wait(10)


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
    sessionfinish, which tears down the session fixtures); a change fails the run. Under xdist the
    controller checks, once every worker has finished: a worker's exit status is not the run's."""
    global _session_status
    changes = [] if XDIST_WORKER else home_changes(_real_home_before, snapshot(REAL_HOME))
    if changes:
        lines = [f"The tests wrote to the real data folder {REAL_HOME}; nothing may reach it "
                 "(SHISUKO_HOME is a temporary folder for the tests):", *(f"  {line}" for line in changes)]
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            print("\n".join(lines), file=sys.stderr)
        else:
            reporter.ensure_newline()  # after the last progress line
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

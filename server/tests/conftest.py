"""Shared pytest set-up for the server tests."""
from __future__ import annotations

import os
import sys

import pytest

# The native libraries that come with faster-whisper (CTranslate2, onnxruntime) keep threads of
# their own, and the C++ runtime can tear them down in the wrong order when the interpreter exits:
# on CI's Python 3.10 the process once aborted with "terminate called without an active exception"
# (exit code 134) after every test had passed, failing the job. On CI, once pytest has written its
# report, the process ends with the session's own exit status, before those destructors run.
# Nothing is lost: the report, the cache and any junit file are written before unconfigure.
_session_status: int | None = None


def pytest_sessionfinish(session, exitstatus):
    global _session_status
    _session_status = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if _session_status is None or os.environ.get("CI") != "true":
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_session_status)

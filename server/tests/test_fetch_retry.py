"""A failed audio fetch must be retried after --retry-after seconds, not stick in status "error"."""
from __future__ import annotations

import time
from types import SimpleNamespace

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"


def make_app(monkeypatch, tmp_path, retry_after=30.0):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3", language="ja",
                           idle_minutes=30, retry_after=retry_after)
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    calls = []
    app.fetcher = SimpleNamespace(fetch=lambda s: calls.append(s.video_id))
    return app, calls


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def fail_session(s, age_seconds):
    with s.lock:
        s.status = "error"
        s.error = "boom"
        s.error_at = time.time() - age_seconds
        s.fetching = False


def test_first_sync_starts_a_fetch(monkeypatch, tmp_path):
    app, calls = make_app(monkeypatch, tmp_path)
    app.get_session(VIDEO, "u")
    assert wait_for(lambda: calls == [VIDEO])


def test_failed_fetch_is_retried_once_the_delay_has_passed(monkeypatch, tmp_path):
    app, calls = make_app(monkeypatch, tmp_path, retry_after=30.0)
    s = app.get_session(VIDEO, "u")
    assert wait_for(lambda: len(calls) == 1)
    fail_session(s, age_seconds=31)
    app.get_session(VIDEO, "u")
    assert wait_for(lambda: len(calls) == 2)


def test_recent_failure_is_not_retried_yet(monkeypatch, tmp_path):
    app, calls = make_app(monkeypatch, tmp_path, retry_after=30.0)
    s = app.get_session(VIDEO, "u")
    assert wait_for(lambda: len(calls) == 1)
    fail_session(s, age_seconds=1)
    app.get_session(VIDEO, "u")
    time.sleep(0.2)
    assert calls == [VIDEO]


def test_fetch_failure_records_the_time(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    fetcher = server.Fetcher(SimpleNamespace(cookies_from_browser="", cookies="", allow_remote_ejs=False,
                                             js_runtime="auto", language="ja", language_patience=60.0))
    monkeypatch.setattr(fetcher, "download", lambda s: (_ for _ in ()).throw(RuntimeError("Video unavailable")))
    s = server.Session(video_id=VIDEO, url="u")
    before = time.time()
    fetcher.fetch(s)
    assert s.status == "error"
    assert s.error == "Video unavailable"
    assert s.error_at >= before
    assert s.fetching is False

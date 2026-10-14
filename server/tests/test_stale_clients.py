"""Work is only scheduled for videos whose client is still syncing."""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"


def make_app(monkeypatch, tmp_path, client_timeout):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3", language="ja",
                           idle_minutes=30, client_timeout=client_timeout)
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


def ready_session(last_sync_age):
    s = server.Session(video_id=VIDEO, url="u", status="ready", audio=np.zeros(16000, dtype=np.float32), duration=300.0)
    s.last_sync = time.time() - last_sync_age
    return s


def test_recently_synced_session_gets_work(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, client_timeout=30.0)
    s = ready_session(last_sync_age=1.0)
    with app.lock:
        app.sessions[VIDEO] = s
    picked = app.pick_work()
    assert picked is not None and picked[0] is s


def test_stale_session_is_skipped(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, client_timeout=30.0)
    with app.lock:
        app.sessions[VIDEO] = ready_session(last_sync_age=31.0)
    assert app.pick_work() is None


def test_zero_timeout_disables_the_check(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, client_timeout=0.0)
    with app.lock:
        app.sessions[VIDEO] = ready_session(last_sync_age=3600.0)
    assert app.pick_work() is not None


def test_a_gap_too_short_for_a_window_is_saved_once_covered(monkeypatch, tmp_path):
    # plan_window() covers a sub-1.5 s gap without a window; unsaved, the record stays short of
    # fully covered and every load decodes the audio again only to cover it in memory once more.
    import json

    app = make_app(monkeypatch, tmp_path, client_timeout=30.0)
    s = ready_session(last_sync_age=1.0)
    s.duration, s.covered, s.want_t = 300.0, [[0.0, 299.2]], 298.0
    with app.lock:
        app.sessions[VIDEO] = s
    assert app.pick_work() is None
    assert s.fully_covered()
    assert json.loads(s.cache_path().read_text(encoding="utf-8"))["covered"] == [[0.0, 300.0]]

    s.cache_path().unlink()
    s.covered, s.want_t = [[0.95, 300.0]], 0.0  # a gap at the start, which changes neither count nor end
    assert app.pick_work() is None
    assert json.loads(s.cache_path().read_text(encoding="utf-8"))["covered"] == [[0.0, 300.0]]
    s.cache_path().unlink()
    assert app.pick_work() is None and not s.cache_path().exists()  # nothing covered, nothing written

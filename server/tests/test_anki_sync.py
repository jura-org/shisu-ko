"""The Anki sync after mining: AnkiWatcher watches Anki's newest notes and syncs after new ones.

Every test drives the watcher with a fake clock and a fake AnkiConnect (`FakeAnki`); nothing here
reaches a real Anki. anki_connect() is checked against a small HTTP server on a free port.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from _serverlib import load_server
from test_update_endpoint import make_app

server = load_server()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeAnki:
    """AnkiConnect's findNotes and sync. `notes` is what the query finds; `fail` the exception the
    next calls raise (findNotes and sync alike when `down`, else sync only)."""

    def __init__(self, clock, notes=()):
        self.clock = clock
        self.notes = set(notes)
        self.syncs: list[float] = []
        self.queries: list[str] = []
        self.down = False
        self.fail = None
        self.sync_seconds = 0.0  # how long a sync takes, on the fake clock

    def __call__(self, action, params, timeout):
        if self.down:
            raise server.AnkiUnreachable("[Errno 111] Connection refused")
        if action == "findNotes":
            self.queries.append(params["query"])
            return sorted(self.notes)
        if action == "sync":
            self.syncs.append(self.clock.t)
            self.clock.t += self.sync_seconds
            if self.fail is not None:
                raise self.fail
            return None
        raise AssertionError(action)


def make(notes=(1, 2, 3)):
    clock = Clock()
    anki = FakeAnki(clock, notes)
    watcher = server.AnkiWatcher(anki, clock=clock, wall=lambda: 1_700_000_000.0)
    return clock, anki, watcher


def run_until(clock, watcher, end, events=()):
    """Step the watcher until `end`, applying each (time, fn) in `events` when its time comes."""
    events = sorted(events, key=lambda e: e[0])
    while clock.t < end:
        wait = watcher.step()
        nxt = clock.t + max(wait, 0.001)
        if events and events[0][0] <= nxt:
            at, fn = events.pop(0)
            clock.t = max(clock.t, at)
            fn()
            continue
        clock.t = min(nxt, end)


def test_the_first_look_is_the_baseline_and_never_syncs():
    clock, anki, watcher = make(notes=range(1, 50))
    run_until(clock, watcher, 600)
    assert anki.syncs == []
    assert watcher.seen == set(range(1, 50))
    assert watcher.status() == {"state": "watching", "last": None, "error": None}
    assert anki.queries and set(anki.queries) == {server.ANKI_QUERY}


def test_one_new_note_syncs_a_minute_after_it_was_seen_and_once():
    clock, anki, watcher = make()
    run_until(clock, watcher, 100, [(100, lambda: anki.notes.add(10))])
    seen_at = clock.t
    run_until(clock, watcher, 400)
    assert len(anki.syncs) == 1
    # Found by the next look (within ANKI_POLL), then ANKI_SYNC_FIRST later.
    assert seen_at + server.ANKI_SYNC_FIRST <= anki.syncs[0] <= seen_at + server.ANKI_POLL + server.ANKI_SYNC_FIRST
    status = watcher.status()
    assert status == {"state": "watching", "last": 1_700_000_000_000, "error": None}


def test_steady_mining_syncs_every_30_s_and_stops_when_the_notes_stop():
    clock, anki, watcher = make()
    # A new note every 10 s for three minutes, from t=100.
    events = [(100 + 10 * i, (lambda i=i: anki.notes.add(100 + i))) for i in range(18)]
    run_until(clock, watcher, 800, events)
    gaps = [b - a for a, b in zip(anki.syncs, anki.syncs[1:])]
    assert len(anki.syncs) >= 4
    assert anki.syncs[0] >= 160  # the first waits its minute
    assert all(abs(g - server.ANKI_SYNC_REPEAT) < 1e-6 for g in gaps), gaps
    assert anki.syncs[-1] < 100 + 180 + server.ANKI_POLL + server.ANKI_SYNC_REPEAT + 1  # no sync long after the last note
    assert watcher.due is None and watcher.cards == 0


def test_a_note_made_during_a_slow_sync_goes_with_the_next_one_at_once():
    clock, anki, watcher = make()
    anki.sync_seconds = 45.0  # longer than ANKI_SYNC_REPEAT
    run_until(clock, watcher, 50, [(20, lambda: anki.notes.add(10))])
    run_until(clock, watcher, 200, [(anki_first := 20 + 15 + 60 + 5, lambda: anki.notes.add(11))])
    assert len(anki.syncs) == 2
    first_end = anki.syncs[0] + 45.0
    assert anki.syncs[0] < anki_first < first_end
    assert anki.syncs[1] == pytest.approx(first_end)  # 30 s after its start have passed: at once


def test_syncs_never_overlap():
    clock, anki, watcher = make()
    watcher.note_added(1, clock.t)
    clock.t = 100
    watcher.running = True  # a sync under way (on another thread, were there one)
    assert watcher.sync() is False
    watcher.running = False
    assert watcher.sync() is True and len(anki.syncs) == 1


def test_a_failed_sync_is_retried_after_1_2_and_then_every_5_minutes():
    clock, anki, watcher = make()
    anki.fail = RuntimeError("network error: connection reset")
    run_until(clock, watcher, 30, [(10, lambda: anki.notes.add(10))])
    run_until(clock, watcher, 2000)
    gaps = [round(b - a) for a, b in zip(anki.syncs, anki.syncs[1:])]
    assert gaps[:4] == [60, 120, 300, 300]
    status = watcher.status()
    assert status["state"] == "pending" and status["error"] == "network error: connection reset" and status["last"] is None
    anki.fail = None
    run_until(clock, watcher, 2400)
    assert watcher.status() == {"state": "watching", "last": 1_700_000_000_000, "error": None}
    assert watcher.failures == 0


@pytest.mark.parametrize("message, text", [
    ("sync: auth not configured", "not signed in to AnkiWeb"),
    ("Sync status 2 not one of [0, 1] - see SyncCollectionResponse.ChangesRequired", "Anki asks for a full sync; press Sync in Anki once"),
])
def test_login_and_full_sync_halt_until_the_next_new_note(message, text):
    clock, anki, watcher = make()
    anki.fail = RuntimeError(message)
    run_until(clock, watcher, 30, [(10, lambda: anki.notes.add(10))])
    run_until(clock, watcher, 3000)
    assert len(anki.syncs) == 1
    assert watcher.status() == {"state": "halted", "last": None, "error": text}
    anki.fail = None
    run_until(clock, watcher, 3200, [(3010, lambda: anki.notes.add(11))])
    assert len(anki.syncs) == 2 and watcher.status()["state"] == "watching"


def test_failure_classes():
    f = server.anki_sync_failure
    assert f(server.AnkiUnreachable("[Errno 111] Connection refused")) == ("Anki is not running", False)
    assert f(server.AnkiUnreachable("timed out")) == ("Anki did not answer in time", False)
    assert f(RuntimeError("sync: auth not configured")) == ("not signed in to AnkiWeb", True)
    assert f(RuntimeError("Sync status 3 not one of [0, 1]"))[1] is True
    assert f(RuntimeError("x" * 500)) == ("x" * 160, False)


def test_the_day_rollover_neither_counts_nor_loses_notes():
    clock, anki, watcher = make(notes=(1, 2, 3))
    run_until(clock, watcher, 30)
    # A note made seconds before Anki's day boundary...
    anki.notes.add(4)
    # ...and the boundary itself: the day before yesterday leaves the query, the note stays
    # (two days are queried), and the next look still finds it.
    anki.notes -= {1, 2}
    run_until(clock, watcher, 200)
    assert len(anki.syncs) == 1
    assert watcher.seen == {3, 4}
    # Another boundary with nothing new: no sync.
    anki.notes -= {3}
    run_until(clock, watcher, 600)
    assert len(anki.syncs) == 1
    assert server.ANKI_QUERY == "added:2"


def test_anki_closed_is_watched_quietly_with_backoff_and_one_log_line(caplog):
    clock, anki, watcher = make()
    anki.down = True
    with caplog.at_level("INFO", logger="shisu-ko"):
        looks = []
        real = anki.__call__

        def counting(action, params, timeout):
            if action == "findNotes":
                looks.append(clock.t)
            return real(action, params, timeout)

        watcher.call = counting
        run_until(clock, watcher, 1000)
    gaps = [b - a for a, b in zip(looks, looks[1:])]
    assert max(gaps) == server.ANKI_POLL_MAX and gaps[0] < server.ANKI_POLL_MAX
    assert len(looks) < 25
    lines = [r.getMessage() for r in caplog.records if "anki sync" in r.getMessage()]
    assert len(lines) == 1 and "not answering" in lines[0], lines
    assert watcher.status() == {"state": "unreachable", "last": None, "error": "AnkiConnect is not answering"}
    assert watcher.seen is None  # no baseline yet: the first answer will be it
    anki.down = False
    anki.notes.add(10)  # made while the server could not look: before its baseline, never synced for
    run_until(clock, watcher, 1300)
    assert anki.syncs == [] and watcher.status()["state"] == "watching"


def test_anki_closed_after_the_baseline_keeps_what_it_saw():
    clock, anki, watcher = make()
    run_until(clock, watcher, 30)
    anki.down = True
    run_until(clock, watcher, 300)
    anki.down = False
    anki.notes.add(10)
    run_until(clock, watcher, 500)
    assert len(anki.syncs) == 1


def test_the_log_says_what_happened(caplog):
    clock, anki, watcher = make()
    with caplog.at_level("INFO", logger="shisu-ko"):
        run_until(clock, watcher, 300, [(10, lambda: anki.notes.update({10, 11}))])
    lines = [r.getMessage() for r in caplog.records]
    assert "anki sync: 2 new note(s), syncing" in lines
    assert "anki sync: done in 0.0 s" in lines


def test_resolve_anki_sync_defaults_and_the_container():
    resolve = server.resolve_anki_sync
    args = lambda **kw: SimpleNamespace(**{"anki_sync": None, "anki_url": None, **kw})  # noqa: E731
    host, container = {}, {"SHISUKO_CONTAINER": "1"}
    assert resolve(args(), host) == (True, server.ANKI_DEFAULT_URL)
    assert resolve(args(anki_sync="off"), host) == (False, server.ANKI_DEFAULT_URL)
    assert resolve(args(), container) == (False, server.ANKI_DEFAULT_URL)
    assert resolve(args(anki_sync="auto"), container) == (True, server.ANKI_DEFAULT_URL)
    url = "http://host.docker.internal:8765"
    assert resolve(args(anki_url=url), container) == (True, url)
    assert resolve(args(anki_url=url, anki_sync="off"), container) == (False, url)
    # An App built by hand, with arguments that predate the options.
    assert resolve(SimpleNamespace(), host) == (True, server.ANKI_DEFAULT_URL)


def test_parse_args_takes_the_options_and_refuses_a_bad_address():
    args = server.parse_args([])
    assert (args.anki_sync, args.anki_url) == (None, None)
    args = server.parse_args(["--anki-sync", "off", "--anki-url", "http://127.0.0.1:9000"])
    assert (args.anki_sync, args.anki_url) == ("off", "http://127.0.0.1:9000")
    for bad in ("127.0.0.1:8765", "https://127.0.0.1:8765", "http://", "http://127.0.0.1:99999"):
        with pytest.raises(SystemExit):
            server.parse_args(["--anki-url", bad])
    with pytest.raises(SystemExit):
        server.parse_args(["--anki-sync", "on"])


def test_anki_sync_off_starts_no_watcher(monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **kw: started.append(kw) or SimpleNamespace(start=lambda: None))
    assert server.start_anki_watcher(SimpleNamespace(anki_sync="off", anki_url=None)) is None
    monkeypatch.setenv("SHISUKO_CONTAINER", "1")
    assert server.start_anki_watcher(SimpleNamespace(anki_sync=None, anki_url=None)) is None
    assert started == []
    watcher = server.start_anki_watcher(SimpleNamespace(anki_sync="auto", anki_url=None))
    assert isinstance(watcher, server.AnkiWatcher) and [kw["name"] for kw in started] == ["anki-sync"]


def test_health_carries_the_sync_state_small_and_additive(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    health = app.health()
    assert health["anki_sync"] == {"state": "off", "last": None, "error": None}
    assert {"version", "model", "language", "launcher"} <= set(health)
    _, _, watcher = make()
    app.anki_watcher = watcher
    assert app.health()["anki_sync"] == {"state": "starting", "last": None, "error": None}
    watcher.step()
    assert app.health()["anki_sync"]["state"] == "watching"
    json.dumps(app.health())


class FakeAnkiConnect(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAnkiConnect.seen.append((dict(self.headers), body))
        if body["action"] == "sync":
            answer = {"result": None, "error": "sync: auth not configured"}
        else:
            answer = {"result": [5, 6], "error": None}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_anki_connect_sends_no_content_type_and_no_origin():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnkiConnect)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        FakeAnkiConnect.seen.clear()
        assert server.anki_connect(url, "findNotes", {"query": "added:2"}) == [5, 6]
        headers, body = FakeAnkiConnect.seen[0]
        lowered = {k.lower() for k in headers}
        assert "content-type" not in lowered and "origin" not in lowered
        assert body == {"action": "findNotes", "version": 6, "params": {"query": "added:2"}}
        with pytest.raises(RuntimeError, match="auth not configured"):
            server.anki_connect(url, "sync")
    finally:
        httpd.shutdown()
        httpd.server_close()
    with pytest.raises(server.AnkiUnreachable):
        server.anki_connect(url, "findNotes", {"query": "added:2"}, timeout=2)

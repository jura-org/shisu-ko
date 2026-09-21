"""The extension can ask /sync for another Whisper model; the transcriber switches between windows.

A switch has two steps: prepare_model() downloads the files on a side thread while the loaded
model keeps working, then switch_model_if_wanted() swaps the models on the transcriber thread.
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"

# A slice of faster_whisper.utils._MODELS: an alias and its repo id are the same weights.
FAKE_MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "small": "Systran/faster-whisper-small",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


@pytest.fixture(autouse=True)
def fresh_alias_table(monkeypatch):
    # canonical_model_name() remembers faster-whisper's table once it could import it; every
    # test starts without one so a fake installed by an earlier test cannot leak into it.
    monkeypatch.setattr(server, "_MODEL_ALIASES", None)


class InertTranscriber:
    """Stands in for the transcriber thread so a test is the only caller of switch_model_if_wanted().

    App.__init__ starts the real thread, which polls switch_model_if_wanted() every 0.15 s and
    would race a test that sets wanted_model and then switches by hand. Without any thread the
    App is otherwise unchanged; test_transcriber_thread_performs_the_switch covers the real one.
    """

    def __init__(self, app):
        self.app = app

    def start(self):
        pass


def make_app(monkeypatch, tmp_path, inert=True, **overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    if inert:
        monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    base = dict(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3", language="ja",
                idle_minutes=30, retry_after=30.0)
    base.update(overrides)
    app = server.App(SimpleNamespace(**base), model=object(), device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


def fake_loader(monkeypatch, fail=(), observe=None):
    """load_model stand-in: records the names it is asked for, raises for those in `fail`."""
    calls = []

    def load(args, name=None, path=None):
        name = name or args.model
        calls.append(name)
        if observe is not None:
            observe(name, path)
        if name in fail:
            raise RuntimeError(f"no such model {name}")
        return (object(), "cpu", "int8")

    monkeypatch.setattr(server, "load_model", load)
    return calls


def fake_whisper(monkeypatch, tmp_path, fail=None, bare=(), block=None, table=None):
    """A faster_whisper module with download_model() only; the real one is not installed for the tests.

    Like the real one it maps size aliases through its table (ValueError for an unknown size) and
    passes a repo id through; the "download" is a directory under tmp_path. Names in `fail` raise
    the given exception, names in `bare` get a directory without model.bin, and with `block` every
    download waits for that Event first, so a test can look at the server mid-download. Returns
    the list of names asked for.
    """
    downloads = []
    fail = fail or {}

    def download_model(name, cache_dir=None, **kwargs):
        downloads.append(name)
        if block is not None:
            block.wait(5.0)
        if name in fail:
            raise fail[name]
        if "/" not in name and name not in FAKE_MODELS:
            raise ValueError(f"Invalid model size '{name}', expected one of: {', '.join(FAKE_MODELS)}")
        directory = tmp_path / "downloads" / name.replace("/", "--")
        directory.mkdir(parents=True, exist_ok=True)
        if name not in bare:
            (directory / "model.bin").write_bytes(b"\0")
        return str(directory)

    utils = SimpleNamespace(_MODELS=dict(FAKE_MODELS if table is None else table))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(download_model=download_model, utils=utils))
    return downloads


def tick(app):
    """One transcriber tick, then wait for the download thread it may have started.

    The real transcriber polls switch_model_if_wanted() every 0.15 s; here the test is the
    caller. A tick that starts a prepare returns at once (the download runs on prepare_thread,
    as in production) and joining that thread makes the next tick deterministic, without a sleep
    loop and without faking threading.Thread, which the fetcher and the HTTP server use too.
    """
    switched = app.switch_model_if_wanted()
    thread = app.prepare_thread
    if thread is not None:
        thread.join(5.0)
        assert not thread.is_alive()
    return switched


def switch(app):
    """A whole switch: the tick that prepares, then the tick that swaps. Returns the second one."""
    assert tick(app) is False
    return tick(app)


def write_cache(tmp_path, model, cues, covered, duration=100.0, title="t", suffix=""):
    data = {
        "video_id": VIDEO, "title": title, "duration": duration, "format": server.CACHE_FORMAT,
        "model": model, "language": "ja", "cues": cues, "covered": covered, "speech": [],
        "lyrics": "auto",  # made under the rule: nothing here is a sung stretch to offer again
    }
    (tmp_path / f"{VIDEO}.{suffix}cues.json").write_text(json.dumps(data), encoding="utf-8")


def read_cache(tmp_path, suffix=""):
    return json.loads((tmp_path / f"{VIDEO}.{suffix}cues.json").read_text(encoding="utf-8"))


def snapshot(s):
    with s.lock:
        return (list(s.cues), list(s.covered), list(s.speech), s.seg_next, s.token, s.status, s.busy)


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def fake_clock(monkeypatch, start=1000.0):
    """server.time with a time() the test moves by hand; everything else stays the real module's."""
    now = [start]
    names = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    names["time"] = lambda: now[0]
    monkeypatch.setattr(server, "time", SimpleNamespace(**names))
    return now


# --------------------------------------------------------------------------- names

@pytest.mark.parametrize("name", [
    "large-v3", "large-v3-turbo", "distil-large-v3", "medium", "small", "base", "tiny",
    "kotoba-tech/kotoba-whisper-v2.0-faster", "deepdml/faster-whisper-large-v3-turbo-ct2",
    "Systran/faster-whisper-large-v3", "a" * 96, "o/" + "n" * 96,
])
def test_valid_model_names(name):
    assert server.valid_model_name(name)


@pytest.mark.parametrize("name", [
    "../x", "/abs", "C:\\x", "a/b/c", "", "a" * 200, "owner/..name", "owner/a..b", ".hidden", "-x",
    "owner/", "/name", "a b", "a\\b", "owner/name/", None, 5, "small\n", "small\r\n", "\nsmall",
])
def test_invalid_model_names(name):
    assert not server.valid_model_name(name)


def test_canonical_model_name_follows_faster_whispers_table(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    assert server.canonical_model_name("Systran/faster-whisper-large-v3") == "large-v3"
    assert server.canonical_model_name("large-v3") == "large-v3"
    assert server.canonical_model_name("large") == "large-v3"  # the first alias of the repo wins
    assert server.canonical_model_name("turbo") == "large-v3-turbo"
    assert server.canonical_model_name("mobiuslabsgmbh/faster-whisper-large-v3-turbo") == "large-v3-turbo"
    assert server.canonical_model_name("kotoba-tech/kotoba-whisper-v2.0-faster") == "kotoba-tech/kotoba-whisper-v2.0-faster"
    assert server.canonical_model_name(None) is None and server.canonical_model_name("") == ""


def test_canonical_model_name_without_faster_whisper_or_its_table(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import fails
    assert server.canonical_model_name("Systran/faster-whisper-large-v3") == "Systran/faster-whisper-large-v3"
    assert server._MODEL_ALIASES is None  # nothing remembered: the library may still appear
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(utils=SimpleNamespace()))  # no _MODELS
    assert server.canonical_model_name("large") == "large"


def test_model_spellings_lists_every_name_of_the_same_weights(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    # The MLX repo of the same weights comes last: on an Apple GPU that is the name the viewer may
    # have typed, and the popup matches the field's text against this list.
    turbo = ["large-v3-turbo", "turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
             "mlx-community/whisper-large-v3-turbo"]
    assert server.model_spellings("large-v3-turbo") == turbo
    assert server.model_spellings("turbo") == turbo and server.model_spellings(turbo[2]) == turbo
    assert server.model_spellings("tiny") == ["tiny", "Systran/faster-whisper-tiny",
                                              "mlx-community/whisper-tiny-mlx"]
    assert server.model_spellings("kotoba-tech/kotoba-whisper-v2.0-faster") == ["kotoba-tech/kotoba-whisper-v2.0-faster"]
    monkeypatch.setattr(server, "_MODEL_ALIASES", None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # without the table a name is its own only spelling
    assert server.model_spellings("large") == ["large"]


# --------------------------------------------------------------------------- /sync

def test_sync_without_a_model_wants_the_default(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0, "")
    assert app.wanted_model == "large-v3"
    assert (resp["model"], resp["model_loading"], resp["model_error"]) == ("large-v3", None, None)


def test_sync_without_the_argument_keeps_working(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert app.wanted_model == "large-v3"
    assert resp["model"] == "large-v3" and resp["model_error"] is None


def test_sync_with_a_valid_name_records_the_wish(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0, " kotoba-tech/kotoba-whisper-v2.0-faster ")
    assert app.wanted_model == "kotoba-tech/kotoba-whisper-v2.0-faster"
    assert resp["model"] == "large-v3"  # still the loaded one until the transcriber switches
    assert resp["model_error"] is None


def test_sync_with_an_invalid_name_reports_it_without_storing_it(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0, "../x")
    assert app.wanted_model == "large-v3"
    assert resp["model_error"].startswith("not a model name")
    assert "owner/name" in resp["model_error"]
    # The complaint belongs to that request alone: a well-formed one gets a clean answer.
    assert app.sync(VIDEO, "u", 0.0, 0, "")["model_error"] is None


def test_empty_request_with_a_path_default_is_not_validated(monkeypatch, tmp_path):
    # The operator's --model may be a local directory; only names sent by a browser are checked.
    default = str(tmp_path / "my-ct2-model")
    app = make_app(monkeypatch, tmp_path, model=default)
    resp = app.sync(VIDEO, "u", 0.0, 0, "")
    assert resp["model"] == default and resp["model_error"] is None
    assert app.wanted_model == default
    app.request_model("small")
    assert app.wanted_model == "small"
    app.request_model("   ")  # the setting was cleared: back to the default
    assert app.wanted_model == default
    assert app.health()["default_model"] == default


def test_the_default_folder_comes_back_without_a_download(monkeypatch, tmp_path):
    # A folder (what ct2-transformers-converter leaves) loads at startup, but the hub refuses the
    # path; switching back to it must not ask the hub at all.
    default = tmp_path / "my-ct2-model"
    default.mkdir()
    downloads = fake_whisper(monkeypatch, tmp_path)
    seen = []
    calls = fake_loader(monkeypatch, observe=lambda name, path: seen.append(path))
    app = make_app(monkeypatch, tmp_path, model=str(default))
    app.request_model("small")
    assert switch(app) is True and app.model_name == "small"
    app.request_model("")  # the setting was cleared: back to the operator's folder
    assert switch(app) is True
    assert (app.model_name, app.model_error, app.model_loading) == (str(default), None, None)
    assert downloads == ["small"] and calls == ["small", str(default)] and seen[-1] == str(default)
    assert app.sync(VIDEO, "u", 0.0, 0, "")["model_error"] is None


def test_request_with_the_repo_id_of_the_loaded_alias_is_a_no_op(monkeypatch, tmp_path):
    downloads = fake_whisper(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0, "Systran/faster-whisper-large-v3")
    assert app.wanted_model == "large-v3"
    assert (resp["model"], resp["model_loading"], resp["model_error"]) == ("large-v3", None, None)
    assert app.switch_model_if_wanted() is False
    assert downloads == [] and calls == [] and app.prepare_thread is None
    app.request_model("large")  # the same weights again
    assert app.wanted_model == "large-v3" and app.switch_model_if_wanted() is False


# --------------------------------------------------------------------------- the switch

def test_no_switch_when_nothing_else_is_wanted(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    assert app.switch_model_if_wanted() is False
    assert calls == [] and app.prepare_thread is None


def test_switch_prepares_first_then_replaces_the_model_and_restarts_sessions(monkeypatch, tmp_path):
    downloads = fake_whisper(monkeypatch, tmp_path)
    app = make_app(monkeypatch, tmp_path)
    seen = []
    calls = fake_loader(monkeypatch, observe=lambda name, path: seen.append((app.model_loading, app.model, path)))
    old_model = app.model
    cached = server.Session(video_id=VIDEO, url="u", status="ready", duration=100.0, covered=[[0.0, 100.0]],
                            cues=[{"id": 0, "start": 1.0, "end": 2.0, "text": "a", "seg": 0}], seg_next=1)
    decoded = server.Session(video_id="bcdefabcdefa", url="u", status="ready", duration=50.0,
                             audio=np.zeros(16000, dtype=np.float32), covered=[[0.0, 20.0]], busy=[20.0, 40.0])
    with app.lock:
        app.sessions = {cached.video_id: cached, decoded.video_id: decoded}
    tokens = (cached.token, decoded.token)
    app.request_model("small")

    assert tick(app) is False  # the files first; the old model is still the one in use
    assert downloads == ["small"] and calls == []
    assert app.model is old_model and app.model_name == "large-v3"
    assert (app.model_loading, app.model_preparing) == ("small", None)
    assert app.model_prepared[0] == "small"
    assert cached.token == tokens[0] and cached.cues  # nothing restarted yet

    assert tick(app) is True
    assert calls == ["small"]
    # The old model was released before the new one loaded, from the prepared directory.
    assert seen == [("small", None, str(tmp_path / "downloads" / "small"))]
    assert app.model is not old_model and app.model is not None
    assert (app.model_name, app.model_loading, app.model_error, app.model_prepared) == ("small", None, None, None)
    for s in (cached, decoded):
        assert (s.cues, s.covered, s.speech, s.seg_next, s.busy) == ([], [], [], 0, None)
        assert s.token not in tokens
    assert cached.status == "pending"  # only the old model's cache made it ready: fetch the audio again
    assert decoded.status == "ready"


def test_the_download_runs_beside_the_working_model(monkeypatch, tmp_path):
    gate = threading.Event()
    downloads = fake_whisper(monkeypatch, tmp_path, block=gate)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    old_model = app.model
    app.request_model("small")
    try:
        t0 = time.time()
        assert app.switch_model_if_wanted() is False  # returns while the download is still blocked
        assert time.time() - t0 < 2.0
        assert wait_for(lambda: downloads == ["small"])
        assert app.prepare_thread.is_alive()
        assert app.model is old_model and calls == []
        assert (app.model_loading, app.model_preparing, app.model_prepared) == ("small", "small", None)
        assert app.sync(VIDEO, "u", 0.0, 0, "small")["model_loading"] == "small"
        assert app.switch_model_if_wanted() is False  # a second tick does not start a second download
        assert downloads == ["small"] and app.model is old_model
    finally:
        gate.set()
    app.prepare_thread.join(5.0)
    assert app.model_prepared == ("small", str(tmp_path / "downloads" / "small"))
    assert app.model is old_model  # the swap waits for the transcriber's tick
    assert app.switch_model_if_wanted() is True
    assert calls == ["small"] and app.model is not old_model and app.model_name == "small"


def test_a_change_of_mind_during_the_download_is_honoured(monkeypatch, tmp_path):
    downloads = fake_whisper(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    app.request_model("small")
    assert tick(app) is False and downloads == ["small"]
    app.request_model("tiny")  # small's files are ready, but tiny is wanted now
    assert tick(app) is False
    assert downloads == ["small", "tiny"] and calls == []
    assert tick(app) is True and app.model_name == "tiny" and calls == ["tiny"]

    app.request_model("small")
    assert tick(app) is False and app.model_prepared[0] == "small"
    app.request_model("tiny")  # back to the loaded model: the prepared files are simply dropped
    assert app.wanted_model == "tiny"
    assert app.switch_model_if_wanted() is False
    assert (app.model_prepared, app.model_loading, app.model_name) == (None, None, "tiny")


def test_a_change_of_mind_while_the_download_still_runs_is_reported_at_once(monkeypatch, tmp_path):
    gate = threading.Event()
    downloads = fake_whisper(monkeypatch, tmp_path, block=gate)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    app.request_model("small")
    try:
        assert app.switch_model_if_wanted() is False
        assert wait_for(lambda: downloads == ["small"])
        assert app.sync(VIDEO, "u", 0.0, 0, "small")["model_loading"] == "small"
        app.request_model("")  # the setting was cleared while small is still downloading
        assert app.wanted_model == "large-v3"
        assert app.switch_model_if_wanted() is False
        assert (app.model_loading, app.model_preparing) == (None, "small")  # nothing will be loaded
        assert app.sync(VIDEO, "u", 0.0, 0, "")["model_loading"] is None
        assert app.health()["model_loading"] is None
        app.request_model("small")  # wanted again: the running download counts as loading once more
        assert app.switch_model_if_wanted() is False
        assert app.sync(VIDEO, "u", 0.0, 0, "small")["model_loading"] == "small"
        app.request_model("")
        assert app.switch_model_if_wanted() is False and app.model_loading is None
    finally:
        gate.set()
    app.prepare_thread.join(5.0)
    assert app.model_prepared[0] == "small"
    assert app.switch_model_if_wanted() is False  # the files nobody wants are dropped
    assert (app.model_prepared, app.model_loading, app.model_name) == (None, None, "large-v3")
    assert downloads == ["small"] and calls == []


def test_a_name_asked_for_while_another_download_runs_is_reported_as_loading(monkeypatch, tmp_path):
    gate = threading.Event()
    downloads = fake_whisper(monkeypatch, tmp_path, block=gate)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    app.request_model("small")
    try:
        assert app.switch_model_if_wanted() is False
        assert wait_for(lambda: downloads == ["small"])
        app.request_model("tiny")  # a typo corrected (or a change of mind) while small still downloads
        assert app.switch_model_if_wanted() is False
        # The running download cannot be cancelled, tiny waits behind it; meanwhile the viewer must
        # not be left with a status that looks as if the new name had been ignored.
        assert (app.model_loading, app.model_preparing, app.wanted_model) == ("tiny", "small", "tiny")
        resp = app.sync(VIDEO, "u", 0.0, 0, "tiny")
        assert (resp["model"], resp["model_loading"], resp["model_error"]) == ("large-v3", "tiny", None)
        assert app.health()["model_loading"] == "tiny" and app.health()["model_error"] is None
        assert app.switch_model_if_wanted() is False and downloads == ["small"]  # no second download yet
    finally:
        gate.set()
    app.prepare_thread.join(5.0)
    assert app.model_prepared[0] == "small"
    assert tick(app) is False  # small's files are dropped and tiny's download starts
    assert downloads == ["small", "tiny"] and app.model_loading == "tiny" and calls == []
    assert tick(app) is True and app.model_name == "tiny" and calls == ["tiny"]
    assert (app.model_loading, app.model_preparing, app.model_prepared) == (None, None, None)


def test_switch_leaves_fetching_and_evicted_sessions_alone(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    app = make_app(monkeypatch, tmp_path)
    fake_loader(monkeypatch)
    fetching = server.Session(video_id=VIDEO, url="u", status="downloading", fetching=True)
    evicted = server.Session(video_id="bcdefabcdefa", url="u", status="evicted")
    with app.lock:
        app.sessions = {fetching.video_id: fetching, evicted.video_id: evicted}
    app.request_model("small")
    assert switch(app) is True
    assert fetching.status == "downloading" and evicted.status == "evicted"


def test_switch_reloads_the_cache_of_the_new_model(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    app = make_app(monkeypatch, tmp_path)
    fake_loader(monkeypatch)
    s = server.Session(video_id=VIDEO, url="u", status="ready", duration=100.0,
                       cues=[{"id": 0, "start": 1.0, "end": 2.0, "text": "old", "seg": 0}], covered=[[0.0, 100.0]])
    with app.lock:
        app.sessions[VIDEO] = s
    write_cache(tmp_path, "small", [{"start": 3.0, "end": 4.0, "text": "new", "seg": 7}], [[0.0, 100.0]])
    app.request_model("small")
    assert switch(app) is True
    assert [c["text"] for c in s.cues] == ["new"] and s.cues[0]["id"] == 0
    assert s.seg_next == 8 and s.covered == [[0.0, 100.0]]
    assert s.status == "ready"  # the new model's cache covers everything: nothing to fetch


def test_cache_is_written_and_read_with_the_loaded_model(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    app = make_app(monkeypatch, tmp_path)
    fake_loader(monkeypatch)
    app.request_model("small")
    assert switch(app) is True
    s = server.Session(video_id=VIDEO, url="u", duration=10.0, title="t",
                       cues=[{"id": 0, "start": 0.0, "end": 1.0, "text": "x", "seg": 0}])
    app.save_cache(s)
    assert read_cache(tmp_path)["model"] == "small"
    fresh = server.Session(video_id=VIDEO, url="u")
    app.load_cache(fresh)
    assert [c["text"] for c in fresh.cues] == ["x"]
    with app.lock:
        app.model_name = "large-v3"  # a cache of another model is not reused (the title is)
    other = server.Session(video_id=VIDEO, url="u")
    app.load_cache(other)
    assert other.cues == [] and other.title == "t"


def test_a_cache_written_under_the_repo_id_is_reused_by_the_alias(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    app = make_app(monkeypatch, tmp_path)
    write_cache(tmp_path, "Systran/faster-whisper-large-v3", [{"start": 3.0, "end": 4.0, "text": "same", "seg": 0}], [[0.0, 100.0]])
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert [c["text"] for c in s.cues] == ["same"]


# --------------------------------------------------------------------------- failures

@pytest.fixture
def sessions():
    """Two sessions in mid-flight; a failed switch must not touch them."""
    cached = server.Session(video_id=VIDEO, url="u", status="ready", duration=100.0, covered=[[0.0, 100.0]],
                            speech=[[0.5, 9.0]], cues=[{"id": 0, "start": 1.0, "end": 2.0, "text": "a", "seg": 0}], seg_next=1)
    decoded = server.Session(video_id="bcdefabcdefa", url="u", status="ready", duration=50.0,
                             audio=np.zeros(16000, dtype=np.float32), covered=[[0.0, 20.0]], busy=[20.0, 40.0])
    return cached, decoded


def install_sessions(app, sessions):
    with app.lock:
        app.sessions = {s.video_id: s for s in sessions}
    return [snapshot(s) for s in sessions]


def test_a_failed_download_costs_nothing_but_the_download(monkeypatch, tmp_path, sessions):
    downloads = fake_whisper(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    old_model = app.model
    before = install_sessions(app, sessions)
    app.request_model("large-v3-turb")

    assert tick(app) is False
    assert downloads == ["large-v3-turb"] and calls == []  # no unload, no reload of the previous model
    assert app.model is old_model and app.model_name == "large-v3"
    assert app.model_error == ("large-v3-turb", "unknown model size 'large-v3-turb'; use a size such as large-v3 "
                                                "or a Hugging Face repo id owner/name")
    assert (app.model_loading, app.model_preparing, app.model_prepared) == (None, None, None)
    assert app.wanted_model == "large-v3"  # nothing retries on its own
    assert [snapshot(s) for s in sessions] == before
    resp = app.sync(VIDEO, "u", 0.0, 0, "large-v3-turb")
    assert resp["model_error"].startswith("unknown model size") and resp["model"] == "large-v3"
    assert app.sync(VIDEO, "u", 0.0, 0, "")["model_error"] is None
    assert app.model_state("small")["model_error"] is None  # the complaint names one model only
    assert tick(app) is False and downloads == ["large-v3-turb"]  # the client keeps sending the name: no second attempt


def test_a_repo_without_model_bin_is_refused_before_loading(monkeypatch, tmp_path, sessions):
    downloads = fake_whisper(monkeypatch, tmp_path, bare=("owner/pytorch-only",))
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    old_model = app.model
    before = install_sessions(app, sessions)
    app.request_model("owner/pytorch-only")
    assert tick(app) is False
    assert downloads == ["owner/pytorch-only"] and calls == [] and app.model is old_model
    assert app.model_error == ("owner/pytorch-only", "owner/pytorch-only is not a CTranslate2/faster-whisper model "
                                                     "(no model.bin); convert it with ct2-transformers-converter or pick "
                                                     "a *-ct2 / faster-whisper repo")
    assert app.wanted_model == "large-v3" and [snapshot(s) for s in sessions] == before


def test_a_failed_swap_brings_the_previous_model_back_and_leaves_sessions_alone(monkeypatch, tmp_path, sessions):
    downloads = fake_whisper(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch, fail=("small",))
    app = make_app(monkeypatch, tmp_path)
    before = install_sessions(app, sessions)
    app.request_model("small")

    assert switch(app) is False
    assert downloads == ["small"] and calls == ["small", "large-v3"]  # the previous model came back
    assert app.model is not None and app.model_name == "large-v3"
    assert app.model_error == ("small", "no such model small")
    assert (app.model_loading, app.model_preparing, app.model_prepared, app.wanted_model) == (None, None, None, "large-v3")
    assert [snapshot(s) for s in sessions] == before  # tokens included: the client is not told to start over
    resp = app.sync(VIDEO, "u", 0.0, 0, "small")
    assert resp["model_error"] == "no such model small" and resp["model"] == "large-v3"
    assert app.sync(VIDEO, "u", 0.0, 0, "")["model_error"] is None
    assert tick(app) is False and calls == ["small", "large-v3"]  # within the cooldown: no second attempt


def test_a_failed_name_is_retried_only_after_the_cooldown_and_only_on_request(monkeypatch, tmp_path):
    now = fake_clock(monkeypatch, 1000.0)
    offline = {"small": OSError("HTTPSConnectionPool(host='huggingface.co', port=443): Max retries exceeded")}
    downloads = fake_whisper(monkeypatch, tmp_path, fail=offline)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path, retry_after=7.0)
    app.request_model("small")
    assert tick(app) is False
    assert downloads == ["small"] and app.model_failed_at == 1000.0
    assert app.model_error == ("small", "could not reach Hugging Face to download 'small'")
    assert app.wanted_model == "large-v3"

    now[0] = 1006.0
    app.sync(VIDEO, "u", 0.0, 0, "small")  # what the extension does every second
    assert app.wanted_model == "large-v3"  # not re-armed inside the cooldown
    assert tick(app) is False and downloads == ["small"]

    now[0] = 1008.0
    assert tick(app) is False and downloads == ["small"]  # no request since the cooldown ended: nothing happens
    app.sync(VIDEO, "u", 0.0, 0, "small")
    assert app.wanted_model == "small"  # re-armed: a retry costs one download attempt on the side thread
    assert tick(app) is False and downloads == ["small", "small"] and app.model_failed_at == 1008.0
    assert calls == [] and app.model is not None

    del offline["small"]  # the network is back
    now[0] = 1014.0
    app.sync(VIDEO, "u", 0.0, 0, "small")
    assert app.wanted_model == "large-v3"
    now[0] = 1016.0
    app.sync(VIDEO, "u", 0.0, 0, "small")
    assert switch(app) is True
    assert (app.model_name, app.model_error) == ("small", None) and calls == ["small"]


def test_a_retry_does_not_report_the_failure_it_retries(monkeypatch, tmp_path):
    now = fake_clock(monkeypatch, 1000.0)
    gate = threading.Event()
    gate.set()
    offline = {"small": OSError("HTTPSConnectionPool(host='huggingface.co', port=443): Max retries exceeded")}
    downloads = fake_whisper(monkeypatch, tmp_path, fail=offline, block=gate)
    fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path, retry_after=7.0)
    app.request_model("small")
    assert tick(app) is False
    assert app.model_error == ("small", "could not reach Hugging Face to download 'small'")

    del offline["small"]  # the network is back; the next download takes a while
    gate.clear()
    now[0] = 1008.0
    app.sync(VIDEO, "u", 0.0, 0, "small")
    assert app.wanted_model == "small"
    try:
        assert app.switch_model_if_wanted() is False
        assert wait_for(lambda: downloads == ["small", "small"])
        # The verdict on the first attempt is not reported beside the second one: the overlay
        # would show the download as failed while it is progressing.
        resp = app.sync(VIDEO, "u", 0.0, 0, "small")
        assert (resp["model_loading"], resp["model_error"]) == ("small", None)
        health = app.health()
        assert (health["model_loading"], health["model_error"]) == ("small", None)
    finally:
        gate.set()
    app.prepare_thread.join(5.0)
    assert tick(app) is True and (app.model_name, app.model_error) == ("small", None)


def test_a_retry_after_a_failed_swap_starts_clean_too(monkeypatch, tmp_path):
    now = fake_clock(monkeypatch, 1000.0)
    fake_whisper(monkeypatch, tmp_path)
    failing = ["small"]
    calls = fake_loader(monkeypatch, fail=failing)
    app = make_app(monkeypatch, tmp_path, retry_after=7.0)
    app.request_model("small")
    assert switch(app) is False and app.model_error == ("small", "no such model small")
    failing.clear()  # say, the GPU memory another program held is free now
    now[0] = 1008.0
    app.sync(VIDEO, "u", 0.0, 0, "small")
    assert tick(app) is False and app.model_prepared[0] == "small"  # prepared again
    assert app.model_error is None and app.sync(VIDEO, "u", 0.0, 0, "small")["model_error"] is None
    assert tick(app) is True and app.model_name == "small" and calls == ["small", "large-v3", "small"]


def test_a_later_successful_switch_clears_the_error(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path, fail={"small": RuntimeError("404 Client Error\n\nRepository Not Found for url: x")})
    fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    app.request_model("small")
    assert tick(app) is False
    assert app.model_error == ("small", "'small' was not found on Hugging Face")
    app.request_model("tiny")
    assert switch(app) is True
    assert (app.model_name, app.model_error) == ("tiny", None)
    assert app.sync(VIDEO, "u", 0.0, 0, "small")["model_error"] is None


def test_failure_of_the_previous_model_too_exits_for_a_restart(monkeypatch, tmp_path):
    class Exited(BaseException):
        pass

    codes = []

    def fake_exit(code):
        codes.append(code)
        raise Exited()

    fake_whisper(monkeypatch, tmp_path)
    app = make_app(monkeypatch, tmp_path)
    fake_loader(monkeypatch, fail=("small", "large-v3"))
    monkeypatch.setattr(server.os, "_exit", fake_exit)
    app.request_model("small")
    assert tick(app) is False
    with pytest.raises(Exited):
        app.switch_model_if_wanted()
    assert codes == [3]


def test_transcriber_thread_performs_the_switch(monkeypatch, tmp_path):
    # The fakes are installed before the wish is recorded: the real thread may act on it at once.
    downloads = fake_whisper(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path, inert=False)
    app.request_model("small")
    assert wait_for(lambda: app.model_name == "small")
    assert downloads == ["small"] and calls == ["small"] and app.model is not None


@pytest.mark.parametrize("exc, expected", [
    (ValueError("Invalid model size 'large-v3-turb', expected one of: tiny, tiny.en, base"),
     "unknown model size 'large-v3-turb'; use a size such as large-v3 or a Hugging Face repo id owner/name"),
    (RuntimeError("404 Client Error. (Request ID: Root=1-abc-def)\n\nRepository Not Found for url: "
                  "https://huggingface.co/api/models/owner/nope/revision/main.\nPlease make sure you specified the "
                  "correct `repo_id` and `repo_type`.\nIf you are trying to access a private or gated repo, make sure "
                  "you are authenticated."),
     "'large-v3-turb' was not found on Hugging Face"),
    (OSError("An error happened while trying to locate the files on the Hub and we cannot find the appropriate "
             "snapshot folder for the specified revision on the local disk. Please check your internet connection "
             "and try again."),
     "could not reach Hugging Face to download 'large-v3-turb'"),
    (OSError("HTTPSConnectionPool(host='huggingface.co', port=443): Read timed out. (read timeout=10)"),
     "could not reach Hugging Face to download 'large-v3-turb'"),
    (ValueError("large-v3-turb is not a CTranslate2/faster-whisper model (no model.bin); convert it with "
                "ct2-transformers-converter or pick a *-ct2 / faster-whisper repo"),
     "large-v3-turb is not a CTranslate2/faster-whisper model (no model.bin); convert it with "
     "ct2-transformers-converter or pick a *-ct2 / faster-whisper repo"),
    (RuntimeError("CUDA failed with error out of memory\n\n" + "  detail " * 60),
     ("  detail " * 60).strip()[:200]),  # the last non-empty line, cut short
    (RuntimeError(""), "RuntimeError"),
])
def test_friendly_model_error(exc, expected):
    assert server.friendly_model_error(exc, "large-v3-turb") == expected


# --------------------------------------------------------------------------- cache archive

def test_switching_models_keeps_each_models_cache(monkeypatch, tmp_path):
    fake_whisper(monkeypatch, tmp_path)
    fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", status="ready", duration=100.0, title="Long video",
                       cues=[{"id": 0, "start": 0.0, "end": 1.0, "text": "two hours of large-v3", "seg": 0}],
                       covered=[[0.0, 100.0]])
    with app.lock:
        app.sessions[VIDEO] = s
    app.save_cache(s)

    app.request_model("small")
    assert switch(app) is True
    assert s.cues == [] and s.status == "pending"  # no cache of small yet; the audio is fetched again
    s.cues = [{"id": 0, "start": 0.0, "end": 1.0, "text": "a minute of small", "seg": 0}]
    s.covered = [[0.0, 40.0]]
    app.save_cache(s)
    primary = read_cache(tmp_path)
    assert primary["model"] == "small" and [c["text"] for c in primary["cues"]] == ["a minute of small"]
    archived = read_cache(tmp_path, "large-v3.")
    assert archived["model"] == "large-v3" and [c["text"] for c in archived["cues"]] == ["two hours of large-v3"]
    assert server.find_cached_audio(VIDEO) is None  # the archive is not mistaken for an audio file

    app.request_model("large-v3")
    assert switch(app) is True
    assert [c["text"] for c in s.cues] == ["two hours of large-v3"] and s.covered == [[0.0, 100.0]]
    assert s.title == "Long video" and s.status == "ready"
    app.save_cache(s)  # the primary follows the loaded model; small's cues are the ones moved aside now
    assert read_cache(tmp_path)["model"] == "large-v3"
    assert [c["text"] for c in read_cache(tmp_path, "small.")["cues"]] == ["a minute of small"]
    assert sorted(p.name for p in tmp_path.glob(f"{VIDEO}.*")) == [
        f"{VIDEO}.cues.json", f"{VIDEO}.large-v3.cues.json", f"{VIDEO}.small.cues.json"]


def test_archive_path_slug_and_title_fallback(monkeypatch, tmp_path):
    s = server.Session(video_id=VIDEO, url="u")
    assert s.model_cache_path("kotoba-tech/kotoba-whisper-v2.0-faster").name == f"{VIDEO}.kotoba-tech_kotoba-whisper-v2.0-faster.cues.json"
    app = make_app(monkeypatch, tmp_path)
    write_cache(tmp_path, "small", [{"start": 5.0, "end": 6.0, "text": "s", "seg": 0}], [[0.0, 10.0]], title="")
    write_cache(tmp_path, "large-v3", [{"start": 0.0, "end": 1.0, "text": "l", "seg": 0}], [[0.0, 10.0]],
                title="From the archive", suffix="large-v3.")
    app.load_cache(s)
    assert [c["text"] for c in s.cues] == ["l"] and s.title == "From the archive"
    write_cache(tmp_path, "small", [], [[0.0, 10.0]], title="From the primary")
    other = server.Session(video_id=VIDEO, url="u")
    app.load_cache(other)
    assert [c["text"] for c in other.cues] == ["l"] and other.title == "From the primary"
    (tmp_path / f"{VIDEO}.cues.json").unlink()  # no primary at all: the archive alone is enough
    alone = server.Session(video_id=VIDEO, url="u")
    app.load_cache(alone)
    assert [c["text"] for c in alone.cues] == ["l"] and alone.title == "From the archive"


# --------------------------------------------------------------------------- /health

def make_models_dir(monkeypatch, tmp_path):
    models_dir = tmp_path / "models"
    for name in ("models--Systran--faster-whisper-large-v3", "models--kotoba-tech--kotoba-whisper-v2.0-faster",
                 "models--junk", "models--a--b--c", "models--", "other"):
        (models_dir / name).mkdir(parents=True)
    (models_dir / "models--not--a-dir").write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "MODELS_DIR", models_dir)


def test_health_reports_the_model_state(monkeypatch, tmp_path):
    make_models_dir(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # the real library may be installed
    app = make_app(monkeypatch, tmp_path)
    health = app.health()
    assert health["model"] == "large-v3" and health["default_model"] == "large-v3"
    assert health["model_loading"] is None and health["model_error"] is None
    # Without faster-whisper's alias table the repo ids are reported as they are.
    assert health["models"] == ["Systran/faster-whisper-large-v3", "kotoba-tech/kotoba-whisper-v2.0-faster"]
    assert health["device"] == "cpu" and health["compute_type"] == "int8" and health["language"] == "ja"

    gate = threading.Event()
    fake_whisper(monkeypatch, tmp_path, block=gate)
    fake_loader(monkeypatch, fail=("small",))
    app.request_model("small")
    assert app.switch_model_if_wanted() is False
    assert app.health()["model_loading"] == "small"
    gate.set()
    app.prepare_thread.join(5.0)
    assert tick(app) is False  # the swap fails
    assert app.health()["model_error"] == {"model": "small", "error": "no such model small",
                                           "names": ["small", "Systran/faster-whisper-small",
                                                     "mlx-community/whisper-small-mlx"]}
    assert app.health()["model_loading"] is None


def test_health_names_every_spelling_of_the_failed_model(monkeypatch, tmp_path):
    # The popup compares the field's text with the names /health sends, so a viewer who typed
    # turbo (or the repo id) while offline gets the verdict too, not only one who typed large-v3-turbo.
    fake_whisper(monkeypatch, tmp_path, fail={"large-v3-turbo": ConnectionError("Connection refused")})
    fake_loader(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    app.request_model("turbo")
    assert app.wanted_model == "large-v3-turbo" and tick(app) is False
    assert app.health()["model_error"] == {
        "model": "large-v3-turbo",
        "error": "could not reach Hugging Face to download 'large-v3-turbo'",
        "names": ["large-v3-turbo", "turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                  "mlx-community/whisper-large-v3-turbo"],
    }
    assert app.health()["model_loading"] is None
    # /sync judges the very spelling the request used; /health hands the popup the list instead.
    for spelling in ("turbo", "large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                     "mlx-community/whisper-large-v3-turbo"):
        assert app.model_state(spelling)["model_error"].startswith("could not reach Hugging Face")
    assert app.model_state("large")["model_error"] is None

    # A repo id outside faster-whisper's table has no other spelling.
    app.model_error = ("kotoba-tech/kotoba-whisper-v2.0-faster", "boom")
    assert app.health()["model_error"]["names"] == ["kotoba-tech/kotoba-whisper-v2.0-faster"]


def test_downloaded_models_are_reported_under_their_aliases(monkeypatch, tmp_path):
    make_models_dir(monkeypatch, tmp_path)
    fake_whisper(monkeypatch, tmp_path)
    assert server.downloaded_models() == ["kotoba-tech/kotoba-whisper-v2.0-faster", "large-v3"]
    app = make_app(monkeypatch, tmp_path)
    assert app.health()["models"] == ["kotoba-tech/kotoba-whisper-v2.0-faster", "large-v3"]


def test_health_without_a_models_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "missing")
    app = make_app(monkeypatch, tmp_path)
    assert app.health()["models"] == []


# --------------------------------------------------------------------------- load_model and the files

def test_load_model_passes_the_name_or_the_prepared_directory(monkeypatch):
    names = []

    class WhisperModel:
        def __init__(self, name, **kwargs):
            names.append(name)

        def transcribe(self, *a, **k):
            return iter(()), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    # --device auto is the CPU only when neither GPU backend answers; without both of these the
    # test would ask for CTranslate2 on a CUDA machine and for MLX on an Apple one (see test_mlx.py).
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    args = SimpleNamespace(model="large-v3", device="auto", compute_type="auto", cpu_threads=0, language="ja")
    model, device, compute = server.load_model(args)
    assert isinstance(model, WhisperModel) and (device, compute) == ("cpu", "int8")
    server.load_model(args, "small")
    server.load_model(args, "small", path="/models/snapshots/abc")  # a client name never reaches WhisperModel raw
    assert names == ["large-v3", "small", "/models/snapshots/abc"]


def test_download_model_files_uses_the_models_directory_and_wants_model_bin(monkeypatch, tmp_path):
    seen = {}

    def download_model(name, cache_dir=None, **kwargs):
        seen["args"] = (name, cache_dir)
        directory = tmp_path / "snap"
        directory.mkdir(exist_ok=True)
        return str(directory)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(download_model=download_model))
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    with pytest.raises(ValueError, match="no model.bin"):
        server.download_model_files("owner/name")
    assert seen["args"] == ("owner/name", str(tmp_path / "models"))
    (tmp_path / "snap" / "model.bin").write_bytes(b"\0")
    assert server.download_model_files("owner/name") == str(tmp_path / "snap")


# --------------------------------------------------------------------------- HTTP

@pytest.fixture
def http_sync():
    calls = []

    def sync(video_id, url, t, since, model=None):
        calls.append(model)
        return {"ok": True}

    server.Handler.app = SimpleNamespace(sync=sync)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.app = None


def post_sync(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/sync", body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def test_post_rejects_a_non_string_model(http_sync):
    port, calls = http_sync
    for bad in (5, None, ["small"], {"name": "small"}):
        status, body = post_sync(port, {"video_id": VIDEO, "t": 0, "since": 0, "model": bad})
        assert (status, body) == (400, {"ok": False, "error": "invalid model"})
    assert calls == []


def test_post_passes_the_model_through_and_defaults_to_empty(http_sync):
    port, calls = http_sync
    assert post_sync(port, {"video_id": VIDEO, "t": 0, "since": 0, "model": "small"})[0] == 200
    assert post_sync(port, {"video_id": VIDEO, "t": 0, "since": 0})[0] == 200
    assert calls == ["small", ""]

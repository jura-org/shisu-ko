"""--model-idle-minutes: the model in memory only while something needs it.

On demand (start_app() under --model-idle-minutes above 0) the server starts without a model: the
transcriber loads it when a window waits or a watched video's audio is on its way, and frees it
after the idle minutes without a window (App.tend_model()). Meanwhile every sync is answered from
the cues the server has, cached or in memory.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"


class InertTranscriber:
    """Stands in for the transcriber thread, so the test is the only caller of tend_model() and
    switch_model_if_wanted(); test_the_transcriber_thread_loads_before_the_window_and_frees_after
    runs the real one."""

    def __init__(self, app):
        self.app = app

    def start(self):
        pass


def clock(monkeypatch, start=1000.0):
    """server.time with a time() the test moves, and a sleep() that only counts the test's own rests.

    server.time is the whole process's: the Transcriber threads of earlier tests (conftest hands
    them a StoppedApp, and they go on looping) rest through it too, and must still really rest, or
    they spin and fill `rests` with their 0.15 s naps."""
    now = [start]
    rests = []
    me = threading.current_thread()
    names = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    names["time"] = lambda: now[0]
    names["sleep"] = lambda seconds: rests.append(seconds) if threading.current_thread() is me else time.sleep(seconds)
    monkeypatch.setattr(server, "time", SimpleNamespace(**names))
    return now, rests


def make_app(monkeypatch, tmp_path, on_demand=True, loaded=None, **overrides):
    """An App as start_app() makes one on demand (`loaded` None), or with `loaded` in memory;
    `overrides` go into its arguments."""
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    monkeypatch.setattr(server, "_MODEL_ALIASES", None)
    base = dict(first_window=20.0, window=30.0, lookahead=180.0, model="large-v3", language="ja",
                idle_minutes=30, retry_after=30.0, client_timeout=30.0, model_idle_minutes=5.0)
    base.update(overrides)
    app = server.App(SimpleNamespace(**base), model=loaded, device="cuda",
                     compute_type=None if loaded is None else "float16", on_demand=on_demand)
    app.fetcher = SimpleNamespace(fetch=lambda s: None, start_human_check=lambda s: None)
    return app


def fake_loader(monkeypatch, fail=()):
    """load_model stand-in: records (name, path) and raises for the names in `fail`."""
    calls = []

    def load(args, name=None, path=None, strict=False):
        calls.append((name, path))
        if name in fail:
            raise RuntimeError(f"no such model {name}")
        return SimpleNamespace(name=name), "cuda", "float16"

    monkeypatch.setattr(server, "load_model", load)
    return calls


def fake_downloads(monkeypatch):
    """download_model_files stand-in: records (name, device) and names a folder for it."""
    asked = []

    def download(name, device="cpu"):
        asked.append((name, device))
        return f"/models/{name}"

    monkeypatch.setattr(server, "download_model_files", download)
    return asked


def write_cache(tmp_path, model, texts, duration=100.0):
    cues = [{"id": i, "start": 2.0 * i, "end": 2.0 * i + 1.5, "text": text, "seg": i} for i, text in enumerate(texts)]
    data = {"video_id": VIDEO, "title": "t", "duration": duration, "format": server.CACHE_FORMAT, "model": model,
            "language": "ja", "cues": cues, "covered": [[0.0, duration]], "speech": [], "lyrics": "auto"}
    (tmp_path / f"{VIDEO}.cues.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def tick(app):
    """One switch step, as the transcriber takes it, then the files' download it may have started."""
    switched = app.switch_model_if_wanted()
    if app.prepare_thread is not None:
        app.prepare_thread.join(5.0)
        assert not app.prepare_thread.is_alive()
    return switched


def watched(app, now, **state):
    """A session a client has just synced, with `state` set on it."""
    s = server.Session(video_id=VIDEO, url="u")
    s.last_sync = now
    for key, value in state.items():
        setattr(s, key, value)
    app.sessions[VIDEO] = s
    return s


# --------------------------------------------------------------------------- the option

def test_model_idle_minutes_defaults_to_one_and_refuses_what_is_no_time(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    assert server.parse_args([]).model_idle_minutes == server.MODEL_IDLE_MINUTES == 1.0
    assert server.parse_args(["--model-idle-minutes", "0"]).model_idle_minutes == 0.0
    assert server.parse_args(["--model-idle-minutes", "0.5"]).model_idle_minutes == 0.5
    for bad in ("-1", "nan", "inf", "soon"):
        with pytest.raises(SystemExit):
            server.parse_args(["--model-idle-minutes", bad])


def test_only_a_start_with_idle_minutes_and_without_the_amd_engine_waits_for_a_video(monkeypatch):
    monkeypatch.setattr(server, "ROCM_ACTIVE", False)
    assert server.model_on_demand(SimpleNamespace(model_idle_minutes=1.0))
    assert not server.model_on_demand(SimpleNamespace(model_idle_minutes=0.0))  # loaded at the start and kept
    assert not server.model_on_demand(SimpleNamespace())  # the tools' arguments know nothing of it
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)  # its crash guard counts starts that load a model
    assert not server.model_on_demand(SimpleNamespace(model_idle_minutes=1.0))


def test_an_on_demand_start_loads_nothing_and_says_so_in_health(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROCM_ACTIVE", False)
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    monkeypatch.setattr(server, "resolve_device", lambda device: "cuda")
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model loaded at the start"))
    args = SimpleNamespace(model="large-v3", device="auto", model_idle_minutes=1.0, language="ja", lookahead=None)
    app = server.start_app(args)
    assert app.on_demand and app.model is None and app.device == "cuda" and app.compute_type is None
    assert args.lookahead == server.CUDA_LOOKAHEAD  # the backend it will run on sets the lookahead already
    health = app.health()
    assert (health["model"], health["model_loaded"], health["compute_type"], health["engine"]) == \
        ("large-v3", False, None, "whisper")
    import inspect
    assert inspect.getsource(server.start_app).count("on_demand=True") == 1  # the server's own App, and only it


def test_health_names_the_engine_of_a_model_that_is_not_loaded(monkeypatch, tmp_path):
    assert make_app(monkeypatch, tmp_path, model="kitsune-0.6b").health()["engine"] == "kitsune"
    assert make_app(monkeypatch, tmp_path).health()["engine"] == "whisper"
    loaded = make_app(monkeypatch, tmp_path, on_demand=False, loaded=SimpleNamespace(family="ctc"))
    assert loaded.health()["engine"] == "kitsune" and loaded.health()["model_loaded"] is True


def test_a_stored_video_is_answered_without_a_model(monkeypatch, tmp_path):
    clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model loaded for a stored video"))
    write_cache(tmp_path, "large-v3", ["こんにちは", "さようなら"])
    answer = app.sync(VIDEO, "u", 1.0, 0)
    assert [c["text"] for c in answer["cues"]] == ["こんにちは", "さようなら"] and answer["status"] == "ready"
    assert app.pick_work() is None and app.tend_model(False) is False and app.model is None


# --------------------------------------------------------------------------- loading and freeing

def test_every_other_app_keeps_the_model_it_was_given(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model loaded"))
    bare = make_app(monkeypatch, tmp_path, on_demand=False)  # the tests' own Apps run without one
    assert bare.tend_model(True) is False and bare.tend_model(False) is False and bare.model is None
    model = object()
    kept = make_app(monkeypatch, tmp_path, on_demand=False, loaded=model)
    now[0] += 3600
    assert kept.tend_model(False) is False and kept.model is model


def test_a_window_loads_the_model_and_nothing_else_does(monkeypatch, tmp_path):
    clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    assert app.tend_model(False) is False and calls == []  # nothing needs it
    assert app.tend_model(True) is True  # a window: loaded, and the loop plans again
    assert calls == [("large-v3", None)]  # the operator's model, by its name as at a start
    assert app.model is not None and app.compute_type == "float16" and app.model_loading is None
    assert app.tend_model(True) is False and len(calls) == 1  # loaded: the window goes ahead
    assert app.health()["model_loaded"] is True


def test_the_model_being_loaded_is_named_meanwhile(monkeypatch, tmp_path):
    # What the badge on the video and the popup say during the load: "Loading model large-v3…".
    clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    seen = []

    def load(args, name=None, path=None, strict=False):
        seen.append((app.model_loading, app.health()["model_loading"], app.model_state("")["model_loading"]))
        return SimpleNamespace(), "cuda", "float16"

    monkeypatch.setattr(server, "load_model", load)
    app.tend_model(True)
    assert seen == [("large-v3", "large-v3", "large-v3")] and app.model_loading is None


def test_the_model_goes_after_the_idle_minutes_without_a_window(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    fake_loader(monkeypatch)
    released = []
    monkeypatch.setattr(server, "release_torch_memory", lambda: released.append("torch"))
    monkeypatch.setattr(server, "release_mlx_memory", lambda: released.append("mlx"))
    app.tend_model(True)
    now[0] += 299
    assert app.tend_model(False) is False and app.model is not None
    assert app.tend_model(True) is False  # a window: the five minutes start again
    now[0] += 299
    assert app.tend_model(False) is False and app.model is not None
    now[0] += 2
    assert app.tend_model(False) is True and app.model is None
    assert released == ["torch", "mlx"]
    health = app.health()
    assert health["model_loaded"] is False and health["model"] == "large-v3" and health["model_loading"] is None


def test_the_model_outlasts_two_windows_whatever_the_idle_minutes(monkeypatch, tmp_path):
    # A video playing gets a window per --window of playback; a long one must not be freed between two.
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path, model_idle_minutes=0.5, window=60.0)
    fake_loader(monkeypatch)
    app.tend_model(True)
    now[0] += 119
    assert app.tend_model(False) is False and app.model is not None
    now[0] += 2
    assert app.tend_model(False) is True and app.model is None


def test_audio_on_its_way_loads_the_model_beside_the_download_and_keeps_it(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    s = watched(app, now[0] - 31, fetching=True)  # a tab closed since
    assert app.tend_model(False) is False and calls == []
    s.last_sync = now[0]
    assert app.tend_model(False) is True and len(calls) == 1  # the window is on its way: load meanwhile
    now[0] += 600
    s.last_sync = now[0]
    assert app.tend_model(False) is False and app.model is not None  # still downloading: kept
    # The fetch ends on a video its cache covers to the end: nothing left for the model.
    s.fetching, s.status, s.duration, s.covered = False, "ready", 60.0, [[0.0, 60.0]]
    assert app.tend_model(False) is True and app.model is None


def test_a_watched_video_with_audio_left_keeps_the_model_through_a_pause(monkeypatch, tmp_path):
    # The owner's log: the windows stopped while the viewer paused to mine, the idle minute passed,
    # the model went, and it came back 53 s later for the next window of the same video.
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path, model_idle_minutes=1.0)
    calls = fake_loader(monkeypatch)
    app.tend_model(True)  # loaded for a window
    # Cues to 1:20:13, the playhead at 1:17:10: 183 s ahead, past the lookahead, so no window is due.
    s = watched(app, now[0], status="ready", duration=5400.0, covered=[[0.0, 4813.0]], want_t=4630.0)
    for _ in range(10):  # ten minutes paused, the tab's heartbeat going on
        now[0] += 60
        s.last_sync = now[0]
        assert app.tend_model(False) is False and app.model is not None
    assert len(calls) == 1


def test_the_model_goes_a_minute_after_the_video_is_transcribed_to_its_end(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path, model_idle_minutes=1.0)
    fake_loader(monkeypatch)
    app.tend_model(True)
    s = watched(app, now[0], status="ready", duration=5400.0, covered=[[0.0, 5400.0]], want_t=5300.0)
    now[0] += 59
    s.last_sync = now[0]
    assert app.tend_model(False) is False  # the idle minute is not over yet
    now[0] += 2
    s.last_sync = now[0]
    assert app.tend_model(False) is True and app.model is None


def test_a_video_started_in_the_middle_is_finished_once_transcribed_from_there_to_its_end(monkeypatch, tmp_path):
    # The planner never goes back from where the viewer started: what lies before stays as it is.
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path, model_idle_minutes=1.0)
    fake_loader(monkeypatch)
    app.tend_model(True)
    watched(app, now[0] + 61, status="ready", duration=5400.0, covered=[[1800.0, 5400.0]], want_t=5000.0)
    now[0] += 61
    assert app.tend_model(False) is True and app.model is None


def test_the_model_goes_once_the_tab_stops_syncing(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path, model_idle_minutes=1.0)  # --client-timeout 30
    fake_loader(monkeypatch)
    app.tend_model(True)
    s = watched(app, now[0], status="ready", duration=5400.0, covered=[[0.0, 4813.0]], want_t=4630.0)
    now[0] += 61
    s.last_sync = now[0] - 29
    assert app.tend_model(False) is False  # a sync 29 s ago: still watched
    s.last_sync = now[0] - 31
    assert app.tend_model(False) is True and app.model is None


def test_what_holds_the_model_and_what_does_not(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    for state, held in (({"live": True}, True),  # a live stream is never finished
                        ({"duration": 0.0}, True),  # its length not known yet
                        ({"language_paused": True}, False),  # no window comes while its language pauses it
                        ({"status": "error"}, False)):  # nor while its fetch has failed
        app = make_app(monkeypatch, tmp_path, model_idle_minutes=1.0)
        fake_loader(monkeypatch)
        app.tend_model(True)
        base = dict(status="ready", duration=5400.0, covered=[[0.0, 4813.0]], want_t=4630.0)
        watched(app, now[0] + 61, **{**base, **state})
        now[0] += 61
        assert (app.tend_model(False) is False) is held, state
        assert (app.model is not None) is held, state


def test_with_client_timeout_0_a_video_is_watched_for_the_idle_minutes(monkeypatch, tmp_path):
    # --client-timeout 0 transcribes ahead for a video however long ago it synced; for the model's
    # sake a video is left after --idle-minutes, as Fetcher.watched() has it.
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path, model_idle_minutes=1.0, client_timeout=0.0, idle_minutes=30)
    fake_loader(monkeypatch)
    app.tend_model(True)
    s = watched(app, now[0], status="ready", duration=5400.0, covered=[[0.0, 4813.0]], want_t=4630.0)
    now[0] += 29 * 60
    assert app.tend_model(False) is False
    now[0] += 2 * 60
    assert app.tend_model(False) is True and app.model is None
    assert s.last_sync < now[0] - 30 * 60


def test_no_audio_is_on_its_way_from_a_paused_finished_or_failing_fetch(monkeypatch, tmp_path):
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    s = watched(app, now[0], fetching=True, language_paused=True)
    assert app.tend_model(False) is False
    s.language_paused, s.duration, s.covered = False, 60.0, [[0.0, 60.0]]
    assert app.tend_model(False) is False
    # A fetch that failed and waits for its next attempt keeps `fetching` set for as long as the tab
    # stays (Fetcher.fetch()): no window is coming from it.
    s.covered, s.status, s.error = [], "error", "HTTP Error 429"
    assert app.tend_model(False) is False and calls == []
    s.status = "downloading"  # the next attempt is under way
    assert app.tend_model(False) is True and len(calls) == 1


def test_a_cached_video_fetching_its_audio_loads_the_model_only_for_a_window_it_will_want(monkeypatch, tmp_path):
    # A video watched partway: its record covers the first half hour, and a session that is not
    # covered to its end fetches the audio. That fetch wants the model only where the planner will
    # want a window: at a gap under the playhead, or where the cues end within the lookahead (180 s).
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    s = watched(app, now[0], fetching=True, status="downloading", duration=7200.0, covered=[[1.7, 1800.0]])
    for t in (10.0, 1500.0):  # inside the cues, the end of them more than 180 s away
        s.want_t = t
        assert app.tend_model(False) is False, t
    s.covered = [[0.4, 1800.0]]
    s.want_t = 0.0  # 0.4 s before the first cue: plan_window() covers that without the model
    assert app.tend_model(False) is False and calls == []
    s.want_t = 1650.0  # the cues end 150 s ahead: a window comes
    assert app.tend_model(False) is True and len(calls) == 1


def test_a_cached_video_with_a_gap_at_the_playhead_loads_the_model(monkeypatch, tmp_path):
    # What 37 of the owner's records look like: the first second or two was never transcribed,
    # since the planner never goes back from where the viewer started.
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    watched(app, now[0], fetching=True, status="downloading", duration=102.9, covered=[[1.7, 102.9]])
    assert app.tend_model(False) is True and len(calls) == 1


def test_a_model_that_does_not_load_is_reported_and_tried_again_after_retry_after(monkeypatch, tmp_path):
    now, rests = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch, fail={"large-v3"})
    assert app.tend_model(True) is True
    assert app.model is None and app.model_loading is None
    assert app.model_error[0] == "large-v3" and "no such model" in app.model_error[1]
    assert app.model_state("")["model_error"]  # a client on the default sees why nothing comes
    assert app.tend_model(True) is True and len(calls) == 1 and rests == [0.5]  # a rest, no second try
    assert app.tend_model(False) is False and len(calls) == 1
    now[0] += 31
    assert app.tend_model(True) is True and len(calls) == 2


# --------------------------------------------------------------------------- switching

def test_a_switch_on_demand_loads_the_new_model_and_reads_its_cues(monkeypatch, tmp_path):
    clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch)
    downloads = fake_downloads(monkeypatch)
    write_cache(tmp_path, "small", ["小さい"])
    s = app.get_session(VIDEO, "u")
    assert s.cues == []  # large-v3 has no record of the video
    app.request_model("small")
    assert tick(app) is False  # the files first, on their own thread
    assert tick(app) is True  # then the swap, which needs no model in memory to begin with
    assert downloads == [("small", "cuda")] and calls == [("small", "/models/small")]
    assert (app.model_name, app.model_path, app.model_loading) == ("small", "/models/small", None)
    assert [c["text"] for c in s.cues] == ["小さい"]
    app.free_model()
    app.tend_model(True)
    assert calls[-1] == ("small", "/models/small")  # loaded again from the folder it came from


def test_a_failed_switch_on_demand_changes_nothing_and_brings_nothing_back(monkeypatch, tmp_path):
    clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch, fail={"small"})
    fake_downloads(monkeypatch)
    write_cache(tmp_path, "large-v3", ["大きい"])
    s = app.get_session(VIDEO, "u")
    token = s.token
    app.tend_model(True)  # large-v3 in memory
    app.request_model("small")
    tick(app)
    assert tick(app) is False  # the swap, which fails
    assert calls == [("large-v3", None), ("small", "/models/small")]  # and no reload of large-v3 now
    assert (app.model, app.model_name, app.wanted_model, app.model_loading) == (None, "large-v3", "large-v3", None)
    assert app.model_error[0] == "small" and app.model_state("small")["model_error"]
    assert s.token == token and [c["text"] for c in s.cues] == ["大きい"]  # the sessions never noticed
    app.tend_model(True)
    assert calls[-1] == ("large-v3", None) and app.model is not None  # back when a window needs it


def test_two_models_that_do_not_load_wait_their_turn(monkeypatch, tmp_path):
    # The default fails on demand, the client's name fails at its swap: each failure overwrites
    # model_error, but neither name may be tried again before --retry-after.
    now, _ = clock(monkeypatch)
    app = make_app(monkeypatch, tmp_path)
    calls = fake_loader(monkeypatch, fail={"large-v3", "small"})
    fake_downloads(monkeypatch)
    for _ in range(20):  # 20 s of the client asking for small once a second while a window waits
        app.request_model("small")
        tick(app)
        app.tend_model(True)
        now[0] += 1.0
    assert calls.count(("large-v3", None)) == 1 and calls.count(("small", "/models/small")) == 1
    now[0] += 11
    app.tend_model(True)
    assert calls.count(("large-v3", None)) == 2


# --------------------------------------------------------------------------- the rest of the server

def test_the_sweep_waits_for_a_freed_model_and_never_loads_one(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("the sweep loaded a model"))
    (tmp_path / f"{VIDEO}.cues.json").write_text(json.dumps({"unheard_rule": server.UNHEARD_RULE}), encoding="utf-8")
    steps = app.old_record_sweep()
    assert next(steps) is False and next(steps) is False  # a rest per step while there is no model
    app.model = object()
    assert list(steps) == []  # the marked record is passed over, and the sweep is done
    # An App that is not on demand never waits, model or none (the tests' own Apps run without one).
    assert list(make_app(monkeypatch, tmp_path, on_demand=False).old_record_sweep()) == []


def test_the_sweep_waits_between_two_ranges_of_a_record_when_the_model_goes(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    record = tmp_path / f"{VIDEO}.cues.json"
    record.write_text(json.dumps({"video_id": VIDEO, "model": "large-v3", "language": "ja", "lyrics": "auto",
                                  "language_state": {"paused": False}, "covered": [[0.0, 30.0]], "speech": [],
                                  "cues": []}), encoding="utf-8")
    monkeypatch.setattr(server, "unheard_ranges", lambda covered, speech, cues: [(0.0, 10.0, []), (20.0, 30.0, [])])
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: tmp_path / f"{VIDEO}.webm")
    monkeypatch.setattr(server, "_decode_range", lambda src, lo, hi, rate: server.np.zeros(int((hi - lo) * rate), dtype="int16"))
    judged = []
    app.transcriber.choose_path = lambda *a: judged.append(a[3:]) or (False, [])
    app.model = object()
    steps = app.old_record_sweep()
    assert next(steps) is None and judged == [(0.0, 10.0)]  # the first range
    app.model = None  # freed between the two (tend_model() runs between the sweep's steps)
    assert next(steps) is False and next(steps) is False and judged == [(0.0, 10.0)]
    app.model = object()
    assert list(steps) == [None, None]  # the second range, then the record's verdict
    assert judged == [(0.0, 10.0), (20.0, 30.0)]
    assert json.loads(record.read_text(encoding="utf-8"))["unheard_rule"] == server.UNHEARD_RULE


def test_release_mlx_memory_empties_mlx_s_cache_in_either_place(monkeypatch):
    cleared = []
    metal = SimpleNamespace(clear_cache=lambda: cleared.append("metal"))
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace(clear_cache=lambda: cleared.append("mx"), metal=metal))
    server.release_mlx_memory()
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace(metal=metal))  # an MLX from before mx.clear_cache()
    server.release_mlx_memory()
    monkeypatch.delitem(sys.modules, "mlx.core")
    server.release_mlx_memory()  # no MLX in the process: nothing to do
    assert cleared == ["mx", "metal"]


def test_the_transcriber_thread_loads_before_the_window_and_frees_after(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    loads = fake_loader(monkeypatch)
    windows = []
    monkeypatch.setattr(server.Transcriber, "process",
                        lambda self, s, start, end: windows.append((self.app.model is not None, s.video_id, start)))
    args = SimpleNamespace(first_window=0.05, window=0.05, lookahead=180.0, model="large-v3", language="ja",
                           idle_minutes=30, retry_after=30.0, client_timeout=30.0, model_idle_minutes=0.002)
    app = server.App(args, None, "cuda", None, on_demand=True)
    s = server.Session(video_id=VIDEO, url="u")
    # The planner offers the window until it has been transcribed, as plan_window() does: the round
    # that loads the model plans again rather than run what it planned before the load.
    monkeypatch.setattr(app, "pick_work", lambda: None if windows else (s, 0.0, 20.0))
    deadline = time.time() + 5.0
    while time.time() < deadline and not (windows and app.model is None):
        time.sleep(0.01)
    assert windows == [(True, VIDEO, 0.0)]  # loaded first, then the window
    assert app.model is None and len(loads) == 1  # and freed once the idle time (0.12 s here) was up

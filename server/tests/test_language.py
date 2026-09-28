"""Focus on one language: vote, pause after prolonged foreign speech, resume when it returns.

Nothing here loads a model; the fake below scripts what detect_language and transcribe return.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE


def session(**kwargs):
    # declared_language=None on purpose: these are the rules for a video YouTube names no language
    # for, where the audio is the only evidence there is. Transcriber.process skips the detector
    # outright once YouTube's word agrees with --language (test_declared_language.py), so every
    # rule below would silently stop being exercised if these sessions carried a declaration.
    base = dict(video_id=VIDEO, url="u", declared_language=None)
    base.update(kwargs)
    return server.Session(**base)


# --------------------------------------------------------------------------- language_vote

def test_patience_zero_never_changes_anything():
    s = session()
    assert server.language_vote(s, "en", 30.0, "ja", 0.0) is True
    assert (s.foreign_seconds, s.heard, s.language_paused) == (0.0, None, False)


def test_foreign_speech_below_the_patience_is_still_transcribed():
    s = session()
    assert server.language_vote(s, "en", 19.0, "ja", 60.0) is True
    assert server.language_vote(s, "en", 19.0, "ja", 60.0) is True
    assert s.foreign_seconds == 38.0
    assert s.heard == "en"
    assert s.language_paused is False


def test_foreign_speech_reaching_the_patience_pauses_the_video():
    s = session(foreign_seconds=50.0, heard="en")
    assert server.language_vote(s, "en", 19.0, "ja", 60.0) is False
    assert s.language_paused is True
    assert s.heard == "en"


def test_a_no_vote_keeps_the_state_as_it_is():
    quiet = session(foreign_seconds=30.0, heard="en")
    assert server.language_vote(quiet, None, 1.0, "ja", 60.0) is True
    assert (quiet.foreign_seconds, quiet.heard, quiet.language_paused) == (30.0, "en", False)

    paused = session(foreign_seconds=80.0, heard="en", language_paused=True)
    assert server.language_vote(paused, None, 1.0, "ja", 60.0) is False
    assert paused.language_paused is True


def test_hearing_the_target_language_resets_and_resumes():
    s = session(foreign_seconds=80.0, heard="en", language_paused=True)
    assert server.language_vote(s, "ja", 19.0, "ja", 60.0) is True
    assert (s.foreign_seconds, s.heard, s.language_paused) == (0.0, None, False)


# --------------------------------------------------------------------------- speech_samples

def test_speech_samples_keeps_only_the_speech_and_caps_the_length():
    audio = np.arange(40 * RATE, dtype=np.float32)
    out = server.speech_samples(audio, [[105.0, 106.0], [110.0, 112.0]], start=100.0)
    assert len(out) == 3 * RATE
    assert out[0] == audio[5 * RATE]

    capped = server.speech_samples(audio, [[100.0, 140.0]], start=100.0, limit=10.0)
    assert len(capped) == 10 * RATE


# --------------------------------------------------------------------------- Transcriber.process

class FakeModel:
    """Scripted language votes; transcribe() returns one clean segment so real cues come out."""

    def __init__(self, votes):
        self.votes = list(votes)
        self.detections = 0
        self.transcribes = 0

    def detect_language(self, audio=None, **kwargs):
        self.detections += 1
        language, probability = self.votes.pop(0) if self.votes else ("ja", 0.99)
        return language, probability, []

    def transcribe(self, audio, **kwargs):
        self.transcribes += 1
        words = [server.Word("これは", 1.0, 2.0, 0.9), server.Word("テストです", 2.0, 3.0, 0.9)]
        seg = SimpleNamespace(text="これはテストです", start=1.0, end=3.0, words=words, compression_ratio=1.0)
        return iter([seg]), None


def make_worker(monkeypatch, tmp_path, votes, **args_overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    # One speech interval per window, from half a second in to half a second before its end.
    monkeypatch.setattr(server, "detect_speech",
                        lambda audio, offset=0.0: [[offset + 0.5, offset + len(audio) / RATE - 0.5]])
    base = dict(first_window=20.0, window=20.0, lookahead=900.0, model="large-v3", language="ja",
                language_patience=30.0, beam_size=1, initial_prompt="", idle_minutes=30, client_timeout=30.0)
    base.update(args_overrides)
    model = FakeModel(votes)
    app = server.App(SimpleNamespace(**base), model=model, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return server.Transcriber(app), model


def ready_session():
    return session(status="ready", audio=np.zeros(600 * RATE, dtype=np.float32), duration=600.0)


def test_foreign_windows_are_transcribed_until_the_patience_runs_out(monkeypatch, tmp_path):
    # 19 s of speech per 20 s window, patience 30: the first window still gets subtitles.
    worker, model = make_worker(monkeypatch, tmp_path, [("en", 0.95)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    assert model.transcribes == 1
    assert s.cues and s.language_paused is False
    assert s.heard == "en" and s.foreign_seconds == 19.0


def test_prolonged_foreign_speech_pauses_the_video_and_the_target_language_resumes_it(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, [("en", 0.95), ("en", 0.95), ("en", 0.95), ("ja", 0.97)])
    s = ready_session()

    worker.process(s, 0.0, 20.0)   # 19 s of English: below the patience, transcribed
    worker.process(s, 20.0, 40.0)  # 38 s: paused, skipped
    assert model.transcribes == 1
    assert s.language_paused is True
    # A window that was only listened to is never claimed as transcribed.
    assert s.covered == [[0.0, 20.0]]
    assert s.probed == [[20.0, 40.0]]  # ... but the planner still moves past it
    assert all(cue["end"] <= 20.0 for cue in s.cues)

    worker.process(s, 40.0, 60.0)  # still English: probed only
    assert model.transcribes == 1 and model.detections == 3
    assert s.busy is None
    assert s.probed == [[20.0, 60.0]] and s.covered == [[0.0, 20.0]]

    worker.process(s, 60.0, 80.0)  # Japanese again
    assert model.transcribes == 2
    assert s.language_paused is False and s.heard is None and s.foreign_seconds == 0.0
    assert s.probed == []  # the audio nobody transcribed is offered to the planner again


def test_a_paused_video_keeps_its_state_in_the_cue_cache(monkeypatch, tmp_path):
    worker, _model = make_worker(monkeypatch, tmp_path, [("en", 0.95), ("en", 0.95)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    worker.process(s, 20.0, 40.0)

    reloaded = session()
    worker.app.load_cache(reloaded)
    assert reloaded.language_paused is True
    assert reloaded.heard == "en"
    assert reloaded.foreign_seconds == 30.0  # counted no further than the patience


def test_a_window_with_too_little_speech_casts_no_vote(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, [("en", 0.95)])
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: [[offset + 1.0, offset + 2.0]])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    assert model.detections == 0
    assert model.transcribes == 1
    assert s.heard is None and s.foreign_seconds == 0.0


def test_an_unsure_detection_casts_no_vote(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, [("en", 0.3)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1 and model.transcribes == 1
    assert s.heard is None and s.foreign_seconds == 0.0


def test_patience_zero_never_runs_the_detector(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, [("en", 0.95)], language_patience=0.0)
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    assert model.detections == 0
    assert model.transcribes == 1


def test_a_failing_detector_transcribes_the_window(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, [])
    monkeypatch.setattr(model, "detect_language", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no encoder")))
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    assert model.transcribes == 1
    assert s.language_paused is False


# --------------------------------------------------------------------------- /sync

def test_sync_reports_the_language_watch(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3",
                           language="ja", language_patience=60.0, idle_minutes=30)
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)

    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert resp["heard"] is None and resp["language_paused"] is False

    s = app.sessions[VIDEO]
    s.heard, s.language_paused = "en", True
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert resp["heard"] == "en" and resp["language_paused"] is True
    assert app.sessions_summary()["sessions"][0]["language_paused"] is True


def test_a_paused_video_is_only_probed_a_short_way_ahead_of_the_playhead():
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0)
    s = server.Session(video_id="abcdefabcdef", url="u", status="ready", audio=object(), duration=1200.0,
                       want_t=10.0, covered=[[0.0, 200.0]])
    assert server.plan_window(s, args) == (200.0, 240.0)
    s.language_paused = True
    assert server.plan_window(s, args) is None            # 190 s ahead is beyond the probe distance
    s.covered = [[0.0, 80.0]]
    assert server.plan_window(s, args) == (80.0, 120.0)   # 70 s ahead is still probed
    args.lookahead = 0.0                                  # "whole video" still does not apply while paused
    s.covered = [[0.0, 200.0]]
    assert server.plan_window(s, args) is None


# --------------------------------------------------------------------------- what a wrong pause costs

def test_a_probed_video_never_looks_fully_transcribed(monkeypatch, tmp_path):
    """The severe case: a video paused by mistake and watched to the end must not cache as done."""
    worker, _model = make_worker(monkeypatch, tmp_path, [("en", 0.95)] * 40)
    s = session(status="ready", audio=np.zeros(60 * RATE, dtype=np.float32), duration=60.0)
    for start in (0.0, 20.0, 40.0):
        worker.process(s, start, start + 20.0)
    assert s.language_paused is True
    assert s.fully_covered() is False  # covered stops where transcription stopped

    reloaded = session()
    worker.app.load_cache(reloaded)
    assert reloaded.probed == []            # probes are never written down
    assert reloaded.fully_covered() is False
    reloaded.status, reloaded.audio, reloaded.duration = "ready", object(), 60.0
    assert server.plan_window(reloaded, worker.app.args) is not None  # there is still work to do


def test_detection_turned_off_ignores_a_pause_in_the_cache(monkeypatch, tmp_path):
    """--language-patience 0 is what the README offers after a wrong pause: it must really clear it."""
    worker, _model = make_worker(monkeypatch, tmp_path, [("en", 0.95), ("en", 0.95)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    worker.process(s, 20.0, 40.0)
    assert s.language_paused is True

    off, _model2 = make_worker(monkeypatch, tmp_path, [], language_patience=0.0)
    reloaded = session()
    off.app.load_cache(reloaded)
    assert (reloaded.language_paused, reloaded.heard, reloaded.foreign_seconds) == (False, None, 0.0)
    reloaded.status, reloaded.audio, reloaded.duration = "ready", object(), 600.0
    reloaded.want_t = 0.0
    assert server.lookahead_for(reloaded, off.app.args) == off.app.args.lookahead


def test_a_language_change_discards_the_pause(monkeypatch, tmp_path):
    worker, _model = make_worker(monkeypatch, tmp_path, [("en", 0.95), ("en", 0.95)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    worker.process(s, 20.0, 40.0)
    assert s.language_paused is True

    other, _m = make_worker(monkeypatch, tmp_path, [], language="en")
    reloaded = session()
    other.app.load_cache(reloaded)
    assert reloaded.language_paused is False and reloaded.cues == []


def test_a_corrupt_language_state_does_not_break_the_video(monkeypatch, tmp_path):
    import json
    worker, _model = make_worker(monkeypatch, tmp_path, [("ja", 0.99)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    path = s.cache_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["language_state"] = "en"  # anything but the dict that was written
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = session()
    worker.app.load_cache(reloaded)  # must not raise: get_session registers the session after this
    assert reloaded.language_paused is False and reloaded.cues


def test_a_failing_detector_lets_a_paused_video_speak_again(monkeypatch, tmp_path):
    """A detector that cannot judge must not be the thing that keeps a video silent."""
    worker, model = make_worker(monkeypatch, tmp_path, [("en", 0.95), ("en", 0.95)])
    s = ready_session()
    worker.process(s, 0.0, 20.0)
    worker.process(s, 20.0, 40.0)
    assert s.language_paused is True and model.transcribes == 1

    monkeypatch.setattr(model, "detect_language", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no encoder")))
    worker.process(s, 40.0, 60.0)
    assert model.transcribes == 2  # transcribed, not skipped
    assert s.covered == [[0.0, 20.0], [40.0, 60.0]]


def test_the_patience_counts_speech_the_detector_never_saw(monkeypatch, tmp_path):
    """Only the samples are capped at 30 s; a long window still counts all the speech in it."""
    worker, _model = make_worker(monkeypatch, tmp_path, [("en", 0.95)], window=40.0)
    s = ready_session()
    worker.process(s, 0.0, 40.0)  # 39 s of speech, of which the detector hears 30
    assert s.foreign_seconds == 30.0  # capped at the patience, but from 39 s of speech
    assert server.speech_seconds([[0.5, 39.5]], 0.0, 40.0) == 39.0


def test_a_stream_that_comes_back_as_a_video_forgets_the_pause():
    s = session(live=True, language_paused=True, heard="en", foreign_seconds=60.0,
                probed=[[0.0, 40.0]], covered=[[0.0, 40.0]], cues=[{"id": 0}])
    # What Fetcher.fetch does when yt-dlp no longer reports a live stream.
    s.live = False
    s.cues, s.covered, s.speech, s.seg_next = [], [], [], 0
    s.probed, s.foreign_seconds, s.heard, s.language_paused = [], 0.0, None, False
    assert (s.probed, s.foreign_seconds, s.heard, s.language_paused) == ([], 0.0, None, False)

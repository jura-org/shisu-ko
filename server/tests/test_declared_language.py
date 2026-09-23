"""YouTube's own word about a video's language, which is free, against the audio detector, which is not.

The metadata call already runs before every download, so `language` in it costs nothing; the audio
detector costs an encoder pass a window, about a second, for as long as the video plays. So a video
YouTube names as another language is refused before a byte is fetched, and a video it names as the
target language never reaches the detector at all.

Nothing here loads a model or touches the network: yt_dlp is a fake in sys.modules and the model is
scripted, the same way test_language.py and test_stale_partial.py do it.
"""
from __future__ import annotations

import json
import sys
import time
import types
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE


# --------------------------------------------------------------------------- declared_language

def test_declared_language_is_the_primary_subtag_in_lower_case():
    # Whisper knows "en", not "en-US", and uploaders write the tag either way; "_" turns up in
    # yt-dlp's own format ids for the same field.
    assert server.declared_language({"language": "ja"}) == "ja"
    assert server.declared_language({"language": "en-US"}) == "en"
    assert server.declared_language({"language": "en_US"}) == "en"
    assert server.declared_language({"language": "JA"}) == "ja"
    assert server.declared_language({"language": "EN-GB"}) == "en"
    assert server.declared_language({"language": "  ja  "}) == "ja"


def test_a_video_that_declares_nothing_usable_declares_nothing():
    """None, never "": the answer is compared with --language, and an empty string is not a claim.

    Everything a video's metadata may hold instead of a code has to read as "YouTube said nothing",
    because the alternative is refusing a video the viewer asked for on a missing dictionary key.
    """
    assert server.declared_language({}) is None
    assert server.declared_language({"language": None}) is None
    assert server.declared_language({"language": ""}) is None
    assert server.declared_language({"language": "   "}) is None
    assert server.declared_language({"language": 5}) is None
    assert server.declared_language({"language": ["ja"]}) is None
    assert server.declared_language({"language": "-ja"}) is None  # a tag with no primary subtag
    assert server.declared_language(object()) is None             # nothing to ask at all
    assert server.declared_language(None) is None


# --------------------------------------------------------------------------- the refusal

class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL and writes down every call, so a download cannot go unnoticed."""

    def __init__(self, opts, info, calls):
        self.opts = opts
        self.info = info
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.calls.append("extract_info")
        return dict(self.info)

    def process_ie_result(self, info, download=True):
        self.calls.append("process_ie_result")
        self._write()

    def download(self, urls):
        self.calls.append("download")
        self._write()

    def _write(self):
        (server.CACHE_DIR / f"{VIDEO}.webm").write_bytes(b"audio")


def make_fetcher(monkeypatch, tmp_path, info, language="ja", language_patience=60.0):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    calls: list = []
    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = lambda opts: FakeYoutubeDL(opts, info, calls)
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)
    args = SimpleNamespace(cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="auto",
                           first_window=20.0, window=40.0, language=language,
                           language_patience=language_patience)
    return server.Fetcher(args), calls


def test_a_foreign_video_is_refused_before_a_byte_is_downloaded(monkeypatch, tmp_path):
    """The whole point: the metadata call is the only cost, and nothing lands on disk."""
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "A talk", "duration": 900.0, "abr": 128.0, "language": "en"})
    s = server.Session(video_id=VIDEO, url="u")
    with pytest.raises(server.ForeignLanguage) as caught:
        fetcher.download_once(s, resume=True)
    assert caught.value.language == "en"
    assert calls == ["extract_info"]         # no process_ie_result, no download
    assert list(tmp_path.iterdir()) == []    # not even a .part file
    # The metadata the refusal rests on is still recorded, so /sync and the log can name the video.
    assert (s.declared_language, s.title, s.duration_hint) == ("en", "A talk", 900.0)


def test_the_resume_wrapper_passes_the_refusal_on_instead_of_retrying_it(monkeypatch, tmp_path):
    """download() retries a 416 from scratch; a refusal is not an error to retry."""
    fetcher, calls = make_fetcher(monkeypatch, tmp_path, {"duration": 60.0, "language": "en-GB"})
    s = server.Session(video_id=VIDEO, url="u")
    with pytest.raises(server.ForeignLanguage) as caught:
        fetcher.download(s)
    assert caught.value.language == "en"
    assert calls == ["extract_info"]  # asked once, not once more without resuming


def test_a_video_youtube_calls_the_target_language_is_downloaded_and_its_word_kept(monkeypatch, tmp_path):
    """The declaration is stored even when it matches: that is what Transcriber.process reads."""
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "t", "duration": 60.0, "language": "ja-JP"})
    s = server.Session(video_id=VIDEO, url="u")
    assert fetcher.download_once(s, resume=True) == tmp_path / f"{VIDEO}.webm"
    assert calls == ["extract_info", "process_ie_result"]
    assert s.declared_language == "ja"


def test_a_video_that_declares_nothing_is_downloaded_as_before(monkeypatch, tmp_path):
    fetcher, calls = make_fetcher(monkeypatch, tmp_path, {"title": "t", "duration": 60.0})
    s = server.Session(video_id=VIDEO, url="u")
    assert fetcher.download_once(s, resume=True) == tmp_path / f"{VIDEO}.webm"
    assert calls == ["extract_info", "process_ie_result"]
    assert s.declared_language is None


def test_patience_zero_downloads_a_foreign_video_like_any_other(monkeypatch, tmp_path):
    """--language-patience 0 means "never judge the language", and YouTube's word is a judgement too.

    It is the cure the README offers for a wrong pause, so it has to lift every kind of pause,
    including the one that happens before the download.
    """
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "t", "duration": 60.0, "language": "en"}, language_patience=0.0)
    s = server.Session(video_id=VIDEO, url="u")
    assert fetcher.download_once(s, resume=True) == tmp_path / f"{VIDEO}.webm"
    assert calls == ["extract_info", "process_ie_result"]
    assert s.declared_language == "en"  # written down, but nothing is done about it


def test_a_foreign_live_stream_is_refused_before_the_live_check(monkeypatch, tmp_path):
    """The refusal comes first, so a live stream is judged on the same word as a video.

    Following a stream costs the same GPU as downloading one: there is no reason to start it for
    audio YouTube says is the wrong language.
    """
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "t", "duration": None, "is_live": True, "language": "en"})
    s = server.Session(video_id=VIDEO, url="u")
    with pytest.raises(server.ForeignLanguage):
        fetcher.download_once(s, resume=True)
    assert calls == ["extract_info"]


def test_a_live_stream_in_the_target_language_is_still_followed(monkeypatch, tmp_path):
    """None from download_once is what sends fetch() to follow_live; the refusal must not eat that."""
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "t", "duration": None, "is_live": True, "language": "ja"})
    s = server.Session(video_id=VIDEO, url="u")
    assert fetcher.download_once(s, resume=True) is None
    assert calls == ["extract_info"]  # a live stream has no track to download


# --------------------------------------------------------------------------- what the viewer sees

def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_fetch_turns_the_refusal_into_the_same_pause_the_detector_would_have_set(monkeypatch, tmp_path):
    """heard + language_paused are the two fields the overlay already knows how to render.

    Ready, not error and not downloading: there is nothing left to wait for, and an error would
    put a red line on a video that is simply not in the subtitle language.
    """
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "A talk", "duration": 900.0, "language": "en-US"})
    s = server.Session(video_id=VIDEO, url="u")
    fetcher.fetch(s)
    assert (s.heard, s.language_paused, s.status) == ("en", True, "ready")
    assert s.duration == 900.0  # the hint is all there is; the audio was never decoded
    assert s.cues == [] and s.audio is None and s.preview is None
    assert s.error is None and s.fetching is False
    assert calls == ["extract_info"]


def test_sync_reports_the_refusal_to_the_client(monkeypatch, tmp_path):
    """The extension needs no new field: it already draws heard + language_paused."""
    fetcher, calls = make_fetcher(monkeypatch, tmp_path,
                                  {"title": "A talk", "duration": 900.0, "language": "en"})
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3",
                           language="ja", language_patience=60.0, idle_minutes=30, retry_after=30.0)
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    app.fetcher = fetcher

    app.sync(VIDEO, "u", 0.0, 0)  # starts the fetch thread
    assert wait_for(lambda: app.sessions[VIDEO].status == "ready")
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert resp["heard"] == "en" and resp["language_paused"] is True
    assert resp["status"] == "ready" and resp["cues"] == []
    assert app.sessions_summary()["sessions"][0]["language_paused"] is True
    assert calls == ["extract_info"]
    # Nothing to transcribe: no audio means the planner has no window to hand the GPU.
    assert server.plan_window(app.sessions[VIDEO], args) is None


# --------------------------------------------------------------------------- the detector that no longer runs

class FakeModel:
    """Scripted votes, one clean segment; counts what each call costs."""

    def __init__(self, votes=()):
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


def make_worker(monkeypatch, tmp_path, votes=(), **args_overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "detect_speech",
                        lambda audio, offset=0.0: [[offset + 0.5, offset + len(audio) / RATE - 0.5]])
    base = dict(first_window=20.0, window=20.0, lookahead=900.0, model="large-v3", language="ja",
                language_patience=30.0, beam_size=1, initial_prompt="", idle_minutes=30,
                client_timeout=30.0, retry_after=30.0)
    base.update(args_overrides)
    model = FakeModel(votes)
    app = server.App(SimpleNamespace(**base), model=model, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    worker = server.Transcriber(app)
    watched: list = []  # the start of every window the audio detector was asked about
    original = worker.watch_language

    def counting(s, audio, speech, start, end):
        watched.append(start)
        return original(s, audio, speech, start, end)

    monkeypatch.setattr(worker, "watch_language", counting)
    return worker, model, watched


def ready_session(**kwargs):
    return server.Session(video_id=VIDEO, url="u", status="ready", duration=600.0,
                          audio=np.zeros(600 * RATE, dtype=np.float32), **kwargs)


def test_the_detector_never_runs_on_a_video_youtube_names_as_the_target_language(monkeypatch, tmp_path):
    """The second a window that this change exists to give back. Drop the guard and it comes back."""
    worker, model, watched = make_worker(monkeypatch, tmp_path)
    s = ready_session(declared_language="ja")
    worker.process(s, 0.0, 20.0)
    worker.process(s, 20.0, 40.0)
    assert watched == []
    assert model.detections == 0   # no encoder pass beyond the one transcribe needs
    assert model.transcribes == 2  # ... and the subtitles are made all the same
    assert s.cues


def test_the_detector_still_runs_when_youtube_names_nothing(monkeypatch, tmp_path):
    """Then the audio is the only evidence there is, and the patience is what decides."""
    worker, model, watched = make_worker(monkeypatch, tmp_path)
    s = ready_session(declared_language=None)
    worker.process(s, 0.0, 20.0)
    assert watched == [0.0] and model.detections == 1
    assert model.transcribes == 1


def test_the_detector_still_runs_when_youtube_names_another_language(monkeypatch, tmp_path):
    """The guard is "YouTube agrees with --language", not "YouTube said something".

    A session can carry a foreign declaration and still reach the transcriber: the audio was
    already in the cache, so the download that would have refused it never ran.
    """
    worker, model, watched = make_worker(monkeypatch, tmp_path)
    s = ready_session(declared_language="en")
    worker.process(s, 0.0, 20.0)
    assert watched == [0.0] and model.detections == 1


def test_patience_zero_still_beats_the_declaration(monkeypatch, tmp_path):
    """Both conditions have to hold; --language-patience 0 alone keeps the detector out."""
    worker, model, watched = make_worker(monkeypatch, tmp_path, language_patience=0.0)
    s = ready_session(declared_language=None)
    worker.process(s, 0.0, 20.0)
    assert watched == [] and model.detections == 0
    assert model.transcribes == 1


# --------------------------------------------------------------------------- lifting a stale pause

def write_paused_cache(worker):
    """The cue cache a run that judged this video by ear left behind: paused on foreign speech.

    Written when the video had no declaration to go on (a server from before this version, or a
    day YouTube's metadata carried no language), which is the only way a pause and a matching
    declaration can meet.
    """
    old = server.Session(video_id=VIDEO, url="u", duration=600.0, covered=[[0.0, 20.0]],
                         cues=[{"id": 0, "seg": 0, "start": 1.0, "end": 3.0, "text": "これはテストです"}],
                         declared_language=None, language_paused=True, heard="en", foreign_seconds=45.0)
    worker.app.save_cache(old)


def reload_paused(worker, declared_language):
    s = server.Session(video_id=VIDEO, url="u")
    worker.app.load_cache(s)
    s.status, s.duration, s.declared_language = "ready", 600.0, declared_language
    s.audio = np.zeros(600 * RATE, dtype=np.float32)
    return s


def test_a_pause_in_the_cache_is_lifted_on_a_video_youtube_names_as_the_target_language(monkeypatch, tmp_path):
    """Nothing else can lift it: language_vote() is the only other way out, and it no longer runs.

    Without this the video stays blank for every later run, because the pause is reloaded, clips
    the lookahead and is written back untouched.
    """
    worker, model, watched = make_worker(monkeypatch, tmp_path)
    write_paused_cache(worker)
    s = reload_paused(worker, declared_language="ja")
    assert (s.language_paused, s.heard, s.foreign_seconds) == (True, "en", 45.0)
    # plan_window() files a sliver of a window as probed while the pause stands, so probes can be
    # waiting when the lift happens; the lift offers them back, the way language_vote() does.
    s.probed = [[20.0, 21.0]]

    worker.process(s, 20.0, 40.0)
    assert (s.language_paused, s.heard, s.foreign_seconds, s.probed) == (False, None, 0.0, [])
    assert model.transcribes == 1 and len(s.cues) == 2  # the cached cue, plus this window's
    assert watched == [] and model.detections == 0      # and not one encoder pass was spent on it


def test_the_lifted_pause_is_written_out_so_it_stops_coming_back(monkeypatch, tmp_path):
    worker, _model, _watched = make_worker(monkeypatch, tmp_path)
    write_paused_cache(worker)
    s = reload_paused(worker, declared_language="ja")
    worker.process(s, 20.0, 40.0)

    stored = json.loads(s.cache_path().read_text(encoding="utf-8"))["language_state"]
    assert stored == {"foreign_seconds": 0.0, "heard": None, "paused": False}
    later = server.Session(video_id=VIDEO, url="u")
    worker.app.load_cache(later)
    assert later.language_paused is False and later.heard is None


def test_the_full_lookahead_comes_back_once_the_pause_is_lifted(monkeypatch, tmp_path):
    """A paused session is planned only LANGUAGE_PROBE_AHEAD ahead, since every window is a probe."""
    worker, _model, _watched = make_worker(monkeypatch, tmp_path)
    write_paused_cache(worker)
    s = reload_paused(worker, declared_language="ja")
    assert server.lookahead_for(s, worker.app.args) == server.LANGUAGE_PROBE_AHEAD

    worker.process(s, 20.0, 40.0)
    assert server.lookahead_for(s, worker.app.args) == worker.app.args.lookahead == 900.0


def test_sync_stops_reporting_the_pause_after_that_window(monkeypatch, tmp_path):
    """What the viewer sees: the overlay drops the pause notice as soon as the first window runs."""
    worker, _model, _watched = make_worker(monkeypatch, tmp_path)
    write_paused_cache(worker)
    s = reload_paused(worker, declared_language="ja")
    worker.app.sessions[VIDEO] = s

    before = worker.app.sync(VIDEO, "u", 20.0, 0)
    assert before["heard"] == "en" and before["language_paused"] is True

    worker.process(s, 20.0, 40.0)
    after = worker.app.sync(VIDEO, "u", 20.0, 0)
    assert after["heard"] is None and after["language_paused"] is False


def test_a_pause_stands_when_youtube_names_another_language(monkeypatch, tmp_path):
    """The lift rests on YouTube agreeing with --language; anything else is still the ear's business."""
    worker, model, watched = make_worker(monkeypatch, tmp_path, votes=[("en", 0.95)])
    write_paused_cache(worker)
    s = reload_paused(worker, declared_language="en")

    worker.process(s, 20.0, 40.0)
    assert watched == [20.0] and model.detections == 1  # judged by ear, as before
    assert s.language_paused is True and s.heard == "en"
    assert model.transcribes == 0                       # the window was listened to, not decoded
    assert s.probed == [[20.0, 40.0]] and s.covered == [[0.0, 20.0]]


def test_a_pause_stands_when_youtube_names_nothing(monkeypatch, tmp_path):
    worker, model, watched = make_worker(monkeypatch, tmp_path, votes=[("en", 0.95)])
    write_paused_cache(worker)
    s = reload_paused(worker, declared_language=None)

    worker.process(s, 20.0, 40.0)
    assert watched == [20.0] and model.detections == 1
    assert s.language_paused is True and model.transcribes == 0


def test_patience_zero_never_reaches_either_branch(monkeypatch, tmp_path):
    """Detection off means no judging at all, and load_cache() is what clears the pause there.

    A session cannot be paused in memory under --language-patience 0 (nothing in the run sets the
    flag), so the cache is the only way in, and it is refused at the door; the hand-made session
    below only pins which guard comes first.
    """
    worker, model, watched = make_worker(monkeypatch, tmp_path, language_patience=30.0)
    write_paused_cache(worker)
    off, off_model, off_watched = make_worker(monkeypatch, tmp_path, language_patience=0.0)
    s = server.Session(video_id=VIDEO, url="u")
    off.app.load_cache(s)
    assert (s.language_paused, s.heard, s.foreign_seconds) == (False, None, 0.0)

    s.status, s.duration, s.declared_language = "ready", 600.0, "ja"
    s.audio = np.zeros(600 * RATE, dtype=np.float32)
    s.language_paused, s.heard = True, "en"  # as if the flag had got in anyway
    off.process(s, 20.0, 40.0)
    assert off_watched == [] and off_model.detections == 0
    assert off_model.transcribes == 1       # the window is decoded, pause or no pause
    assert s.language_paused is True        # ... and the outer guard kept the lift out of it
    assert model.detections == 0 and watched == []

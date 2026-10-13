"""A failed audio fetch is retried with exponential backoff, by the server, while someone watches.

The incident behind it: YouTube answered the media request with 403 twice in one second, the
session went to "error", and nothing tried again until a client synced 30 s later; a manual
download a minute on worked. Transient failures are now retried at 2, 4, 8, 16 ... s (capped at
--retry-after, with jitter), permanent ones never, and a 403 gets fresh format URLs at once.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


# --------------------------------------------------------------------------- classification

@pytest.mark.parametrize("message, cookies, kind", [
    (FORBIDDEN, "", "transient"),
    ("ERROR: unable to download video data: HTTP Error 503: Service Unavailable", "", "transient"),
    ("ERROR: Unable to download webpage: The read operation timed out", "", "transient"),
    ("[Errno 104] Connection reset by peer", "", "transient"),
    ("ERROR: fragment 3 not found, unable to continue", "", "transient"),
    ("something nobody has seen before", "", "transient"),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "", "rate_limited"),
    ("ERROR: [youtube] x: Video unavailable. This content isn't available, try again later.", "", "rate_limited"),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "", "permanent"),
    ("ERROR: [youtube] x: Video unavailable", "", "permanent"),
    ("ERROR: [youtube] aaaaaaaaaaa: This video is unavailable", "", "permanent"),  # what a made-up id gets
    ("ERROR: [youtube] x: This video is not available.", "", "permanent"),
    ("ERROR: [youtube] x: This video has been removed by the uploader", "", "permanent"),
    ("ERROR: [youtube] x: Join this channel to get access to members-only content", "", "permanent"),
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.", "", "permanent"),
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.",
     "firefox's YouTube cookies", "transient"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "", "permanent"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "firefox's YouTube cookies", "transient"),
])
def test_fetch_error_kind(message, cookies, kind):
    assert server.fetch_error_kind(RuntimeError(message), cookies) == kind


# --------------------------------------------------------------------------- the schedule

def test_backoff_doubles_from_two_seconds_up_to_retry_after():
    assert [server.retry_delay(n, "transient", 30.0, 0.0) for n in range(1, 8)] == [2, 4, 8, 16, 30, 30, 30]


def test_a_rate_limit_backs_off_harder():
    assert [server.retry_delay(n, "rate_limited", 30.0, 0.0) for n in range(1, 5)] == [8, 16, 30, 30]


def test_jitter_only_shortens_the_wait():
    assert server.retry_delay(5, "transient", 30.0, 0.999) == pytest.approx(30.0 * (1 - 0.2 * 0.999))
    assert server.retry_delay(1, "transient", 30.0, 0.5) == pytest.approx(1.8)
    assert all(server.retry_delay(n, "transient", 30.0, 0.999) <= 30.0 for n in range(1, 40))


def test_ytdlp_gets_its_own_retries_with_a_growing_sleep():
    fetcher = server.Fetcher(SimpleNamespace(cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="deno"))
    opts = fetcher.ytdlp_options(VIDEO)
    assert opts["retries"] == opts["fragment_retries"] == opts["extractor_retries"] == 3
    assert [opts["retry_sleep_functions"]["http"](n) for n in range(5)] == [0.5, 1, 2, 4, 4]


# --------------------------------------------------------------------------- the fetch loop

class Clock:
    """time.time and time.sleep for one test: sleep moves the clock, and a watching client syncs."""

    def __init__(self, session=None, watching=True):
        self.now = 1000.0
        self.slept: list = []
        self.session, self.watching = session, watching

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds
        if self.watching and self.session is not None:
            self.session.last_sync = self.now  # the extension syncs about every second


def make_fetcher(monkeypatch, tmp_path, outcomes, clock, **args):
    """A Fetcher whose download() raises each exception in `outcomes` in turn, then succeeds."""
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    base = dict(cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="deno", language="ja",
                language_patience=60.0, retry_after=30.0, client_timeout=30.0, idle_minutes=30,
                first_window=20.0, window=30.0)
    base.update(args)
    fetcher = server.Fetcher(SimpleNamespace(**base), clock=clock, sleep=clock.sleep, rng=lambda: 0.0)
    pending = list(outcomes)
    calls = []

    def download(s):
        calls.append(clock.now)
        if pending:
            raise pending.pop(0)
        path = tmp_path / f"{VIDEO}.webm"
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(fetcher, "download", download)
    monkeypatch.setattr(fetcher, "make_preview", lambda s, path: False)
    audio = types.ModuleType("faster_whisper.audio")
    audio.decode_audio = lambda path, sampling_rate: np.zeros(sampling_rate, dtype=np.float32)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio)
    return fetcher, calls


def session(clock):
    s = server.Session(video_id=VIDEO, url="u", fetching=True)
    s.last_sync = clock.now
    clock.session = s
    return s


def test_transient_failures_are_retried_on_the_schedule_and_success_resets_it(monkeypatch, tmp_path, caplog):
    clock = Clock()
    s = session(clock)
    fetcher, calls = make_fetcher(monkeypatch, tmp_path, [RuntimeError(FORBIDDEN)] * 3, clock)
    caplog.set_level("INFO", logger="shisu-ko")
    fetcher.fetch(s)
    assert clock.slept == [2.0, 4.0, 8.0]
    assert calls == [1000.0, 1002.0, 1006.0, 1014.0]
    assert s.status == "ready" and s.error is None and s.fetching is False
    assert (s.fail_count, s.retry_at, s.permanent) == (0, 0.0, False)
    text = caplog.text
    assert "failure 1), retrying in 2.0 s" in text and "retrying the audio fetch (attempt 4)" in text


def test_a_rate_limit_waits_longer(monkeypatch, tmp_path):
    clock = Clock()
    s = session(clock)
    fetcher, _ = make_fetcher(monkeypatch, tmp_path, [RuntimeError("HTTP Error 429: Too Many Requests")] * 2, clock)
    fetcher.fetch(s)
    assert clock.slept == [8.0, 16.0] and s.status == "ready"


def test_a_permanent_error_is_not_retried(monkeypatch, tmp_path):
    clock = Clock()
    s = session(clock)
    fetcher, calls = make_fetcher(monkeypatch, tmp_path, [RuntimeError("ERROR: Private video")] * 5, clock)
    fetcher.fetch(s)
    assert calls == [1000.0] and clock.slept == []
    assert s.status == "error" and s.error == "This video is private"
    assert s.permanent and s.retry_at == 0.0 and s.fetching is False


def test_the_server_stops_retrying_once_nobody_syncs(monkeypatch, tmp_path):
    clock = Clock(watching=False)
    s = session(clock)
    fetcher, calls = make_fetcher(monkeypatch, tmp_path, [RuntimeError(FORBIDDEN)] * 10, clock)
    fetcher.fetch(s)
    # 2 + 4 + 8 + 16 = 30 s without a sync is still within --client-timeout, so the fifth attempt
    # runs; after its 30 s wait nobody has synced for a minute, and the sixth is left to the next /sync.
    assert clock.slept == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert len(calls) == 5 and s.status == "error" and not s.permanent
    assert s.retry_at == pytest.approx(clock.now) and s.fetching is False


def test_the_language_refusal_writes_its_state_and_no_failure(monkeypatch, tmp_path):
    clock = Clock()
    s = session(clock)
    fetcher, calls = make_fetcher(monkeypatch, tmp_path, [server.ForeignLanguage("en")], clock)
    s.duration_hint = 42.0
    fetcher.fetch(s)
    assert calls == [1000.0] and clock.slept == []
    assert (s.status, s.heard, s.language_paused, s.duration, s.error) == ("ready", "en", True, 42.0, None)
    assert (s.fail_count, s.retry_at, s.permanent) == (0, 0.0, False)


# --------------------------------------------------------------------------- the 403 re-extract

class FakeYoutubeDL:
    """extract_info() counts; the first media request answers `first`, a fresh one writes the file."""

    first: str = FORBIDDEN
    log: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        FakeYoutubeDL.log.append("extract")
        return {"title": "t", "duration": 30.0, "abr": 128.0}

    def process_ie_result(self, info, download=True):
        FakeYoutubeDL.log.append("download")
        raise RuntimeError(FakeYoutubeDL.first)

    def download(self, urls):
        self.extract_info(urls[0])  # yt-dlp's download() extracts the info anew: fresh format URLs
        FakeYoutubeDL.log.append("download")
        (server.CACHE_DIR / f"{VIDEO}.webm").write_bytes(b"x")


def fake_ytdlp(monkeypatch, tmp_path, first):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    FakeYoutubeDL.first, FakeYoutubeDL.log = first, []
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    args = SimpleNamespace(cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="deno",
                           language="ja", language_patience=60.0)
    return server.Fetcher(args)


def test_a_403_fetches_fresh_format_urls_and_tries_again_at_once(monkeypatch, tmp_path):
    fetcher = fake_ytdlp(monkeypatch, tmp_path, FORBIDDEN)
    path = fetcher.download(server.Session(video_id=VIDEO, url="u"))
    assert path == tmp_path / f"{VIDEO}.webm"
    assert FakeYoutubeDL.log == ["extract", "download", "extract", "download"]


@pytest.mark.parametrize("message", ["HTTP Error 429: Too Many Requests", "ERROR: Private video"])
def test_a_rate_limit_or_a_permanent_error_is_not_tried_again_at_once(monkeypatch, tmp_path, message):
    fetcher = fake_ytdlp(monkeypatch, tmp_path, message)
    with pytest.raises(RuntimeError):
        fetcher.download(server.Session(video_id=VIDEO, url="u"))
    assert FakeYoutubeDL.log == ["extract", "download"]


# --------------------------------------------------------------------------- /sync and get_session

def make_app(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = SimpleNamespace(first_window=20.0, window=30.0, lookahead=900.0, model="large-v3", language="ja",
                           idle_minutes=30, retry_after=30.0, client_timeout=30.0)
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    calls = []
    app.fetcher = SimpleNamespace(fetch=lambda s: calls.append(s.video_id))
    return app, calls


def errored(app, now, **fields):
    s = server.Session(video_id=VIDEO, url="u", status="error", error="boom", error_at=now - 1.0)
    for k, v in fields.items():
        setattr(s, k, v)
    app.sessions[VIDEO] = s
    return s


def test_sync_says_when_the_next_attempt_is(monkeypatch, tmp_path):
    import time

    app, _ = make_app(monkeypatch, tmp_path)
    now = time.time()
    errored(app, now, fail_count=2, retry_at=now + 4.0)
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert resp["retrying"] is True and 3.0 <= resp["retry_in"] <= 4.0


def test_sync_of_a_permanent_error_says_no_retry(monkeypatch, tmp_path):
    import time

    app, calls = make_app(monkeypatch, tmp_path)
    errored(app, time.time(), permanent=True, fail_count=1)
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert resp["retrying"] is False and resp["retry_in"] is None
    app.sync(VIDEO, "u", 0.0, 0)
    assert calls == []


def test_a_permanent_error_is_asked_again_only_after_the_viewer_left(monkeypatch, tmp_path):
    import time

    app, _ = make_app(monkeypatch, tmp_path)
    now = time.time()
    s = errored(app, now, permanent=True)
    s.last_sync = now - 60
    assert not app.retry_due(s, now)
    s.last_sync = now - 31 * 60
    assert app.retry_due(s, now)


def test_a_due_retry_starts_on_the_next_sync(monkeypatch, tmp_path):
    import time

    app, calls = make_app(monkeypatch, tmp_path)
    now = time.time()
    s = errored(app, now, fail_count=5, retry_at=now - 0.1)
    assert app.retry_due(s, now)
    s.retry_at = now + 5.0
    assert not app.retry_due(s, now)


def test_a_session_without_an_error_answers_without_the_retry_keys(monkeypatch, tmp_path):
    app, _ = make_app(monkeypatch, tmp_path)
    app.sessions[VIDEO] = server.Session(video_id=VIDEO, url="u", status="ready")
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert "retrying" not in resp and "retry_in" not in resp

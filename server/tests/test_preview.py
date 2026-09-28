"""The preview decode: a few windows around the playhead, ready before the whole track is decoded.

Nothing here touches PyAV, faster-whisper or the network; the decode helpers are stubbed.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE


def make_args(**overrides):
    base = dict(first_window=20.0, window=40.0, lookahead=900.0)
    base.update(overrides)
    return SimpleNamespace(**base)


def preview(offset: float, seconds: float, fill=None):
    n = int(seconds * RATE)
    samples = np.arange(n, dtype=np.float32) if fill is None else np.full(n, fill, dtype=np.float32)
    return (offset, samples)


def session(**kwargs):
    base = dict(video_id=VIDEO, url="u", status="ready", duration=600.0)
    base.update(kwargs)
    return server.Session(**base)


# --------------------------------------------------------------------------- plan_window with a preview

def test_plan_window_uses_the_preview_when_the_full_audio_is_missing():
    s = session(preview=preview(29.0, 61.0), want_t=30.0)
    assert server.plan_window(s, make_args(first_window=20.0)) == (29.5, 49.5)


def test_plan_window_clamps_the_window_to_the_end_of_the_preview():
    s = session(preview=preview(29.0, 16.0), want_t=30.0)  # preview covers 29 - 45
    assert server.plan_window(s, make_args(first_window=20.0)) == (29.5, 45.0)


def test_plan_window_none_when_the_playhead_is_before_the_preview():
    s = session(preview=preview(100.0, 30.0), want_t=10.0)
    assert server.plan_window(s, make_args()) is None


def test_plan_window_none_when_the_playhead_is_past_the_preview():
    s = session(preview=preview(100.0, 30.0), want_t=200.0)
    assert server.plan_window(s, make_args()) is None


def test_plan_window_none_and_no_coverage_when_the_clamped_window_is_too_short():
    # The full-audio path marks such a sliver covered; the preview path must not, because the
    # audio for it simply has not been decoded yet.
    s = session(preview=preview(29.0, 1.0), want_t=30.0)
    assert server.plan_window(s, make_args()) is None
    assert s.covered == []


def test_plan_window_none_without_audio_or_preview():
    assert server.plan_window(session(want_t=30.0), make_args()) is None


def test_plan_window_prefers_the_full_audio_once_it_is_there():
    # Same session as the clamped case, but with the full decode present: no clamping.
    s = session(preview=preview(29.0, 16.0), audio=object(), want_t=30.0)
    assert server.plan_window(s, make_args(first_window=20.0)) == (29.5, 49.5)


# --------------------------------------------------------------------------- audio_slice

def test_audio_slice_reads_the_preview_at_the_right_offset():
    offset, samples = preview(10.0, 10.0)
    s = session(preview=(offset, samples))
    out = server.audio_slice(s, 12.0, 13.0)
    assert np.array_equal(out, samples[2 * RATE: 3 * RATE])


def test_audio_slice_none_outside_the_preview():
    s = session(preview=preview(10.0, 10.0))
    assert server.audio_slice(s, 5.0, 8.0) is None      # before the preview
    assert server.audio_slice(s, 19.0, 25.0) is None    # runs past its end
    assert server.audio_slice(s, 12.0, 12.0) is None    # empty range


def test_audio_slice_none_without_audio_or_preview():
    assert server.audio_slice(session(), 0.0, 5.0) is None


def test_audio_slice_uses_the_full_audio_when_present():
    audio = np.arange(20 * RATE, dtype=np.float32)
    s = session(audio=audio, preview=preview(10.0, 10.0, fill=-1.0))
    out = server.audio_slice(s, 12.0, 13.0)
    assert np.array_equal(out, audio[12 * RATE: 13 * RATE])


# --------------------------------------------------------------------------- Fetcher.fetch

def fake_decode_range(src, start, end, rate=server.CLIP_RATE):
    return np.full(int((end - start) * rate), 1000, dtype=np.int16)


def make_fetcher(monkeypatch, tmp_path, decode_audio, duration=90.0, duration_hint=0.0, decode_range=fake_decode_range):
    """A Fetcher whose audio file is already cached and whose decoders are stubs."""
    path = tmp_path / f"{VIDEO}.webm"
    path.write_bytes(b"pretend audio")
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: path)
    monkeypatch.setattr(server, "probe_duration", lambda p: duration)
    monkeypatch.setattr(server, "_decode_range", decode_range)

    module = types.ModuleType("faster_whisper.audio")
    module.decode_audio = decode_audio
    package = types.ModuleType("faster_whisper")
    package.audio = module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    # language / language_patience: download_once() weighs YouTube's declared language against them
    # before it downloads anything, and these fakes declare none, so the download always goes ahead.
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, language="ja", language_patience=60.0,
                           cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="auto")
    s = server.Session(video_id=VIDEO, url="u", want_t=30.0, duration_hint=duration_hint)
    return server.Fetcher(args), s


def test_fetch_makes_the_session_usable_before_the_full_decode_returns(monkeypatch, tmp_path):
    seen = {}

    def decode_audio(path, sampling_rate=RATE):
        # The state the transcriber would see while the slow decode is still running.
        with state["session"].lock:
            s = state["session"]
            seen.update(status=s.status, preview=s.preview, duration=s.duration, audio=s.audio)
        return np.zeros(int(90.0 * RATE), dtype=np.float32)

    state = {}
    fetcher, s = make_fetcher(monkeypatch, tmp_path, decode_audio, duration=90.0)
    state["session"] = s
    fetcher.fetch(s)

    assert seen["status"] == "ready"
    assert seen["audio"] is None
    assert seen["duration"] == pytest.approx(90.0)
    offset, samples = seen["preview"]
    assert offset == pytest.approx(29.0)                       # want_t - 1
    assert len(samples) == int((90.0 - 29.0) * RATE)           # clamped to the end of the video
    assert samples.dtype == np.float32
    assert samples[0] == pytest.approx(1000 / 32768.0)
    # A window can be planned from the preview alone.
    assert server.plan_window(server.Session(video_id=VIDEO, url="u", status="ready", duration=90.0,
                                             want_t=30.0, preview=seen["preview"]), make_args()) == (29.5, 49.5)

    assert s.status == "ready"
    assert s.preview is None
    assert s.audio is not None and len(s.audio) == int(90.0 * RATE)
    assert s.duration == pytest.approx(90.0)
    assert s.fetching is False


def test_fetch_falls_back_to_the_ytdlp_duration_when_the_container_has_none(monkeypatch, tmp_path):
    fetcher, s = make_fetcher(monkeypatch, tmp_path, lambda p, sampling_rate=RATE: np.zeros(RATE, dtype=np.float32),
                              duration=None, duration_hint=90.0)
    fetcher.make_preview(s, tmp_path / f"{VIDEO}.webm")
    assert s.status == "ready"
    assert s.preview is not None and s.preview[0] == pytest.approx(29.0)
    assert s.duration == pytest.approx(90.0)


def test_fetch_skips_the_preview_when_the_duration_is_unknown(monkeypatch, tmp_path):
    calls = []

    def decode_range(src, start, end, rate=server.CLIP_RATE):
        calls.append((start, end))
        return fake_decode_range(src, start, end, rate)

    fetcher, s = make_fetcher(monkeypatch, tmp_path, lambda p, sampling_rate=RATE: np.zeros(RATE, dtype=np.float32),
                              duration=None, decode_range=decode_range)
    fetcher.fetch(s)
    assert calls == []
    assert s.audio is not None
    assert s.preview is None
    assert s.status == "ready"


def test_fetch_survives_a_failing_preview_decode(monkeypatch, tmp_path):
    def boom(src, start, end, rate=server.CLIP_RATE):
        raise RuntimeError("no decoder for this file")

    fetcher, s = make_fetcher(monkeypatch, tmp_path,
                              lambda p, sampling_rate=RATE: np.zeros(int(90.0 * RATE), dtype=np.float32),
                              decode_range=boom)
    fetcher.fetch(s)
    assert s.status == "ready"
    assert s.error is None
    assert s.preview is None
    assert s.audio is not None


# --------------------------------------------------------------------------- preview from a partial download

def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL: fires the progress hooks, then writes the finished file."""

    def __init__(self, opts, plan):
        self.opts = opts
        self.plan = plan

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {"title": "A video", "duration": self.plan["duration"], "abr": self.plan["abr"]}

    def process_ie_result(self, info, download=True):
        for downloaded in self.plan["progress"]:
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": downloaded,
                      "total_bytes": self.plan["progress"][-1], "tmpfilename": str(self.plan["part"]),
                      "info_dict": {"abr": self.plan["abr"]}})
        wait_for(self.plan["settled"], timeout=self.plan["settle_timeout"])
        s = self.plan["session"]
        with s.lock:
            self.plan["at_return"] = {"status": s.status, "preview": s.preview is not None}
        self.plan["final"].write_bytes(b"complete audio")


def setup_download(monkeypatch, tmp_path, *, want_t, duration=90.0, abr=128.0,
                   progress=(100_000, 500_000, 2_000_000), part_decodes=True):
    """A Fetcher wired to FakeYoutubeDL, with the decoders and the cache lookup stubbed."""
    final = tmp_path / f"{VIDEO}.webm"
    part = tmp_path / f"{VIDEO}.webm.part"
    part.write_bytes(b"partial audio")
    decoded = []

    def decode_range(src, start, end, rate=server.CLIP_RATE):
        decoded.append((Path(src).name, start, end))
        if str(src).endswith(".part") and not part_decodes:
            raise RuntimeError("stream ends before the header is complete")
        return np.full(int((end - start) * rate), 1000, dtype=np.int16)

    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: final if final.exists() else None)
    # A partial container usually has no readable duration, so the yt-dlp hint is what is left.
    monkeypatch.setattr(server, "probe_duration", lambda p: None if str(p).endswith(".part") else duration)
    monkeypatch.setattr(server, "_decode_range", decode_range)

    module = types.ModuleType("faster_whisper.audio")
    module.decode_audio = lambda p, sampling_rate=RATE: np.zeros(int(duration * RATE), dtype=np.float32)
    package = types.ModuleType("faster_whisper")
    package.audio = module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    s = server.Session(video_id=VIDEO, url="u", want_t=want_t)
    plan = {"duration": duration, "abr": abr, "progress": list(progress), "part": part, "final": final,
            "session": s, "settled": lambda: s.preview is not None, "settle_timeout": 2.0, "at_return": None}
    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = lambda opts: FakeYoutubeDL(opts, plan)
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)

    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, language="ja", language_patience=60.0,
                           cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="auto")
    return server.Fetcher(args), s, plan, decoded


def test_preview_is_published_while_the_download_is_still_running(monkeypatch, tmp_path):
    fetcher, s, plan, decoded = setup_download(monkeypatch, tmp_path, want_t=5.0)
    fetcher.fetch(s)

    # The session was already usable when yt-dlp was still downloading.
    assert plan["at_return"] == {"status": "ready", "preview": True}
    # Only the .part file was previewed: fetch must not redo the work after the download.
    assert [name for name, _, _ in decoded] == [f"{VIDEO}.webm.part"]
    assert decoded[0][1] == pytest.approx(4.0)   # want_t - 1
    assert decoded[0][2] == pytest.approx(66.0)  # + first_window + window + 2

    assert s.status == "ready"
    assert s.preview is None
    assert s.audio is not None and len(s.audio) == int(90.0 * RATE)
    assert s.duration == pytest.approx(90.0)
    assert s.title == "A video"


def test_early_preview_waits_for_enough_bytes_and_fires_once(monkeypatch, tmp_path):
    # (66 + 5) seconds at 128 kbit/s is about 1.1 MB, so only the last two samples qualify.
    fetcher, s, plan, decoded = setup_download(monkeypatch, tmp_path, want_t=5.0,
                                               progress=(100_000, 500_000, 2_000_000, 3_000_000))
    fetcher.fetch(s)
    assert [name for name, _, _ in decoded] == [f"{VIDEO}.webm.part"]


def test_no_early_preview_after_a_seek_past_the_first_minute(monkeypatch, tmp_path):
    fetcher, s, plan, decoded = setup_download(monkeypatch, tmp_path, want_t=300.0, duration=600.0)
    plan["settled"] = lambda: False  # give the hook time to prove it does not fire
    plan["settle_timeout"] = 0.2
    fetcher.fetch(s)

    assert plan["at_return"] == {"status": "downloading", "preview": False}
    # Only the finished file was previewed, by the normal after-download path.
    assert [name for name, _, _ in decoded] == [f"{VIDEO}.webm"]
    assert decoded[0][1] == pytest.approx(299.0)
    assert s.audio is not None
    assert s.preview is None


def test_a_partial_file_that_does_not_decode_falls_back_to_the_normal_preview(monkeypatch, tmp_path):
    fetcher, s, plan, decoded = setup_download(monkeypatch, tmp_path, want_t=5.0, part_decodes=False)
    plan["settled"] = lambda: any(name.endswith(".part") for name, _, _ in decoded)
    fetcher.fetch(s)

    assert plan["at_return"] == {"status": "downloading", "preview": False}
    assert [name for name, _, _ in decoded] == [f"{VIDEO}.webm.part", f"{VIDEO}.webm"]
    assert s.status == "ready"
    assert s.audio is not None
    assert s.preview is None


def test_a_download_failure_clears_a_preview_from_the_partial_file(monkeypatch, tmp_path):
    fetcher, s, plan, decoded = setup_download(monkeypatch, tmp_path, want_t=5.0)
    monkeypatch.setattr(fetcher, "download", lambda session: (_ for _ in ()).throw(RuntimeError("Video unavailable")))
    with s.lock:
        s.preview = preview(4.0, 62.0)  # as if the hook had already published one
    fetcher.fetch(s)
    assert s.status == "error"
    assert s.error == "Video unavailable"
    assert s.preview is None


def test_make_preview_does_not_overwrite_the_full_audio(monkeypatch, tmp_path):
    fetcher, s, plan, decoded = setup_download(monkeypatch, tmp_path, want_t=5.0)
    audio = np.zeros(int(90.0 * RATE), dtype=np.float32)
    with s.lock:
        s.audio = audio
        s.duration_hint = 90.0
    assert fetcher.make_preview(s, plan["part"]) is False
    assert s.preview is None
    assert s.audio is audio


# --------------------------------------------------------------------------- stream_bytes_per_second

def test_stream_bytes_per_second_prefers_the_reported_bitrate():
    assert server.stream_bytes_per_second({"info_dict": {"abr": 128.0}}, 0.0) == pytest.approx(16000.0)


def test_stream_bytes_per_second_falls_back_to_the_extracted_then_default_bitrate():
    assert server.stream_bytes_per_second({}, 64.0) == pytest.approx(8000.0)
    assert server.stream_bytes_per_second({"info_dict": {"abr": None}}, 0.0) == pytest.approx(20000.0)

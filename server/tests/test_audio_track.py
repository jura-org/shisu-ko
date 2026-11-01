"""A fetched video's decoded track is kept as int16 (decode_track()), half the memory of the
float32 copy faster-whisper's decode_audio() makes; a window (audio_slice()) and a clip
(make_clip()) come out of it as they did before.

Only test_decode_track_gives_decode_audio_s_numbers_on_a_real_file needs PyAV and faster-whisper;
it is skipped where they are not installed (the CI's test requirements).
"""
from __future__ import annotations

import io
import sys
import types
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE


def pcm(seconds: float, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).integers(-32768, 32768, size=int(seconds * RATE), dtype=np.int16)


def as_decode_audio_gives(samples: np.ndarray) -> np.ndarray:
    """faster-whisper's own conversion of its s16 decode (decode_audio())."""
    return samples.astype(np.float32) / 32768.0


def test_a_window_of_an_int16_track_is_the_float32_decode_audio_gives():
    track = pcm(3.0)
    s = server.Session(video_id=VIDEO, url="u", status="ready", duration=3.0, audio=track)
    window = server.audio_slice(s, 1.0, 2.5)
    assert window.dtype == np.float32
    assert np.array_equal(window, as_decode_audio_gives(track[RATE: int(2.5 * RATE)]))
    s.audio = as_decode_audio_gives(track)  # the tools decode their own float32 track: as it is
    assert np.array_equal(server.audio_slice(s, 1.0, 2.5), window)


def test_the_fetch_keeps_the_track_as_int16(monkeypatch, tmp_path):
    track = pcm(4.0)
    path = tmp_path / f"{VIDEO}.webm"
    path.write_bytes(b"pretend audio")
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: path)
    monkeypatch.setattr(server, "probe_duration", lambda p: None)  # no preview: the full decode alone
    module = types.ModuleType("faster_whisper.audio")  # a faster-whisper without the decoder's steps
    module.decode_audio = lambda p, sampling_rate=RATE: as_decode_audio_gives(track)
    package = types.ModuleType("faster_whisper")
    package.audio = module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, language="ja", language_patience=60.0,
                           cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="auto")
    s = server.Session(video_id=VIDEO, url="u", want_t=0.0)
    server.Fetcher(args).fetch(s)
    assert s.status == "ready" and s.duration == pytest.approx(4.0)
    assert s.audio.dtype == np.int16 and np.array_equal(s.audio, track)  # exact: an int16 over 32768 is
    assert np.array_equal(server.audio_slice(s, 0.5, 3.0), as_decode_audio_gives(track[RATE // 2: 3 * RATE]))


def test_decode_track_runs_faster_whisper_s_own_steps_and_keeps_their_int16(monkeypatch, tmp_path):
    chunks = [pcm(0.5, seed=2), pcm(1.25, seed=3)]
    seen = {}

    class Frame:
        def __init__(self, samples):
            self.samples = samples

        def to_ndarray(self):
            return self.samples.reshape(1, -1)  # PyAV's packed mono s16: one row

    class Container:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def decode(self, audio):
            seen["stream"] = audio
            return ["frame"]

    def opened(path, mode, metadata_errors):
        seen["open"] = (path, mode, metadata_errors)
        return Container()

    class Resampler:
        def __init__(self, **kwargs):
            seen["resampler"] = kwargs

    av = types.ModuleType("av")
    av.open = opened
    av.audio = SimpleNamespace(resampler=SimpleNamespace(AudioResampler=Resampler))
    monkeypatch.setitem(sys.modules, "av", av)

    def ignore(frames):
        seen["ignored"] = list(frames)
        return iter(seen["ignored"])

    def group(frames, num_samples):
        seen["group"] = num_samples
        return list(frames)

    def resample(frames, resampler):
        seen["resample"] = (frames, type(resampler).__name__)
        return (Frame(c) for c in chunks)

    module = types.ModuleType("faster_whisper.audio")
    module._ignore_invalid_frames, module._group_frames, module._resample_frames = ignore, group, resample
    module.decode_audio = lambda *a, **k: pytest.fail("decode_audio() would make the float32 copy")
    package = types.ModuleType("faster_whisper")
    package.audio = module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)

    track = server.decode_track(tmp_path / "a.webm")
    assert track.dtype == np.int16 and np.array_equal(track, np.concatenate(chunks))
    # decode_audio()'s own pipeline, step for step: the same container options, stream, grouping and resampler.
    assert seen["open"] == (str(tmp_path / "a.webm"), "r", "ignore") and seen["stream"] == 0
    assert seen["ignored"] == ["frame"] and seen["group"] == 500000
    assert seen["resampler"] == {"format": "s16", "layout": "mono", "rate": RATE}
    assert seen["resample"] == (["frame"], "Resampler")


def test_decode_track_gives_decode_audio_s_numbers_on_a_real_file(tmp_path):
    pytest.importorskip("av")
    fw_audio = pytest.importorskip("faster_whisper.audio")
    if not all(hasattr(fw_audio, n) for n in ("_ignore_invalid_frames", "_group_frames", "_resample_frames")):
        pytest.skip("this faster-whisper names its decoding steps otherwise")
    rate = 22050  # resampled on the way to 16 kHz, as nearly every video's audio is
    t = np.arange(int(5.3 * rate)) / rate
    signal = 0.4 * np.sin(2 * np.pi * (220 + 400 * t) * t) + 0.05 * np.random.default_rng(4).standard_normal(len(t))
    path = tmp_path / "speechlike.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.clip(signal * 32767, -32768, 32767).astype("<i2").tobytes())
    expected = fw_audio.decode_audio(str(path), sampling_rate=RATE)
    track = server.decode_track(path)
    assert track.dtype == np.int16 and len(track) == len(expected)
    assert np.array_equal(as_decode_audio_gives(track), expected)


def test_a_clip_from_an_int16_track_keeps_its_samples(monkeypatch):
    track = pcm(3.0)
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: None)  # the source file is gone
    data, mime, ext = server.make_clip(VIDEO, 1.0, 2.0, "wav", track)
    assert (mime, ext) == ("audio/wav", "wav")
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == RATE
        assert np.array_equal(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), track[RATE: 2 * RATE])
    # A float32 copy (a live stream's buffer, the tools' decode) is scaled as before.
    data, _, _ = server.make_clip(VIDEO, 1.0, 2.0, "wav", as_decode_audio_gives(track))
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnframes() == RATE

"""decode_audio_file(): faster-whisper's decode_audio(), which PyAV 19 breaks.

PyAV 19 dropped av.open()'s metadata_errors, which faster-whisper 1.2.1's decode_audio() still
passes, so it raises TypeError on every track. decode_audio_file() then runs decode_audio()'s own
steps on a file opened without the option, for the same numbers.

Only test_the_fallback_gives_decode_audio_s_numbers_on_a_real_file needs PyAV and faster-whisper;
it is skipped where they are not installed.
"""
from __future__ import annotations

import sys
import types
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
RATE = server.SAMPLE_RATE
REFUSED = TypeError("open() got an unexpected keyword argument 'metadata_errors'")


def faster_whisper_audio(monkeypatch, decode_audio, steps=True, chunks=()):
    """faster_whisper.audio in sys.modules: decode_audio(), and the three steps it runs (which hand
    out `chunks` as resampled frames) unless `steps` is False. Returns what the steps saw."""
    seen = {}
    module = types.ModuleType("faster_whisper.audio")
    module.decode_audio = decode_audio
    if steps:
        def ignore(frames):
            seen["ignored"] = list(frames)
            return iter(seen["ignored"])

        def group(frames, num_samples):
            seen["group"] = num_samples
            return list(frames)

        def resample(frames, resampler):
            seen["resample"] = list(frames)
            return (SimpleNamespace(to_ndarray=lambda c=c: c.reshape(1, -1)) for c in chunks)

        module._ignore_invalid_frames, module._group_frames, module._resample_frames = ignore, group, resample
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", module)
    return seen


def pyav(monkeypatch, seen):
    """An av module whose open() takes no options, as PyAV 19's open() takes no metadata_errors."""
    class Container:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def decode(self, audio):
            seen["stream"] = audio
            return ["frame"]

    def opened(path, mode="r", **options):
        seen["open"] = (path, mode, options)
        if options:
            raise REFUSED
        return Container()

    class Resampler:
        def __init__(self, **kwargs):
            seen["resampler"] = kwargs

    av = types.ModuleType("av")
    av.open = opened
    av.audio = SimpleNamespace(resampler=SimpleNamespace(AudioResampler=Resampler))
    monkeypatch.setitem(sys.modules, "av", av)


def test_decode_audio_is_taken_as_it_is_where_it_works(monkeypatch, tmp_path):
    audio = np.linspace(-0.5, 0.5, RATE, dtype=np.float32)
    seen = faster_whisper_audio(monkeypatch, lambda path, sampling_rate=RATE: audio)
    assert server.decode_audio_file(tmp_path / "a.webm") is audio
    assert seen == {}  # its steps never ran


def test_pyav_19_s_refusal_runs_decode_audio_s_own_steps_without_the_option(monkeypatch, tmp_path):
    chunks = [np.array([0, 16384, -32768], dtype=np.int16), np.array([32767, -1], dtype=np.int16)]

    def decode_audio(path, sampling_rate=RATE):
        raise REFUSED

    seen = faster_whisper_audio(monkeypatch, decode_audio, chunks=chunks)
    pyav(monkeypatch, seen)
    audio = server.decode_audio_file(tmp_path / "a.webm")
    assert seen["open"] == (str(tmp_path / "a.webm"), "r", {})  # opened without metadata_errors
    assert seen["stream"] == 0 and seen["ignored"] == ["frame"] and seen["group"] == 500000
    assert seen["resampler"] == {"format": "s16", "layout": "mono", "rate": RATE}
    assert audio.dtype == np.float32
    assert np.array_equal(audio, np.concatenate(chunks).astype(np.float32) / 32768.0)  # decode_audio()'s conversion


def test_any_other_failure_is_decode_audio_s_own(monkeypatch, tmp_path):
    def other(path, sampling_rate=RATE):
        raise TypeError("expected str, bytes or os.PathLike object")

    faster_whisper_audio(monkeypatch, other)
    with pytest.raises(TypeError, match="PathLike"):
        server.decode_audio_file(tmp_path / "a.webm")

    def refused(path, sampling_rate=RATE):
        raise REFUSED

    faster_whisper_audio(monkeypatch, refused, steps=False)  # a faster-whisper that names its steps otherwise
    with pytest.raises(TypeError, match="metadata_errors"):
        server.decode_audio_file(tmp_path / "a.webm")


def test_the_fallback_gives_decode_audio_s_numbers_on_a_real_file(monkeypatch, tmp_path):
    av = pytest.importorskip("av")
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
    with monkeypatch.context() as patch:
        # The reference: decode_audio() itself, with the option dropped where this PyAV refuses it.
        real_open = av.open

        def open_(*args, **kwargs):
            try:
                return real_open(*args, **kwargs)
            except TypeError:
                kwargs.pop("metadata_errors", None)
                return real_open(*args, **kwargs)

        patch.setattr(av, "open", open_)
        expected = fw_audio.decode_audio(str(path), sampling_rate=RATE)
    real_decode = fw_audio.decode_audio

    def refused(*args, **kwargs):
        raise REFUSED

    monkeypatch.setattr(fw_audio, "decode_audio", refused)  # the fallback, whatever this PyAV takes
    audio = server.decode_audio_file(path)
    assert audio.dtype == np.float32 and len(audio) == len(expected) > RATE * 5
    assert np.array_equal(audio, expected)
    monkeypatch.setattr(fw_audio, "decode_audio", real_decode)

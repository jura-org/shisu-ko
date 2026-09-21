"""Sung lyrics: a window Silero hears nothing in, whose audio is not silence, is transcribed
without the detector and gated on Whisper's own confidence (docs/subtitle-quality.md, P0.3).

Nothing here loads a model; the fake below records what transcribe() was asked for and answers
with scripted segments, and detect_speech is replaced by what the test says Silero heard.
"""
from __future__ import annotations

import importlib.util
import logging
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE
W = server.Word


def words(spec, prob: float = 0.9):
    """[("text", start, end), ...] -> [Word]."""
    return [W(text, start, end, prob) for text, start, end in spec]


def seg(text: str, ws, no_speech: float = 0.1, logprob: float = -0.2, compression: float = 1.2):
    """A faster-whisper segment as lyrics_reason reads it: text, words and the decoder's own scores."""
    return SimpleNamespace(text=text, start=ws[0].start, end=ws[-1].end, words=ws, no_speech_prob=no_speech,
                           avg_logprob=logprob, compression_ratio=compression)


def line(text: str, start: float, end: float, prob: float = 0.9, **scores):
    """One sung line whose words share [start, end) evenly, two characters a word."""
    pieces = [text[i:i + 2] for i in range(0, len(text), 2)]
    step = (end - start) / len(pieces)
    ws = words([(p, start + i * step, start + (i + 1) * step) for i, p in enumerate(pieces)], prob)
    return seg(text, ws, **scores)


def tone(seconds: float, amplitude: float = 0.3) -> np.ndarray:
    """A 440 Hz sine: rms amplitude / sqrt(2), 0.21 at the default, well above LYRICS_MIN_RMS."""
    t = np.arange(int(seconds * RATE), dtype=np.float32) / RATE
    return (amplitude * np.sin(2 * math.pi * 440.0 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.float32)


SUNG = [line("君の声が聞こえる夜に", 1.0, 4.0), line("星を数えて眠る", 6.0, 8.5)]
PAD = server.VAD_PARAMS["speech_pad_ms"] / 1000.0  # what lyrics_spans() puts around every run, as Silero does
SUNG_SPANS = [[1.0 - PAD, 4.0 + PAD], [6.0 - PAD, 8.5 + PAD]]


def close(intervals, expected) -> bool:
    """[[a, b], ...] == [[a, b], ...] to the millisecond (a padded span is a float sum)."""
    return len(intervals) == len(expected) and all(
        a == pytest.approx(x, abs=1e-3) and b == pytest.approx(y, abs=1e-3) for (a, b), (x, y) in zip(intervals, expected))


def timed(cues) -> list:
    return [(round(c["start"], 2), round(c["end"], 2), c["text"]) for c in cues]


# --------------------------------------------------------------------------- rms and the decision

def test_rms_of_silence_is_zero_and_of_a_tone_its_amplitude_over_root_two():
    assert server.rms(silence(2.0)) == 0.0
    assert server.rms(silence(0.0)) == 0.0
    assert server.rms(tone(2.0, 0.3)) == pytest.approx(0.3 / math.sqrt(2), abs=1e-3)
    assert server.rms(tone(2.0, 0.001)) < server.LYRICS_MIN_RMS  # room tone stays below the floor


def test_wants_lyrics_needs_next_to_no_speech_and_a_signal():
    args = SimpleNamespace(lyrics="auto")
    loud = tone(20.0)
    assert server.wants_lyrics(args, loud, [], 0.0, 20.0) is True
    assert server.wants_lyrics(args, loud, [[5.0, 5.9]], 0.0, 20.0) is True      # under a second: a transient
    assert server.wants_lyrics(args, loud, [[5.0, 6.0]], 0.0, 20.0) is False     # a second of speech is talk
    assert server.wants_lyrics(args, loud, [[30.0, 40.0]], 0.0, 20.0) is True    # speech outside the window does not count
    assert server.wants_lyrics(args, silence(20.0), [], 0.0, 20.0) is False
    assert server.wants_lyrics(SimpleNamespace(lyrics="off"), loud, [], 0.0, 20.0) is False
    assert server.wants_lyrics(SimpleNamespace(), loud, [], 0.0, 20.0) is True   # the default is auto


# --------------------------------------------------------------------------- lyrics_reason

def test_a_confident_sung_line_passes():
    s = SUNG[0]
    assert server.lyrics_reason(s, s.words) is None


def test_a_segment_without_words_or_text_is_empty():
    assert server.lyrics_reason(seg("テキスト", words([("テキスト", 0.0, 1.0)])), []) == "empty"
    ws = words([("…", 0.0, 1.0)])
    assert server.lyrics_reason(seg("…", ws), ws) == "empty"


def test_the_decoder_doubting_its_own_segment_is_unsure():
    s = line("君の声が聞こえる夜に", 1.0, 4.0, no_speech=0.95)
    assert server.lyrics_reason(s, s.words) == "unsure"
    s = line("君の声が聞こえる夜に", 1.0, 4.0, logprob=-1.0)
    assert server.lyrics_reason(s, s.words) == "unsure"
    s = line("君の声が聞こえる夜に", 1.0, 4.0, prob=0.2)
    assert server.lyrics_reason(s, s.words) == "unsure"
    at_the_edges = line("君の声が聞こえる夜に", 1.0, 4.0, prob=0.35, no_speech=0.9, logprob=-0.8)
    assert server.lyrics_reason(at_the_edges, at_the_edges.words) is None
    chorus = line("君の声が聞こえる夜に", 1.0, 4.0, no_speech=0.8)  # an 18-voice chorus scored this, every line right
    assert server.lyrics_reason(chorus, chorus.words) is None


def test_a_repetition_loop_is_rejected_whatever_its_scores():
    s = line("ラララ" * 6, 1.0, 4.0)
    assert server.lyrics_reason(s, s.words) == "repetition"


def test_a_sign_off_phrase_is_rejected_without_any_vad_evidence_to_rescue_it():
    s = line("ご視聴ありがとうございました", 1.0, 4.0)
    assert server.lyrics_reason(s, s.words) == "blocklist"


def test_an_anomalous_segment_is_rejected_whatever_its_scores():
    # Ten words crammed into a tenth of a second: faster-whisper's own anomaly score, applied
    # unconditionally since there is no VAD overlap to excuse it.
    ws = words([(c, i * 0.01, (i + 1) * 0.01) for i, c in enumerate("あいうえおかきくけこ")])
    assert server.lyrics_reason(seg("あいうえおかきくけこ", ws), ws) == "anomaly"


# --------------------------------------------------------------------------- spans and cues

def test_lyrics_spans_are_the_padded_word_runs_of_the_segments_that_pass():
    segs = SUNG + [line("ご視聴ありがとうございました", 10.0, 12.0)]
    assert close(server.lyrics_spans(segs, 100.0), [[101.0 - PAD, 104.0 + PAD], [106.0 - PAD, 108.5 + PAD]])
    assert server.lyrics_spans([], 0.0) == []


def test_lyrics_spans_cut_a_segment_at_a_pause_and_pad_each_run_like_the_detector():
    # Two lines in one segment, the usual shape without the VAD filter: the breath between them is a
    # gap in the spans, as it would be in Silero's intervals, so the cue builder can see it.
    ws = line("君の声が聞こえる夜に", 1.0, 3.2).words + line("星を数えて眠るまで", 4.2, 6.4).words
    spans = server.lyrics_spans([seg("君の声が聞こえる夜に星を数えて眠るまで", ws)], 0.0)
    assert close(spans, [[1.0 - PAD, 3.2 + PAD], [4.2 - PAD, 6.4 + PAD]])
    # A gap under pause_split is no pause: one run, as a Silero interval would be.
    ws = line("君の声が聞こえる夜に", 1.0, 3.2).words + line("星を数えて眠るまで", 3.6, 5.8).words
    assert close(server.lyrics_spans([seg("君の声が聞こえる夜に星を数えて眠るまで", ws)], 0.0), [[1.0 - PAD, 5.8 + PAD]])


def test_lyrics_spans_start_past_a_stranded_first_word():
    # Whisper anchored 星を flush with the previous line's end, a second before the rest of its line
    # (the artifact repair_lead_words() slides). A span over it would join the previous line's.
    ws = [W("星を", 4.0, 4.1, 0.9)] + line("数えて眠るまで", 5.0, 7.5).words
    assert close(server.lyrics_spans([SUNG[0], seg("星を数えて眠るまで", ws)], 0.0), [[1.0 - PAD, 4.0 + PAD], [5.0 - PAD, 7.5 + PAD]])
    assert server.stranded_head(ws) == 1
    assert server.stranded_head(ws[1:]) == 0                                    # no gap after the first word
    assert server.stranded_head([W("あいうえおかき", 4.0, 4.1, 0.9)] + ws[1:]) == 0  # seven characters: a line, not a fragment
    assert server.stranded_head([W("星を", 4.0, 4.1, 0.9), W("数え", 4.35, 4.5, 0.9)]) == 0  # 0.25 s: a pause, not the artifact
    assert server.stranded_head(ws[:1]) == 0


def test_build_window_cues_on_a_lyrics_window_snaps_to_the_segments_and_reaches_into_the_gaps():
    segs = SUNG + [line("ご視聴ありがとうございました", 10.0, 12.0)]
    spans = server.lyrics_spans(segs, 0.0)
    drops: dict = {}
    cues, next_id = server.build_window_cues(segs, 0.0, spans, server.CueLimits(), 3, drops, lyrics=True)
    assert [c["text"] for c in cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert [c["seg"] for c in cues] == [3, 4]
    assert next_id == 5
    assert cues[0]["start"] == pytest.approx(1.0)        # the line starts where its first word does
    assert cues[0]["end"] == pytest.approx(4.0 + server.CueLimits().lead_out)  # ... and holds a beat into the gap
    assert cues[1]["start"] == pytest.approx(6.0)
    assert drops == {"blocklist": 1, "_text": [("blocklist", "ご視聴ありがとうございました")]}


def talk_cues(segs, spoken):
    """The same words through the talk path, on intervals shaped like Silero's (a pad each side)."""
    speech = server.merge_intervals([[a - PAD, b + PAD] for a, b in spoken])
    cues, _ = server.build_window_cues(segs, 0.0, speech, server.CueLimits(), 0, {}, 40.0)
    return timed(cues)


def lyrics_cues(segs):
    spans = server.lyrics_spans(segs, 0.0)
    cues, _ = server.build_window_cues(segs, 0.0, spans, server.CueLimits(), 0, {}, 40.0, lyrics=True)
    return timed(cues)


def test_a_breath_between_two_sung_lines_keeps_them_apart():
    # Whole segment spans hid the breath: the lead-out ate it down to 0.1 s and merge_segments()
    # glued the lines into one row. Padded runs give the talk path's cues for every breath.
    for breath in (0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
        segs = [line("君の声が聞こえる夜に", 1.0, 4.0), line("星を数えて眠るまで", 4.0 + breath, 6.5 + breath)]
        cues = lyrics_cues(segs)
        assert [c[2] for c in cues] == ["君の声が聞こえる夜に", "星を数えて眠るまで"], breath
        assert cues == talk_cues(segs, [[1.0, 4.0], [4.0 + breath, 6.5 + breath]]), breath
    # Two short lines merge into one cue, on a seam the viewer sees as a new line.
    segs = [line("会いたくて震える", 1.0, 3.0), line("夜空を見上げて", 3.6, 5.6)]
    assert lyrics_cues(segs) == [(1.0, 6.3, "会いたくて震える\n夜空を見上げて")]
    assert lyrics_cues(segs) == talk_cues(segs, [[1.0, 3.0], [3.6, 5.6]])


def test_a_pause_inside_a_sung_segment_splits_the_lines():
    # One Whisper segment holding two lines with a breath between them, the usual shape without
    # the VAD filter: the second line must not be on screen while the first is still sung.
    for pause in (1.0, 1.5):
        ws = line("君の声が聞こえる夜に", 1.0, 3.2).words + line("星を数えて眠るまで", 3.2 + pause, 5.4 + pause).words
        segs = [seg("君の声が聞こえる夜に星を数えて眠るまで", ws)]
        cues = lyrics_cues(segs)
        assert [c[2] for c in cues] == ["君の声が聞こえる夜に", "星を数えて眠るまで"], pause
        assert cues[1][0] == pytest.approx(3.2 + pause), pause
        assert cues == talk_cues(segs, [[1.0, 3.2], [3.2 + pause, 5.4 + pause]]), pause


def test_a_stranded_first_word_is_slid_onto_its_sung_line():
    # The anchoring artifact: 星を at 4.00-4.10, flush with the previous line's end, its line sung
    # from 5.0 (also 6.0, 7.0). The line must start where it is sung, and the previous line keeps
    # its lead-out instead of being cut back by the overlap rule.
    for b_start in (5.0, 6.0, 7.0):
        ws = [W("星を", 4.0, 4.1, 0.9)] + line("数えて眠るまで", b_start, b_start + 2.5).words
        segs = [line("君の声が聞こえる夜に", 1.0, 4.0), seg("星を数えて眠るまで", ws)]
        cues = lyrics_cues(segs)
        assert [c[2] for c in cues] == ["君の声が聞こえる夜に", "星を数えて眠るまで"], b_start
        assert cues[0][1] == pytest.approx(4.0 + server.CueLimits().lead_out), b_start
        assert cues[1][0] == pytest.approx(b_start - PAD), b_start   # the onset, as on the talk path
        assert cues == talk_cues(segs, [[1.0, 4.0], [b_start, b_start + 2.5]]), b_start


# --------------------------------------------------------------------------- Transcriber.process

class FakeModel:
    """Records what transcribe() was asked for; answers with the scripted segments.

    As faster-whisper does, it decodes nothing with `vad_filter` on when the detector `heard`
    nothing. detect_language() answers `verdict` for every window and keeps the samples it was
    given: sung Japanese by default, so a loud speechless window takes the lyrics path.
    """

    def __init__(self, segs, verdict=("ja", 0.95), heard=()):
        self.segs = list(segs)
        self.verdict = verdict
        self.heard = list(heard)
        self.calls: list = []
        self.detections = 0
        self.judged: list = []

    def detect_language(self, audio=None, **kwargs):
        self.detections += 1
        self.judged.append(audio)
        return self.verdict[0], self.verdict[1], []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("vad_filter") and not self.heard:
            return iter([]), None
        return iter(list(self.segs)), None


def make_worker(monkeypatch, tmp_path, segs=SUNG, speech=(), verdict=("ja", 0.95), **args_overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    heard = [list(iv) for iv in speech]
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: [[offset + a, offset + b] for a, b in heard])
    base = dict(first_window=20.0, window=20.0, lookahead=900.0, model="large-v3", language="ja",
                language_patience=30.0, beam_size=1, initial_prompt="", idle_minutes=30, client_timeout=30.0,
                lyrics="auto")
    base.update(args_overrides)
    model = FakeModel(segs, verdict, heard)
    app = server.App(SimpleNamespace(**base), model=model, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return server.Transcriber(app), model


def session(audio):
    return server.Session(video_id=VIDEO, url="u", status="ready", audio=audio, duration=len(audio) / RATE)


def test_no_speech_in_a_loud_window_is_transcribed_without_the_detector(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path)
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 1
    assert model.calls[0]["vad_filter"] is False
    assert "vad_parameters" not in model.calls[0]
    assert model.calls[0]["word_timestamps"] is True  # everything else as before
    assert [c["text"] for c in s.cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert [c["seg"] for c in s.cues] == [0, 1]
    assert close(s.speech, SUNG_SPANS)  # the sung lines, padded like the detector's intervals, are the window's speech
    assert s.covered == [[0.0, 20.0]]
    assert s.busy is None


def test_a_quiet_window_without_speech_takes_the_usual_path(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path)
    s = session(silence(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is True
    assert model.calls[0]["vad_parameters"] == server.VAD_PARAMS
    assert s.speech == []


def test_speech_heard_takes_the_usual_path(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, speech=[[0.5, 3.0]])
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is True
    assert "vad_parameters" in model.calls[0]
    assert s.speech == [[0.5, 3.0]]


def test_lyrics_off_takes_the_usual_path(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, lyrics="off")
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is True
    assert "vad_parameters" in model.calls[0]
    assert s.speech == []


def test_a_rejected_line_takes_no_segment_id_and_no_span(monkeypatch, tmp_path):
    segs = [SUNG[0], line("ご視聴ありがとうございました", 10.0, 12.0), SUNG[1]]
    worker, _model = make_worker(monkeypatch, tmp_path, segs=segs)
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert [c["text"] for c in s.cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert [c["seg"] for c in s.cues] == [0, 1]
    assert s.seg_next == 2
    assert close(s.speech, SUNG_SPANS)


def test_the_log_line_says_which_way_the_window_went(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="shisu-ko")
    segs = SUNG + [line("ご視聴ありがとうございました", 10.0, 12.0)]
    worker, _model = make_worker(monkeypatch, tmp_path, segs=segs)
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert any("2 cues" in r.getMessage() and "[lyrics] [dropped blocklist:1]" in r.getMessage() for r in caplog.records)

    caplog.clear()
    worker.process(s, 20.0, 40.0)
    assert any("cues" in r.getMessage() and "[lyrics]" in r.getMessage() for r in caplog.records)

    caplog.clear()
    usual, _model = make_worker(monkeypatch, tmp_path, speech=[[0.5, 3.0]])
    usual.process(session(tone(40.0)), 0.0, 20.0)
    assert not any("[lyrics]" in r.getMessage() for r in caplog.records)


def test_a_lyrics_window_casts_no_language_vote(monkeypatch, tmp_path):
    # The watch runs before the decision and hears no speech, so it asks the head nothing; the
    # one detection is the lyrics rule's own, on the window's samples, and it is no vote.
    worker, model = make_worker(monkeypatch, tmp_path)
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1
    assert len(model.judged[0]) == 20 * RATE
    assert s.heard is None and s.foreign_seconds == 0.0 and s.language_paused is False
    assert s.cues


# --------------------------------------------------------------------------- a song beside the MC line

class TimelineModel(FakeModel):
    """FakeModel over segments and heard intervals on the video's clock, whatever the window.

    As faster-whisper does, with `vad_filter` on it decodes only what the detector heard: the
    segments touching a heard interval. The window's offset is what detect_speech() was last
    given (process() calls it first); see timeline_worker().
    """

    def __init__(self, segs, heard, verdict=("ja", 0.95)):
        super().__init__(segs, verdict, heard)
        self.offset = 0.0

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        lo, hi = self.offset, self.offset + len(audio) / RATE
        out = []
        for s in self.segs:
            if s.start < lo or s.end > hi:
                continue
            if kwargs.get("vad_filter") and not any(a < s.end and b > s.start for a, b in self.heard):
                continue
            out.append(seg(s.text, words([(w.word, w.start - lo, w.end - lo) for w in s.words]),
                           no_speech=s.no_speech_prob, logprob=s.avg_logprob))
        return iter(out), None


def timeline_worker(monkeypatch, tmp_path, segs, heard, **args_overrides):
    worker, _ = make_worker(monkeypatch, tmp_path, segs=[], **args_overrides)
    model = TimelineModel(segs, heard)
    worker.app.model = model

    def detect_speech(audio, offset=0.0):
        model.offset = offset
        hi = offset + len(audio) / RATE
        return [[a, b] for a, b in heard if a < hi and b > offset]

    monkeypatch.setattr(server, "detect_speech", detect_speech)
    return worker, model


def sung_text(cues) -> str:
    return "".join(c["text"] for c in cues).replace("\n", "")


LEAD_OUT = server.CueLimits().lead_out
UTAWAKU = [line("はい次の曲いきます", 0.5, 2.5)] + [
    line(text, 6.0 + 3.0 * i, 8.5 + 3.0 * i) for i, text in enumerate([
        "君の声が聞こえる夜に", "星を数えて眠るまで", "会いたくて震える", "夜空を見上げて", "遠く離れても",
        "同じ月を見てる", "この歌が届くなら", "もう一度だけ", "君の名前を呼ぶ", "夜が明けるまで"])]


def test_the_song_after_the_mc_line_is_planned_again_rather_than_covered_blank(monkeypatch, tmp_path, caplog):
    # A 歌枠: the singer announces the song (two seconds of detected speech), then sings for thirty
    # seconds the detector hears nothing of. The window is talk to wants_lyrics(), the decoder
    # sees the announcement alone, and the singing must not be covered with nothing in it.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    worker, model = timeline_worker(monkeypatch, tmp_path, UTAWAKU, heard=[[0.5, 2.5]], window=40.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert model.calls[0]["vad_filter"] is True
    assert [c["text"] for c in s.cues] == ["はい次の曲いきます"]
    assert close(s.covered, [[0.0, 2.5 + LEAD_OUT]])  # the singing is left to a window of its own
    assert close(s.speech, [[0.5, 2.5]])
    assert any("37 s heard nothing in, planned again" in r.getMessage() for r in caplog.records)
    record = worker.app.read_cache(s, s.cache_path())
    assert close(record["covered"], [[0.0, 2.5 + LEAD_OUT]]) and record["lyrics"] == "auto"

    window = server.plan_window(s, worker.app.args)
    assert window == pytest.approx((2.5 + LEAD_OUT, 42.5 + LEAD_OUT))
    worker.process(s, *window)
    assert model.calls[1]["vad_filter"] is False  # next to no speech: the lyrics rule judges it alone
    assert sung_text(s.cues[1:]) == "".join(x.text for x in UTAWAKU[1:])
    assert close(s.covered, [[0.0, 42.5 + LEAD_OUT]])


def test_the_song_before_the_mc_line_and_a_bridge_between_two_lines_are_planned_again(monkeypatch, tmp_path):
    # The other transitions: singing at the window's start, talk after it, and a sung bridge
    # between two spoken lines. Each loud unheard stretch comes back as a window of its own.
    heard = [[20.5, 22.5], [30.5, 32.5]]
    segs = [line("君の声が聞こえる夜に", 2.0, 5.0), line("星を数えて眠るまで", 12.0, 15.0),
            line("次の曲いきます", 20.5, 22.5), line("会いたくて震える", 25.0, 28.0), line("ありがとう", 30.5, 32.5)]
    worker, model = timeline_worker(monkeypatch, tmp_path, segs, heard=heard, window=40.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert [c["text"] for c in s.cues] == ["次の曲いきます", "ありがとう"]
    assert close(s.covered, [[20.5, 22.5 + LEAD_OUT], [30.5, 32.5 + LEAD_OUT]])

    # The planner walks the stretches before the last spoken line (the first as a first window,
    # then the sliver up to the MC line's range, then the bridge); each is sung Japanese to the head.
    texts, windows = [], []
    for _ in range(8):
        window = server.plan_window(s, worker.app.args)
        windows.append(window)
        if window is None or window[0] >= 32.5:
            continue
        before = len(s.cues)
        worker.process(s, *window)
        assert model.calls[-1]["vad_filter"] is False, window
        texts += [c["text"] for c in s.cues[before:]]
    assert texts == ["君の声が聞こえる夜に", "星を数えて眠るまで", "会いたくて震える"]
    assert windows[0] == (0.0, 20.0)
    assert windows[2] == pytest.approx((22.5 + LEAD_OUT, 30.5))
    assert close(s.covered, [[0.0, 32.5 + LEAD_OUT]])


def test_a_short_or_quiet_unheard_stretch_stays_covered(monkeypatch, tmp_path):
    # Under LYRICS_MIN_STRETCH_S a stretch is a pause with music under it more often than a line,
    # and a silent one costs nothing to cover: neither is planned again.
    segs = [line("はい次の曲いきます", 0.5, 2.5), line("会いたくて震える", 3.5, 6.0), line("次いきます", 6.5, 8.5)]
    worker, _model = timeline_worker(monkeypatch, tmp_path, segs, heard=[[0.5, 2.5], [6.5, 8.5]], window=13.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 13.0)
    assert [c["text"] for c in s.cues] == ["はい次の曲いきます", "次いきます"]
    assert close(s.covered, [[0.0, 13.0]])  # 3.2-6.5 and 9.2-13 are under the floor

    worker, _model = timeline_worker(monkeypatch, tmp_path, segs[:1], heard=[[0.5, 2.5]], window=40.0)
    s = session(np.concatenate([tone(3.0), silence(77.0)]))
    worker.process(s, 0.0, 40.0)
    assert close(s.covered, [[0.0, 40.0]])  # 3.2-40 s is silence: nothing to plan again


def test_a_window_the_head_refused_is_covered_whole(monkeypatch, tmp_path):
    # Without this a loud window the head heard no Japanese in would be planned for ever: the
    # stretches are only left out of a window that the speech heard in it kept from the lyrics path.
    worker, model = timeline_worker(monkeypatch, tmp_path, UTAWAKU[1:], heard=[], window=40.0)
    model.verdict = ("en", 0.99)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert model.calls[0]["vad_filter"] is True and s.cues == []
    assert close(s.covered, [[0.0, 40.0]])

    worker, model = timeline_worker(monkeypatch, tmp_path, UTAWAKU, heard=[[0.5, 2.5]], window=40.0, lyrics="off")
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert [c["text"] for c in s.cues] == ["はい次の曲いきます"]
    assert close(s.covered, [[0.0, 40.0]])


def test_unsung_stretches_are_the_loud_unheard_parts_of_a_window():
    audio = np.concatenate([tone(10.0), silence(10.0), tone(20.0)])
    speech = [[9.5, 10.5], [19.5, 20.5]]
    cues = [{"start": 9.5, "end": 11.2, "text": "a"}, {"start": 19.5, "end": 21.2, "text": "b"}]
    assert close(server.unsung_stretches(audio, 0.0, 40.0, speech, cues), [[0.0, 9.5], [21.2, 40.0]])  # 11.2-19.5 is silent
    assert close(server.unsung_stretches(audio, 0.0, 40.0, speech, cues, min_seconds=10.0), [[21.2, 40.0]])
    assert server.unsung_stretches(audio, 0.0, 40.0, [[0.0, 40.0]], []) == []
    assert close(server.unsung_stretches(audio, 0.0, 40.0, [], []), [[0.0, 40.0]])
    assert server.unsung_stretches(silence(40.0), 0.0, 40.0, [], []) == []


# --------------------------------------------------------------------------- the language verdict

def test_a_loud_window_sung_in_another_language_stays_with_the_detector(monkeypatch, tmp_path):
    # An English song under a montage: loud, no speech to Silero, and English to the head. The
    # VAD path decodes nothing of it, and the verdict is no vote: nothing pauses.
    worker, model = make_worker(monkeypatch, tmp_path, verdict=("en", 0.99))
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1
    assert model.calls[0]["vad_filter"] is True
    assert model.calls[0]["vad_parameters"] == server.VAD_PARAMS
    assert s.cues == [] and s.speech == []
    assert s.covered == [[0.0, 20.0]]
    assert s.heard is None and s.foreign_seconds == 0.0 and s.language_paused is False


def test_a_loud_window_the_head_is_unsure_of_stays_with_the_detector(monkeypatch, tmp_path):
    # Rain, a crowd, an engine: the head guesses, below LANGUAGE_MIN_PROB, and nothing is decoded.
    worker, model = make_worker(monkeypatch, tmp_path, verdict=("ja", 0.4))
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1
    assert model.calls[0]["vad_filter"] is True
    assert s.cues == [] and s.speech == []
    assert s.heard is None and s.language_paused is False


def test_a_loud_window_sung_in_the_target_language_takes_the_lyrics_path(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, verdict=("ja", server.LANGUAGE_MIN_PROB))
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1
    assert model.calls[0]["vad_filter"] is False
    assert [c["text"] for c in s.cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]


def test_a_song_starting_in_the_last_seconds_of_a_long_window_is_heard(monkeypatch, tmp_path):
    # Window 40-80: an instrumental intro the head hears no Japanese in, vocals from 72 s. The
    # head judges thirty seconds at a time, so the window's last thirty are judged as well before
    # it is refused; a window sung throughout costs one pass, a short one is judged once.
    worker, model = make_worker(monkeypatch, tmp_path, window=40.0)
    marker = np.concatenate([tone(72.0), tone(8.0, 0.9)])  # the vocals, loud enough to tell apart

    def detect_language(audio=None, **kwargs):
        model.judged.append(audio)
        return (("ja", 0.95) if float(np.abs(audio).max()) > 0.5 else ("en", 0.6)) + ([],)

    model.detect_language = detect_language
    s = session(marker)
    worker.process(s, 40.0, 80.0)
    assert model.calls[0]["vad_filter"] is False
    assert len(model.judged) == 2
    assert len(model.judged[0]) == 30 * RATE and float(np.abs(model.judged[0]).max()) < 0.5  # 40-70 s: the intro
    assert len(model.judged[1]) == 30 * RATE and float(np.abs(model.judged[1]).max()) > 0.5  # 50-80 s: the vocals
    assert s.cues

    worker, model = make_worker(monkeypatch, tmp_path, window=40.0)  # sung from the start: one pass
    worker.process(session(tone(80.0)), 40.0, 80.0)
    assert model.detections == 1 and model.calls[0]["vad_filter"] is False

    worker, model = make_worker(monkeypatch, tmp_path, verdict=("en", 0.99), window=40.0)  # refused twice
    worker.process(session(tone(80.0)), 40.0, 80.0)
    assert model.detections == 2 and model.calls[0]["vad_filter"] is True

    worker, model = make_worker(monkeypatch, tmp_path, verdict=("en", 0.99))  # a 20 s window: once
    worker.process(session(tone(40.0)), 0.0, 20.0)
    assert model.detections == 1


def test_a_failing_head_leaves_the_window_to_the_lyrics_path(monkeypatch, tmp_path, caplog):
    # A detector failure must never silence a video: decoded as sung, unjudged, with a warning.
    caplog.set_level(logging.WARNING, logger="shisu-ko")
    worker, model = make_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(model, "detect_language", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no encoder")))
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is False
    assert [c["text"] for c in s.cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert s.heard is None and s.language_paused is False
    assert any("language detection of a sung window failed" in r.getMessage() for r in caplog.records)


def test_the_verdict_is_asked_whatever_the_language_patience(monkeypatch, tmp_path):
    # --language-patience 0 turns the pause off; this check guards a decode, not the pause.
    worker, model = make_worker(monkeypatch, tmp_path, verdict=("en", 0.99), language_patience=0.0)
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1
    assert model.calls[0]["vad_filter"] is True
    assert s.cues == []

    worker, model = make_worker(monkeypatch, tmp_path, language_patience=0.0)
    s = session(tone(40.0))
    worker.process(s, 0.0, 20.0)
    assert model.detections == 1
    assert model.calls[0]["vad_filter"] is False
    assert s.cues


def test_the_head_is_not_asked_when_the_window_is_talk_or_silence_or_lyrics_off(monkeypatch, tmp_path):
    # Whatever the loudness rule refuses is never judged: no encoder pass is spent on it.
    worker, model = make_worker(monkeypatch, tmp_path)
    worker.process(session(silence(40.0)), 0.0, 20.0)
    assert model.detections == 0

    worker, model = make_worker(monkeypatch, tmp_path, lyrics="off")
    worker.process(session(tone(40.0)), 0.0, 20.0)
    assert model.detections == 0

    # Speech heard, below the watch's LANGUAGE_MIN_SPEECH but at least a second: talk, unjudged.
    worker, model = make_worker(monkeypatch, tmp_path, speech=[[0.5, 2.0]])
    worker.process(session(tone(40.0)), 0.0, 20.0)
    assert model.detections == 0
    assert model.calls[0]["vad_filter"] is True


def test_a_live_stream_takes_the_same_path(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", status="ready", live=True, live_audio=server.LiveAudio())
    s.live_audio.add(100.0, tone(30.0))
    worker.process(s, 100.0, 120.0)
    assert model.calls[0]["vad_filter"] is False
    assert [c["text"] for c in s.cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert close(s.speech, [[a + 100.0, b + 100.0] for a, b in SUNG_SPANS])


# --------------------------------------------------------------------------- the cue cache

def test_subtract_intervals_cuts_the_holes_out():
    assert server.subtract_intervals([[0.0, 10.0]], [[2.0, 3.0], [8.0, 12.0]]) == [[0.0, 2.0], [3.0, 8.0]]
    assert server.subtract_intervals([[0.0, 10.0]], [[0.0, 10.0]]) == []
    assert server.subtract_intervals([[0.0, 10.0]], []) == [[0.0, 10.0]]
    assert server.subtract_intervals([], [[0.0, 10.0]]) == []
    assert server.subtract_intervals([[0.0, 4.0], [6.0, 10.0]], [[3.0, 7.0]]) == [[0.0, 3.0], [7.0, 10.0]]
    assert server.subtract_intervals([[0.0, 10.0]], [[-5.0, 1.0], [9.0, 15.0]]) == [[1.0, 9.0]]


def test_unheard_stretches_are_the_covered_parts_no_speech_and_no_cue_touches():
    cues = [{"start": 5.0, "end": 8.0, "text": "a"}]
    # The gap before the speech is under the floor; the one between speech and cue is not.
    assert server.unheard_stretches([[0.0, 30.0]], [[1.0, 2.0]], cues) == [[2.0, 5.0], [8.0, 30.0]]
    assert server.unheard_stretches([[0.0, 30.0]], [[1.0, 2.0]], cues, min_seconds=25.0) == []
    assert server.unheard_stretches([[0.0, 34.04]], [], []) == [[0.0, 34.04]]   # the Short
    assert server.unheard_stretches([[0.0, 10.0]], [[0.0, 10.0]], []) == []
    assert server.unheard_stretches([], [], []) == []
    # A speech interval straddling the start of a gap shortens it; one touching the end closes it.
    assert server.unheard_stretches([[0.0, 20.0]], [[0.0, 4.0], [18.5, 25.0]], []) == [[4.0, 18.5]]


def test_unheard_stretches_hold_a_hole_between_two_lines_to_the_inner_floor():
    # A talk's pauses are holes of a few seconds between its cues; only the ends of a covered range
    # (an intro, an outro) count from min_seconds. The whole of a Short touches both ends.
    cues = [{"start": 4.0, "end": 8.0, "text": "a"}, {"start": 12.5, "end": 20.0, "text": "b"},
            {"start": 31.0, "end": 40.0, "text": "c"}]
    assert server.unheard_stretches([[0.0, 60.0]], [[3.0, 4.0]], cues, inner_seconds=8.0) == [[0.0, 3.0], [20.0, 31.0], [40.0, 60.0]]
    assert server.unheard_stretches([[0.0, 60.0]], [[3.0, 4.0]], cues) == [[0.0, 3.0], [8.0, 12.5], [20.0, 31.0], [40.0, 60.0]]
    assert server.unheard_stretches([[0.0, 60.0]], [[0.0, 4.0]], cues, inner_seconds=8.0) == [[20.0, 31.0], [40.0, 60.0]]
    assert server.unheard_stretches([[0.0, 34.04]], [], [], inner_seconds=8.0) == [[0.0, 34.04]]
    # A covered range's own edge is an end, whatever lies beyond it.
    assert server.unheard_stretches([[0.0, 10.0], [50.0, 60.0]], [], cues[:1], inner_seconds=8.0) == [[0.0, 4.0], [8.0, 10.0], [50.0, 60.0]]


def make_app(monkeypatch, tmp_path, **args_overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    base = dict(model="large-v3", language="ja", idle_minutes=30, language_patience=60.0, lyrics="auto")
    base.update(args_overrides)
    return server.App(SimpleNamespace(**base), model=None, device="cpu", compute_type="int8")


def write_record(tmp_path, cues, covered, speech, duration=34.04, **extra):
    """A format-3 record as a 0.11.2 server wrote it: no "lyrics" key (the key, or the rule, in `extra`)."""
    import json

    data = {"video_id": VIDEO, "title": "OP", "duration": duration, "format": server.CACHE_FORMAT,
            "model": "large-v3", "language": "ja", "cues": cues, "covered": covered, "speech": speech,
            "language_state": {"foreign_seconds": 0.0, "heard": None, "paused": False}}
    data.update(extra)
    (tmp_path / f"{VIDEO}.cues.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_a_music_video_cached_before_the_rule_is_offered_to_the_planner_again(monkeypatch, tmp_path, caplog):
    # The Short's own record: covered to the end, nothing heard, nothing decoded. Loading it as
    # ready would keep it blank for ever; instead nothing is covered and the audio is fetched anew.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[])
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.title == "OP" and s.duration == 34.04
    assert s.covered == [] and s.cues == [] and s.speech == []
    assert s.status == "pending"
    assert any("34 s were covered before the lyrics rule" in r.getMessage() for r in caplog.records)


def test_a_talk_video_cached_before_the_rule_keeps_its_cues_and_gives_back_its_blank_stretch(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    cues = [{"start": 1.0, "end": 4.0, "text": "これは", "seg": 0}, {"start": 4.2, "end": 20.0, "text": "テスト", "seg": 1}]
    write_record(tmp_path, cues=cues, covered=[[0.0, 60.0]], speech=[[0.8, 19.5]], duration=60.0)
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert [c["text"] for c in s.cues] == ["これは", "テスト"]
    assert [c["seg"] for c in s.cues] == [0, 1] and s.seg_next == 2
    assert s.speech == [[0.8, 19.5]]
    assert s.covered == [[0.0, 20.0]]  # the song bridge after the talk is transcribed again
    assert s.status == "pending"


def test_a_talk_record_gives_back_its_long_holes_and_its_ends_but_not_its_pauses(monkeypatch, tmp_path, caplog):
    # A 0.11.2 talk record: an intro, lines with pauses of a few seconds between them (holes of
    # 2.5 and 3.5 s, under LYRICS_MIN_STRETCH_S), a ten-second bridge, and an outro. The pauses
    # stay covered, or every talk video would be fetched again and walked a window per pause.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    cues = [{"start": 3.0, "end": 10.0, "text": "a", "seg": 0}, {"start": 12.5, "end": 20.0, "text": "b", "seg": 1},
            {"start": 23.5, "end": 30.0, "text": "c", "seg": 2}, {"start": 40.0, "end": 50.0, "text": "d", "seg": 3}]
    write_record(tmp_path, cues=cues, covered=[[0.0, 60.0]], speech=[[3.2, 9.5], [12.7, 19.5], [23.7, 29.5], [40.2, 49.5]], duration=60.0)
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert len(s.cues) == 4
    assert s.covered == [[3.0, 30.0], [40.0, 50.0]]  # the intro, the bridge and the outro are offered again
    assert s.status == "pending"
    assert any("23 s were covered before the lyrics rule" in r.getMessage() for r in caplog.records)


def test_a_format_2_record_is_dropped_by_the_format_check_not_migrated(monkeypatch, tmp_path, caplog):
    # 0.11.0 and 0.11.1 wrote format 2 (with the key on the branch that made the rule, without it
    # on main); either way the format check drops the record whole, title kept, and the video is
    # transcribed from scratch. The migration keys on the lyrics field of a format-3 record only.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[{"start": 1.0, "end": 4.0, "text": "これは", "seg": 0}], covered=[[0.0, 34.04]],
                 speech=[[0.8, 4.0]], format=2)
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.title == "OP"
    assert s.cues == [] and s.covered == [] and s.speech == []
    assert s.status == "pending"
    assert not any("covered before the lyrics rule" in r.getMessage() for r in caplog.records)
    assert not any("loaded" in r.getMessage() and "cached cues" in r.getMessage() for r in caplog.records)


def test_a_record_written_under_the_rule_loads_untouched(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[], lyrics="auto")
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.covered == [[0.0, 34.04]]
    assert s.status == "ready"


def test_a_record_written_with_lyrics_off_is_offered_again_under_auto(monkeypatch, tmp_path):
    # --lyrics off covers a sung window with nothing in it just as the old server did.
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[], lyrics="off")
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.covered == [] and s.status == "pending"


def test_lyrics_off_loads_an_old_record_untouched(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, lyrics="off")
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[])
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.covered == [[0.0, 34.04]]
    assert s.status == "ready"


def test_save_cache_writes_the_rule_and_its_own_record_reloads_as_covered(monkeypatch, tmp_path):
    import json

    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=34.04)
    s.covered = [[0.0, 34.04]]  # a window the head heard no Japanese in: covered, nothing in it
    app.save_cache(s)
    data = json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert data["lyrics"] == "auto"
    # The migration keys on the lyrics field, not on the format: a 0.11.2 record (format 3, no
    # key) is migrated, a format-2 record is dropped by the format check like any older cache.
    assert data["format"] == server.CACHE_FORMAT == 3

    reloaded = server.Session(video_id=VIDEO, url="u")
    app.load_cache(reloaded)
    assert reloaded.covered == [[0.0, 34.04]]
    assert reloaded.status == "ready"

    off = make_app(monkeypatch, tmp_path, lyrics="off")
    off.save_cache(s)
    assert json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))["lyrics"] == "off"


# --------------------------------------------------------------------------- --lyrics

def test_parse_args_takes_auto_or_off(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    assert server.parse_args([]).lyrics == "auto"
    assert server.parse_args(["--lyrics", "off"]).lyrics == "off"
    with pytest.raises(SystemExit):
        server.parse_args(["--lyrics", "maybe"])


def load_tool(filename: str = "retranscribe.py", name: str = "shisuko_retranscribe"):
    """A server/tools script as a module, the way test_setup_model.py loads retranscribe.py: its
    own load_server() finds shisuko_server in sys.modules, so it drives this test's server module,
    and the HF_HUB_OFFLINE=1 retranscribe.py's import sets for the process is put back afterwards."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    offline = os.environ.get("HF_HUB_OFFLINE")
    path = Path(__file__).resolve().parent.parent / "tools" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = offline
    return module


def test_retranscribe_takes_the_lyrics_switch_too(monkeypatch, tmp_path):
    """The measuring tool drives process() like the server, so a cache can be measured both ways."""
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    tool = load_tool()
    assert tool.server is server
    assert tool.parse_args([VIDEO, "--out", str(tmp_path / "x")]).lyrics == "auto"
    assert tool.parse_args([VIDEO, "--out", str(tmp_path / "x"), "--lyrics", "off"]).lyrics == "off"
    with pytest.raises(SystemExit):
        tool.parse_args([VIDEO, "--out", str(tmp_path / "x"), "--lyrics", "maybe"])


def test_retranscribe_writes_a_record_of_the_cache_shape(monkeypatch, tmp_path):
    """Its .new.cues.json carries the "lyrics" key and the rest of what save_cache() writes, so
    put in the cache directory it is a record made under the rule, not one to migrate."""
    import json

    tool = load_tool()
    (tmp_path / "cache").mkdir()
    app = make_app(monkeypatch, tmp_path / "cache")
    s = server.Session(video_id=VIDEO, url="", duration=60.0, title="OP")
    s.cues = [{"start": 1.0, "end": 4.0, "text": "これは", "seg": 0, "id": 0}]
    s.covered, s.speech = [[0.0, 60.0]], [[0.8, 4.2]]
    tool.write_results(app, s, tmp_path / "out")
    record = json.loads((tmp_path / "out" / f"{VIDEO}.new.cues.json").read_text(encoding="utf-8"))
    app.save_cache(s)
    assert record == json.loads((tmp_path / "cache" / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert record["lyrics"] == "auto" and record["format"] == server.CACHE_FORMAT
    assert json.loads((tmp_path / "out" / f"{VIDEO}.speech.json").read_text(encoding="utf-8")) == [[0.8, 4.2]]

    (tmp_path / "cache" / f"{VIDEO}.cues.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    reloaded = server.Session(video_id=VIDEO, url="u")
    app.load_cache(reloaded)
    assert reloaded.covered == [[0.0, 60.0]] and reloaded.status == "ready"  # nothing offered again


def test_replay_cues_takes_a_lyrics_window_through_the_lyrics_gates():
    """The A/B rig's half without a GPU: a record dump_words.py marked `lyrics` builds its cues on
    the spans of the segments that pass lyrics_reason(), as process() does, and reports those
    spans as the window's speech; without the mark the same segments are gated against the
    record's (empty) speech list and every one is "vad"."""
    replay = load_tool("replay_cues.py", "shisuko_replay_cues")
    assert replay.server is server

    def dump(text, start, end):
        s = line(text, start, end)
        return {"start": start, "end": end, "text": text, "avg_logprob": -0.3, "no_speech_prob": 0.1,
                "compression_ratio": 1.2,
                "words": [{"w": w.word, "s": w.start, "e": w.end, "p": w.probability} for w in s.words]}

    record = {"window": [100.0, 140.0], "speech": [], "lyrics": True,
              "segments": [dump("夜空を見上げて", 101.0, 103.5), dump("君の名前を呼ぶ", 106.0, 108.5),
                           dump("ご視聴ありがとうございました", 120.0, 122.0)]}
    cues, speech, drops = replay.build([record], server.CueLimits())
    assert [c["text"] for c in cues] == ["夜空を見上げて", "君の名前を呼ぶ"]
    assert [c["seg"] for c in cues] == [0, 1]
    assert close(speech, [[101.0 - PAD, 103.5 + PAD], [106.0 - PAD, 108.5 + PAD]])
    assert {k: v for k, v in drops.items() if k != "_text"} == {"blocklist": 1}

    talk = dict(record)
    del talk["lyrics"]
    cues, speech, drops = replay.build([talk], server.CueLimits())
    assert cues == [] and speech == []
    assert {k: v for k, v in drops.items() if k != "_text"} == {"vad": 2, "blocklist": 1}


class RigModel(FakeModel):
    """FakeModel that answers the scripted segments on every call and one verdict per detection."""

    def __init__(self, segs, verdicts):
        super().__init__(segs, heard=[[0.0, 1.0]])
        self.verdicts = list(verdicts)

    def detect_language(self, audio=None, **kwargs):
        self.detections += 1
        self.judged.append(audio)
        language, probability = self.verdicts.pop(0)
        return language, probability, []


def fake_faster_whisper(monkeypatch, model, audio):
    """faster_whisper in sys.modules for dump_words.main(): WhisperModel() is `model`, decode_audio() `audio`."""
    import types

    package = types.ModuleType("faster_whisper")
    package.WhisperModel = lambda *args, **kwargs: model
    package.audio = types.ModuleType("faster_whisper.audio")
    package.audio.decode_audio = lambda path, sampling_rate=RATE: audio
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", package.audio)


def test_dump_words_decides_the_lyrics_path_per_window_and_marks_the_record(monkeypatch, tmp_path):
    """The A/B rig's GPU half decides every window as process() does (wants_lyrics(), then the
    head), decodes a sung one without the detector and writes "lyrics" into its record, which
    replay_cues.py reads; --lyrics off sends every window through the detector."""
    import json

    tool = load_tool("dump_words.py", "shisuko_dump_words")
    assert tool.server is server
    cache, out = tmp_path / "cache", tmp_path / "out"
    cache.mkdir()
    (cache / f"{VIDEO}.webm").write_bytes(b"x")
    # Three loud windows: nothing heard and sung in Japanese; a line heard (talk); nothing heard
    # and, to the head, English.
    heard = {20.0: [[21.0, 25.0]]}
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: heard.get(offset, []))
    model = RigModel(SUNG, [("ja", 0.95), ("en", 0.9)])
    fake_faster_whisper(monkeypatch, model, tone(60.0))
    # The tool builds its model through server.load_model(), so --device auto would answer with
    # whatever backend this computer has: on an Apple Silicon Mac it would build a real
    # MlxWhisperModel and fetch the weights from Hugging Face instead of using the fake below.
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    argv = [VIDEO, "--cache", str(cache), "--out", str(out), "--to", "60", "--window", "20", "--model", "small"]

    assert tool.main(argv) == 0
    # load_model() decodes two seconds of silence before it returns, so the windows start at the
    # second call; the rig sees the same warm-up the server's own start pays for.
    warm_up, calls = model.calls[0], model.calls[1:]
    assert warm_up["vad_filter"] is False and warm_up["beam_size"] == 1
    assert [call["vad_filter"] for call in calls] == [False, True, True]
    assert "vad_parameters" not in calls[0]
    assert calls[1]["vad_parameters"] == server.VAD_PARAMS and calls[1]["word_timestamps"] is True
    assert model.detections == 2  # the talk window never reaches the head
    records = json.loads((out / f"{VIDEO}.words.json").read_text(encoding="utf-8"))
    assert [r["window"] for r in records] == [[0.0, 20.0], [20.0, 40.0], [40.0, 60.0]]
    assert [r["lyrics"] for r in records] == [True, False, False]
    assert [r["speech"] for r in records] == [[], [[21.0, 25.0]], []]
    assert [s["text"] for s in records[0]["segments"]] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert records[2]["segments"][0]["start"] == 41.0  # absolute seconds, as the talk records are

    replay = load_tool("replay_cues.py", "shisuko_replay_cues")
    cues, speech, _drops = replay.build(records[:1], server.CueLimits())
    assert [c["text"] for c in cues] == ["君の声が聞こえる夜に", "星を数えて眠る"]
    assert close(speech, SUNG_SPANS)

    model = RigModel(SUNG, [("ja", 0.95), ("ja", 0.95)])
    fake_faster_whisper(monkeypatch, model, tone(60.0))
    assert tool.main(argv + ["--lyrics", "off"]) == 0
    assert [call["vad_filter"] for call in model.calls[1:]] == [True, True, True]  # past the warm-up again
    assert model.detections == 0
    records = json.loads((out / f"{VIDEO}.words.json").read_text(encoding="utf-8"))
    assert [r["lyrics"] for r in records] == [False, False, False]
    with pytest.raises(SystemExit):
        tool.parse_args(argv + ["--lyrics", "maybe"])

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


def spoken(loud: float, total: float) -> np.ndarray:
    """A tone for the first `loud` seconds, silence after: speech with nothing loud beside it."""
    return np.concatenate([tone(loud), silence(total - loud)])


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
    # Two words stretched over eight seconds: faster-whisper's own anomaly score, applied
    # unconditionally since there is no VAD overlap to excuse it.
    ws = words([("あいうえお", 0.0, 4.0), ("かきくけこ", 4.0, 8.0)])
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
    """The same words through the talk path, on intervals shaped like Silero's (a pad each side).

    With the sentence rule off on both sides: the claim under test is that lyrics_spans() hands the
    cue builder what the detector would have handed it, which is about geometry. The rule itself is
    a talk rule and never runs on a sung window (a padded breath inside a line is not a pause).
    """
    speech = server.merge_intervals([[a - PAD, b + PAD] for a, b in spoken])
    cues, _ = server.build_window_cues(segs, 0.0, speech, server.CueLimits(sentence_ends=False), 0, {}, 40.0)
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


def test_a_sung_window_gets_no_sentence_marks():
    # The rule is a talk rule: the spans of a sung window are padded word runs, so every breath
    # reads as a pause, and 会いたくて震える + a breath would take a 。 in the middle of the line.
    segs = [line("会いたくて震える", 1.0, 3.0), line("夜空を見上げて", 3.6, 5.6)]
    assert lyrics_cues(segs) == [(1.0, 6.3, "会いたくて震える\n夜空を見上げて")]
    spans = server.lyrics_spans(segs, 0.0)
    talk, _ = server.build_window_cues(segs, 0.0, spans, server.CueLimits(), 0, {}, 40.0)
    assert [c["text"] for c in talk] == ["会いたくて震える。\n夜空を見上げて"]  # the same window, judged as talk


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


def test_a_lyrics_window_is_decoded_without_the_prompt(monkeypatch, tmp_path):
    # The lyrics gates were measured on unprompted decodes and the blocklist holds no sentence of
    # the prompt, so a noisy window the language head lets through could echo the prompt into the
    # cache with nothing to catch it. A talk window keeps it.
    prompt = server.DEFAULT_PROMPTS["ja"]
    worker, model = make_worker(monkeypatch, tmp_path, initial_prompt=prompt)
    worker.process(session(tone(40.0)), 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is False and model.calls[0]["initial_prompt"] is None

    worker, model = make_worker(monkeypatch, tmp_path, initial_prompt=prompt, speech=[[1.0, 6.5]])
    worker.process(session(spoken(6.5, 40.0)), 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is True and model.calls[0]["initial_prompt"] == prompt


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
    s = session(spoken(3.0, 40.0))
    worker.process(s, 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is True
    assert "vad_parameters" in model.calls[0]
    assert s.speech == [[0.5, 3.0]]
    assert model.detections == 0


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


def test_the_song_after_the_mc_line_is_decoded_with_it(monkeypatch, tmp_path, caplog):
    # A 歌枠: the singer announces the song (two seconds of detected speech), then sings for thirty
    # seconds the detector hears nothing of. The window is talk to wants_lyrics(), but its loud
    # unheard stretch is sung Japanese to the head, so the window is decoded whole, prompt and all,
    # with the stretch counted as speech: one window, nothing left to plan again.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    prompt = server.DEFAULT_PROMPTS["ja"]
    worker, model = timeline_worker(monkeypatch, tmp_path, UTAWAKU, heard=[[0.5, 2.5]], window=40.0,
                                    initial_prompt=prompt, language_patience=0.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert model.detections == 1
    assert model.calls[0]["vad_filter"] is False and "vad_parameters" not in model.calls[0]
    assert model.calls[0]["initial_prompt"] == prompt
    assert sung_text(s.cues) == "".join(x.text for x in UTAWAKU).replace("ます", "ます。", 1)
    assert close(s.covered, [[0.0, 40.0]])
    assert close(s.speech, [[0.5, 40.0]])
    assert any("38 s the detector missed, decoded whole" in r.getMessage() for r in caplog.records)


def test_singing_before_the_mc_line_and_a_bridge_between_two_lines_are_decoded_with_the_talk(monkeypatch, tmp_path):
    # The other transitions: singing at the window's start, talk after it, and a sung bridge
    # between two spoken lines. Every loud unheard stretch goes to the head together, once.
    heard = [[20.5, 22.5], [30.5, 32.5]]
    sung = ["君の声が聞こえる夜に", "星を数えて眠るまで", "夜空を見上げて", "遠く離れても"]
    segs = [line(text, 0.5 + 5.0 * i, 4.5 + 5.0 * i) for i, text in enumerate(sung)] + [
        line("次の曲いきます", 20.5, 22.5), line("会いたくて震える", 25.0, 28.0), line("ありがとう", 30.5, 32.5)]
    worker, model = timeline_worker(monkeypatch, tmp_path, segs, heard=heard, window=36.0, language_patience=0.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 36.0)
    assert model.detections == 1 and len(model.calls) == 1
    assert sung_text(s.cues) == "".join(sung) + "次の曲いきます。会いたくて震える。ありがとう"
    assert close(s.speech, [[0.0, 32.5]])  # 32.5-36 is under LYRICS_MIN_STRETCH_S
    assert close(s.covered, [[0.0, 36.0]])


def test_a_late_onset_line_reaches_the_decoder_whole(monkeypatch, tmp_path):
    # The case this path was built for: a quietly mixed video whose talk measures 0.016 rms, with
    # shrieks at 0.019 the detector hears nothing of, under LYRICS_MIN_RMS. The detector catches the
    # next line a second late. The window is decoded whole, so the line keeps its head.
    talk_amp = 0.016 * math.sqrt(2)
    audio = np.concatenate([tone(4.0, talk_amp), tone(9.0, 0.019 * math.sqrt(2)), tone(4.0, talk_amp), silence(23.0)])
    segs = [line("こんにちは", 0.5, 3.5), line("やだやだやだ", 5.0, 8.0),
            line("ほんとは葉っぱを相手に向けて飛ばす技", 11.0, 16.0)]
    worker, model = timeline_worker(monkeypatch, tmp_path, segs, heard=[[0.5, 3.5], [12.0, 16.5]], window=40.0)
    s = session(np.concatenate([audio, silence(40.0)]))
    worker.process(s, 0.0, 40.0)
    assert model.calls[0]["vad_filter"] is False
    texts = [c["text"] for c in s.cues]
    assert any(t.startswith("やだやだやだ") for t in texts) and any(t.startswith("ほんとは") for t in texts)
    assert close(s.covered, [[0.0, 40.0]])


def test_a_sign_off_in_a_rescued_stretch_answers_to_the_lyrics_gates_and_the_song_comes_from_the_retry(
        monkeypatch, tmp_path, caplog):
    # The stretch counts as speech, so the talk gates would keep a sign-off the prompted decode
    # wrote over the music; the decoder's own confidence judges it there instead, as on the lyrics
    # path. The song the prompted decode skipped comes from the decode without the prompt, in the
    # same window. The music after it, to the window's end, holds nothing: it goes to the next
    # window, which hears nothing there either and covers it for good.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    song = [line(text, 6.0 + 3.0 * i, 8.5 + 3.0 * i) for i, text in enumerate(["君の声が聞こえる夜に", "星を数えて眠るまで"])]
    worker, model = timeline_worker(monkeypatch, tmp_path, [UTAWAKU[0]] + song, heard=[[0.5, 2.5]], window=40.0,
                                    initial_prompt=server.DEFAULT_PROMPTS["ja"])
    first = [seg(UTAWAKU[0].text, UTAWAKU[0].words), line("ご視聴ありがとうございました", 20.0, 22.0)]
    decode = model.transcribe
    model.transcribe = lambda audio, **kw: (model.calls.append(kw), (iter(first), None))[1] if not model.calls \
        else decode(audio, **kw)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert len(model.calls) == 2 and model.calls[1]["initial_prompt"] is None
    assert sung_text(s.cues) == "はい次の曲いきます。君の声が聞こえる夜に星を数えて眠るまで"
    assert any("blocklist:1" in r.getMessage() for r in caplog.records)
    given_back = s.covered[-1][1]
    assert close(s.covered, [[0.0, given_back]]) and given_back < 13.0
    worker.process(s, given_back, given_back + 40.0)
    assert close(s.covered, [[0.0, given_back + 40.0]])


def test_a_vouched_stretch_the_decode_wrote_nothing_in_stays_covered_inside_the_window(monkeypatch, tmp_path):
    # The stretch had a whole window of context; planning it again alone would decode a few
    # seconds padded to thirty, where Whisper invents a sign-off. Only a stretch at the window's
    # end goes back to the planner.
    segs = [line("はい次の曲いきます", 0.5, 2.5), line("ありがとう", 30.5, 32.5)]
    worker, model = timeline_worker(monkeypatch, tmp_path, segs, heard=[[0.5, 2.5], [30.5, 32.5]], window=36.0,
                                    language_patience=0.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 36.0)
    assert model.calls[0]["vad_filter"] is False
    assert close(s.covered, [[0.0, 36.0]])


def test_a_short_or_quiet_unheard_stretch_stays_covered(monkeypatch, tmp_path):
    # Under LYRICS_MIN_STRETCH_S a stretch is a pause with music under it more often than a line,
    # and a silent one costs nothing to cover: neither is put to the head.
    segs = [line("はい次の曲いきます", 0.5, 2.5), line("会いたくて震える", 3.0, 5.5), line("次いきます", 6.5, 8.5)]
    worker, model = timeline_worker(monkeypatch, tmp_path, segs, heard=[[0.5, 2.5], [6.0, 8.5]], window=12.0,
                                    language_patience=0.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 12.0)
    assert [c["text"] for c in s.cues] == ["はい次の曲いきます。", "次いきます"]
    assert model.detections == 0
    assert close(s.covered, [[0.0, 12.0]])  # 2.5-6.0 and 8.5-12 are under the floor

    worker, model = timeline_worker(monkeypatch, tmp_path, segs[:1], heard=[[0.5, 2.5]], window=40.0,
                                    language_patience=0.0)
    s = session(np.concatenate([tone(2.5), silence(77.5)]))
    worker.process(s, 0.0, 40.0)
    assert model.detections == 0
    assert close(s.covered, [[0.0, 40.0]])  # 2.5-40 s is silence


def test_a_window_the_head_refused_is_covered_whole(monkeypatch, tmp_path):
    # A loud stretch the head hears no Japanese in stays with the detector, and the window is
    # covered whole: nothing of it is planned again.
    worker, model = timeline_worker(monkeypatch, tmp_path, UTAWAKU, heard=[[0.5, 2.5]], window=40.0,
                                    language_patience=0.0)
    model.verdict = ("en", 0.99)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    # 37.5 s of stretch: the head hears its first thirty seconds, then, refused, its last thirty.
    assert model.detections == 2 and model.calls[0]["vad_filter"] is True
    assert [c["text"] for c in s.cues] == ["はい次の曲いきます"]
    assert close(s.covered, [[0.0, 40.0]])

    worker, model = timeline_worker(monkeypatch, tmp_path, UTAWAKU, heard=[[0.5, 2.5]], window=40.0, lyrics="off",
                                    language_patience=0.0)
    s = session(tone(80.0))
    worker.process(s, 0.0, 40.0)
    assert model.detections == 0
    assert [c["text"] for c in s.cues] == ["はい次の曲いきます"]
    assert close(s.covered, [[0.0, 40.0]])


def test_unsung_stretches_are_the_loud_unheard_parts_of_a_window():
    audio = np.concatenate([tone(10.0), silence(10.0), tone(20.0)])
    speech = [[9.5, 10.5], [19.5, 20.5]]
    assert close(server.unsung_stretches(audio, 0.0, 40.0, speech), [[0.0, 9.5], [20.5, 40.0]])  # 10.5-19.5 is silent
    assert close(server.unsung_stretches(audio, 0.0, 40.0, speech, min_seconds=12.0), [[20.5, 40.0]])
    assert server.unsung_stretches(audio, 0.0, 40.0, [[0.0, 40.0]]) == []


def test_loud_is_measured_against_the_window_s_own_speech():
    # A quietly mixed video: talk at 0.016 rms, a missed line at 0.019. Under the absolute floor
    # (0.02) it was silence; next to talk this quiet it is loud. Beside ordinary talk (0.2) the
    # floor stays LYRICS_MIN_RMS, and next to near-silent talk it never sinks below LYRICS_QUIET_RMS.
    def window(talk_rms, stretch_rms):
        return np.concatenate([tone(10.0, talk_rms * math.sqrt(2)), tone(10.0, stretch_rms * math.sqrt(2))])
    speech = [[0.0, 10.0]]
    assert close(server.unsung_stretches(window(0.016, 0.019), 0.0, 20.0, speech), [[10.0, 20.0]])
    assert server.unsung_stretches(window(0.2, 0.019), 0.0, 20.0, speech) == []
    assert server.unsung_stretches(window(0.001, 0.003), 0.0, 20.0, speech) == []


def test_vouched_segments_judge_what_lies_in_a_rescued_stretch_by_the_lyrics_gates():
    stretches = [[5.0, 15.0]]
    speech = [[0.0, 15.0]]
    kept = line("会いたくて震える", 6.0, 8.0)
    sign_off = line("ご視聴ありがとうございました", 10.0, 12.0)
    unsure = line("それられ", 12.0, 13.0, logprob=-1.2)
    outside = line("ご視聴ありがとうございました", 1.0, 3.0)  # heard by the detector: the talk gates' call
    drops: dict = {}
    assert server.vouched_segments([kept, sign_off, unsure, outside], 0.0, speech, stretches, drops) == [kept, outside]
    assert {k: v for k, v in drops.items() if not k.startswith("_")} == {"blocklist": 1, "unsure": 1}


def test_a_song_cut_by_the_window_end_carries_its_verdict_into_the_next_window(monkeypatch, tmp_path, caplog):
    # The MC line, then a song the window's end cuts at 30 s. The head vouched for 2.5-30; the
    # next window's stretch starting at 30 is the same song carrying on, so it is decoded whole
    # without a second ask, which on a 12 s tail of a real song scored 0.62 and lost two lines.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    song = [line(text, 6.0 + 4.0 * i, 9.0 + 4.0 * i) for i, text in enumerate([
        "君の声が聞こえる夜に", "星を数えて眠るまで", "会いたくて震える", "夜空を見上げて", "遠く離れても",
        "同じ月を見てる", "この歌が届くなら", "もう一度だけ", "君の名前を呼ぶ", "夜が明けるまで", "ありがとう"])]
    worker, model = timeline_worker(monkeypatch, tmp_path, [UTAWAKU[0]] + song + [line("次いきます", 50.5, 52.5)],
                                    heard=[[0.5, 2.5], [50.5, 52.5]], window=30.0, language_patience=0.0)
    s = session(np.concatenate([tone(52.5), silence(27.5)]))
    worker.process(s, 0.0, 30.0)
    assert model.detections == 1 and model.calls[0]["vad_filter"] is False
    start = s.covered[-1][1]
    model.verdict = ("ja", 0.62)  # what the tail alone would get: refused, were it asked
    worker.process(s, start, start + 30.0)
    assert model.detections == 1
    assert model.calls[-1]["vad_filter"] is False
    assert sung_text(s.cues).endswith("夜が明けるまでありがとう次いきます")
    assert any("not asked again" in r.getMessage() for r in caplog.records)


def test_only_a_stretch_carrying_on_the_speech_before_it_skips_the_head(monkeypatch, tmp_path):
    def run(first_verdict, heard):
        worker, model = timeline_worker(monkeypatch, tmp_path, [], heard=heard, window=30.0, language_patience=0.0)
        model.verdict = first_verdict
        s = session(tone(80.0))
        worker.process(s, 0.0, 30.0)
        model.verdict = ("ja", 0.62)
        worker.process(s, 30.0, 60.0)
        return model

    # Refused before, after detected speech running up to the seam: asked again, refused again.
    model = run(("en", 0.99), [[0.5, 2.5], [20.0, 30.0], [50.5, 52.5]])
    assert model.detections == 2 and model.calls[-1]["vad_filter"] is True
    model = run(("ja", 0.95), [[0.5, 2.5], [30.0, 32.0]])  # speech at the seam: a stretch of its own
    assert model.detections == 2 and model.calls[-1]["vad_filter"] is True


def test_loudness_floor_is_half_the_talk_held_between_the_quiet_and_the_absolute_floors():
    assert server.loudness_floor(0.0) == server.LYRICS_MIN_RMS        # nothing known yet
    assert server.loudness_floor(0.2) == server.LYRICS_MIN_RMS
    assert server.loudness_floor(0.04) == server.LYRICS_MIN_RMS       # ordinary talk keeps the absolute floor
    assert server.loudness_floor(0.016) == pytest.approx(0.008)
    assert server.loudness_floor(0.001) == server.LYRICS_QUIET_RMS


def test_a_quiet_video_s_whole_song_window_is_measured_against_its_talk(monkeypatch, tmp_path):
    # A quietly mixed video: talk at 0.016 rms, a song at 0.012, under LYRICS_MIN_RMS. A window
    # holding the song alone has no speech of its own to measure it against; the talk heard
    # earlier in the video sets its floor. Nothing heard yet, or loud talk, keeps the absolute one.
    def song_window(talk_rms):
        worker, model = timeline_worker(monkeypatch, tmp_path, SUNG, heard=[[0.0, 10.0]] if talk_rms else [],
                                        window=20.0, language_patience=0.0)
        talk = tone(20.0, talk_rms * math.sqrt(2)) if talk_rms else silence(20.0)
        s = session(np.concatenate([talk, tone(20.0, 0.012 * math.sqrt(2))]))
        if talk_rms:
            worker.process(s, 0.0, 20.0)
        model.heard = []
        model.offset = 20.0
        lyrics, _ = worker.choose_path(s, s.audio[20 * RATE:], [], 20.0, 40.0)
        return lyrics, s.talk_rms

    lyrics, level = song_window(0.016)
    assert lyrics is True and level == pytest.approx(0.016, rel=0.01)
    assert song_window(0.2)[0] is False
    assert song_window(0.0) == (False, 0.0)


def test_wants_lyrics_takes_the_floor_it_is_given():
    quiet = tone(20.0, 0.012 * math.sqrt(2))
    args = SimpleNamespace(lyrics="auto")
    assert server.wants_lyrics(args, quiet, [], 0.0, 20.0) is False
    assert server.wants_lyrics(args, quiet, [], 0.0, 20.0, floor=0.008) is True


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

    # Speech heard, below the watch's LANGUAGE_MIN_SPEECH but at least a second, and nothing loud
    # beside it: talk, unjudged.
    worker, model = make_worker(monkeypatch, tmp_path, speech=[[0.5, 2.0]])
    worker.process(session(spoken(2.0, 40.0)), 0.0, 20.0)
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
    """A record of the current format; `extra` carries the "lyrics" rule, or another format."""
    import json

    data = {"video_id": VIDEO, "title": "OP", "duration": duration, "format": server.CACHE_FORMAT,
            "model": "large-v3", "language": "ja", "cues": cues, "covered": covered, "speech": speech,
            "language_state": {"foreign_seconds": 0.0, "heard": None, "paused": False}}
    data.update(extra)
    (tmp_path / f"{VIDEO}.cues.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_a_format_3_record_is_dropped_by_the_format_check(monkeypatch, tmp_path, caplog):
    # 0.11.x wrote format 3. The cue geometry has changed since (sentence marks, the row boundary
    # at one, the anomaly gate), and a bumped format is the only way a change reaches a video
    # someone has already watched: the record goes whole, the title stays, the video is
    # transcribed again. With the key or without it - format 4 is what is read, not "lyrics".
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    for extra in ({}, {"lyrics": "auto"}, {"lyrics": "off"}):
        write_record(tmp_path, cues=[{"start": 1.0, "end": 4.0, "text": "これは", "seg": 0}],
                     covered=[[0.0, 34.04]], speech=[[0.8, 4.0]], format=3, **extra)
        s = server.Session(video_id=VIDEO, url="u")
        app.load_cache(s)
        assert s.title == "OP"
        assert s.cues == [] and s.covered == [] and s.speech == [], extra
        assert s.status == "pending"
    assert not any("loaded" in r.getMessage() and "cached cues" in r.getMessage() for r in caplog.records)


def test_a_lyrics_off_talk_record_gives_back_its_long_holes_and_its_ends_but_not_its_pauses(monkeypatch, tmp_path, caplog):
    # A talk record written with the switch off: an intro, lines with pauses of a few seconds
    # between them (holes of 2.5 and 3.5 s, under LYRICS_MIN_STRETCH_S), a ten-second bridge and
    # an outro. The pauses stay covered, or every talk video would be fetched again and walked a
    # window per pause; the cues and the speech are kept whatever is given back.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    cues = [{"start": 3.0, "end": 10.0, "text": "a", "seg": 0}, {"start": 12.5, "end": 20.0, "text": "b", "seg": 1},
            {"start": 23.5, "end": 30.0, "text": "c", "seg": 2}, {"start": 40.0, "end": 50.0, "text": "d", "seg": 3}]
    write_record(tmp_path, cues=cues, covered=[[0.0, 60.0]], duration=60.0, lyrics="off",
                 speech=[[3.2, 9.5], [12.7, 19.5], [23.7, 29.5], [40.2, 49.5]])
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert [c["text"] for c in s.cues] == ["a", "b", "c", "d"]
    assert [c["seg"] for c in s.cues] == [0, 1, 2, 3] and s.seg_next == 4
    assert s.speech == [[3.2, 9.5], [12.7, 19.5], [23.7, 29.5], [40.2, 49.5]]
    assert s.covered == [[3.0, 30.0], [40.0, 50.0]]  # the intro, the bridge and the outro are offered again
    assert s.status == "pending"
    assert any("23 s were covered with --lyrics off" in r.getMessage() for r in caplog.records)


def test_a_format_2_record_is_dropped_by_the_format_check(monkeypatch, tmp_path, caplog):
    # 0.11.0 and 0.11.1 wrote format 2. Like every other older cache it goes whole, title kept.
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[{"start": 1.0, "end": 4.0, "text": "これは", "seg": 0}], covered=[[0.0, 34.04]],
                 speech=[[0.8, 4.0]], format=2)
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.title == "OP"
    assert s.cues == [] and s.covered == [] and s.speech == []
    assert s.status == "pending"
    assert not any("loaded" in r.getMessage() and "cached cues" in r.getMessage() for r in caplog.records)


def test_a_record_written_under_the_rule_loads_untouched(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[], lyrics="auto")
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.covered == [[0.0, 34.04]]
    assert s.status == "ready"


def test_a_record_written_with_lyrics_off_is_offered_again_under_auto(monkeypatch, tmp_path):
    # --lyrics off covers a sung window with nothing in it; taking the switch off must not leave
    # the video blank for ever, so those stretches go back to the planner.
    app = make_app(monkeypatch, tmp_path)
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[], lyrics="off")
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.covered == [] and s.status == "pending"


def test_lyrics_off_loads_a_blank_record_untouched(monkeypatch, tmp_path):
    # Nothing is offered again while the switch is off: it is the switch that covered it.
    app = make_app(monkeypatch, tmp_path, lyrics="off")
    write_record(tmp_path, cues=[], covered=[[0.0, 34.04]], speech=[], lyrics="off")
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
    # Every record carries the rule and the current format; an older one is dropped whole.
    assert data["format"] == server.CACHE_FORMAT == 6
    assert data["unheard_rule"] == server.UNHEARD_RULE  # made under the rule: never rechecked

    reloaded = server.Session(video_id=VIDEO, url="u")
    app.load_cache(reloaded)
    assert reloaded.covered == [[0.0, 34.04]]
    assert reloaded.status == "ready"

    off = make_app(monkeypatch, tmp_path, lyrics="off")
    off.save_cache(s)
    assert json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))["lyrics"] == "off"


# --------------------------------------------------------------------------- records from before the unheard rule

TALK = 0.016 * math.sqrt(2)  # a quietly mixed video's talk, 0.016 rms
SHRIEK = 0.019 * math.sqrt(2)  # its missed line, under LYRICS_MIN_RMS but loud next to the talk


def quiet_video(shriek: float = SHRIEK) -> np.ndarray:
    """0-6 s talk, 6-16 s a line the detector missed, 16-22 s talk, then 18 s of silence."""
    return np.concatenate([tone(6.0, TALK), tone(10.0, shriek), tone(6.0, TALK), silence(18.0)])


QUIET_SPEECH = [[0.0, 6.0], [16.0, 22.0]]
QUIET_CUES = [{"start": 0.2, "end": 5.8, "text": "こんにちは", "seg": 0},
              {"start": 16.2, "end": 21.8, "text": "ほんとは葉っぱを相手に", "seg": 1}]


def test_unheard_ranges_reach_into_the_talk_around_each_stretch():
    # With the talk beside it, the 10 s stretch is judged against that talk, as its window would.
    assert server.unheard_ranges([[0.0, 40.0]], QUIET_SPEECH, QUIET_CUES) == [
        (2.0, 20.0, [[2.0, 6.0], [16.0, 20.0]]), (18.0, 40.0, [[18.0, 22.0]])]
    assert server.unheard_ranges([[0.0, 15.0]], [[0.0, 6.0], [9.0, 15.0]], []) == []  # a 3 s pause


def sweeper(monkeypatch, tmp_path, verdict=("ja", 0.95), audio=None, on_disk=True, **record):
    """An app whose cache holds one unmarked quiet record and, with `on_disk`, its audio (`audio`,
    the quiet video by default); its decodes are counted in `app.decodes`."""
    worker, model = make_worker(monkeypatch, tmp_path, segs=[], verdict=verdict)
    app = worker.app
    audio = quiet_video() if audio is None else audio
    app.decodes = []

    def decode(src, a, b, rate):
        app.decodes.append((a, b))
        return (audio[int(a * RATE):int(b * RATE)] * 32768.0).astype(np.int16)

    monkeypatch.setattr(server, "_decode_range", decode)
    if on_disk:
        (tmp_path / f"{VIDEO}.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    write_record(tmp_path, **{"cues": QUIET_CUES, "covered": [[0.0, 40.0]], "speech": QUIET_SPEECH,
                              "duration": 40.0, "lyrics": "auto", **record})
    return app, model


def record_file(tmp_path):
    return tmp_path / f"{VIDEO}.cues.json"


def stored(tmp_path) -> dict:
    import json

    return json.loads(record_file(tmp_path).read_text(encoding="utf-8"))


def sweep(app) -> int:
    """Run the sweep to its end; the idle steps it took."""
    steps = 0
    for _ in app.old_record_sweep():
        steps += 1
    return steps


def test_a_record_the_new_rule_rescues_speech_in_is_removed_to_be_transcribed_again(monkeypatch, tmp_path):
    app, model = sweeper(monkeypatch, tmp_path)
    assert sweep(app) == 2  # one range, then the verdict: no step longer than one range
    assert model.detections == 1 and app.decodes == [(2.0, 20.0)]  # the first range that rescues ends it
    assert not record_file(tmp_path).exists()
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.cues == [] and s.status == "pending"  # the next open transcribes it from the disk audio


def test_a_record_with_nothing_to_rescue_is_marked_and_never_rechecked(monkeypatch, tmp_path):
    # The head hears no Japanese in the loud stretch (music, say), or the stretch is silent and
    # never reaches the head: the record is right as it is.
    for verdict, shriek, heads in ((("en", 0.99), SHRIEK, 1), (("ja", 0.95), 0.0, 0)):
        app, model = sweeper(monkeypatch, tmp_path, verdict=verdict, audio=quiet_video(shriek))
        before = stored(tmp_path)
        sweep(app)
        assert model.detections == heads and len(app.decodes) == 2
        assert stored(tmp_path) == {**before, "unheard_rule": server.UNHEARD_RULE}
        assert sweep(app) == 0 and len(app.decodes) == 2  # a marked record is never read again


def test_a_record_without_an_unheard_stretch_is_marked_without_its_audio(monkeypatch, tmp_path):
    app, model = sweeper(monkeypatch, tmp_path, on_disk=False, covered=[[0.0, 22.0]],
                         speech=[[0.0, 6.0], [6.0, 22.0]], duration=22.0)
    sweep(app)
    assert stored(tmp_path)["unheard_rule"] == server.UNHEARD_RULE and app.decodes == []


def test_records_the_sweep_leaves_as_they_are(monkeypatch, tmp_path):
    # No audio on disk (a recheck never downloads), another rule, another model, a pause.
    for extra in ({"on_disk": False}, {"lyrics": "off"}, {"model": "small"}, {"language": "en"},
                  {"language_state": {"foreign_seconds": 90.0, "heard": "en", "paused": True}}):
        app, model = sweeper(monkeypatch, tmp_path, **extra)
        before = record_file(tmp_path).read_bytes()
        sweep(app)
        assert record_file(tmp_path).read_bytes() == before and model.detections == 0, extra
        record_file(tmp_path).unlink()
        (tmp_path / f"{VIDEO}.webm").unlink(missing_ok=True)
    app, model = sweeper(monkeypatch, tmp_path, lyrics="off")
    off = make_app(monkeypatch, tmp_path, lyrics="off")
    before = record_file(tmp_path).read_bytes()
    sweep(off)
    assert record_file(tmp_path).read_bytes() == before


def test_a_record_a_session_opens_meanwhile_is_left_to_it(monkeypatch, tmp_path):
    app, model = sweeper(monkeypatch, tmp_path)
    before = record_file(tmp_path).read_bytes()
    steps = app.old_record_sweep()
    next(steps)  # the range is judged: this record would be removed
    s = app.get_session(VIDEO, "u")  # a viewer opens the video, which loads the record
    assert list(steps) == []
    assert record_file(tmp_path).read_bytes() == before
    assert s.sung == [] and s.talk_rms == 0.0  # the sweep's verdicts stay in its own probe session


def test_only_the_server_s_own_app_sweeps(monkeypatch, tmp_path):
    app, _ = sweeper(monkeypatch, tmp_path)
    assert next(app.sweep, False) is False  # an App the tools and tests make sweeps nothing
    import inspect
    assert inspect.getsource(server.start_app).count("sweep=True") == 2  # both of the server's own


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
    # Three loud windows: nothing heard and sung in Japanese; a line heard beside a loud stretch
    # sung in Japanese (decoded whole); nothing heard and, to the head, English.
    heard = {20.0: [[21.0, 25.0]]}
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: heard.get(offset, []))
    model = RigModel(SUNG, [("ja", 0.95), ("ja", 0.95), ("en", 0.9)])
    fake_faster_whisper(monkeypatch, model, tone(60.0))
    # The tool builds its model through server.load_model(), so --device auto would answer with
    # whatever backend this computer has: on an Apple Silicon Mac it would build a real
    # MlxWhisperModel and fetch the weights from Hugging Face instead of using the fake below.
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    argv = [VIDEO, "--cache", str(cache), "--out", str(out), "--to", "60", "--window", "20", "--model", "small"]

    assert tool.main(argv) == 0
    # load_model() decodes two seconds of silence before it returns, so the windows start at the
    # second call; the rig sees the same warm-up the server's own start pays for, and that decode
    # carries no prompt of its own.
    warm_up, calls = model.calls[0], model.calls[1:]
    assert warm_up["vad_filter"] is False and warm_up["beam_size"] == 1
    assert "initial_prompt" not in warm_up
    # The rescued window's prompted decode leaves the stretch's end unreached, so it is decoded
    # once more without the prompt, as process() would.
    assert [call["vad_filter"] for call in calls] == [False, False, False, True]
    # The dump is only an A/B on the server's own decode if it resolves the prompt as parse_args()
    # does and withholds it from a lyrics window as process() does.
    assert tool.parse_args(argv).initial_prompt == server.DEFAULT_PROMPTS["ja"]
    assert tool.parse_args(argv + ["--initial-prompt", ""]).initial_prompt == ""
    assert tool.parse_args(argv + ["--language", "en"]).initial_prompt == ""
    assert [call["initial_prompt"] for call in calls] == [None, server.DEFAULT_PROMPTS["ja"], None,
                                                          server.DEFAULT_PROMPTS["ja"]]
    assert "vad_parameters" not in calls[0] and "vad_parameters" not in calls[1]
    assert calls[3]["vad_parameters"] == server.VAD_PARAMS and calls[1]["word_timestamps"] is True
    assert model.detections == 3
    records = json.loads((out / f"{VIDEO}.words.json").read_text(encoding="utf-8"))
    assert [r["window"] for r in records] == [[0.0, 20.0], [20.0, 40.0], [40.0, 60.0]]
    assert [r["lyrics"] for r in records] == [True, False, False]
    assert [r["speech"] for r in records] == [[], [[21.0, 40.0]], []]  # the rescued stretch counts
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

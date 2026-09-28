"""The prompt-skip retry: a talk window whose prompted decode jumped past detected speech is
decoded once more without the prompt, and the segments that fill the skip are spliced in.

No model is loaded; the fake below answers one script with the prompt and another without it,
and detect_speech is replaced by what the test says Silero heard.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RATE = server.SAMPLE_RATE
PROMPT = server.DEFAULT_PROMPTS["ja"]


def talk(text: str, start: float, end: float, prob: float = 0.9):
    """One spoken segment whose words share [start, end) evenly, two characters a word."""
    pieces = [text[i:i + 2] for i in range(0, len(text), 2)]
    step = (end - start) / len(pieces)
    ws = [server.Word(p, start + i * step, start + (i + 1) * step, prob) for i, p in enumerate(pieces)]
    return SimpleNamespace(text=text, start=start, end=end, words=ws, no_speech_prob=0.1,
                           avg_logprob=-0.2, compression_ratio=1.2)


# --------------------------------------------------------------------------- the pure skip finder

def test_a_skip_at_the_start_of_the_window_is_found():
    speech = [[1.0, 8.0], [10.0, 14.0]]
    assert server.skipped_speech([[10.0, 14.0]], speech) == [[1.0, 8.0, pytest.approx(7.0)]]


def test_a_skip_in_the_middle_runs_across_the_pauses_of_what_it_skipped():
    speech = [[1.0, 3.0], [5.0, 7.0], [7.5, 9.0], [9.4, 11.0], [13.0, 15.0]]
    found = server.skipped_speech([[1.0, 3.0], [13.0, 15.0]], speech)
    assert found == [[5.0, 11.0, pytest.approx(5.1)]]


def test_a_kept_segment_between_two_unreached_pieces_splits_them():
    speech = [[0.0, 2.5], [4.0, 5.0], [6.5, 9.0]]
    # Each side holds 2.5 s: neither reaches the floor on its own, and the kept line keeps them apart.
    assert server.skipped_speech([[4.0, 5.0]], speech) == []


def test_a_stretch_under_three_seconds_is_left_alone():
    assert server.skipped_speech([[5.0, 9.0]], [[1.0, 3.9], [5.0, 9.0]]) == []
    assert server.skipped_speech([[5.0, 9.0]], [[1.0, 4.0], [5.0, 9.0]]) != []  # 3 s is enough, the slack aside


def test_the_slack_lets_a_kept_segment_reach_speech_half_a_second_away():
    speech = [[1.0, 10.0]]
    # A kept line ending 0.4 s before the stretch reaches into it by 0.1 s; 8.9 s stay skipped.
    assert server.skipped_speech([[0.0, 0.6]], speech) == [[pytest.approx(1.1), 10.0, pytest.approx(8.9)]]
    assert server.skipped_speech([[0.0, 0.6]], speech, slack=0.0) == [[1.0, 10.0, pytest.approx(9.0)]]
    # Kept lines half a second from each other leave no piece at all.
    assert server.skipped_speech([[1.0, 4.0], [4.9, 7.5], [8.4, 10.0]], [[1.0, 10.0]]) == []


def test_a_gated_segment_does_not_count_as_covering():
    speech = [[1.0, 18.9]]
    # A line the detector heard almost nothing under: the VAD gate drops it, so it covers nothing.
    stretched = SimpleNamespace(text="言い返す!", start=1.24, end=18.87, no_speech_prob=0.1, avg_logprob=-0.4,
                                compression_ratio=1.0,
                                words=[server.Word("言い", 1.24, 4.77, 0.5), server.Word("返", 4.77, 18.47, 0.3),
                                       server.Word("す!", 18.47, 18.87, 0.4)])
    words, reason = server.gate_segment(stretched, 0.0, [[0.0, 0.5]])
    assert reason is not None
    assert server.kept_spans([stretched], 0.0, [[0.0, 0.5]]) == []
    assert server.skipped_speech(server.kept_spans([stretched], 0.0, [[0.0, 0.5]]), speech) != []
    # A line with no speech under it at all is refused the same way.
    ghost = talk("ご視聴ありがとうございました", 12.0, 14.0)
    assert server.kept_spans([ghost], 0.0, [[1.0, 8.0]]) == []
    assert server.skipped_speech(server.kept_spans([ghost], 0.0, [[1.0, 8.0]]), [[1.0, 8.0]]) == [[1.0, 8.0, 7.0]]


JC_SPEECH = [[1.24, 2.12], [5.27, 15.75], [15.93, 20.0]]
# A prompted first window as dump_words.py recorded it: the gates keep it (the detector
# heard most of its span), and its 返 runs over thirteen seconds of speech it never decoded.
JC_PROMPTED = SimpleNamespace(text="言い返す!", start=1.24, end=18.87, no_speech_prob=0.1, avg_logprob=-0.4,
                              compression_ratio=1.0,
                              words=[server.Word("言", 1.24, 1.48, 0.44), server.Word("い", 1.48, 1.62, 0.97),
                                     server.Word("返", 4.77, 18.47, 0.93), server.Word("す!", 18.65, 18.87, 0.85)])


def test_a_word_stretched_over_speech_covers_nothing_though_the_gates_keep_it():
    assert server.gate_segment(JC_PROMPTED, 0.0, JC_SPEECH)[1] is None
    assert server.kept_spans([JC_PROMPTED], 0.0, JC_SPEECH) == []
    found = server.skipped_speech([], JC_SPEECH)
    assert [g[:2] for g in found] == [[1.24, 20.0]]


def test_a_last_word_stretched_over_silence_still_covers():
    seg = talk("今日はいい天気ですね", 1.0, 3.0)
    seg.words[-1] = server.Word(seg.words[-1].word, seg.words[-1].start, 7.0, 0.9)  # over the pause after it
    assert server.kept_spans([seg], 0.0, [[1.0, 3.2], [7.5, 8.0]]) != []


def test_kept_spans_are_on_the_videos_clock():
    seg = talk("今日はいい天気ですね", 1.0, 3.0)
    assert server.kept_spans([seg], 100.0, [[101.0, 103.0]]) == [[pytest.approx(101.0), pytest.approx(103.0)]]


# --------------------------------------------------------------------------- Transcriber.process

class PromptModel:
    """Answers `prompted` when asked with an initial prompt and `plain` without one; records every call."""

    def __init__(self, prompted, plain):
        self.prompted, self.plain = list(prompted), list(plain)
        self.calls: list = []

    def detect_language(self, audio=None, **kwargs):
        return "ja", 0.99, []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(list(self.prompted if kwargs.get("initial_prompt") else self.plain)), None


def make_worker(monkeypatch, tmp_path, prompted, plain, speech, **args_overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    heard = [list(iv) for iv in speech]
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: [[offset + a, offset + b] for a, b in heard])
    base = dict(first_window=20.0, window=20.0, lookahead=900.0, model="large-v3", language="ja",
                language_patience=0.0, beam_size=1, initial_prompt=PROMPT, idle_minutes=30, client_timeout=30.0,
                lyrics="auto")
    base.update(args_overrides)
    model = PromptModel(prompted, plain)
    app = server.App(SimpleNamespace(**base), model=model, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return server.Transcriber(app), model


def session(seconds: float = 40.0, amplitude: float = 0.0):
    audio = np.full(int(seconds * RATE), amplitude, dtype=np.float32)
    return server.Session(video_id=VIDEO, url="u", status="ready", audio=audio, duration=seconds)


SPEECH = [[1.0, 4.2], [4.5, 8.0], [10.0, 14.0]]
LATE = talk("それでは始めましょうか", 10.0, 14.0)         # the prompted decode opens here, 9 s late
OPENING = [talk("こんにちは皆さん元気ですか", 1.0, 4.2), talk("今日もよろしくお願いします", 4.5, 8.0)]
LATE_TWIN = talk("それじゃあ始めましょうか", 10.0, 14.0)  # the unprompted decode of the same line


def text_of(cues) -> str:
    return "".join(c["text"] for c in cues)


def test_a_skip_is_decoded_again_without_the_prompt_and_spliced_in(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="shisu-ko")
    worker, model = make_worker(monkeypatch, tmp_path, [LATE], OPENING + [LATE_TWIN], SPEECH)
    s = session()
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 2
    assert model.calls[0]["initial_prompt"] == PROMPT and model.calls[1]["initial_prompt"] is None
    # Everything else identical.
    assert {k: v for k, v in model.calls[1].items() if k != "initial_prompt"} == \
        {k: v for k, v in model.calls[0].items() if k != "initial_prompt"}
    text = text_of(s.cues)
    assert "こんにちは" in text and "よろしくお願いします" in text
    assert "それでは始めましょうか" in text and "それじゃあ" not in text  # the prompted line stays
    assert s.cues[0]["start"] <= 1.0
    assert [c["start"] for c in s.cues] == sorted(c["start"] for c in s.cues)
    assert any("[7 s skipped with the prompt, 2 segments from a decode without it]" in r.getMessage()
               for r in caplog.records)


def test_a_line_stretched_over_the_window_is_replaced_by_the_unprompted_decode(monkeypatch, tmp_path):
    clean = [talk("言い返すなよお前", 1.24, 2.1), talk("だってさあそれはないでしょう", 5.3, 9.0),
             talk("いやいや本当にそうなんだって", 9.5, 15.7), talk("まあそうかもしれないけど", 16.0, 18.5)]
    worker, model = make_worker(monkeypatch, tmp_path, [JC_PROMPTED], clean, JC_SPEECH)
    s = session()
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 2
    text = text_of(s.cues)
    for seg in clean:
        assert seg.text[:4] in text
    assert "返す!" not in text and not any(c["text"] in ("返", "す!") for c in s.cues)
    assert all(c["end"] - c["start"] < 8.0 for c in s.cues)


def test_no_skip_means_one_decode(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="shisu-ko")
    worker, model = make_worker(monkeypatch, tmp_path, OPENING + [LATE], [], SPEECH)
    s = session()
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 1
    assert "こんにちは" in text_of(s.cues)
    assert not any("skipped with the prompt" in r.getMessage() for r in caplog.records)


def test_a_lyrics_window_is_never_retried(monkeypatch, tmp_path):
    # Nothing heard in a loud window: the lyrics path, decoded unprompted once, whatever it holds.
    worker, model = make_worker(monkeypatch, tmp_path, [], [talk("君の声が聞こえる夜に", 12.0, 15.0)], [])
    worker.process(session(amplitude=0.3), 0.0, 20.0)
    assert len(model.calls) == 1 and model.calls[0]["vad_filter"] is False


def test_an_empty_prompt_is_never_retried(monkeypatch, tmp_path):
    worker, model = make_worker(monkeypatch, tmp_path, [LATE], [LATE], SPEECH, initial_prompt="")
    worker.process(session(), 0.0, 20.0)
    assert len(model.calls) == 1 and model.calls[0]["initial_prompt"] is None


def test_a_retry_that_finds_nothing_keeps_the_prompted_result(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="shisu-ko")
    # Music, say: the unprompted decode skips the same stretch.
    worker, model = make_worker(monkeypatch, tmp_path, [LATE], [LATE_TWIN], SPEECH)
    s = session()
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 2  # one retry, never a second
    assert text_of(s.cues).startswith("それでは始めましょうか")
    assert any("[7 s skipped with the prompt, 0 segments from a decode without it]" in r.getMessage()
               for r in caplog.records)
    assert s.covered == [[0.0, 20.0]]


def test_an_unprompted_segment_overlapping_a_kept_one_is_not_added(monkeypatch, tmp_path):
    # Its midpoint lies in the skip, but it runs a second into the kept prompted line.
    straddling = talk("今日もよろしくお願いしますそれでは", 4.5, 11.0)
    worker, model = make_worker(monkeypatch, tmp_path, [LATE], [OPENING[0], straddling], SPEECH)
    s = session()
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 2
    text = text_of(s.cues)
    assert "こんにちは" in text and "よろしく" not in text


def test_splice_segments_takes_only_what_fills_a_stretch():
    speech = [[1.0, 8.0], [10.0, 14.0], [16.0, 18.0]]
    stretches = [[1.0, 8.0, 7.0]]
    far = talk("あとで話しましょうね", 16.0, 18.0)  # kept, but outside every stretch
    spliced, added = server.splice_segments([LATE], OPENING + [far], 0.0, speech, stretches)
    assert added == 2
    assert [seg.text for seg in spliced] == [OPENING[0].text, OPENING[1].text, LATE.text]


# --------------------------------------------------------------------------- dump_words.py

def test_dump_words_retries_as_process_does_and_marks_the_record(monkeypatch, tmp_path):
    from test_lyrics import fake_faster_whisper, load_tool

    tool = load_tool("dump_words.py", "shisuko_dump_words")
    cache, out = tmp_path / "cache", tmp_path / "out"
    cache.mkdir()
    (cache / f"{VIDEO}.webm").write_bytes(b"x")
    heard = {0.0: SPEECH, 20.0: [[30.0, 34.0]]}
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: heard.get(offset, []))
    # Window two hears its speech at 10-14 of the window, where the prompted decode puts LATE.
    model = PromptModel([LATE], OPENING + [LATE_TWIN])
    fake_faster_whisper(monkeypatch, model, np.zeros(40 * RATE, dtype=np.float32))
    # The tool builds its model through server.load_model(), so --device auto would answer with
    # whatever backend this computer has: on an Apple Silicon Mac it would build a real
    # MlxWhisperModel and fetch the weights from Hugging Face instead of using the fake above.
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    argv = [VIDEO, "--cache", str(cache), "--out", str(out), "--to", "40", "--window", "20", "--model", "small"]
    assert tool.main(argv) == 0
    # load_model() decodes two seconds of silence before it returns, so the windows start at the
    # second call, and that warm-up carries no prompt.
    warm_up, calls = model.calls[0], model.calls[1:]
    assert "initial_prompt" not in warm_up
    assert [c["initial_prompt"] for c in calls] == [PROMPT, None, PROMPT]
    records = json.loads((out / f"{VIDEO}.words.json").read_text(encoding="utf-8"))
    assert [r["prompt_retry"] for r in records] == [True, False]
    assert [s["text"] for s in records[0]["segments"]] == [OPENING[0].text, OPENING[1].text, LATE.text]
    assert records[0]["segments"][0]["start"] == 1.0

    replay = load_tool("replay_cues.py", "shisuko_replay_cues")
    cues, _speech, _drops = replay.build(records[:1], server.CueLimits())
    assert "こんにちは" in text_of(cues) and "それでは始めましょうか" in text_of(cues)

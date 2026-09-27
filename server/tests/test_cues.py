"""Unit tests for the cue pipeline: VAD helpers, hallucination gates and build_cues.

The rules under test are the ones written down in docs/subtitle-quality.md, sections
P0.1 (speech intervals), P0.2 (hallucination gates) and P1 (cue geometry). Nothing here
touches a model, the GPU or the network: synthetic word lists in, cues out.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()
W = server.Word


def limits(**overrides):
    return server.CueLimits(**overrides)


def words(spec, prob: float = 0.9):
    """[("text", start, end), ...] -> [Word]."""
    return [W(text, start, end, prob) for text, start, end in spec]


def seg(text: str, start: float, end: float, ws=None, compression: float = 1.0):
    return SimpleNamespace(text=text, start=start, end=end, words=ws, compression_ratio=compression)


# --------------------------------------------------------------------------- interval helpers

def test_interval_overlap_counts_only_the_covered_part():
    assert server.interval_overlap(0.0, 4.0, [[1.0, 2.0], [3.0, 10.0]]) == pytest.approx(2.0)


def test_speech_ratio_of_a_cue_fully_inside_speech_is_one():
    assert server.speech_ratio(1.0, 2.0, [[0.0, 5.0]]) == pytest.approx(1.0)


def test_distance_to_speech_is_zero_without_intervals():
    # No VAD data must never make the trimmer eat every word.
    assert server.distance_to_speech(99.0, []) == 0.0


def test_distance_to_speech_measures_the_nearest_edge():
    assert server.distance_to_speech(6.0, [[0.0, 5.0], [8.0, 9.0]]) == pytest.approx(1.0)


def test_next_silence_starts_at_the_end_of_the_containing_interval():
    assert server.next_silence(2.0, [[0.0, 3.0], [5.0, 6.0]]) == (3.0, 5.0)


def test_next_silence_is_unbounded_when_no_speech_follows():
    start, end = server.next_silence(4.0, [[0.0, 3.0]])
    assert (start, end) == (4.0, float("inf"))


def test_silence_around_reports_zero_for_unknown_neighbours():
    # At a window edge there is no interval before, which must not read as isolation.
    assert server.silence_around(1.0, 2.0, [[0.9, 2.1]]) == (0.0, 0.0)


def test_silence_around_measures_from_the_utterance_own_interval():
    # The cue sits on its own speech island: the silence is measured from that island outwards.
    before, after = server.silence_around(5.0, 6.0, [[0.0, 1.0], [5.0, 6.0], [10.0, 11.0]])
    assert (before, after) == (4.0, 4.0)


def test_silence_around_of_an_isolated_cue():
    before, after = server.silence_around(5.0, 6.0, [[0.0, 1.0], [10.0, 11.0]])
    assert (before, after) == (4.0, 4.0)


def test_nearest_onset_ignores_a_cue_deep_inside_an_interval():
    assert server.nearest_onset([[0.0, 30.0]], 12.0, reach=0.6) is None
    assert server.nearest_onset([[0.0, 30.0]], 0.4, reach=0.6) == 0.0


# --------------------------------------------------------------------------- VAD API pin

def test_faster_whisper_vad_api_is_still_there():
    # faster_whisper.vad is not in the package __all__; this is the canary for an upgrade.
    pytest.importorskip("faster_whisper")
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: F401

    options = VadOptions(**server.VAD_PARAMS, max_speech_duration_s=server.VAD_MAX_SPEECH_SECONDS)
    assert options.min_speech_duration_ms == 250
    assert options.neg_threshold == 0.35


def test_vad_parameters_returns_a_fresh_dict_each_time():
    # transcribe() pops keys out of the dict it is handed.
    a = server.vad_parameters()
    a.pop("threshold")
    assert "threshold" in server.vad_parameters()


# --------------------------------------------------------------------------- hallucination gates

def test_gate_drops_a_segment_without_words():
    assert server.hallucination_reason(seg("テキスト", 0.0, 1.0), [], [[0.0, 1.0]]) == "empty"


def test_gate_drops_punctuation_only_text():
    ws = words([("...", 0.0, 1.0)])
    assert server.hallucination_reason(seg("...", 0.0, 1.0), ws, [[0.0, 1.0]]) == "empty"


def test_gate_keeps_a_normal_segment_on_speech():
    ws = words([("これは", 0.0, 0.5), ("テストです", 0.5, 1.2)])
    assert server.hallucination_reason(seg("これはテストです", 0.0, 1.2), ws, [[0.0, 1.5]]) is None


def test_vad_gate_drops_a_short_segment_off_speech():
    ws = words([("あっ", 10.0, 10.6)])
    assert server.hallucination_reason(seg("あっ", 10.0, 10.6), ws, [[0.0, 5.0]]) == "vad"


def test_vad_gate_keeps_a_long_confident_segment_off_speech():
    ws = words([("これはかなり", 10.0, 10.8), ("長い文章です", 10.8, 11.6)], prob=0.8)
    assert server.hallucination_reason(seg("これはかなり長い文章です", 10.0, 11.6), ws, [[0.0, 5.0]]) is None


def test_vad_gate_drops_a_long_segment_off_speech_when_the_model_is_unsure():
    ws = words([("これはかなり", 10.0, 10.8), ("長い文章です", 10.8, 11.6)], prob=0.2)
    assert server.hallucination_reason(seg("これはかなり長い文章です", 10.0, 11.6), ws, [[0.0, 5.0]]) == "vad"


def test_anomaly_gate_drops_improbable_words_with_partial_speech_overlap():
    ws = words([("あ", 0.0, 0.05), ("い", 0.05, 0.10), ("う", 0.10, 0.15), ("えおかきくけ", 0.15, 0.20)], prob=0.05)
    # Half the span is speech, so the VAD gate passes it on the length/probability exception path.
    assert server.hallucination_reason(seg("あいうえおかきくけ", 0.0, 0.2), ws, [[0.0, 0.1]]) in ("anomaly", "vad")
    ws_long = words([("あいうえおかきくけこ", 0.0, 0.05), ("さしすせそたちつてと", 0.05, 0.10)], prob=0.05)
    assert server.hallucination_reason(seg("あいうえおかきくけこさしすせそたちつてと", 0.0, 0.1), ws_long, [[0.0, 0.08]]) == "anomaly"


def test_anomaly_gate_keeps_an_anomalous_segment_that_sits_on_speech():
    ws = words([("あいうえおかきくけこ", 0.0, 0.05), ("さしすせそたちつてと", 0.05, 0.10)], prob=0.05)
    assert server.hallucination_reason(seg("あいうえおかきくけこさしすせそたちつてと", 0.0, 0.1), ws, [[0.0, 1.0]]) is None


def test_the_talk_gate_keeps_a_segment_of_one_kana_words():
    # Whisper's Japanese words are sub-tokens, usually one kana, so they are shorter than the
    # 133 ms the short-word term penalises whatever the speaker did. Scoring on it deleted five
    # real utterances in 15 minutes and no hallucination, so the talk path does not.
    ws = words([(kana, 0.1 * i, 0.1 * i + 0.08) for i, kana in enumerate("いちおうたんにんの")])
    text = "".join(w.word for w in ws)
    assert server.is_segment_anomaly(ws) is True                      # with the term, as ported
    assert server.is_segment_anomaly(ws, short_term=False) is False
    # Overlap 0.6: past the VAD gate, and under the 0.8 that would excuse the segment unasked.
    assert server.hallucination_reason(seg(text, 0.0, 1.5), ws, [[0.0, 0.53]]) is None


def test_the_talk_gate_still_drops_improbable_words():
    ws = words([(kana, 0.1 * i, 0.1 * i + 0.08) for i, kana in enumerate("いちおうたんにんの")], prob=0.05)
    text = "".join(w.word for w in ws)
    assert server.is_segment_anomaly(ws, short_term=False) is True
    assert server.hallucination_reason(seg(text, 0.0, 1.5), ws, [[0.0, 0.53]]) == "anomaly"


def test_the_lyrics_gate_keeps_the_short_word_term():
    # Its thresholds were measured with it, and nothing here re-measures them.
    ws = words([(kana, 0.1 * i, 0.1 * i + 0.08) for i, kana in enumerate("いちおうたんにんの")])
    line = SimpleNamespace(text="".join(w.word for w in ws), start=0.0, end=1.5, words=ws,
                           compression_ratio=1.0, avg_logprob=-0.2, no_speech_prob=0.1)
    assert server.lyrics_reason(line, ws) == "anomaly"


def test_is_segment_anomaly_ignores_punctuation_only_words():
    assert server.is_segment_anomaly(words([("。", 0.0, 0.01)])) is False


def test_repetition_gate_drops_a_looping_segment():
    text = "ありがとうございますありがとうございますありがとうございます"
    ws = words([(text, 0.0, 4.0)])
    assert server.hallucination_reason(seg(text, 0.0, 4.0), ws, [[0.0, 4.0]]) == "repetition"


def test_repetition_gate_keeps_ordinary_text():
    text = "今日は天気がいいので散歩に行きます"
    ws = words([(text, 0.0, 4.0)])
    assert server.hallucination_reason(seg(text, 0.0, 4.0), ws, [[0.0, 4.0]]) is None


def test_repetition_gate_uses_the_compression_ratio_too():
    text = "".join(f"これは{i}番目のテストの文章です。" for i in range(14))  # no exact run, still very compressible
    ws = words([(text, 0.0, 8.0)])
    assert not server.has_repetition(text)
    assert server.compression_ratio(text) > server.COMPRESSION_LIMIT
    assert server.hallucination_reason(seg(text, 0.0, 8.0), ws, [[0.0, 8.0]]) == "repetition"


def test_repetition_gate_ignores_the_chunk_wide_compression_ratio():
    # faster-whisper gives every segment of a 30 s decode the same compression_ratio, so a loop
    # elsewhere in the chunk must not delete this line.
    text = "はいかしこまりました"
    ws = words([(text, 0.0, 1.5)])
    assert server.hallucination_reason(seg(text, 0.0, 1.5, compression=3.4), ws, [[0.0, 1.6]]) is None


def test_has_repetition_keeps_japanese_backchannels():
    for ok in ("そうそう", "そうそうそう", "そうそうそうそう", "違う違う違う", "シューシューシュー"):
        assert not server.has_repetition(ok), ok


def test_has_repetition_catches_long_runs_and_long_loops():
    assert server.has_repetition("ああああああ")  # six repeats
    assert server.has_repetition("おはようございますおはようございますおはようございます")  # three, but 30 chars


def test_blocklist_drops_a_sign_off_isolated_by_silence():
    text = "ご視聴ありがとうございました"
    ws = words([(text, 10.0, 12.0)])
    speech = [[0.0, 5.0], [10.0, 12.0], [20.0, 25.0]]
    assert server.hallucination_reason(seg(text, 10.0, 12.0), ws, speech) == "blocklist"


def test_blocklist_keeps_a_sign_off_in_the_middle_of_speech():
    text = "ご視聴ありがとうございました"
    ws = words([(text, 10.0, 12.0)])
    speech = [[8.0, 14.0]]
    assert server.hallucination_reason(seg(text, 10.0, 12.0), ws, speech) is None


# --------------------------------------------------------------------------- build_cues

def test_build_cues_trims_a_stretched_leading_word():
    # Whisper stretched "えー" back into a silence; its midpoint is far outside speech.
    ws = words([("えー", 0.0, 2.0), ("こんにちは", 5.0, 5.8), ("。", 5.8, 5.9)])
    speech = [[4.9, 6.2]]
    cues = server.build_cues(ws, speech, limits())
    assert len(cues) == 1
    assert cues[0]["text"] == "こんにちは。"
    assert cues[0]["start"] == pytest.approx(4.9, abs=0.1)


def test_build_cues_keeps_edge_words_that_sit_on_speech():
    ws = words([("あの", 5.0, 5.3), ("こんにちは", 5.3, 5.9)])
    cues = server.build_cues(ws, [[4.9, 6.0]], limits())
    assert [c["text"] for c in cues] == ["あのこんにちは"]


def test_build_cues_splits_on_a_pause_that_is_real_silence():
    ws = words([("こんにちは", 0.0, 1.0), ("さようなら", 3.0, 4.0)])
    speech = [[0.0, 1.1], [2.9, 4.1]]
    cues = server.build_cues(ws, speech, limits())
    assert [c["text"] for c in cues] == ["こんにちは", "さようなら"]


def test_build_cues_does_not_split_a_pause_that_is_still_speech():
    # A 0.5 s inter-word gap that Silero calls speech (a drawn-out vowel) is not a cue break.
    ws = words([("こんにちは", 0.0, 1.0), ("さようなら", 1.5, 2.4)])
    cues = server.build_cues(ws, [[0.0, 2.5]], limits())
    assert [c["text"] for c in cues] == ["こんにちはさようなら"]


def test_build_cues_snaps_the_start_to_the_speech_onset():
    ws = words([("こんにちは", 5.4, 6.0), ("。", 6.0, 6.1)])
    cues = server.build_cues(ws, [[5.0, 6.3]], limits())
    assert cues[0]["start"] == pytest.approx(5.30)  # onset 5.0 + the 0.30 s cap


def test_build_cues_leaves_a_mid_interval_cue_where_it_is():
    ws = words([("あいうえおかきくけこさしすせそ", 10.0, 12.0), ("。", 12.0, 12.1)])
    cues = server.build_cues(ws, [[0.0, 30.0]], limits())
    assert cues[0]["start"] == pytest.approx(10.0)


def test_build_cues_adds_a_lead_out_into_the_following_silence():
    ws = words([("あいうえおかきくけこ", 0.0, 2.0), ("。", 2.0, 2.1)])
    cues = server.build_cues(ws, [[0.0, 2.2], [9.0, 10.0]], limits())
    assert cues[0]["end"] == pytest.approx(2.1 + limits().lead_out)


def test_build_cues_does_not_lead_out_into_the_next_utterance():
    ws = words([("あいうえおかきくけこ", 0.0, 2.0), ("。", 2.0, 2.1)])
    cues = server.build_cues(ws, [[0.0, 2.2], [2.35, 5.0]], limits())
    assert cues[0]["end"] == pytest.approx(2.1)  # the silence is only 0.15 s


def test_build_cues_extends_a_short_cue_to_the_minimum_duration():
    ws = words([("はい", 0.0, 0.3), ("。", 0.3, 0.35)])
    cues = server.build_cues(ws, [[0.0, 0.4], [9.0, 10.0]], limits())
    # The lead-out alone already carries it past min_seconds; both floors are satisfied.
    assert cues[0]["end"] - cues[0]["start"] == pytest.approx(0.35 + limits().lead_out, abs=0.06)
    assert cues[0]["end"] - cues[0]["start"] >= limits().min_seconds


def test_build_cues_merges_a_short_cue_into_its_neighbour():
    ws = words([("えっと", 0.0, 0.3), ("、", 0.3, 0.35), ("そうですね", 0.6, 1.4), ("。", 1.4, 1.5)])
    cues = server.build_cues(ws, [[0.0, 1.6]], limits())
    assert [c["text"] for c in cues] == ["えっと、そうですね。"]


def test_build_cues_never_glues_two_sentences_onto_one_row():
    # A sentence mark is a hard row boundary: the two get a row each however short they are. Two
    # speakers on one row is what a mined card would otherwise quote.
    ws = words([("はい", 0.0, 0.3), ("。", 0.3, 0.35), ("そうですね", 0.6, 1.4), ("。", 1.4, 1.5)])
    cues = server.build_cues(ws, [[0.0, 1.6]], limits())
    assert [c["text"] for c in cues] == ["はい。\nそうですね。"]


def test_build_cues_puts_two_readable_sentences_on_two_rows():
    ws = words([("今日はいい天気", 0.0, 1.0), ("。", 1.0, 1.05),
                ("電車で行きます", 1.2, 2.0), ("。", 2.0, 2.05)])
    cues = server.build_cues(ws, [[0.0, 2.1]], limits())
    assert [c["text"] for c in cues] == ["今日はいい天気。\n電車で行きます。"]


def test_merge_adjacent_counts_the_rows_and_not_the_newline_between_them():
    # merge_segments() measures len(text) - newlines; merge_adjacent() counted the newline as a
    # character, so a cue that already had a row lost one from its budget at every later merge.
    prev = {"start": 0.0, "end": 1.0, "text": "今日はいい天気。\n電車で行き", "seg": 0}
    cue_ = {"start": 1.1, "end": 2.0, "text": "ます", "seg": 0}
    out = server.merge_adjacent([dict(prev), dict(cue_)], limits(max_chars=15), 0.35, only_short=False)
    assert [c["text"] for c in out] == ["今日はいい天気。\n電車で行きます"]  # 15 characters on two rows
    out = server.merge_adjacent([dict(prev), dict(cue_)], limits(max_chars=14), 0.35, only_short=False)
    assert len(out) == 2


def test_build_cues_does_not_merge_past_the_character_limit():
    ws = words([("あいうえおかきくけこさしすせそ", 0.0, 2.0), ("。", 2.0, 2.1),
                ("たちつてとなにぬねのはひふへほ", 2.2, 4.0), ("。", 4.0, 4.1)])
    cues = server.build_cues(ws, [[0.0, 4.2]], limits(max_chars=20))
    assert len(cues) == 2


def test_build_cues_closes_a_blink_sized_gap():
    ws = words([("あいうえおかきくけこ", 0.0, 2.0), ("。", 2.0, 2.05),
                ("たちつてとなにぬねの", 2.4, 4.4), ("。", 4.4, 4.45)])
    cues = server.build_cues(ws, [[0.0, 2.1], [2.35, 4.5]], limits(max_chars=12, merge_gap=0.0))
    assert len(cues) == 2
    assert cues[1]["start"] - cues[0]["end"] == pytest.approx(0.10)


def test_build_cues_breaks_at_the_character_limit_on_a_clause_boundary():
    ws = words([("あいうえおかきくけこ", 0.0, 2.0), ("、", 2.0, 2.1), ("さしすせそたちつてと", 2.1, 4.0),
                ("。", 4.0, 4.1)])
    cues = server.build_cues(ws, [[0.0, 4.2]], limits(max_chars=16, max_seconds=100.0))
    assert [c["text"] for c in cues] == ["あいうえおかきくけこ、", "さしすせそたちつてと。"]


def test_build_cues_breaks_at_the_duration_limit():
    ws = words([("あいうえおかきくけ", 0.0, 4.0), ("こさしすせそたちつ", 4.0, 8.0)])
    cues = server.build_cues(ws, [[0.0, 8.2]], limits(max_seconds=3.0, max_chars=100))
    assert [c["text"] for c in cues] == ["あいうえおかきくけ", "こさしすせそたちつ"]


def test_build_cues_never_returns_a_cue_over_the_duration_limit():
    ws = words([(f"語{i}", float(i), float(i) + 1.0) for i in range(20)])
    cues = server.build_cues(ws, [[0.0, 20.5]], limits())
    assert cues
    assert max(c["end"] - c["start"] for c in cues) <= limits().max_seconds


def test_build_cues_on_empty_input():
    assert server.build_cues([], [[0.0, 1.0]], limits()) == []


# --------------------------------------------------------------------------- build_window_cues

def test_build_window_cues_stamps_one_segment_id_per_segment():
    ws_a = words([("あいうえおかきくけこさしすせそ", 0.0, 2.0), ("。", 2.0, 2.1),
                  ("たちつてとなにぬねのはひふへほ", 2.2, 4.0), ("。", 4.0, 4.1)])
    ws_b = words([("まみむめも", 6.0, 7.0), ("。", 7.0, 7.1)])
    segs = [seg("".join(w.word for w in ws_a), 0.0, 4.1, ws_a),
            seg("".join(w.word for w in ws_b), 6.0, 7.1, ws_b)]
    cues, next_id = server.build_window_cues(segs, 0.0, [[0.0, 4.2], [5.9, 7.2]], limits(max_chars=20), 7)
    assert next_id == 9
    assert sorted({c["seg"] for c in cues}) == [7, 8]
    assert len([c for c in cues if c["seg"] == 7]) == 2  # split for display, one spoken sentence


def test_build_window_cues_uses_the_phrase_after_a_short_pause_token():
    ws = words([("今日はいい天気", 0.0, 1.0), ("電車", 4.0, 4.5),
                ("で", 4.5, 4.7), ("行きます", 4.7, 5.5)])
    segment = seg("今日はいい天気電車で行きます", 0.0, 5.5, ws)
    cues, _ = server.build_window_cues([segment], 0.0, [[0.0, 1.1], [3.95, 5.6]], limits(), 0)
    assert len(cues) == 2
    assert [cue["text"] for cue in cues] == ["今日はいい天気", "電車で行きます"]
    assert cues[0]["end"] < cues[1]["start"]


def test_build_window_cues_preserves_the_first_segment_id_after_a_later_merge():
    ws_a = words([("こんにちは", 0.0, 1.0)])
    ws_b = words([("電車で行きます", 1.2, 2.0), ("明日は晴れます", 4.0, 5.0),
                  ("散歩します", 7.0, 8.0)])
    segs = [seg("こんにちは", 0.0, 1.0, ws_a),
            seg("電車で行きます明日は晴れます散歩します", 1.2, 8.0, ws_b)]
    cues, _ = server.build_window_cues(segs, 0.0, [[0.0, 2.0], [4.0, 5.0], [7.0, 8.0]],
                                       limits(max_chars=18), 0)
    # ます before a two-second pause: punctuate_words() writes the mark Whisper left out.
    assert [cue["text"] for cue in cues] == ["こんにちは電車で行きます。", "明日は晴れます。\n散歩します"]
    assert [cue["seg"] for cue in cues] == [0, 0]


def test_build_window_cues_applies_the_window_offset():
    ws = words([("こんにちは", 1.0, 2.0), ("。", 2.0, 2.1)])
    segs = [seg("こんにちは。", 1.0, 2.1, ws)]
    cues, _ = server.build_window_cues(segs, 100.0, [[101.0, 102.2]], limits(), 0)
    assert cues[0]["start"] == pytest.approx(101.0, abs=0.1)


def test_build_window_cues_counts_what_each_gate_dropped():
    good = words([("こんにちは", 0.0, 1.0), ("。", 1.0, 1.1)])
    noise = words([("あ", 20.0, 20.4)])
    drops: dict = {}
    segs = [seg("こんにちは。", 0.0, 1.1, good), seg("あ", 20.0, 20.4, noise)]
    cues, next_id = server.build_window_cues(segs, 0.0, [[0.0, 1.2]], limits(), 0, drops)
    assert [c["text"] for c in cues] == ["こんにちは。"]
    assert drops["vad"] == 1
    assert next_id == 1


# --------------------------------------------------------------------------- dedup (P1.8)

def test_cue_overlaps_detects_a_reworded_boundary_segment():
    a = {"start": 10.0, "end": 12.0, "text": "これはテストです"}
    b = {"start": 10.2, "end": 12.1, "text": "これはテストだ"}
    assert server.cue_overlaps(a, b)


def test_cue_overlaps_is_false_for_neighbouring_cues():
    a = {"start": 10.0, "end": 12.0, "text": "あ"}
    b = {"start": 12.0, "end": 14.0, "text": "い"}
    assert not server.cue_overlaps(a, b)


def test_cue_overlaps_uses_the_shorter_cue():
    long_cue = {"start": 0.0, "end": 10.0, "text": "長い"}
    short_cue = {"start": 9.0, "end": 9.9, "text": "短い"}
    assert server.cue_overlaps(long_cue, short_cue)


def test_build_window_cues_keeps_the_lead_out_inside_the_window():
    ws = words([("あいうえおかきくけこ", 0.0, 2.0), ("。", 2.0, 2.1)])
    segs = [seg("あいうえおかきくけこ。", 0.0, 2.1, ws)]
    cues, _ = server.build_window_cues(segs, 0.0, [[0.0, 2.2]], limits(), 0, None, window_end=2.3)
    assert cues[0]["end"] == pytest.approx(2.3)  # 2.1 + LEAD_OUT would spill into the next window


def test_build_window_cues_window_clamp_respects_the_hard_minimum():
    ws = words([("はい", 2.0, 2.2), ("。", 2.2, 2.25)])
    segs = [seg("はい。", 2.0, 2.25, ws)]
    cues, _ = server.build_window_cues(segs, 0.0, [[1.9, 2.3]], limits(), 0, None, window_end=2.3)
    assert cues[0]["end"] - cues[0]["start"] >= 0.5


def test_build_window_cues_drops_a_cue_that_belongs_to_the_next_window():
    ws = words([("こんにちは", 9.0, 9.8), ("。", 9.8, 9.9)])
    segs = [seg("こんにちは。", 9.0, 9.9, ws)]
    cues, _ = server.build_window_cues(segs, 0.0, [[8.9, 10.0]], limits(), 0, None, window_end=8.0)
    assert cues == []


# --------------------------------------------------------------------------- minimum duration

def test_normalise_gaps_keeps_an_overlap_rather_than_flashing_a_cue():
    cues = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 0.3, "end": 2.0, "text": "b"}]
    server.normalise_gaps(cues, limits())
    assert cues[0]["end"] == pytest.approx(1.0)  # trimming to 0.2 s would be unreadable


def test_normalise_gaps_closes_an_overlap_when_there_is_room():
    cues = [{"start": 0.0, "end": 3.0, "text": "a"}, {"start": 2.0, "end": 4.0, "text": "b"}]
    server.normalise_gaps(cues, limits())
    assert cues[0]["end"] == pytest.approx(1.9)


def test_build_cues_never_emits_a_cue_under_the_hard_minimum():
    ws = words([("あ", 0.0, 0.1), ("。", 0.1, 0.12), ("いうえおかきくけこさ", 0.2, 2.0), ("。", 2.0, 2.1)])
    cues = server.build_cues(ws, [[0.0, 2.2]], limits(max_chars=6))
    assert min(c["end"] - c["start"] for c in cues) >= 0.5


# --------------------------------------------------------------------------- lead word repair

def timings(ws):
    return [(w.start, w.end) for w in ws]


def test_repair_slides_a_stranded_head_onto_the_next_utterance():
    # word[0] is anchored to the segment start, a whole speech interval behind the sentence it opens.
    ws = words([("言", 3.0, 4.0), ("ってた", 6.0, 6.8)])
    speech = [[2.9, 4.1], [5.5, 7.0]]
    server.repair_lead_words(ws, speech)
    assert ws[0].start == pytest.approx(5.5)  # the onset of word[1]'s interval
    assert ws[0].end == pytest.approx(6.0)    # flush against word[1]


def test_repair_leaves_the_head_alone_below_the_gap():
    ws = words([("言", 3.0, 4.0), ("ってた", 4.2, 5.0)])
    before = timings(ws)
    server.repair_lead_words(ws, [[2.9, 4.05], [4.15, 5.1]])
    assert timings(ws) == before  # 0.20 s is a pause, not the anchoring artifact


def test_repair_leaves_a_long_head_alone():
    ws = words([("あいうえおかき", 3.0, 4.0), ("ってた", 6.0, 6.8)])  # seven characters
    before = timings(ws)
    server.repair_lead_words(ws, [[2.9, 4.1], [5.5, 7.0]])
    assert timings(ws) == before


def test_repair_never_moves_the_head_backwards():
    # word[1]'s interval already began before the head: sliding to its onset would drop the head on
    # the previous utterance, where cue_overlaps() deletes a whole good cue.
    ws = words([("えー", 5.0, 5.3), ("こんにちは", 6.0, 6.5)])
    before = timings(ws)
    server.repair_lead_words(ws, [[4.5, 7.0]])
    assert timings(ws) == before


def test_repair_leaves_a_head_that_already_shares_the_interval():
    # Whisper stretched the head a hair past the onset, but it sits on the right utterance already.
    ws = words([("言", 4.45, 4.7), ("ってた", 6.0, 6.5)])
    before = timings(ws)
    server.repair_lead_words(ws, [[4.5, 7.0]])
    assert timings(ws) == before


def test_repair_never_moves_the_words_after_the_head():
    ws = words([("言", 3.0, 4.0), ("ってた", 6.0, 6.8), ("ね", 6.8, 7.0)])
    server.repair_lead_words(ws, [[2.9, 4.1], [5.5, 7.1]])
    assert timings(ws)[1:] == [(6.0, 6.8), (6.8, 7.0)]


def test_repair_returns_short_lists_and_unknown_speech_unchanged():
    single = words([("言", 3.0, 4.0)])
    assert server.repair_lead_words(single, [[2.9, 4.1]]) is single
    assert timings(single) == [(3.0, 4.0)]
    assert server.repair_lead_words([], [[0.0, 1.0]]) == []
    ws = words([("言", 3.0, 4.0), ("ってた", 6.0, 6.8)])
    server.repair_lead_words(ws, [])
    assert timings(ws) == [(3.0, 4.0), (6.0, 6.8)]


# --------------------------------------------------------------------------- may_break

def test_may_break_refuses_a_tail_opening_with_a_small_kana():
    assert server.may_break("なんか靴舐めますって言", "ってた") is False


def test_may_break_allows_a_break_after_a_sentence_end():
    # "ている" is both under MIN_PIECE_CHARS and particle-initial; the speaker's 。 outranks both.
    assert server.may_break("これはテストです。", "ている") is True


def test_may_break_checks_the_line_start_rule_before_the_sentence_end():
    assert server.may_break("これはテストです。", "って言った") is False


def test_may_break_refuses_a_piece_too_short_to_read():
    assert server.may_break("あい", "うえおか") is False
    assert server.may_break("あいうえお", "かき") is False


def test_may_break_allows_a_break_after_a_clause_break():
    assert server.may_break("そうですね、", "でもやっぱり") is True


def test_may_break_refuses_okurigana():
    assert server.may_break("これやばい、動", "いた動いた") is False


def test_may_break_allows_hiragana_after_katakana():
    assert server.may_break("私がこう自撮りカメラ", "こうやって配信します") is True


def test_may_break_refuses_a_tail_opening_with_a_particle():
    assert server.may_break("ここに来た", "のはなぜか") is False


def test_may_break_allows_an_ordinary_boundary():
    assert server.may_break("そうですね", "電車で行きます") is True


def test_may_break_allows_a_break_against_nothing():
    assert server.may_break("", "ってた") is True
    assert server.may_break("なんか靴", "") is True


# --------------------------------------------------------------------------- split_for_break

def test_split_for_break_takes_a_legal_clause_boundary():
    buf = words([("あいうえおかきくけこ", 0.0, 1.0), ("、", 1.0, 1.1), ("さしすせそ", 1.1, 2.0)])
    head, tail = server.split_for_break(buf, limits(), "たちつてと")
    assert (server.word_text(head), server.word_text(tail)) == ("あいうえおかきくけこ、", "さしすせそ")


def test_split_for_break_backs_off_an_illegal_clause_boundary():
    # split_at_clause() would cut before っていう, which may not open a line.
    buf = words([("これはちょっと", 0.0, 1.0), ("違うと思って", 1.0, 2.0), ("、", 2.0, 2.1),
                 ("っていう", 2.1, 2.8)])
    head, tail = server.split_for_break(buf, limits(), "こと")
    assert (server.word_text(head), server.word_text(tail)) == ("これはちょっと", "違うと思って、っていう")


def test_split_for_break_emits_the_whole_buffer_on_a_legal_seam():
    buf = words([("そうですね", 0.0, 1.0), ("電車で", 1.0, 2.0)])
    head, tail = server.split_for_break(buf, limits(), "行きます")
    assert head is buf and tail == []


def test_split_for_break_backs_off_an_illegal_seam_against_the_next_word():
    buf = words([("なんかくつ", 0.0, 1.0), ("舐めますって言", 1.0, 2.0)])
    head, tail = server.split_for_break(buf, limits(), "ってた")
    assert (server.word_text(head), server.word_text(tail)) == ("なんかくつ", "舐めますって言")


def test_joins_compound_between_two_kanji_or_two_katakana():
    assert server.joins_compound("できる能", "力って") is True
    assert server.joins_compound("自撮りカメ", "ラこうやって") is True
    assert server.joins_compound("できる", "能力って") is False
    assert server.joins_compound("カメラ", "能力") is False
    assert server.joins_compound("", "力って") is False


def test_may_split_refuses_a_compound_that_may_break_allows():
    assert server.may_break("言われたとおりできる能", "力って必要になる") is True
    assert server.may_split("言われたとおりできる能", "力って必要になる") is False
    assert server.may_split("私がこう自撮りカメ", "ラこうやって配信します") is False


def test_may_split_allows_a_clause_mark_and_a_kanji_after_hiragana():
    assert server.may_split("それは学校、", "行ったほうがいい") is True
    assert server.may_split("言われたとおりできる", "能力って必要になる") is True


def test_split_for_break_backs_off_a_seam_inside_a_kanji_compound():
    # may_break() would emit the buffer against 性がある; 要|性 is a compound, so it backs off.
    buf = words([("言われたとおりできる", 0.0, 1.0), ("能力って必要", 1.0, 2.0)])
    head, tail = server.split_for_break(buf, limits(), "性がある")
    assert (server.word_text(head), server.word_text(tail)) == ("言われたとおりできる", "能力って必要")


def test_split_for_break_backs_off_a_seam_inside_a_katakana_word():
    buf = words([("私がこう自撮り", 0.0, 1.0), ("カメラとマイク", 1.0, 2.0)])
    head, tail = server.split_for_break(buf, limits(), "スタンドです")
    assert (server.word_text(head), server.word_text(tail)) == ("私がこう自撮り", "カメラとマイク")


def test_split_for_break_emits_the_buffer_at_a_clause_mark_or_before_a_kanji_word():
    head, tail = server.split_for_break(words([("それはやっぱり学校、", 0.0, 1.0)]), limits(), "行ったほうがいい")
    assert (server.word_text(head), tail) == ("それはやっぱり学校、", [])
    head, tail = server.split_for_break(words([("言われたとおりできる", 0.0, 1.0)]), limits(), "能力って必要")
    assert (server.word_text(head), tail) == ("言われたとおりできる", [])


def test_split_for_break_gives_up_when_no_position_is_legal():
    buf = words([("言", 0.0, 1.0), ("ってた", 1.0, 2.0)])
    head, tail = server.split_for_break(buf, limits(), "って")
    assert head is buf and tail == []


# --------------------------------------------------------------------------- seam_for

def test_seam_for_breaks_the_line_after_a_sentence_end():
    assert server.seam_for("これはテストです。", 0.0, limits()) == "\n"


def test_seam_for_breaks_the_line_on_a_pause():
    assert server.seam_for("これはテスト", 0.30, limits()) == "\n"


def test_seam_for_joins_mid_sentence():
    assert server.seam_for("これはテスト", 0.10, limits()) == ""


# --------------------------------------------------------------------------- merge_segments

def cue(text: str, start: float, end: float, seg_id: int = 0):
    return {"start": start, "end": end, "text": text, "seg": seg_id}


def test_merge_segments_folds_two_neighbours_into_one():
    cues = [cue("これはテストです", 0.0, 2.0), cue("電車で行きます", 2.2, 4.0, 1)]
    out = server.merge_segments(cues, limits())
    assert len(out) == 1
    assert out[0]["text"] == "これはテストです電車で行きます"
    assert (out[0]["start"], out[0]["end"]) == (0.0, 4.0)


def test_merge_segments_breaks_the_line_at_a_sentence_boundary():
    cues = [cue("これはテストです。", 0.0, 2.0), cue("電車で行きます", 2.2, 4.0, 1)]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["これはテストです。\n電車で行きます"]


def test_merge_segments_never_lets_a_broken_word_override_a_mark():
    # breaks_word() is true whenever the next cue opens on ー or a small kana, and its flat join
    # put two sentences on one row. Nothing is split inside a word after a sentence mark.
    cues = [cue("そうですね。", 0.0, 1.0), cue("ーっと言います", 1.2, 3.0, 1)]
    assert [c["text"] for c in server.merge_segments(cues, limits())] == ["そうですね。\nーっと言います"]
    # Without the mark the seam is still dropped for the broken word, as it always was.
    cues = [cue("そうですね", 0.0, 1.0), cue("ーっと言います", 1.2, 3.0, 1)]
    assert [c["text"] for c in server.merge_segments(cues, limits())] == ["そうですねーっと言います"]


def test_merge_segments_drops_the_seam_rather_than_leave_a_stub():
    # Without a mark the seam is a guess, and a guess that leaves a row nobody can read gives way
    # to a plain join: refusing would leave the stub flashing on its own.
    cues = [cue("はい", 0.0, 1.0), cue("電車で行きます", 1.2, 3.0, 1)]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["はい電車で行きます"]


def test_merge_segments_gives_a_sentence_its_own_row_however_short_it_is():
    # With the mark there the seam is the speaker's own, so it is never dropped for a short row:
    # うん。 is a whole turn, not a stub we left behind by breaking badly.
    cues = [cue("はい。", 0.0, 1.0), cue("電車で行きます", 1.2, 3.0, 1)]
    assert [c["text"] for c in server.merge_segments(cues, limits())] == ["はい。\n電車で行きます"]
    cues = [cue("今日はいい天気ですね。", 0.0, 2.0), cue("うん。", 2.2, 3.0, 1)]
    assert [c["text"] for c in server.merge_segments(cues, limits())] == ["今日はいい天気ですね。\nうん。"]


def test_merge_segments_refuses_a_third_row_rather_than_glue_two_sentences():
    # The one thing a mark seam cannot do is give way: with two rows already up, flattening would
    # put two sentences on one row, so the merge goes instead.
    cues = [cue("今日はいい天気。\n電車で行きます。", 0.0, 2.0), cue("うん。", 2.2, 3.0, 1)]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["今日はいい天気。\n電車で行きます。", "うん。"]


def test_merge_segments_lets_a_short_cue_reach_further():
    cues = [cue("はいはい", 0.0, 1.0), cue("電車で行きます", 2.0, 4.0, 1)]
    assert len(server.merge_segments(cues, limits())) == 1  # 1.0 s gap, inside cross_reach
    # Both sides longer than reach_chars, so the same gap is judged by merge_gap instead.
    long = [cue("これはテストの文章です", 0.0, 2.0), cue("明日は電車で行きますね", 3.0, 4.5, 1)]
    assert len(server.merge_segments(long, limits())) == 2


# --------------------------------------------------------------------------- breaks_word

# may_break() also says no on taste - a piece too short to read, a line opening on a particle -
# and a merge that overrules its own length budget on taste builds a 41-character line, which it
# did on the first live run. Only breaks_word(), the evidence, may do that.

def test_breaks_word_reports_a_kinsoku_character():
    assert server.breaks_word("なんか靴舐めますって言", "ってた") is True


def test_breaks_word_reports_okurigana_after_a_kanji():
    assert server.breaks_word("これやばい、動", "いた動いた") is True


def test_breaks_word_ignores_a_long_tail_after_a_kanji():
    # Okurigana is a few kana; a whole clause after a kanji is the next word, and forcing that
    # merge past the length budget is what put a 37-character line on screen.
    assert server.breaks_word("ピンクのも身に付けてるから全然", "こういうピンクとかでもいけちゃいそう") is False
    assert server.breaks_word("こう、透明感", "むらさきってそう、透明感が出るらしいよ") is False
    # A kinsoku character is evidence whatever follows it, because nothing can open a line with it.
    assert server.breaks_word("と思", "っていうことなんですけどねそれで") is True


def test_breaks_word_ignores_hiragana_after_katakana():
    assert server.breaks_word("私がこう自撮りカメラ", "こうやって配信します") is False


def test_breaks_word_ignores_a_particle_and_a_short_piece():
    # Both of these make may_break() say no, and neither is evidence of a broken word.
    assert server.may_break("コラボ配信しようかということで", "ということで、コラボ配信を") is False
    assert server.breaks_word("コラボ配信しようかということで", "ということで、コラボ配信を") is False
    assert server.may_break("押して", "うんでなんかね") is False
    assert server.breaks_word("押して", "うんでなんかね") is False


def test_breaks_word_respects_the_speakers_own_punctuation():
    assert server.breaks_word("そうですね。", "いきましょう") is False
    assert server.breaks_word("そうですね、", "いきましょう") is False


def test_breaks_word_of_nothing():
    assert server.breaks_word("", "ってた") is False
    assert server.breaks_word("言", "") is False


def test_merge_segments_does_not_force_a_seam_past_the_character_ceiling():
    # The live-run regression: a forced merge may repair a word, not build a paragraph.
    head = cue("あ" * 38, 0.0, 2.0)
    tail = cue("ってた", 2.2, 3.0, 1)
    assert server.breaks_word(head["text"], tail["text"]) is True
    assert len(server.merge_segments([head, tail], limits())) == 2
    assert len(server.merge_segments([cue("あ" * 30, 0.0, 2.0), cue("ってた", 2.2, 3.0, 1)], limits())) == 1


def test_merge_segments_closes_a_seam_inside_a_word_over_the_char_limit():
    cues = [cue("なんかこれは違うと思", 0.0, 2.0), cue("っております", 2.5, 4.0, 1)]
    out = server.merge_segments(cues, limits(max_chars=8))
    assert [c["text"] for c in out] == ["なんかこれは違うと思っております"]


def test_merge_segments_does_not_force_a_seam_past_the_ceiling():
    cues = [cue("なんかこれは違うと思", 0.0, 5.0), cue("っております", 5.5, 10.0, 1)]
    out = server.merge_segments(cues, limits(max_chars=8))
    assert len(out) == 2  # 10.0 s would be longer than cross_ceiling


def test_merge_segments_does_not_force_a_seam_past_the_reach():
    cues = [cue("なんかこれは違うと思", 0.0, 2.0), cue("っております", 4.0, 5.5, 1)]
    out = server.merge_segments(cues, limits(max_chars=8))
    assert len(out) == 2  # a 2.0 s gap is past cross_reach


def test_merge_segments_refuses_a_legal_seam_past_the_character_limit():
    cues = [cue("これはテストです", 0.0, 2.0), cue("電車で行きます", 2.2, 4.0, 1)]
    assert len(server.merge_segments(cues, limits(max_chars=10))) == 2


def test_merge_segments_refuses_a_legal_seam_past_the_duration_limit():
    over = limits().max_seconds + 0.5
    cues = [cue("これはテストです", 0.0, 3.0), cue("電車で行きます", 3.2, over, 1)]
    assert len(server.merge_segments(cues, limits())) == 2
    # Just inside it, the same pair merges: the duration is the only thing refusing them.
    inside = [cue("これはテストです", 0.0, 3.0), cue("電車で行きます", 3.2, limits().max_seconds, 1)]
    assert len(server.merge_segments(inside, limits())) == 1


def test_merge_segments_drops_the_seam_rather_than_make_a_third_line():
    # Refusing over the line limit would leave the second cue alone on screen; two rows of text
    # beat an orphan, so the break is what gives way.
    cues = [cue("あいうえお\nかきくけこ", 0.0, 2.0), cue("電車で行きます", 2.3, 4.0, 1)]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["あいうえお\nかきくけこ電車で行きます"]


def test_merge_segments_still_refuses_when_the_plain_join_does_not_fit_either():
    # Dropping the seam is the only fallback; past max_chars there is nothing left to give up.
    cues = [cue("あ" * 15 + "\n" + "い" * 14, 0.0, 2.0), cue("電車で行きます", 2.3, 4.0, 1)]
    assert len(server.merge_segments(cues, limits())) == 2


def test_merge_segments_renames_the_segment_id_of_a_swallowed_sibling():
    # B (seg 6) is merged into A (seg 5), so C, the rest of segment 6, has to follow it or
    # sentenceForCue() in content.js would rejoin half a sentence.
    cues = [cue("これはテストです", 0.0, 2.0, 5), cue("電車で行きます", 2.2, 3.0, 6),
            cue("散歩に行きました", 5.0, 6.5, 6)]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["これはテストです電車で行きます", "散歩に行きました"]
    assert [c["seg"] for c in out] == [5, 5]
    assert "_merged" not in out[0]


def test_merge_segments_on_empty_input():
    assert server.merge_segments([], limits()) == []


# --------------------------------------------------------------------------- group_words and may_break

def test_group_words_does_not_split_a_pause_inside_a_word():
    # A full second of real silence between 言 and ってた: VAD agrees, and it is still one word.
    ws = words([("なんか靴舐めますって言", 0.0, 2.0), ("ってた", 3.0, 3.6)])
    groups = server.group_words(ws, [[0.0, 2.1], [2.95, 3.7]], limits())
    assert [server.word_text(g) for g in groups] == ["なんか靴舐めますって言ってた"]


def test_group_words_splits_a_confirmed_pause_between_two_kanji():
    # A detected silence is evidence of a boundary; the compound rule is only for blind cuts.
    ws = words([("今日はいい天気", 0.0, 1.0), ("電車で行きます", 4.0, 5.0)])
    groups = server.group_words(ws, [[0.0, 1.1], [3.95, 5.1]], limits())
    assert [server.word_text(g) for g in groups] == ["今日はいい天気", "電車で行きます"]


def test_group_words_still_splits_a_pause_at_a_legal_boundary():
    ws = words([("こんにちは", 0.0, 1.0), ("電車で行きます", 2.0, 3.0)])
    groups = server.group_words(ws, [[0.0, 1.1], [1.95, 3.1]], limits())
    assert [server.word_text(g) for g in groups] == ["こんにちは", "電車で行きます"]


# --------------------------------------------------------------------------- repair before the gates

def test_build_window_cues_repairs_the_lead_word_before_the_vad_gate():
    ws = words([("は", 0.0, 0.2), ("じめまして", 5.0, 5.8)])
    speech = [[0.0, 0.25], [4.9, 6.0]]
    segment = seg("はじめまして", 0.0, 5.8, ws)
    # Unrepaired, the segment's span covers 5.6 s of silence it never contained.
    raw = server.absolute_words(segment, 0.0)
    assert server.hallucination_reason(segment, raw, speech) == "vad"

    drops: dict = {}
    cues, next_id = server.build_window_cues([segment], 0.0, speech, limits(), 0, drops)
    assert [c["text"] for c in cues] == ["はじめまして"]
    assert "vad" not in drops
    assert next_id == 1


def test_build_cues_never_cuts_inside_a_kanji_or_katakana_compound():
    # 22:13 of 5csq1MlSspA: one 13 s segment with no pause, split only by the hard limits. The cut
    # used to land inside 能|力 and 学|校 (and, on this synthetic timing, 絶|対 and 必|要).
    text = ("結局、社会に出たときに、たぶん、人と関わることって絶対必要になるし、言われたことを言われたとおり"
            "できる能力って必要になるから、それを養うのって学校じゃないかなって思うんで、行ったほうがいいとは思う。")
    start, end = 1325.14, 1338.18
    step = (end - start) / len(text)
    ws = [W(ch, start + i * step, start + (i + 1) * step, 0.9) for i, ch in enumerate(text)]
    lim = limits()
    cues = server.build_cues(ws, [[1320.07, 1339.48]], lim)
    texts = [c["text"] for c in cues]
    assert "".join(texts) == text
    for head, tail in zip(texts, texts[1:]):
        a, b = head[-1], tail[0]
        assert not (server.is_kanji(a) and server.is_kanji(b)), (head, tail)
        assert not (server.is_katakana(a) and server.is_katakana(b)), (head, tail)
    assert all(len(t) <= lim.max_chars for t in texts)

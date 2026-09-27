"""Unit tests for punctuate_words(): the sentence mark Whisper leaves out.

Whisper writes Japanese punctuation inconsistently, and the cue builder has no other sentence
signal, so a sentence-final expression followed by a pause writes the mark here. Nothing below
touches a model: synthetic word lists and speech intervals in, rewritten words out.
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


def punctuated(ws, speech, next_word=None, **overrides):
    """next_word is the next segment's first Word, as build_window_cues() hands it over."""
    return server.word_text(server.punctuate_words(ws, speech, next_word, limits(**overrides)))


# --------------------------------------------------------------------------- the pause

def test_longest_silence_takes_the_gap_between_two_intervals():
    assert server.longest_silence(0.0, 5.0, [[0.0, 1.0], [2.0, 5.0]]) == pytest.approx(1.0)


def test_longest_silence_counts_the_uncovered_ends():
    assert server.longest_silence(0.0, 5.0, [[1.0, 1.5]]) == pytest.approx(3.5)


def test_longest_silence_of_a_fully_covered_range_is_zero():
    assert server.longest_silence(1.0, 2.0, [[0.0, 9.0]]) == 0.0


# --------------------------------------------------------------------------- the shapes

def test_a_strong_shape_before_a_pause_gets_a_full_stop():
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9), ("おじいちゃん", 1.4, 2.0)])
    assert punctuated(ws, [[0.0, 0.9], [1.4, 2.0]]) == "そうなんですよね。おじいちゃん"


def test_a_question_shape_gets_a_question_mark():
    for tail, rest in (("か", "行きます"), ("かな", "行く"), ("でしょ", "行く")):
        ws = words([(rest, 0.0, 0.6), (tail, 0.6, 0.9), ("うん", 1.5, 2.0)])
        assert punctuated(ws, [[0.0, 0.9], [1.5, 2.0]]) == f"{rest}{tail}？うん"


def test_a_weak_shape_needs_the_longer_pause():
    def run(next_start):
        ws = words([("昨日", 0.0, 0.4), ("食べた", 0.4, 0.9), ("うん", next_start, next_start + 0.5)])
        return punctuated(ws, [[0.0, 0.9], [next_start, next_start + 0.5]])

    assert run(1.3) == "昨日食べたうん"    # 0.4 s: shorter than sentence_pause_weak
    assert run(1.6) == "昨日食べた。うん"  # 0.7 s


def test_a_shape_without_a_pause_is_left_alone():
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9), ("だから", 0.95, 1.4)])
    assert punctuated(ws, [[0.0, 1.4]]) == "そうなんですよねだから"


def test_text_that_already_ends_in_a_mark_is_untouched():
    for mark in ("。", "、"):
        ws = words([("そうですよね", 0.0, 0.6), (mark, 0.6, 0.7), ("あの", 1.5, 2.0)])
        assert punctuated(ws, [[0.0, 0.7], [1.5, 2.0]]) == f"そうですよね{mark}あの"


def test_desu_before_ka_is_left_for_the_ka_to_answer():
    ws = words([("行きます", 0.0, 0.5), ("です", 0.5, 0.9), ("か", 1.5, 1.8), ("うん", 2.4, 2.9)])
    out = punctuated(ws, [[0.0, 0.9], [1.5, 1.8], [2.4, 2.9]])
    assert out == "行きますですか？うん"


def test_a_filler_ka_gets_no_mark():
    # The speaker is still choosing the next word, not asking anything.
    for text in ("悩んで、なんか", "行きたい人多そうなんか", "美術系とか", "っていうか"):
        ws = words([(text, 0.0, 0.8), ("さ", 1.5, 1.8)])
        assert punctuated(ws, [[0.0, 0.8], [1.5, 1.8]]) == text + "さ"


def test_koto_ka_is_a_question_and_keeps_its_mark():
    # ことか ends in とか, and is the nominaliser plus a real question.
    ws = words([("老害教師少なめってことか", 0.0, 1.5), ("うん", 2.2, 2.6)])
    assert punctuated(ws, [[0.0, 1.5], [2.2, 2.6]]) == "老害教師少なめってことか？うん"


def test_an_interjectional_particle_after_a_connective_gets_no_mark():
    # めっちゃ偏見だけどさ ... the sentence went on; さ was holding the floor.
    for stem in ("めっちゃ偏見だけど", "それだから", "話を聞いて", "行くのに"):
        for particle in ("さ", "ね", "よ", "な"):
            ws = words([(stem + particle, 0.0, 1.0), ("はいはい", 1.8, 2.3)])
            assert punctuated(ws, [[0.0, 1.0], [1.8, 2.3]]) == stem + particle + "はいはい"


def test_a_sentence_initial_connective_does_not_block_the_mark():
    # Whisper writes the connective with its own comma (で、 / でも、), which says this で opens a
    # sentence instead of closing a phrase, so the particle test does not apply to it.
    ws = words([("そうなんですよね", 0.0, 0.9), ("で、", 1.4, 1.7), ("おじいちゃん", 1.7, 2.2)])
    assert punctuated(ws, [[0.0, 0.9], [1.4, 2.2]]) == "そうなんですよね。で、おじいちゃん"
    bare = words([("そうなんですよね", 0.0, 0.9), ("で", 1.4, 1.7), ("おじいちゃん", 1.7, 2.2)])
    assert punctuated(bare, [[0.0, 0.9], [1.4, 2.2]]) == "そうなんですよねでおじいちゃん"


def test_a_word_that_merely_ends_in_a_shape_kana_gets_no_mark():
    # A shape is a suffix test, so without SENTENCE_NOT_ENDINGS 何か行きたい becomes 何か？行きたい.
    for head, tail in (("何か", "行きたい"), ("誰か", "来る"), ("そんな", "感じ"), ("また", "明日"),
                       ("まだ", "先です"), ("確か", "そうでした")):
        ws = words([(head, 0.0, 0.6), (tail, 1.4, 2.0)])
        assert punctuated(ws, [[0.0, 0.6], [1.4, 2.0]]) == head + tail
    # The exception still stands: ことか is the nominaliser and a question.
    ws = words([("老害教師少なめってことか", 0.0, 1.5), ("うん", 2.2, 2.6)])
    assert punctuated(ws, [[0.0, 1.5], [2.2, 2.6]]) == "老害教師少なめってことか？うん"


def test_only_a_next_word_that_is_a_particle_blocks_the_mark():
    # The test is on the whole word: はい, やっぱり and もう all open a sentence and all start with
    # a particle kana, so keying on the first character silenced the commonest openers.
    for tail in ("はい", "やっぱり", "もう", "もちろん", "ところで", "ねえ"):
        ws = words([("そうですね", 0.0, 0.6), (tail, 1.4, 2.0)])
        assert punctuated(ws, [[0.0, 0.6], [1.4, 2.0]]) == "そうですね。" + tail, tail
    for tail in ("は", "が", "から", "とか", "のに"):
        ws = words([("そうですね", 0.0, 0.6), (tail, 1.4, 2.0)])
        assert punctuated(ws, [[0.0, 0.6], [1.4, 2.0]]) == "そうですね" + tail, tail


def test_a_comma_split_off_as_its_own_token_still_opens_a_clause():
    # Whisper writes the connective as で、 or as で and 、; both open a sentence.
    joined = words([("よね", 0.0, 0.6), ("で、", 1.4, 1.7), ("おじいちゃん", 1.7, 2.4)])
    assert punctuated(joined, [[0.0, 0.6], [1.4, 2.4]]) == "よね。で、おじいちゃん"
    split = words([("よね", 0.0, 0.6), ("で", 1.4, 1.7), ("、", 1.7, 1.75), ("おじいちゃん", 1.75, 2.4)])
    assert punctuated(split, [[0.0, 0.6], [1.4, 2.4]]) == "よね。で、おじいちゃん"
    bare = words([("よね", 0.0, 0.6), ("で", 1.4, 1.7), ("おじいちゃん", 1.7, 2.4)])
    assert punctuated(bare, [[0.0, 0.6], [1.4, 2.4]]) == "よねでおじいちゃん"


def test_a_next_word_opening_on_a_small_kana_gets_no_mark():
    # っ can never open a word: the pause is Whisper's sub-token timing, not a sentence end.
    ws = words([("わかった", 0.0, 0.6), ("よ", 0.6, 0.9), ("っと", 1.6, 2.0)])
    assert punctuated(ws, [[0.0, 0.9], [1.6, 2.0]]) == "わかったよっと"


def test_a_last_word_stretched_over_the_silence_still_gets_its_mark():
    # Whisper anchors the segment's last word to the end of the decode, so the pause lies inside
    # the word's own span; measuring from w.end would find no silence at all.
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 2.0), ("おじいちゃん", 2.0, 2.6)])
    assert punctuated(ws, [[0.0, 0.9], [2.0, 2.6]]) == "そうなんですよね。おじいちゃん"


def test_without_speech_intervals_the_raw_gap_decides():
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9), ("おじいちゃん", 1.4, 2.0)])
    assert punctuated(ws, []) == "そうなんですよね。おじいちゃん"
    tight = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9), ("おじいちゃん", 1.0, 1.6)])
    assert punctuated(tight, []) == "そうなんですよねおじいちゃん"


def test_the_last_word_uses_the_next_segment_first_word():
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9)])
    nxt = W("はい", 1.5, 2.0, 0.9)
    assert punctuated(ws, [[0.0, 0.9], [1.5, 2.0]], next_word=nxt) == "そうなんですよね。"


def test_the_next_segment_is_judged_like_any_next_word():
    # It carries the pause and what follows it: a segment opening on っと or on a particle
    # continues the one before it, and a mark between them is as wrong as inside a segment.
    speech = [[0.0, 0.9], [1.5, 2.0]]
    for text, expected in (("っと", "そうなんですよね"), ("は", "そうなんですよね"),
                           ("はい", "そうなんですよね。"), ("やっぱり", "そうなんですよね。")):
        ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9)])
        assert punctuated(ws, speech, next_word=W(text, 1.5, 2.0, 0.9)) == expected, text


def test_the_last_word_without_anything_after_it_gets_no_mark():
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9)])
    assert punctuated(ws, [[0.0, 0.9]]) == "そうなんですよね"


def test_the_flag_off_leaves_every_word_as_whisper_wrote_it():
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 0.9), ("おじいちゃん", 1.4, 2.0)])
    assert punctuated(ws, [[0.0, 0.9], [1.4, 2.0]], sentence_ends=False) == "そうなんですよねおじいちゃん"


def test_cue_limits_reads_the_flag():
    assert server.cue_limits(SimpleNamespace()).sentence_ends is True
    assert server.cue_limits(SimpleNamespace(sentence_ends="auto")).sentence_ends is True
    assert server.cue_limits(SimpleNamespace(sentence_ends="off")).sentence_ends is False
    assert server.parse_args(["--sentence-ends", "off"]).sentence_ends == "off"


def test_sentence_shape_prefers_the_longest_match():
    assert server.sentence_shape("そうですよね") == ("よね", True)
    assert server.sentence_shape("行くでしょう") == ("でしょう", True)
    assert server.sentence_shape("食べました") == ("ました", True)
    assert server.sentence_shape("食べた") == ("た", False)
    assert server.sentence_shape("東京") == (None, False)


# --------------------------------------------------------------------------- merge_segments

def test_a_finished_short_sentence_is_not_a_stub_worth_reaching_for():
    cues = [{"start": 0.0, "end": 1.0, "text": "そうですよね。", "seg": 0},
            {"start": 2.0, "end": 4.0, "text": "おじいちゃん先生とゲームの話", "seg": 1}]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["そうですよね。", "おじいちゃん先生とゲームの話"]


def test_an_unfinished_short_piece_still_reaches_for_a_partner():
    cues = [{"start": 0.0, "end": 1.0, "text": "そうですよね", "seg": 0},
            {"start": 2.0, "end": 4.0, "text": "おじいちゃん先生とゲーム", "seg": 1}]
    out = server.merge_segments(cues, limits())
    assert [c["text"] for c in out] == ["そうですよね\nおじいちゃん先生とゲーム"]


# --------------------------------------------------------------------------- end to end

def test_a_mark_on_a_word_the_trim_drops_moves_onto_the_cue():
    # よね is stretched to 2.0 s over silence that ends at 0.9, so trim_words() drops it - and with
    # it the mark punctuate_words() had just written. The timings go, the sentence end stays.
    ws = words([("そうなんです", 0.0, 0.6), ("よね", 0.6, 2.0)])
    nxt = words([("おじいちゃん先生と", 2.0, 3.0)])
    segs = [seg("そうなんですよね", 0.0, 2.0, ws), seg("おじいちゃん先生と", 2.0, 3.0, nxt)]
    cues, _ = server.build_window_cues(segs, 0.0, [[0.0, 0.9], [2.0, 3.0]], limits(), 0)
    assert [c["text"] for c in cues] == ["そうなんです。", "おじいちゃん先生と"]


def test_carry_trailing_mark_leaves_an_already_finished_cue_alone():
    kept = words([("はい", 0.0, 0.3), ("。", 0.3, 0.35)])
    dropped = kept + words([("よね。", 0.4, 2.0)])
    assert server.word_text(server.carry_trailing_mark(dropped, kept)) == "はい。"


def measured_case(silence: float):
    """5csq1MlSspA at 18:06: one Whisper segment, no mark after よね, then a pause.

    Words of about 0.2 s each, as the dump has them, and the detector heard the two halves as
    two intervals.
    """
    resume = 0.6 + silence
    ws = words([("そう", 0.0, 0.2), ("なんです", 0.2, 0.4), ("よね", 0.4, 0.6),
                ("おじいちゃん", resume, resume + 0.4), ("先生と", resume + 0.4, resume + 0.8),
                ("ゲームの", resume + 0.8, resume + 1.2), ("話", resume + 1.2, resume + 1.4),
                ("したりする", resume + 1.4, resume + 1.9), ("の", resume + 1.9, resume + 2.1)])
    text = "".join(w.word for w in ws)
    return [seg(text, 0.0, resume + 2.1, ws)], [[0.0, 0.6], [resume, resume + 2.1]], text


def test_build_window_cues_writes_the_missing_mark_of_the_measured_case():
    segs, speech, text = measured_case(0.5)
    plain, _ = server.build_window_cues(segs, 0.0, speech, limits(sentence_ends=False), 0)
    assert [c["text"] for c in plain] == [text]  # the 29-character line the live run showed

    segs, speech, _ = measured_case(0.5)
    marked, _ = server.build_window_cues(segs, 0.0, speech, limits(), 0)
    # The mark is a hard row boundary, so the two sentences never share a row even where the char
    # budget would fit them both: the viewer reads two lines and a mined card quotes one of them.
    assert [c["text"] for c in marked] == ["そうなんですよね。\nおじいちゃん先生とゲームの話したりするの"]


def test_build_window_cues_splits_the_unpunctuated_sentence_at_a_readable_pause():
    segs, speech, text = measured_case(1.4)
    plain, _ = server.build_window_cues(segs, 0.0, speech, limits(sentence_ends=False), 0)
    assert [c["text"] for c in plain] == ["そうなんですよね\nおじいちゃん先生とゲームの話したりするの"]

    segs, speech, _ = measured_case(1.4)
    marked, _ = server.build_window_cues(segs, 0.0, speech, limits(), 0)
    assert len(marked) == 2
    assert marked[0]["text"] == "そうなんですよね。"
    assert marked[1]["text"] == "おじいちゃん先生とゲームの話したりするの"

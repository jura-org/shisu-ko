"""The creator's own subtitles: fetched beside the audio, cleaned, aligned with what Whisper heard.

Every track here is invented. Nothing loads a model or touches the network: yt_dlp is a fake in
sys.modules, the model is scripted and detect_speech is what the test says Silero heard, as in
test_lyrics.py and test_declared_language.py.
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
LIMITS = server.CueLimits()

# One invented track, in the three formats YouTube serves it in.
JSON3 = json.dumps({"events": [
    {"tStartMs": 0, "dDurationMs": 60000, "id": 1, "wpWinPosId": 1},  # a window definition, no text
    {"tStartMs": 1000, "dDurationMs": 2500, "segs": [{"utf8": "明日は雨が降りそうですね。"}]},
    {"tStartMs": 4000, "dDurationMs": 2000, "segs": [{"utf8": "（佐藤）そうですね。\n散歩に行こうか。"}]},
    {"tStartMs": 6500, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 8000, "dDurationMs": 1500, "segs": [{"utf8": "[笑]いいよ！"}]},
]}, ensure_ascii=False)
SRV3 = """<?xml version="1.0" encoding="utf-8" ?><timedtext format="3"><body>
<p t="1000" d="2500">明日は雨が降りそうですね。</p>
<p t="4000" d="2000">（佐藤）そうですね。
散歩に行こうか。</p>
<p t="8000" d="1500">[笑]いいよ！</p>
</body></timedtext>"""
VTT = """WEBVTT
Kind: captions
Language: ja

00:00:01.000 --> 00:00:03.500 align:start position:0%
明日は雨が降りそうですね。

00:00:04.000 --> 00:00:06.000
（佐藤）そうですね。
散歩に行こうか。

00:00:08.000 --> 00:00:09.500
[笑]いいよ！
"""
CLEAN = [[1.0, 3.5, "明日は雨が降りそうですね。"], [4.0, 6.0, "そうですね。\n散歩に行こうか。"],
         [8.0, 9.5, "いいよ！"]]


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw,ext", [(JSON3, "json3"), (SRV3, "srv3"), (VTT, "vtt")])
def test_the_three_formats_give_the_same_clean_lines(raw, ext):
    assert server.human_subtitle_lines(raw.encode("utf-8"), ext) == CLEAN


def test_the_parsers_keep_the_track_line_breaks_and_times():
    lines, timed = server.parse_json3(JSON3)
    assert timed == 0
    assert lines[1] == (4.0, 6.0, "（佐藤）そうですね。\n散歩に行こうか。")
    assert server.parse_srv3(SRV3)[0][1] == (4.0, 6.0, "（佐藤）そうですね。\n散歩に行こうか。")
    assert server.parse_vtt(VTT)[0][1] == (4.0, 6.0, "（佐藤）そうですね。\n散歩に行こうか。")


def test_vtt_reads_hours_markup_and_entities():
    raw = "WEBVTT\n\n01:02:03.250 --> 01:02:04.000\n<c.yellow>雨&amp;風</c>\n"
    assert server.parse_vtt(raw)[0] == [(3723.25, 3724.0, "雨&風")]


def test_an_unknown_format_or_an_empty_track_is_no_track():
    assert server.human_subtitle_lines(b"anything", "ttml") is None
    assert server.human_subtitle_lines(b'{"events": []}', "json3") is None


def test_a_line_the_track_split_in_two_is_one_line():
    raw = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "よろしくね。"}]},
                                 {"tStartMs": 1000, "dDurationMs": 800, "segs": [{"utf8": "よろしくね。"}]}]},
                     ensure_ascii=False)
    assert server.human_subtitle_lines(raw.encode("utf-8"), "json3") == [[0.0, 1.8, "よろしくね。"]]


# --------------------------------------------------------------------------- cleaning

@pytest.mark.parametrize("raw,clean", [
    ("（田中）こんにちは。", "こんにちは。"),        # a speaker label in brackets
    ("(田中)こんにちは。", "こんにちは。"),
    ("田中：こんにちは。", "こんにちは。"),          # ... with a colon
    ("田中: こんにちは。", "こんにちは。"),
    ("【田中】こんにちは。", "こんにちは。"),
    (">> こんにちは。", "こんにちは。"),             # a turn mark
    ("- こんにちは。", "こんにちは。"),
    ("[笑]", ""),                                    # sound tags, anywhere
    ("そうだね［拍手］", "そうだね"),
    ("（拍手）", ""),
    ("それは（笑）すごい", "それはすごい"),
    ("(laughs) 本当に", "本当に"),
    ("新しい(case)です", "新しい(case)です"),        # a Latin word is a tag only whole
    ("♪ 春の風 ♪", "春の風"),
    ("（小声で）", ""),                              # a stage note alone on its row
    ("紅葉（もみじ）が綺麗", "紅葉が綺麗"),          # furigana after its kanji
    ("その日（土曜日）に会う", "その日（土曜日）に会う"),  # an aside, not a reading: kept
    ("会議は10:30から", "会議は10:30から"),          # a time is not a speaker
    ("えっと　　それで  ね", "えっと　それで ね"),  # runs of space squeezed, the full-width kept
])
def test_the_cleaner_drops_what_is_not_speech(raw, clean):
    assert server.clean_subtitle_text(raw) == clean


def test_the_cleaner_keeps_row_breaks_and_drops_emptied_rows():
    assert server.clean_subtitle_text("（佐藤）\nそうですね。\n[音楽]\n行こう。") == "そうですね。\n行こう。"
    assert server.clean_subtitle_text("今日は\u200bね\ue000") == "今日はね"  # model-text cleaning too


# --------------------------------------------------------------------------- speech recognition in disguise

def test_a_json3_track_timed_word_by_word_is_speech_recognition():
    events = [{"tStartMs": i * 2000, "dDurationMs": 2000,
               "segs": [{"utf8": "今日は"}, {"utf8": "晴れ", "tOffsetMs": 400}, {"utf8": "です。", "tOffsetMs": 900}]}
              for i in range(5)]
    raw = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    assert server.human_subtitle_lines(raw, "json3") is None


def test_an_srv3_track_timed_word_by_word_is_speech_recognition():
    rows = "".join(f'<p t="{i * 2000}" d="2000"><s ac="200">今日は</s><s t="400" ac="180">晴れです。</s></p>'
                   for i in range(4))
    assert server.human_subtitle_lines(f"<timedtext><body>{rows}</body></timedtext>".encode("utf-8"), "srv3") is None


def test_a_vtt_track_with_inline_word_times_is_speech_recognition():
    raw = "WEBVTT\n\n" + "".join(
        f"00:00:0{i}.000 --> 00:00:0{i + 1}.000\n今日は<00:00:0{i}.500><c>晴れ</c>\n\n" for i in range(4))
    assert server.human_subtitle_lines(raw.encode("utf-8"), "vtt") is None


def test_a_track_without_one_mark_or_space_is_speech_recognition():
    """The recogniser's text uploaded as a track: no punctuation anywhere, lines cut at a length."""
    rows = ["こんばんは今日は川の話をして", "みましょう私は田中と申します", "どうぞよろしくそれではは"]
    events = [{"tStartMs": i * 3000, "dDurationMs": 3000, "segs": [{"utf8": rows[i % 3]}]} for i in range(24)]
    raw = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    assert server.human_subtitle_lines(raw, "json3") is None
    # A short track is too little to judge on, and one marked row in twenty is a person's.
    assert server.human_subtitle_lines(json.dumps({"events": events[:5]}, ensure_ascii=False).encode("utf-8"),
                                       "json3")
    events[3]["segs"][0]["utf8"] = "みましょう。私は田中と申します"
    events[9]["segs"][0]["utf8"] = "みましょう 私は田中と申します"
    assert server.human_subtitle_lines(json.dumps({"events": events}, ensure_ascii=False).encode("utf-8"), "json3")


def fmt(ext, url="https://www.youtube.com/api/timedtext?v=x&lang=ja", name="Japanese"):
    return {"ext": ext, "url": url + f"&fmt={ext}", "name": name}


def test_the_plain_language_tag_comes_first_and_json3_before_vtt():
    subs = {"ja-JP": [fmt("json3")], "ja": [fmt("vtt"), fmt("srv3"), fmt("json3")], "en": [fmt("json3")]}
    key, chosen = server.pick_human_track(subs, "ja")
    assert key == "ja" and chosen["ext"] == "json3"
    assert server.pick_human_track({"ja-JP": [fmt("vtt")]}, "ja")[0] == "ja-JP"


def test_recognition_tracks_are_never_picked():
    assert server.pick_human_track({"ja-orig": [fmt("json3")]}, "ja") is None
    assert server.pick_human_track({"ja": [fmt("json3", url="https://x/api/timedtext?kind=asr&lang=ja")]}, "ja") is None
    assert server.pick_human_track({"ja": [fmt("json3", name="Japanese (auto-generated)")]}, "ja") is None
    assert server.pick_human_track({"ja": [fmt("ttml")]}, "ja") is None
    assert server.pick_human_track(None, "ja") is None
    assert server.pick_human_track({"live_chat": [fmt("json3")], "jav": [fmt("json3")]}, "ja") is None


# --------------------------------------------------------------------------- the aligner

def test_fold_chars_folds_width_case_and_script_and_drops_marks():
    chars, src = server.fold_chars("ＡＢ、カメラー。です")
    assert "".join(chars) == "abかめらです"
    assert src == [0, 1, 3, 4, 5, 8, 9]


def test_align_line_finds_the_line_inside_a_longer_stretch():
    line = list("いいてんき")
    region = list("えーとてもいいてんきだね")
    pairs, matches, first, last = server.align_line(line, region)
    assert matches == 5 and (first, last) == (5, 10)
    assert pairs == [5, 6, 7, 8, 9]


def test_align_line_counts_a_different_spelling_as_substitutions():
    pairs, matches, first, last = server.align_line(list("きょうはさむい"), list("今日はさむい"))
    assert matches == 4 and pairs[-4:] == [2, 3, 4, 5]


def wcue(text, start, end, seg=0):
    """A Whisper cue whose characters share [start, end) evenly, one word each."""
    step = (end - start) / len(text)
    return {"start": start, "end": end, "text": text, "seg": seg,
            "words": [{"start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3), "text": ch}
                      for i, ch in enumerate(text)]}


def counts(stats):
    return {k: v for k, v in stats.items() if not k.startswith("_")}


def assert_words_spell(cue):
    ws = cue.get("words")
    if ws is None:
        return
    assert "".join(w["text"] for w in ws) == cue["text"]
    assert all(w["start"] <= w["end"] for w in ws)
    assert all(a["end"] <= b["start"] for a, b in zip(ws, ws[1:]))
    assert all(cue["start"] <= w["start"] and w["end"] <= cue["end"] for w in ws)


def test_an_agreeing_line_takes_its_text_from_the_track_and_its_time_from_whisper():
    cues = [wcue("明日は雨が降りそうですね", 1.4, 3.2)]
    speech = [[1.3, 3.3]]
    stats: dict = {}
    out, seg_id = server.assist_cues(cues, [[1.0, 3.5, "明日は雨が降りそうですね。"]], speech, LIMITS, 7, None, stats)
    assert counts(stats) == {"aligned": 1}
    (cue,) = out
    assert cue["text"] == "明日は雨が降りそうですね。" and cue["src"] == "human" and cue["seg"] == 7 and seg_id == 8
    # Snapped to the onset as a Whisper cue is, not the track's 1.0; out a beat after the audio.
    assert cue["start"] == pytest.approx(1.4, abs=0.01)
    assert 3.2 < cue["end"] <= 3.2 + LIMITS.lead_out + 0.01
    assert_words_spell(cue)
    assert [w["text"] for w in cue["words"]] == ["明日", "は", "雨", "が", "降", "りそうですね。"]


def test_a_line_whisper_heard_differently_keeps_whisper():
    cues = [wcue("全然違う話をしている", 1.4, 3.2)]
    stats: dict = {}
    out, _ = server.assist_cues([dict(c) for c in cues], [[1.0, 3.5, "明日は雨が降りそうですね。"]], [[1.3, 3.3]],
                                LIMITS, 0, None, stats)
    assert counts(stats) == {"kept_whisper": 1}
    assert [c["text"] for c in out] == ["全然違う話をしている"] and "src" not in out[0]


def test_a_line_over_audio_whisper_wrote_nothing_for_keeps_its_own_times():
    """Music, or a stretch the detector missed: the person heard it, Whisper did not."""
    cues = [wcue("明日は雨が降りそうですね", 1.4, 3.2)]
    lines = [[1.0, 3.5, "明日は雨が降りそうですね。"], [10.0, 14.0, "♪春の風が吹く"]]
    stats: dict = {}
    out, _ = server.assist_cues(cues, [[a, b, server.clean_subtitle_text(t)] for a, b, t in lines],
                                [[1.3, 3.3]], LIMITS, 0, None, stats)
    assert counts(stats) == {"aligned": 1, "track": 1}
    assert out[1] == {"start": 10.0, "end": 14.0, "text": "春の風が吹く", "src": "human", "seg": 1}


def test_a_line_timed_away_from_its_speech_leaves_it_to_whisper():
    """The person's line sits ten seconds before the speech: Whisper heard that speech, so the
    line is not over music, and showing it on its own times would show the text twice."""
    cues = [wcue("みんなで泳ぎましたね", 43.5, 44.5, seg=0)]
    stats: dict = {}
    out, _ = server.assist_cues(cues, [[33.7, 34.3, "みんなで泳ぎましたね。"]], [[43.4, 44.6]], LIMITS, 1, None, stats)
    assert [(c["text"], c.get("src")) for c in out] == [("みんなで泳ぎましたね", None)]
    assert counts(stats) == {"kept_whisper": 1}


def test_lines_after_one_that_claimed_their_speech_leave_it_to_whisper():
    """An earlier line that reached over the next lines' speech leaves them nothing to align:
    they are not lines over music, and must not push Whisper's cues aside."""
    cues = [wcue("おつかれさまでしたまたあした", 1.0, 6.0, seg=0)]
    lines = [[1.0, 2.0, "おつかれさまでした。またあした。"], [3.0, 5.0, "またあした。"]]
    stats: dict = {}
    out, _ = server.assist_cues(cues, lines, [[0.9, 6.1]], LIMITS, 1, None, stats)
    assert counts(stats) == {"aligned": 1, "kept_whisper": 1}
    assert [c["text"] for c in out] == ["おつかれさまでした。またあした。"]


def test_speech_the_track_has_no_line_for_keeps_whisper():
    cues = [wcue("明日は雨が降りそうですね", 1.4, 3.2, seg=0), wcue("ちょっと待ってください", 20.0, 22.0, seg=1)]
    out, _ = server.assist_cues(cues, [[1.0, 3.5, "明日は雨が降りそうですね。"]], [[1.3, 3.3], [19.9, 22.1]],
                                LIMITS, 2)
    assert [(c["text"], c.get("src")) for c in out] == [("明日は雨が降りそうですね。", "human"),
                                                       ("ちょっと待ってください", None)]


def test_whisper_words_beside_an_aligned_line_stay_as_a_cue_and_a_fringe_goes():
    cues = [wcue("明日は雨が降りそうですねところで明日の予定は", 1.0, 5.0)]
    lines = [[0.8, 3.0, "明日は雨が降りそうですね。"]]
    out, _ = server.assist_cues(cues, lines, [[0.9, 5.1]], LIMITS, 1)
    assert [c["text"] for c in out] == ["明日は雨が降りそうですね。", "ところで明日の予定は"]
    assert "src" not in out[1] and out[1]["seg"] == 0 and out[0]["end"] < out[1]["start"]
    for c in out:
        assert_words_spell(c)
    # Two characters left over are the line's fringe, not a line of their own.
    cues = [wcue("明日は雨が降りそうですねえと", 1.0, 3.5)]
    out, _ = server.assist_cues(cues, lines, [[0.9, 3.6]], LIMITS, 1)
    assert [c["text"] for c in out] == ["明日は雨が降りそうですね。"]


def test_two_lines_inside_one_whisper_cue_each_take_their_own_words():
    cues = [wcue("そうですね散歩に行こうか", 4.1, 6.0)]
    out, _ = server.assist_cues(cues, [[4.0, 5.0, "そうですね。"], [5.0, 6.2, "散歩に行こうか。"]], [[4.0, 6.1]],
                                LIMITS, 0)
    assert [c["text"] for c in out] == ["そうですね。", "散歩に行こうか。"]
    assert out[0]["end"] <= out[1]["start"]
    for c in out:
        assert_words_spell(c)


def test_a_sentence_said_twice_and_written_once_is_one_cue_over_both():
    """The person covered the repeat with their line; Whisper's second spelling does not stand beside it."""
    cues = [wcue("あれは烏龍茶です", 1.0, 2.5, seg=0), wcue("あれは烏龍茶です", 3.5, 5.0, seg=1)]
    out, _ = server.assist_cues(cues, [[0.8, 5.5, "あれはウーロン茶です。"]], [[0.9, 2.6], [3.4, 5.1]], LIMITS, 2)
    assert [c["text"] for c in out] == ["あれはウーロン茶です。"]
    assert out[0]["start"] == pytest.approx(0.9, abs=0.1) and out[0]["end"] >= 5.0
    assert_words_spell(out[0])


def test_a_line_whose_times_are_too_short_for_its_text_still_finds_it():
    """Three questions written over three seconds: the search reaches as far as the text could take."""
    text = "何を食べますか誰と来ますか何時に帰りますか"
    cues = [wcue(text, 10.0, 16.0)]
    out, _ = server.assist_cues(cues, [[10.0, 13.0, "何を食べますか？誰と来ますか？何時に帰りますか？"]],
                                [[9.9, 16.1]], LIMITS, 1)
    assert [c.get("src") for c in out] == ["human"] and out[0]["end"] >= 16.0


def test_a_word_spelt_in_kana_on_one_side_gets_half_credit():
    line, region = list("やさしいひとだね"), list("優しい人だね")
    pairs, matches, first, last = server.align_line(line, region)
    assert 2 * matches / (len(line) + last - first) < server.HUMAN_MIN_SIMILARITY
    assert server.line_similarity(line, region, pairs, matches, last - first) >= server.HUMAN_MIN_SIMILARITY
    # Different script on every character and nothing shared stays under the bar.
    line, region = list("あいうえ"), list("漢字熟語")
    pairs, matches, first, last = server.align_line(line, region)
    assert server.line_similarity(line, region, pairs, matches, last - first) <= 0.5


def test_a_window_end_cuts_what_the_window_does_not_own():
    cues = [wcue("明日は雨が降りそうですね", 1.4, 3.2)]
    out, _ = server.assist_cues(cues, [[1.0, 3.5, "明日は雨が降りそうですね。"]], [[1.3, 3.3]], LIMITS, 0,
                                window_end=3.4)
    assert out[0]["end"] <= 3.4


def test_a_whisper_cue_echoing_the_next_windows_line_goes():
    """The head of a line whose speech starts before the window's end: the next window shows it."""
    cues = [wcue("明日は雨が降りそうですね", 1.4, 3.2, seg=0), wcue("ちょっと待っ", 28.6, 29.9, seg=1)]
    nxt = [30.2, 33.0, "ちょっと待ってください。"]
    stats: dict = {}
    out, _ = server.assist_cues(cues, [[1.0, 3.5, "明日は雨が降りそうですね。"]], [[1.3, 3.3], [28.5, 33.0]],
                                LIMITS, 2, 30.0, stats, [nxt])
    assert [c["text"] for c in out] == ["明日は雨が降りそうですね。"] and stats["echoes"] == 1
    # Not a cue saying something else, nor one far from the line.
    other = [wcue("全然違う話", 28.6, 29.9, seg=1)]
    assert len(server.assist_cues(other, [], [[28.5, 33.0]], LIMITS, 2, 30.0, None, [nxt])[0]) == 1
    far = [wcue("ちょっと待っ", 20.0, 21.0, seg=1)]
    assert len(server.assist_cues(far, [], [[19.0, 33.0]], LIMITS, 2, 30.0, None, [nxt])[0]) == 1
    assert server.neighbour_lines([[1.0, 3.0, "あ"], [10.0, 12.0, "い"], [31.0, 33.0, "う"], [40.0, 41.0, "え"]],
                                  5.0, 30.0) == [[1.0, 3.0, "あ"], [31.0, 33.0, "う"]]


def test_a_line_too_long_for_the_screen_is_cut_at_its_own_sentence_marks():
    text = "今週は川の話をしました。橋を渡って向こう岸まで歩いてみましたがどうでしたか？" \
           "足は疲れませんでしたか？来週は山の話をしますから地図を用意してまた見に来てください。"
    pieces = server.split_long_line([10.0, 30.0, text], LIMITS)
    assert "".join(p[2] for p in pieces) == text and len(pieces) == 2
    assert all(len(p[2]) <= LIMITS.max_chars * LIMITS.max_lines for p in pieces)
    # Shared over the line's reach: a line this long had times too short for it.
    assert pieces[0][0] == 10.0 and pieces[-1][1] == pytest.approx(server.line_reach([10.0, 30.0, text]), abs=0.01)
    assert pieces[0][2].endswith("？")
    short = [1.0, 2.0, "はい。そうです。"]
    assert server.split_long_line(short, LIMITS) == [short]


def test_a_cue_without_words_counts_as_one_word():
    cues = [{"start": 1.4, "end": 3.2, "text": "明日は雨が降りそうですね", "seg": 0}]
    out, _ = server.assist_cues(cues, [[1.0, 3.5, "明日は雨が降りそうですね。"]], [[1.3, 3.3]], LIMITS, 1)
    assert out[0]["text"] == "明日は雨が降りそうですね。" and out[0]["src"] == "human"
    assert_words_spell(out[0])


def test_a_window_ends_at_the_speech_before_a_line_that_straddles_it():
    lines = [[1.0, 3.0, "あ"], [28.0, 33.0, "い"]]
    speech = [[0.9, 3.1], [27.6, 33.2]]
    # The onset of the line's speech, not the line's own start: the person's times run late.
    assert server.human_window_end(lines, speech, 0.0, 30.0, 30.0) == pytest.approx(27.3)  # in the silence before it
    assert server.human_window_end(lines[:1], speech, 0.0, 30.0, 30.0) == 30.0  # nothing straddles
    assert server.human_window_end(lines, [[0.9, 3.1], [5.0, 33.2]], 0.0, 30.0, 30.0) == 30.0  # no onset late enough
    # An onset inside another line is no place to cut, unless within the slack of that line's edges.
    flush = [[14.0, 24.0, "う"], [24.0, 33.0, "え"]]
    assert server.human_window_end(flush, [[13.8, 23.0], [23.4, 33.0]], 0.0, 30.0, 30.0) == pytest.approx(23.2)
    assert server.human_window_end(flush, [[2.0, 3.0], [13.8, 18.0], [18.5, 33.0]], 0.0, 30.0, 30.0) == \
        pytest.approx(13.5)
    assert server.human_window_end(flush, [[5.0, 18.0], [18.5, 33.0]], 0.0, 30.0, 30.0) == 30.0  # too early
    # A line whose times are too short for its text reaches past them.
    long = [[26.0, 27.0, "あいうえおかきくけこさしすせそたちつてとなにぬねの"]]
    assert server.human_window_end(long, [[20.0, 25.0], [25.8, 33.0]], 0.0, 30.0, 30.0) == pytest.approx(25.5)


def test_a_line_belongs_to_the_window_holding_most_of_it():
    lines = [[1.0, 3.0, "あ"], [27.0, 33.0, "い"], [29.5, 30.5, "う"]]
    assert server.lines_in_window(lines, 0.0, 30.0) == [lines[0]]
    assert server.lines_in_window(lines, 30.0, 60.0) == lines[1:]


# --------------------------------------------------------------------------- one window, end to end

class FakeModel:
    def __init__(self, segs):
        self.segs = segs

    def detect_language(self, audio=None, **kwargs):
        return "ja", 0.99, []

    def transcribe(self, audio, **kwargs):
        return iter(list(self.segs)), None


def seg(text, start, end):
    step = (end - start) / len(text)
    ws = [server.Word(ch, start + i * step, start + (i + 1) * step, 0.9) for i, ch in enumerate(text)]
    return SimpleNamespace(text=text, start=start, end=end, words=ws, no_speech_prob=0.05, avg_logprob=-0.2,
                           compression_ratio=1.2)


def make_worker(monkeypatch, tmp_path, segs, speech, **overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: [[offset + a, offset + b] for a, b in speech])
    base = dict(first_window=20.0, window=30.0, lookahead=900.0, model="large-v3", language="ja",
                language_patience=0.0, beam_size=1, initial_prompt="", idle_minutes=30, client_timeout=30.0,
                lyrics="off", human_subtitles="auto")
    base.update(overrides)
    app = server.App(SimpleNamespace(**base), model=FakeModel(segs), device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None, start_human_check=lambda s, info=None: None)
    return server.Transcriber(app), app


def spoken_session(seconds=60.0):
    audio = np.full(int(seconds * RATE), 0.05, dtype=np.float32)
    return server.Session(video_id=VIDEO, url="u", status="ready", audio=audio, duration=seconds)


SEGS = [seg("明日は雨が降りそうですね", 1.4, 3.2), seg("ちょっと待ってください", 10.0, 12.0)]
SPEECH = [[1.3, 3.3], [9.9, 12.1]]
TRACK = [[1.0, 3.5, "明日は雨が降りそうですね。"], [15.0, 17.0, "春の風が吹く"]]


def test_a_window_puts_the_track_in(monkeypatch, tmp_path, caplog):
    worker, app = make_worker(monkeypatch, tmp_path, SEGS, SPEECH)
    s = spoken_session()
    s.human_track, s.human_lines = "ja.json3:abc", [list(line) for line in TRACK]
    with caplog.at_level("INFO", logger=server.log.name):
        worker.process(s, 0.0, 30.0)
    assert [(c["text"], c.get("src")) for c in s.cues] == [("明日は雨が降りそうですね。", "human"),
                                                          ("ちょっと待ってください", None),
                                                          ("春の風が吹く", "human")]
    assert s.covered == [[0.0, 30.0]]  # covered is still what Whisper has seen
    assert "creator's subtitles: 1 lines aligned, 1 on their own times, 0 left to Whisper" in caplog.text


def test_human_subtitles_off_builds_whisper_alone(monkeypatch, tmp_path):
    worker, app = make_worker(monkeypatch, tmp_path, SEGS, SPEECH, human_subtitles="off")
    s = spoken_session()
    s.human_track, s.human_lines = "ja.json3:abc", [list(line) for line in TRACK]
    worker.process(s, 0.0, 30.0)
    assert [c["text"].rstrip("。") for c in s.cues] == ["明日は雨が降りそうですね", "ちょっと待ってください"]
    assert all("src" not in c for c in s.cues)  # and no line on its own times
    assert not server.human_subtitles_on(app.args)
    assert app.human_lines(s) == []


def test_a_line_straddling_the_window_end_is_left_to_the_next_window(monkeypatch, tmp_path):
    worker, app = make_worker(monkeypatch, tmp_path, SEGS, SPEECH)
    s = spoken_session()
    s.human_track, s.human_lines = "ja.json3:abc", [[1.0, 3.5, "明日は雨が降りそうですね。"], [27.0, 33.0, "それでは"]]
    worker, app = make_worker(monkeypatch, tmp_path, SEGS, SPEECH + [[26.8, 33.0]])
    worker.process(s, 0.0, 30.0)
    assert s.covered == [[0.0, 26.5]]
    assert all(c["start"] < 26.5 for c in s.cues)


def test_a_live_session_never_uses_a_track(monkeypatch, tmp_path):
    worker, app = make_worker(monkeypatch, tmp_path, SEGS, SPEECH)
    s = spoken_session()
    s.live = True
    s.human_lines = [list(line) for line in TRACK]
    assert app.human_lines(s) == []


# --------------------------------------------------------------------------- cache and /sync

def make_app(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    base = dict(first_window=20.0, window=30.0, lookahead=900.0, model="large-v3", language="ja",
                idle_minutes=30, language_patience=60.0, lyrics="auto", human_subtitles="auto")
    base.update(overrides)
    app = server.App(SimpleNamespace(**base), model=None, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None, start_human_check=lambda s, info=None: None)
    return app


HUMAN_CUE = {"start": 1.0, "end": 2.0, "text": "はい。", "words": [{"start": 1.0, "end": 1.6, "text": "はい。"}],
             "src": "human", "seg": 0}
WHISPER_CUE = {"start": 3.0, "end": 4.0, "text": "そう", "words": [{"start": 3.0, "end": 3.5, "text": "そう"}],
               "seg": 1}


def test_the_track_and_the_source_survive_a_cache_round_trip(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=10.0)
    s.cues = [dict(HUMAN_CUE, id=0), dict(WHISPER_CUE, id=1)]
    s.covered = [[0.0, 10.0]]
    s.human_track, s.human_lines = "ja.json3:abc", [[1.0, 2.0, "はい。"]]
    app.save_cache(s)
    data = json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert data["format"] == server.CACHE_FORMAT == 6  # optional fields: no format bump
    assert data["human_subs"] == {"track": "ja.json3:abc", "lines": [[1.0, 2.0, "はい。"]]}
    again = server.Session(video_id=VIDEO, url="u")
    app.load_cache(again)
    assert again.cues[0]["src"] == "human" and "src" not in again.cues[1]
    assert (again.human_track, again.human_lines) == ("ja.json3:abc", [[1.0, 2.0, "はい。"]])


def test_a_video_asked_and_found_without_a_track_is_not_asked_again(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=10.0)
    s.human_track = ""
    app.save_cache(s)
    data = json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert data["human_subs"] == {"track": None}
    again = server.Session(video_id=VIDEO, url="u")
    app.load_cache(again)
    assert (again.human_track, again.human_lines) == ("", [])


def test_a_record_never_asked_has_no_key_and_loads_as_never_asked(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=10.0)
    s.cues = [dict(WHISPER_CUE, id=0)]
    app.save_cache(s)
    data = json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert "human_subs" not in data
    again = server.Session(video_id=VIDEO, url="u")
    app.load_cache(again)
    assert again.human_track is None
    assert server.human_lines_from_record({"track": "t", "lines": [[2.0, 1.0, "x"], "bad", [1, 2, ""]]}) == ("t", [])


def test_sync_sends_no_source_unless_the_words_are_asked_for(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=10.0, status="ready")
    s.cues = [dict(HUMAN_CUE, id=0), dict(WHISPER_CUE, id=1)]
    s.human_track = "ja.json3:abc"
    app.sessions[VIDEO] = s
    plain = app.sync(VIDEO, "u", 0.0, 0)["cues"]
    assert json.dumps(plain, ensure_ascii=False) == (
        '[{"start": 1.0, "end": 2.0, "text": "はい。", "seg": 0, "id": 0}, '
        '{"start": 3.0, "end": 4.0, "text": "そう", "seg": 1, "id": 1}]')
    worded = app.sync(VIDEO, "u", 0.0, 0, words=True)["cues"]
    assert worded[0]["src"] == "human" and "src" not in worded[1] and worded[0]["words"] == HUMAN_CUE["words"]


def test_a_whisper_only_answer_is_byte_for_byte_what_it_was():
    cue = {"start": 3.0, "end": 4.0, "text": "そう", "seg": 1, "id": 1}
    assert server.without_words(cue) is cue
    assert server.without_words(dict(cue, words=[])) == cue


def test_a_track_that_arrives_late_rebuilds_the_cues_under_a_new_token(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=30.0, status="ready")
    s.cues = [dict(wcue("明日は雨が降りそうですね", 1.4, 3.2, seg=0), id=0),
              dict(wcue("ちょっと待ってください", 10.0, 12.0, seg=1), id=1)]
    s.covered, s.speech, s.seg_next = [[0.0, 20.0]], [[1.3, 3.3], [9.9, 12.1]], 2
    token = s.token
    app.sessions[VIDEO] = s
    app.apply_human_track(s, "ja.json3:abc", [[1.0, 3.5, "明日は雨が降りそうですね。"], [19.0, 23.0, "先の行"]])
    assert s.token != token
    assert [(c["id"], c["text"], c.get("src")) for c in s.cues] == [(0, "明日は雨が降りそうですね。", "human"),
                                                                   (1, "ちょっと待ってください", None)]
    assert s.seg_next == 3
    data = json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert data["human_subs"]["track"] == "ja.json3:abc" and data["cues"][0]["src"] == "human"
    # A second arrival (a restart, a stale thread) rebuilds nothing already built with a track.
    token = s.token
    app.apply_human_track(s, "ja.json3:abc", [[1.0, 3.5, "明日は雨が降りそうですね。"]])
    assert s.token == token


# --------------------------------------------------------------------------- the request

class FakeResponse:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]

    def close(self):
        pass


class FakeYoutubeDL:
    def __init__(self, opts, info, calls, track=JSON3.encode("utf-8")):
        self.opts, self.info, self.calls, self.track = opts, info, calls, track

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        self.calls.append("extract_info")
        return dict(self.info)

    def process_ie_result(self, info, download=True):
        self.calls.append("process_ie_result")
        (server.CACHE_DIR / f"{VIDEO}.webm").write_bytes(b"audio")

    def urlopen(self, url):
        self.calls.append(("urlopen", url))
        return FakeResponse(self.track)


SUBS_INFO = {"title": "t", "duration": 60.0, "language": "ja",
             "subtitles": {"ja": [fmt("vtt"), fmt("json3")]},
             "automatic_captions": {"ja-orig": [fmt("json3", url="https://x/api/timedtext?kind=asr")]}}


def make_fetcher(monkeypatch, tmp_path, info, **overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    calls: list = []
    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = lambda opts: FakeYoutubeDL(opts, info, calls)
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)
    base = dict(cookies_from_browser="firefox", cookies="", allow_remote_ejs=False, js_runtime="auto",
                first_window=20.0, window=30.0, language="ja", language_patience=60.0, human_subtitles="auto")
    base.update(overrides)
    fetcher = server.Fetcher(SimpleNamespace(**base))
    got: list = []
    fetcher.on_human_subtitles = lambda s, track, lines: got.append((track, lines))
    return fetcher, calls, got


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def test_the_download_asks_for_the_track_beside_the_audio(monkeypatch, tmp_path):
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO)
    s = server.Session(video_id=VIDEO, url="u")
    assert fetcher.download_once(s, resume=True) == tmp_path / f"{VIDEO}.webm"
    assert wait_for(lambda: got)
    track, lines = got[0]
    assert track.startswith("ja.json3:") and lines == CLEAN
    # One metadata request, the download's own; the track is the creator's json3, never ja-orig.
    assert calls.count("extract_info") == 1
    assert [c for c in calls if isinstance(c, tuple)] == [("urlopen", SUBS_INFO["subtitles"]["ja"][1]["url"])]


def test_the_request_carries_the_downloads_cookies(monkeypatch, tmp_path):
    seen: list = []
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO)
    real = sys.modules["yt_dlp"].YoutubeDL
    sys.modules["yt_dlp"].YoutubeDL = lambda opts: seen.append(opts) or real(opts)
    fetcher.check_human_subtitles(server.Session(video_id=VIDEO, url="u"))
    assert seen[0]["cookiesfrombrowser"] == ("firefox",) and got


def test_cached_audio_asks_for_the_metadata_itself(monkeypatch, tmp_path):
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO)
    fetcher.check_human_subtitles(server.Session(video_id=VIDEO, url="u"))
    assert calls[0] == "extract_info" and got[0][1] == CLEAN


def test_no_track_a_foreign_video_or_live_gives_none(monkeypatch, tmp_path):
    for info in ({"language": "ja"}, dict(SUBS_INFO, language="en"), dict(SUBS_INFO, is_live=True),
                 {"language": "ja", "subtitles": {}, "automatic_captions": SUBS_INFO["automatic_captions"]}):
        fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, info)
        fetcher.check_human_subtitles(server.Session(video_id=VIDEO, url="u"))
        assert got == [("", [])] and not [c for c in calls if isinstance(c, tuple)], info


def test_a_failed_request_leaves_the_video_unasked_and_whisper_alone(monkeypatch, tmp_path):
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO)

    def broken(opts):
        raise OSError("HTTP Error 429: Too Many Requests")
    sys.modules["yt_dlp"].YoutubeDL = broken
    s = server.Session(video_id=VIDEO, url="u")
    fetcher.check_human_subtitles(s)
    assert got == [] and s.human_track is None


def test_a_session_asks_once_and_off_never_asks(monkeypatch, tmp_path):
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO)
    s = server.Session(video_id=VIDEO, url="u")
    fetcher.start_human_check(s)
    fetcher.start_human_check(s)
    assert wait_for(lambda: got) and time.sleep(0.05) is None and len(got) == 1
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO, human_subtitles="off")
    fetcher.start_human_check(server.Session(video_id=VIDEO, url="u"))
    fetcher, calls, got2 = make_fetcher(monkeypatch, tmp_path, SUBS_INFO, language="en")
    fetcher.start_human_check(server.Session(video_id=VIDEO, url="u"))
    time.sleep(0.05)
    assert calls == [] and got == [] and got2 == []


def test_human_subtitles_off_downloads_as_before(monkeypatch, tmp_path):
    fetcher, calls, got = make_fetcher(monkeypatch, tmp_path, SUBS_INFO, human_subtitles="off")
    s = server.Session(video_id=VIDEO, url="u")
    assert fetcher.download_once(s, resume=True) == tmp_path / f"{VIDEO}.webm"
    time.sleep(0.05)
    assert calls == ["extract_info", "process_ie_result"] and got == []


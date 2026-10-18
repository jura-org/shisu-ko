"""Characters no transcript holds, dropped from what the model writes and from cached cues.

Whisper spells a character its tokenizer has no token for in UTF-8 bytes, and large-v3 spelled
U+F7ED of the private use area that way in s1UVis0tPiE, three times, which Firefox drew as boxes
reading F7 ED. clean_model_text() drops it and its kind; absolute_words() cleans every word a
segment brings onto the video's clock, whichever backend wrote it, and App.load_cache() cleans the
cues of a record written before, with CACHE_FORMAT left as it is.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()
W = server.Word
VIDEO = "s1UVis0tPiE"
PUA = "\uf7ed"


# --------------------------------------------------------------------------- clean_model_text

def test_the_private_use_character_of_s1UVis0tPiE_goes():
    assert server.clean_model_text("ももすずねねを励ましたい\uf7ed励まされている\uf7ed") == "ももすずねねを励ましたい励まされている"


@pytest.mark.parametrize("ch", [
    "\ue000", "\uf7ed", "\uf8ff", "\U000f0000", "\U0010fffd",   # Co: the private use areas
    "\U00040000", "\ufdd0", "\uffff", "\U0001fffe",             # Cn: unassigned, and noncharacters
    "\ud800",                                                   # Cs: a lone surrogate
    "\x00", "\x07", "\t", "\r", "\x1b", "\x7f", "\x85",         # Cc
    "\u200b", "\u200d", "\u202e", "\ufeff", "\u00ad",           # Cf
    "\u2028", "\u2029",                                         # Zl, Zp
    "\ufffd",                                                   # bytes that never made a character
])
def test_every_character_no_transcript_holds_goes(ch):
    assert server.clean_model_text(f"励まし{ch}たい") == "励ましたい"
    assert server.clean_model_text(f"励まし{ch}たい", keep_newline=True) == "励ましたい"


def test_the_row_break_stays_only_where_asked():
    assert server.clean_model_text("うん。\nそうだね") == "うん。そうだね"
    assert server.clean_model_text("うん。\nそうだね\uf7ed", keep_newline=True) == "うん。\nそうだね"
    assert server.clean_model_text("うん。\r\nそうだね\t", keep_newline=True) == "うん。\nそうだね"


def test_japanese_and_its_punctuation_are_untouched():
    text = ("はい、そうですね。今日はよろしくお願いします！それで、どう思いますか？"
            "「カメラ」（ｶﾒﾗ）～…ー々〜・　ABC 123 ♪🎵 iPhone")
    assert server.clean_model_text(text) == text
    assert server.clean_model_text(text, keep_newline=True) == text
    assert server.clean_model_text("") == ""


# --------------------------------------------------------------------------- words on the clock

def segment(spec, prob: float = 0.9):
    """A segment from (text, start, end) words, its text their join."""
    ws = [W(t, a, b, prob) for t, a, b in spec]
    return SimpleNamespace(text="".join(t for t, _, _ in spec), start=ws[0].start, end=ws[-1].end, words=ws,
                           no_speech_prob=0.1, avg_logprob=-0.2, compression_ratio=1.2)


# The 207.3 s line of s1UVis0tPiE as large-v3 wrote it, on made-up timings: U+F7ED a word of its
# own, twice, each where a sentence mark belongs.
LINE = [("もも", 0.0, 0.3), ("すず", 0.3, 0.6), ("ねね", 0.6, 0.9), ("を", 0.9, 1.0), ("励まし", 1.0, 1.4),
        ("たい", 1.4, 1.7), (PUA, 1.7, 2.4), ("励まさ", 2.4, 2.9), ("れて", 2.9, 3.1), ("いる", 3.1, 3.4),
        (PUA, 3.4, 3.9)]


def test_absolute_words_drop_a_word_that_was_nothing_else():
    words = server.absolute_words(segment(LINE), 200.0)
    assert [w.word for w in words] == ["もも", "すず", "ねね", "を", "励まし", "たい", "励まさ", "れて", "いる"]
    # The others keep their own times, on the video's clock: the 0.7 s the first U+F7ED spanned is a gap.
    assert (words[5].end, words[6].start) == (pytest.approx(201.7), pytest.approx(202.4))


def test_absolute_words_clean_a_character_inside_a_word():
    words = server.absolute_words(segment([("裏切り\uf7ed", 0.0, 0.6), ("いや\ufffd、", 0.6, 1.0)]), 10.0)
    assert [(w.word, w.start, w.end) for w in words] == [("裏切り", 10.0, 10.6), ("いや、", 10.6, 11.0)]


def built_text(cues) -> str:
    for c in cues:
        assert "".join(w["text"] for w in c["words"]) == c["text"]
    return "".join(c["text"] for c in cues)


def test_the_gap_a_dropped_word_leaves_is_a_pause_to_punctuate_words():
    cues, _ = server.build_window_cues([segment(LINE)], 200.0, [[200.0, 204.0]], server.CueLimits(), 0)
    text = built_text(cues)
    assert PUA not in text
    # たい is a weak sentence end and 0.7 s is past sentence_pause_weak, so a mark goes where the
    # U+F7ED stood. Left in as a word it hid the pause, and the text ended in it instead of in たい.
    assert text.replace("\n", "") == "ももすずねねを励ましたい。励まされている"


def test_a_character_inside_a_word_is_cleaned_out_of_the_cue():
    seg = segment([("ミュート", 0.0, 0.5), ("は", 0.5, 0.6), ("裏切り\uf7ed", 0.6, 1.2), ("いや、", 1.2, 1.6),
                   ("もうね", 1.6, 2.1)])
    cues, _ = server.build_window_cues([seg], 219.0, [[219.0, 221.5]], server.CueLimits(), 0)
    assert built_text(cues) == "ミュートは裏切りいや、もうね"


def test_a_kitsune_segment_is_cleaned_on_the_same_path():
    # The cleaning knows no backend: Kitsune's segments reach absolute_words() as Whisper's do.
    engine = server.kitsune_engine()
    segs = engine.make_segments([engine.Word(t, a, b, 0.9) for t, a, b in LINE])
    assert PUA in segs[0].text
    cues, _ = server.build_window_cues(segs, 200.0, [[200.0, 204.0]], server.CueLimits(), 0)
    assert built_text(cues).replace("\n", "") == "ももすずねねを励ましたい。励まされている"


def test_the_gates_judge_the_text_cleaned():
    # A sign-off with U+F7ED inside it is still the sign-off. Over 0.6 speech nothing but the
    # blocklist refuses it, so the blocklist has to find the phrase in the segment's text.
    seg = segment([("ご視聴", 10.0, 10.8), (PUA, 10.8, 11.0), ("ありがとうございました", 11.0, 12.0)])
    assert server.gate_segment(seg, 0.0, [[10.0, 11.2]])[1] == "blocklist"
    assert server.lyrics_reason(seg, server.absolute_words(seg, 0.0)) == "blocklist"
    assert server.segment_text(seg) == "ご視聴ありがとうございました"


def test_splice_segments_counts_the_words_absolute_words_kept():
    # unheard_runs() indexes the cleaned word list; the piece must take those words from the segment,
    # not the ones at the same places among the model's own, where a U+F7ED comes first.
    speech = [[1.0, 8.0], [8.1, 14.0]]
    late = segment([("ということで、", 8.3, 8.7), ("始め", 8.7, 10.5), ("ましょうか", 10.5, 13.0)])
    plain = segment([(PUA, 4.2, 4.5), ("今日も", 4.5, 5.5), ("お願いします", 5.5, 7.4), ("という", 7.6, 7.9),
                     ("こと", 7.9, 8.28)])
    stretches = server.skipped_speech(server.kept_spans([late], 0.0, speech), speech)
    spliced, added = server.splice_segments([late], [plain], 0.0, speech, stretches)
    assert added == 1
    assert [seg.text for seg in spliced] == ["今日もお願いします", "ということで、始めましょうか"]


# --------------------------------------------------------------------------- the cue cache

def test_clean_cached_cue_leaves_a_clean_cue_as_it_is():
    cue = {"start": 1.0, "end": 2.0, "text": "うん。\nそうだね", "seg": 0,
           "words": [{"start": 1.0, "end": 1.4, "text": "うん。\n"}, {"start": 1.5, "end": 2.0, "text": "そうだね"}]}
    assert server.clean_cached_cue(cue) is cue


def test_clean_cached_cue_keeps_the_row_break_and_cleans_the_words_with_the_text():
    cue = {"start": 1.0, "end": 3.0, "text": "うん。\nそうだ\uf7edね\uf7ed", "seg": 2, "id": 4,
           "words": [{"start": 1.0, "end": 1.4, "text": "うん。\n"}, {"start": 1.5, "end": 2.0, "text": "そうだ\uf7ed"},
                     {"start": 2.0, "end": 2.4, "text": "ね"}, {"start": 2.4, "end": 2.9, "text": PUA}]}
    out = server.clean_cached_cue(cue)
    assert out["text"] == "うん。\nそうだね"
    assert out["words"] == [{"start": 1.0, "end": 1.4, "text": "うん。\n"}, {"start": 1.5, "end": 2.0, "text": "そうだ"},
                            {"start": 2.0, "end": 2.4, "text": "ね"}]
    assert {k: out[k] for k in ("start", "end", "seg", "id")} == {"start": 1.0, "end": 3.0, "seg": 2, "id": 4}
    assert PUA in cue["text"] and len(cue["words"]) == 4  # a copy: the record's own cue is left alone


def test_clean_cached_cue_strips_a_row_that_held_nothing_else():
    cue = {"start": 1.0, "end": 3.0, "text": "\uf7ed\nそうだね", "seg": 0,
           "words": [{"start": 1.0, "end": 1.4, "text": "\uf7ed\n"}, {"start": 1.5, "end": 2.0, "text": "そうだね"}]}
    out = server.clean_cached_cue(cue)
    assert out["text"] == "そうだね" and out["words"] == [{"start": 1.5, "end": 2.0, "text": "そうだね"}]


def test_clean_cached_cue_drops_words_that_no_longer_spell_the_text_and_a_cue_left_empty():
    stale = {"start": 1.0, "end": 2.0, "text": "はい\uf7ed", "seg": 0, "words": [{"start": 1.0, "end": 2.0, "text": "はい。"}]}
    out = server.clean_cached_cue(stale)
    assert out["text"] == "はい" and "words" not in out
    assert server.clean_cached_cue({"start": 1.0, "end": 2.0, "text": PUA, "seg": 0}) is None
    assert server.clean_cached_cue({"start": 1.0, "end": 2.0, "text": "\uf7ed\n\ufffd", "seg": 0,
                                    "words": [{"start": 1.0, "end": 2.0, "text": "\uf7ed\n\ufffd"}]}) is None


def make_app(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = SimpleNamespace(first_window=20.0, window=30.0, lookahead=900.0, model="large-v3", language="ja",
                           idle_minutes=30, language_patience=60.0, lyrics="auto")
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


WORDED = {"start": 223.09, "end": 225.33, "text": "そうそう", "seg": 10,
          "words": [{"start": 223.1, "end": 223.6, "text": "そう"}, {"start": 223.6, "end": 224.2, "text": "そう"}]}


def test_load_cache_cleans_a_record_written_before_and_the_next_save_writes_it_clean(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="shisu-ko")
    app = make_app(monkeypatch, tmp_path)
    # s1UVis0tPiE's two cues as the record holds them, a cue that was nothing but the character,
    # and two clean ones around them.
    cues = [
        {"start": 203.0, "end": 206.5, "text": "みんなありがとう", "seg": 4},
        {"start": 207.3, "end": 211.38, "text": "ももすずねねを励ましたい\uf7ed励まされている\uf7ed", "seg": 5},
        {"start": 216.8, "end": 217.6, "text": PUA, "seg": 6},
        {"start": 219.79, "end": 222.99, "text": "ミュートは裏切り\uf7edいや、もうね、あの、可愛げがない。", "seg": 9},
        WORDED,
    ]
    record = {"video_id": VIDEO, "title": "t", "duration": 300.0, "format": 6, "model": "large-v3",
              "language": "ja", "cues": cues, "covered": [[0.0, 300.0]], "speech": [[203.0, 225.0]],
              "lyrics": "auto", "unheard_rule": 2,
              "language_state": {"foreign_seconds": 0.0, "heard": None, "paused": False}}
    path = tmp_path / f"{VIDEO}.cues.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    resp = app.sync(VIDEO, "u", 207.0, 0)  # a restarted server's first sync loads the record
    want = ["みんなありがとう", "ももすずねねを励ましたい励まされている",
            "ミュートは裏切りいや、もうね、あの、可愛げがない。", "そうそう"]
    assert [c["text"] for c in resp["cues"]] == want
    s = app.sessions[VIDEO]
    assert s.status == "ready"  # covered to its end: cleaned, not transcribed again
    assert [c["id"] for c in s.cues] == [0, 1, 2, 3] and [c["seg"] for c in s.cues] == [4, 5, 9, 10]
    assert s.cues[3]["words"] == WORDED["words"]
    assert any("3 cached cues held characters" in r.getMessage() for r in caplog.records)

    app.save_cache(s)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == server.CACHE_FORMAT == 6
    assert [c["text"] for c in data["cues"]] == want
    assert PUA not in path.read_text(encoding="utf-8")

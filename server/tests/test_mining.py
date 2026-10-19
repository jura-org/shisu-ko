"""Mining on the server: the match.js port, the Miner's attach with a fake AnkiConnect, the frame
grab, /sync's `anki`, `mine` and `mined`, and the switches.

The matching cases are addon/tests/match.test.js's from 0.21.1, ported one for one: the extension's
matcher moved here with its thresholds. Every fixture is invented; nothing reaches a real Anki or
YouTube.
"""
from __future__ import annotations

import copy
import io
import math
from types import SimpleNamespace

import pytest

from _serverlib import load_server
from test_update_endpoint import Served, make_app

server = load_server()
normalize, similarity, match_cue = server.match_normalize, server.match_similarity, server.match_cue
MIN_SIMILARITY = server.MIN_SIMILARITY


def rank_first(cue_id):
    return lambda c: 0 if c["id"] == cue_id else math.inf


# ------------------------------------------------------------------ normalize

def test_normalize_strips_the_markup_yomitan_puts_around_the_word():
    assert normalize("これは<b>猫</b>です。") == "これは猫です"
    assert normalize('<span class="x">猫</span>') == "猫"


def test_normalize_decodes_the_entities_a_card_can_carry():
    assert normalize("a&nbsp;b&amp;c") == "ab&c"
    assert normalize("&lt;tag&gt; &quot;q&quot; &#39;s&#39;") == "<tag>qs"


def test_normalize_drops_bracket_furigana_whitespace_and_punctuation():
    assert normalize(" 食[た]べる") == "食べる"
    assert normalize("「はい」、そうです！") == "はいそうです"
    assert normalize("Look at the cat.") == "Lookatthecat"
    assert normalize("はい．そうです．") == "はいそうです"
    assert normalize("全角　空白﻿") == "全角空白"
    assert normalize(None) == ""


def test_normalize_drops_the_readings_of_html_ruby():
    ruby = ("<ruby>今日<rt>きょう</rt></ruby>は<ruby>学校<rt>がっこう</rt></ruby>で<ruby>友達<rt>ともだち</rt></ruby>と"
            "<b><ruby>勉強<rt>べんきょう</rt></ruby></b>した")
    assert normalize(ruby) == "今日は学校で友達と勉強した"
    assert normalize('<ruby>食<rp>(</rp><rt class="r">た</rt><rp>)</rp></ruby>べる') == "食べる"
    assert normalize("<ruby>食<rt>た</ruby>べる") == "食べる"
    assert normalize("<RUBY>食<RT>た</RT></RUBY>べる") == "食べる"


# addon/tests/words.test.js's RUBY_SHAPES: the word colours read a card's word the same way.
RUBY_SHAPES = [
    "<ruby>食<rt>た</rt></ruby>べる", "<ruby>食<rt>た</ruby>べる", "<ruby>食<rp>(</rp><rt>た</rt><rp>)</rp></ruby>べる",
    "<ruby>食<rp>(<rt>た<rp>)</ruby>べる", "<RUBY>食<RT>た</RUBY>べる", "<ruby><b>食</b><rt>た</ruby>べる",
    " 食[た]べる", "<b>食</b>べる",
]


def test_normalize_reads_every_ruby_shape_as_the_word_colours_do():
    for shape in RUBY_SHAPES:
        assert normalize(shape) == "食べる", shape
    assert normalize("<ruby>日本<rt>にほん</ruby><ruby>語<rt>ご</ruby>") == "日本語"
    assert normalize("<ruby>日本<rt>にほん</rt></ruby><ruby>語<rt>ご</rt></ruby>") == "日本語"


def test_a_ruby_card_matches_its_line_and_earns_the_word_bonus():
    sentence = "<ruby>今日<rt>きょう</rt></ruby>は<ruby>学校<rt>がっこう</rt></ruby>で<b><ruby>勉強<rt>べんきょう</rt></ruby></b>した"
    cues = [{"id": 0, "start": 0, "text": "今日は学校で勉強した"}, {"id": 1, "start": 5, "text": "猫が窓から外を見ている"}]
    assert similarity(sentence, cues[0]["text"]) == 1
    assert match_cue(cues, {"sentence": sentence, "word": "<ruby>勉強<rt>べんきょう</rt></ruby>"})["id"] == 0
    assert match_cue([{"id": 2, "start": 9, "text": "ご飯を食べる"}],
                     {"sentence": "", "word": "<ruby>食<rt>た</rt></ruby>べる"})["id"] == 2


# ------------------------------------------------------------------ similarity

def test_similarity_is_1_when_either_text_contains_the_other():
    assert similarity("これは猫です。", "これは<b>猫</b>です。とても可愛い。") == 1
    assert similarity("これは猫です。とても可愛い。", "これは猫です") == 1
    assert similarity("これは猫です", "これは猫です、とても可愛い、ずっと見ていられる。") == 1


def test_similarity_ignores_a_scrap_swallowed_by_a_long_card_sentence():
    assert similarity("今日はとてもいい天気ですね", "ですね") == 0
    assert similarity("はい、そうですね、わかりました。", "はい") == 0
    assert similarity("ですね。", "ですね") == 1
    assert similarity("そうですね", "そうですね。ええ") == 1


def test_similarity_accepts_a_short_card_sentence_a_longer_line_said():
    assert similarity("<b>嘘</b>でしょ。", "嘘でしょ。本当にそんなことがあったの") == 1
    assert similarity("はい", "はい、そうですね、わかりました。") == 1


def test_similarity_stays_high_across_a_one_character_difference():
    value = similarity("今日はとてもいい天気ですね", "今日はとてもいい天気ですよ")
    assert value > 0.9 and value >= MIN_SIMILARITY


def test_similarity_refuses_two_sentences_that_merely_end_alike():
    assert similarity("別の字幕です", "これはテスト字幕です") < MIN_SIMILARITY


def test_similarity_is_low_for_two_unrelated_sentences():
    assert similarity("今日はとてもいい天気ですね", "猫が窓から外を見ている") < MIN_SIMILARITY


def test_similarity_refuses_strings_too_short_for_bigrams():
    assert similarity("猫", "犬") == 0
    assert similarity("", "これは猫です") == 0
    assert similarity("これは猫です", "") == 0


# ------------------------------------------------------------------ match_cue

CUES = [
    {"id": 0, "start": 0, "text": "おはようございます"},
    {"id": 1, "start": 5, "text": "これは猫です"},
    {"id": 2, "start": 9, "text": "とても可愛いですね"},
]


def test_match_cue_finds_the_line_a_sentence_came_from():
    assert match_cue(CUES, {"sentence": "これは<b>猫</b>です。", "word": "猫"})["id"] == 1


def test_match_cue_breaks_a_tie_by_rank_then_by_the_playhead():
    twins = [{"id": 0, "start": 10, "text": "これは猫です"}, {"id": 1, "start": 40, "text": "これは猫です"}]
    note = {"sentence": "これは猫です。", "word": ""}
    assert match_cue(twins, note, t=38)["id"] == 1
    assert match_cue(twins, note, t=12)["id"] == 0
    assert match_cue(twins, note, rank=rank_first(0), t=38)["id"] == 0


def test_match_cue_lets_the_word_decide_between_two_that_pass():
    cues = [{"id": 0, "start": 0, "text": "今日はとてもいい天気でした"}, {"id": 1, "start": 4, "text": "今日はとてもいい天気ですよ猫"}]
    sentence = "今日はとてもいい天気ですね"
    assert match_cue(cues, {"sentence": sentence, "word": ""})["id"] == 1
    assert match_cue(cues, {"sentence": sentence, "word": "でした"})["id"] == 0


def test_match_cue_falls_back_to_the_word_without_a_sentence():
    cues = [{"id": 0, "start": 0, "text": "猫が好きです"}, {"id": 1, "start": 5, "text": "黒い猫だ"}]
    assert match_cue(cues, {"sentence": "", "word": "猫"}, rank=rank_first(1))["id"] == 1
    assert match_cue(cues, {"sentence": "", "word": "犬"}) is None


def test_match_cue_refuses_a_sentence_below_the_threshold_even_with_the_word():
    assert match_cue(CUES, {"sentence": "まったく別の文です。", "word": "猫"}) is None


LONG_SENTENCE = "昨日の夜は遅くまで起きていましたね"
WITH_SCRAP = [{"id": 0, "start": 100, "text": "ですね"}, {"id": 1, "start": 200, "text": "昨日の夜は遅くまで起きていましたね"}]


def test_match_cue_ignores_a_line_too_short_to_be_the_card():
    cue = match_cue(WITH_SCRAP, {"sentence": LONG_SENTENCE, "word": ""}, rank=lambda c: 0 if c["id"] == 0 else 1, t=100)
    assert cue["id"] == 1
    assert match_cue([WITH_SCRAP[0]], {"sentence": LONG_SENTENCE, "word": ""}) is None


MERGED = [
    {"id": 0, "start": 0, "text": "おはようございます"},
    {"id": 1, "start": 4, "text": "嘘でしょ。本当にそんなことがあったの"},
    {"id": 2, "start": 9, "text": "分かった。じゃあ明日また来るね"},
    {"id": 3, "start": 14, "text": "駄目だ。もう二度と会わない"},
    {"id": 4, "start": 19, "text": "行こう。時間がないんだ"},
]


def test_match_cue_finds_the_merged_line_a_short_sentence_was_cut_from():
    assert match_cue(MERGED, {"sentence": "<b>嘘</b>でしょ。", "word": "嘘"})["id"] == 1
    assert match_cue(MERGED, {"sentence": "分かった。", "word": "分かる"})["id"] == 2
    assert match_cue(MERGED, {"sentence": "駄目だ。", "word": "駄目"})["id"] == 3
    assert match_cue(MERGED, {"sentence": "行こう。", "word": "行く"})["id"] == 4
    assert match_cue(MERGED, {"sentence": "行こう。", "word": ""})["id"] == 4
    assert match_cue(MERGED, {"sentence": "嘘だよ。", "word": "嘘"}) is None


def test_match_cue_lets_rank_decide_between_the_sentence_and_a_line_running_past_it():
    cues = [{"id": 0, "start": 100, "text": "嘘でしょ"}, {"id": 1, "start": 400, "text": "嘘でしょ。本当にそんなことがあったの"}]
    note = {"sentence": "<b>嘘</b>でしょ。", "word": "嘘"}
    assert match_cue(cues, note, rank=rank_first(1), t=100)["id"] == 1
    assert match_cue(cues, note, rank=rank_first(0), t=400)["id"] == 0
    assert match_cue(cues, note, t=390)["id"] == 1
    assert match_cue(cues, note, t=110)["id"] == 0


def test_match_cue_gives_a_card_its_own_line_not_one_holding_its_words_mid_clause():
    cues = [{"id": 1, "start": 598, "text": "はい、そうですね"}, {"id": 2, "start": 602, "text": "はいはい、わかりました"},
            {"id": 3, "start": 2400, "text": "はい"}]
    note = {"sentence": "はい", "word": "はい"}
    assert match_cue(cues, note, t=600)["id"] == 3
    assert match_cue(cues, note, rank=rank_first(1), t=600)["id"] == 3
    longer = [{"id": 1, "start": 598, "text": "本当にそうですね、そう思いますよ"}, {"id": 3, "start": 2400, "text": "本当にそうですね"}]
    assert match_cue(longer, {"sentence": "本当にそうですね", "word": ""}, rank=rank_first(1), t=600)["id"] == 3
    assert match_cue(cues[:2], note, t=600)["id"] == 1


def test_match_cue_treats_a_quote_as_an_end_of_the_sentence():
    cues = [{"id": 0, "start": 100, "text": "はい"}, {"id": 1, "start": 400, "text": "彼は「はい」と答えた"}]
    note = {"sentence": "はい", "word": "はい"}
    assert match_cue(cues, note, rank=rank_first(1), t=100)["id"] == 1
    assert match_cue(cues, note, rank=rank_first(0), t=400)["id"] == 0
    assert match_cue(cues, note, t=390)["id"] == 1
    quoting = [{"id": 0, "start": 100, "text": "彼は「はい」と答えた"}, {"id": 1, "start": 400, "text": "そうか。彼は「はい」と答えた。"}]
    outside = {"sentence": "彼は「はい」と<b>答えた</b>。", "word": "答える"}
    assert match_cue(quoting, outside, rank=rank_first(1), t=100)["id"] == 1


def test_match_cue_prefers_the_line_that_explains_more_of_the_sentence():
    cues = [{"id": 0, "start": 10, "text": "昨日の夜は遅くまで"}, {"id": 1, "start": 20, "text": "昨日の夜は遅くまで起きていましたね"}]
    assert match_cue(cues, {"sentence": LONG_SENTENCE, "word": ""}, rank=lambda c: 0 if c["id"] == 0 else 1)["id"] == 1


def test_match_cue_matches_a_long_line_inside_a_longer_typed_sentence():
    cues = [{"id": 0, "start": 5, "text": "夜は遅くまで起きて"}]
    assert match_cue(cues, {"sentence": "昨日の夜は遅くまで起きていましたね、本当に眠いです", "word": ""})["id"] == 0


def test_match_cue_returns_none_with_nothing_to_match_on():
    assert match_cue(CUES, {"sentence": "", "word": ""}) is None
    assert match_cue(CUES, {}) is None
    assert match_cue([], {"sentence": "これは猫です"}) is None
    assert match_cue(None, {"sentence": "これは猫です"}) is None


def test_match_cue_stays_quick_on_a_full_transcript():
    import time

    cues = [{"id": i, "start": i * 3, "text": f"{i}番目の文です"} for i in range(5000)]
    cues.append({"id": 5000, "start": 15000, "text": "これは猫です"})
    started = time.perf_counter()
    assert match_cue(cues, {"sentence": "これは<b>猫</b>です。", "word": "猫"}, t=0)["id"] == 5000
    assert time.perf_counter() - started < 1.0


# ------------------------------------------------------------------ fields and settings

def test_field_key_names_a_field_in_whatever_case_the_note_type_spells_it():
    eminent = {"picture": {}, "sentenceAudio": {}, "sentence": {}}
    assert server.field_key(eminent, "Picture") == "picture"
    assert server.field_key(eminent, "SentenceAudio") == "sentenceAudio"
    assert server.field_key(eminent, " picture ") == "picture"
    assert server.field_key(eminent, "Image") is None
    assert server.field_key(eminent, "") is None
    assert server.field_key({"Picture": {}, "picture": {}}, "PICTURE") is None  # two candidates: no guess
    assert server.field_key({"Picture": {}, "picture": {}}, "picture") == "picture"


def test_escape_html_keeps_text_text_and_the_seam_a_line_break():
    assert server.escape_html("1<2 & \"A\" 'B'") == "1&lt;2 &amp; &quot;A&quot; &#39;B&#39;"
    assert server.escape_html("一行目\n二行目\r\n三") == "一行目<br>二行目<br>三"
    assert server.escape_html("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"


# ------------------------------------------------------------------ the whole line, not Yomitan's fragment
# addon/tests/background.test.js's extendSentenceField() cases from 0.21.1, ported one for one.

grow = server.grow_sentence


def test_grow_sentence_grows_a_fragment_into_the_cue_and_keeps_yomitans_bold():
    assert grow("これは<b>猫</b>です。", "これは猫です。とても可愛い。") == "これは<b>猫</b>です。とても可愛い。"


def test_grow_sentence_writes_the_cue_plain_when_the_bold_word_cannot_be_located():
    # Normalising drops the space inside the bold span, so "thecat" is not in the raw cue.
    assert grow("Look at <b>the cat</b>.", "Look at the cat. It sleeps.") == "Look at the cat. It sleeps."


def test_grow_sentence_leaves_a_field_that_already_holds_the_whole_cue_alone():
    assert grow("これは<b>猫</b>です。", "これは猫です。") is None
    assert grow("これは<b>猫</b>です。とても可愛い。", "これは猫です。とても可愛い。") is None


def test_grow_sentence_refuses_text_that_is_not_part_of_the_cue():
    assert grow("まったく別の文です。", "これは猫です。とても可愛い。") is None
    assert grow("", "これは猫です。") is None


def test_grow_sentence_writes_the_transcription_as_text_with_only_its_own_bold_as_markup():
    assert (grow("これは<b>猫</b>です。", "これは猫です。<script>alert(1)</script>とても可愛い。")
            == "これは<b>猫</b>です。&lt;script&gt;alert(1)&lt;/script&gt;とても可愛い。")
    assert grow("Look at <b>the cat</b>.", "Look at the cat. 1<2 & \"so\" it's") == "Look at the cat. 1&lt;2 &amp; &quot;so&quot; it&#39;s"


def test_grow_sentence_grows_inside_its_own_row_never_across_the_seam():
    assert grow("これは<b>猫</b>です。", "これは猫です。とても可愛い\n待って!") == "これは<b>猫</b>です。とても可愛い"
    assert grow("<b>体</b>動かない", "体動かない\n待って!") is None  # the whole row already
    assert grow("<b>待って</b>", "体動かない。もう\n待って!今すぐ") == "<b>待って</b>!今すぐ"


def test_anki_settings_fill_the_defaults_and_refuse_what_is_not_a_field_name():
    assert server.anki_settings({}) == server.ANKI_SETTINGS_DEFAULT
    got = server.anki_settings({"picture": " picture ", "audio": "sentenceAudio", "sentence": "Sentence",
                                "word": "Expression", "auto": False, "pad_ms": 350, "format": "wav", "future": 1})
    assert got == {"auto": False, "picture": "picture", "audio": "sentenceAudio", "sentence": "Sentence",
                   "word": "Expression", "pad_ms": 350, "format": "wav"}
    for bad in ({"picture": "a" * 65}, {"picture": 'x"y'}, {"audio": "{{Audio}}"}, {"word": "a:b"},
                {"sentence": "<b>"}, {"picture": "a\nb"}, {"picture": 5}, {"auto": "yes"}, {"pad_ms": -1},
                {"pad_ms": 99999}, {"pad_ms": True}, {"format": "ogg"}, [], "Picture"):
        with pytest.raises(ValueError):
            server.anki_settings(bad)


# ------------------------------------------------------------------ the attach, with a fake AnkiConnect

NOW = 1_800_000_000.0
VIDEO = "AbCdEfGhIjK"
OTHER = "ZyXwVuTsRqP"
LINES = [
    {"id": 0, "start": 10.0, "end": 12.5, "text": "おはようございます"},
    {"id": 1, "start": 13.0, "end": 15.0, "text": "これはテスト字幕です"},
    {"id": 2, "start": 16.0, "end": 18.0, "text": "今日は<いい>天気&晴れ"},
]


def note_fields(**values):
    """notesInfo's fields of an invented Lapis-like note type, the given values filled in."""
    names = ["Expression", "Sentence", "Picture", "SentenceAudio", "Glossary"]
    return {name: {"value": values.get(name, ""), "order": i} for i, name in enumerate(names)}


class FakeAnki:
    def __init__(self):
        self.notes: dict = {}
        self.media: dict = {}
        self.updates: list = []
        self.actions: list = []
        self.down = False
        self.on_update = None  # called before notesInfo answers, to fill a field meanwhile

    def add(self, note_id, fields):
        self.notes[note_id] = fields

    def __call__(self, action, params, timeout):
        self.actions.append(action)
        if self.down:
            raise server.AnkiUnreachable("[Errno 111] Connection refused")
        if action == "findNotes":
            return sorted(self.notes)
        if action == "notesInfo":
            if self.on_update is not None:
                self.on_update()
            # A copy, as AnkiConnect's JSON is: a later write must not change an earlier answer.
            return [{"noteId": i, "fields": copy.deepcopy(self.notes[i])} if i in self.notes else {} for i in params["notes"]]
        if action == "storeMediaFile":
            name = params["filename"].lower()  # recent Ankis store the name lowercased
            self.media[name] = params["data"]
            return name
        if action == "updateNoteFields":
            note = params["note"]
            self.updates.append(note)
            for key, value in note["fields"].items():
                self.notes[note["id"]][key]["value"] = value
            return None
        raise AssertionError(action)


def fresh_id(seconds_ago=5.0):
    return int((NOW - seconds_ago) * 1000)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    anki = FakeAnki()
    clips, grabs = [], []

    def clip(video_id, start, end, fmt):
        clips.append((video_id, round(start, 3), round(end, 3), fmt))
        return b"ID3-audio-" + video_id.encode(), "audio/mpeg", fmt

    def decode(url, t):
        grabs.append((url, round(t, 3)))
        return b"\xff\xd8jpeg-" + url.encode()

    app.clip = clip
    frames = server.FrameGrabber(resolve=lambda vid: f"stream-{vid}", decode=decode, clock=lambda: NOW)
    miner = server.Miner(app, anki, frames=frames, clock=lambda: NOW, sleep=lambda s: None)
    app.miner = miner

    def session(video_id=VIDEO, cues=LINES, synced_ago=2.0, settings=None, t=14.0):
        s = app.get_session(video_id, f"https://www.youtube.com/watch?v={video_id}")
        with s.lock:
            s.cues = [dict(c) for c in cues]
            s.anki = server.anki_settings(settings if settings is not None else {})
            s.last_sync = NOW - synced_ago
            s.want_t = t
        return s

    return SimpleNamespace(app=app, anki=anki, miner=miner, clips=clips, grabs=grabs, session=session, frames=frames)


def test_a_new_note_gets_the_audio_and_picture_of_its_line(rig):
    s = rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Expression="字幕", Sentence="これは<b>テスト</b>字幕です。"))
    event = rig.miner.attach_new(nid)
    assert event["ok"] and event["status"] == "attached" and event["note"] == nid
    # The cue's own span with the default 200 ms padding, and a frame a little into the line.
    assert rig.clips == [(VIDEO, 12.8, 15.2, "mp3")]
    assert rig.grabs == [(f"stream-{VIDEO}", 13.75)]
    fields = rig.anki.notes[nid]
    assert fields["SentenceAudio"]["value"] == f"[sound:shisuko_{VIDEO.lower()}_13000.mp3]"
    assert fields["Picture"]["value"] == f'<img src="shisuko_{VIDEO.lower()}_13000.jpg">'
    assert fields["Sentence"]["value"] == "これは<b>テスト</b>字幕です。"  # a filled field is never touched
    assert rig.anki.media[f"shisuko_{VIDEO.lower()}_13000.mp3"]  # base64 of the clip
    assert [e["seq"] for e in s.mined] == [0] and s.mined[0]["note"] == nid


def test_only_empty_fields_are_filled(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です", Picture='<img src="mine.jpg">'))
    event = rig.miner.attach_new(nid)
    assert event["ok"] and rig.grabs == []
    assert rig.anki.updates[-1]["fields"].keys() == {"SentenceAudio"}
    assert rig.anki.notes[nid]["Picture"]["value"] == '<img src="mine.jpg">'


def test_a_complete_note_is_left_alone(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です", Picture='<img src="p.jpg">', SentenceAudio="[sound:a.mp3]"))
    assert rig.miner.attach_new(nid) is None
    assert rig.anki.updates == [] and rig.clips == [] and "storeMediaFile" not in rig.anki.actions


def test_a_field_filled_while_the_media_was_made_is_not_overwritten(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    reads = []

    def fill_on_second_read():
        reads.append(1)
        if len(reads) == 2:
            rig.anki.notes[nid]["Picture"]["value"] = '<img src="other.jpg">'

    rig.anki.on_update = fill_on_second_read
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.anki.updates[-1]["fields"].keys() == {"SentenceAudio"}
    assert rig.anki.notes[nid]["Picture"]["value"] == '<img src="other.jpg">'


def test_a_note_is_judged_once(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid)["ok"]
    rig.anki.notes[nid]["Picture"]["value"] = ""  # emptied by hand afterwards
    assert rig.miner.attach_new(nid) is None
    assert len(rig.anki.updates) == 1


def test_a_note_that_matches_no_line_is_left_alone_and_the_log_says_why(rig, caplog):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="まったく関係のない別の文章です"))
    with caplog.at_level("DEBUG", logger="shisu-ko"):
        assert rig.miner.attach_new(nid) is None
    assert rig.anki.updates == [] and "storeMediaFile" not in rig.anki.actions
    assert any(f"note {nid} matches no line of {VIDEO}" in r.getMessage() for r in caplog.records)


def test_a_note_without_a_sentence_is_left_alone(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Expression="字幕"))
    assert rig.miner.attach_new(nid) is None and rig.anki.updates == []


def test_a_stale_session_or_an_old_note_gets_nothing(rig):
    rig.session(synced_ago=server.ATTACH_SYNC_WINDOW + 1)  # an old tab, its switch off, or closed
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid) is None
    rig.session(synced_ago=1)
    old = fresh_id(server.ATTACH_NOTE_MAX_AGE + 5)  # made long before it was found: not a lookup
    rig.anki.add(old, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(old) is None
    assert rig.anki.updates == []


def test_a_session_with_auto_off_or_no_settings_gets_nothing(rig):
    rig.session(settings={"auto": False})
    portal = rig.session(OTHER, cues=[{"id": 0, "start": 1.0, "end": 2.0, "text": "これはテスト字幕です"}])
    with portal.lock:
        portal.anki = None  # the portal syncs without `anki`: its notes are its own business
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid) is None and rig.anki.updates == []


def test_the_best_matching_watched_video_wins(rig):
    rig.session(OTHER, cues=[{"id": 0, "start": 1.0, "end": 2.0, "text": "これはテスト字幕ですか"}], synced_ago=1)
    rig.session(VIDEO, synced_ago=3)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.clips[0][0] == VIDEO  # the line that is the sentence beats one running on past it


def test_field_names_match_in_any_case(rig):
    rig.session(settings={"picture": "PICTURE", "audio": "sentenceaudio", "sentence": "sentence"})
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.anki.updates[-1]["fields"].keys() == {"Picture", "SentenceAudio"}


def test_server_text_reaches_html_fields_escaped(rig):
    s = rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields())
    rig.anki.media.clear()
    with s.lock:
        s.anki = server.anki_settings({"sentence": "Sentence"})
    event = rig.miner.mine_now(s, s.anki, {"cue_id": 2, "nonce": "n1", "session": None})
    assert event["ok"]
    assert rig.anki.notes[nid]["Sentence"]["value"] == "今日は&lt;いい&gt;天気&amp;晴れ"


def test_a_stored_name_with_markup_is_escaped_in_the_field(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    real = rig.anki.__call__

    def odd_names(action, params, timeout):
        if action == "storeMediaFile":
            real(action, params, timeout)
            return 'x"><script>.jpg'
        return real(action, params, timeout)

    rig.miner.call = odd_names
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.anki.notes[nid]["Picture"]["value"] == '<img src="x&quot;&gt;&lt;script&gt;.jpg">'


CAT = [{"id": 0, "start": 10.0, "end": 14.0, "text": "これは猫です。とても可愛い。"}]


def test_yomitans_fragment_grows_to_the_whole_line_and_says_so(rig):
    rig.session(cues=CAT)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Expression="猫", Sentence="これは<b>猫</b>です。"))
    event = rig.miner.attach_new(nid)
    assert event["ok"] and "sentence extended" in event["message"]
    assert rig.anki.updates[-1]["fields"].keys() == {"Picture", "SentenceAudio", "Sentence"}
    assert rig.anki.notes[nid]["Sentence"]["value"] == "これは<b>猫</b>です。とても可愛い。"


def test_a_fragment_from_further_into_the_line_keeps_its_bold_where_it_was(rig):
    rig.session(cues=CAT)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="とても<b>可愛い</b>。"))
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.anki.notes[nid]["Sentence"]["value"] == "これは猫です。とても<b>可愛い</b>。"


def test_a_card_with_its_media_still_gets_the_whole_line(rig):
    rig.session(cues=CAT)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これは<b>猫</b>です。", Picture='<img src="p.jpg">', SentenceAudio="[sound:a.mp3]"))
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.anki.updates[-1]["fields"] == {"Sentence": "これは<b>猫</b>です。とても可愛い。"}
    assert rig.clips == [] and rig.grabs == []


def test_a_sentence_edited_while_the_media_was_made_is_not_grown(rig):
    rig.session(cues=CAT)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これは<b>猫</b>です。"))
    reads = []

    def edit_on_second_read():
        reads.append(1)
        if len(reads) == 2:
            rig.anki.notes[nid]["Sentence"]["value"] = "猫がいる。"

    rig.anki.on_update = edit_on_second_read
    assert rig.miner.attach_new(nid)["ok"]
    assert "Sentence" not in rig.anki.updates[-1]["fields"]
    assert rig.anki.notes[nid]["Sentence"]["value"] == "猫がいる。"


def test_a_line_that_merely_resembles_the_sentence_leaves_it_alone(rig):
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕ですよ"))  # matched by bigrams, not contained
    assert rig.miner.attach_new(nid)["ok"]
    assert "Sentence" not in rig.anki.updates[-1]["fields"]


def test_the_viewers_mine_grows_the_newest_cards_fragment_too(rig):
    s = rig.session(cues=CAT)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これは<b>猫</b>です。"))
    event = rig.miner.mine_now(s, s.anki, {"cue_id": 0, "nonce": "n1", "session": None})
    assert event["ok"] and rig.anki.notes[nid]["Sentence"]["value"] == "これは<b>猫</b>です。とても可愛い。"


def test_a_mine_that_fails_inside_anki_is_answered(rig):
    s = rig.session()
    rig.anki.add(fresh_id(), note_fields(Sentence="これはテスト字幕です"))
    real = rig.anki.__call__

    def refuse(action, params, timeout):
        if action == "updateNoteFields":
            raise RuntimeError("collection is not available")
        return real(action, params, timeout)

    rig.miner.call = refuse
    event = rig.miner.mine_now(s, s.anki, {"cue_id": 1, "nonce": "n1", "session": None})
    assert event["status"] == "error" and "collection is not available" in event["message"]
    assert s.mined[-1]["nonce"] == "n1"


def test_a_picture_that_fails_mid_decode_still_gives_the_audio(rig):
    rig.session()

    def broken(url, t):
        raise OSError("Invalid data found when processing input")

    rig.frames.decode = broken
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    event = rig.miner.attach_new(nid)
    assert event["ok"] and "no picture" in event["message"]


def test_no_picture_attaches_the_audio_alone(rig):
    rig.session()
    rig.frames.resolve = lambda vid: (_ for _ in ()).throw(server.FrameUnavailable("the video is DRM-protected"))
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    event = rig.miner.attach_new(nid)
    assert event["ok"] and "no picture" in event["message"]
    assert rig.anki.updates[-1]["fields"].keys() == {"SentenceAudio"}


def test_mine_picture_off_never_grabs_a_frame(rig):
    rig.miner.frames = None
    rig.session()
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid)["ok"]
    assert rig.grabs == [] and rig.anki.updates[-1]["fields"].keys() == {"SentenceAudio"}
    # A note whose audio is there already has nothing left to want.
    other = fresh_id(4)
    rig.anki.add(other, note_fields(Sentence="これはテスト字幕です", SentenceAudio="[sound:a.mp3]"))
    assert rig.miner.attach_new(other) is None


def test_audio_still_being_fetched_is_waited_for(rig):
    rig.session()
    calls = []

    def clip(video_id, start, end, fmt):
        calls.append(1)
        if len(calls) < 3:
            raise server.ClipNotReady()
        return b"ID3", "audio/mpeg", "mp3"

    rig.app.clip = clip
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これはテスト字幕です"))
    assert rig.miner.attach_new(nid)["ok"] and len(calls) == 3


def test_an_import_attaches_to_nothing(rig):
    rig.miner.notes_added(list(range(1, server.ATTACH_MAX_BATCH + 2)))
    assert rig.miner.jobs.empty()
    rig.miner.notes_added([fresh_id()])
    assert rig.miner.jobs.qsize() == 1


def test_the_watcher_hands_new_notes_over_and_looks_often_while_mining(rig):
    clock = SimpleNamespace(t=0.0)
    seen, fast = [], [False]
    notes = {1, 2}
    watcher = server.AnkiWatcher(lambda action, params, timeout: sorted(notes), clock=lambda: clock.t,
                                 sync=False, on_new=seen.append, fast=lambda: fast[0])
    watcher.poll()  # the baseline: never handed over
    notes.add(3)
    fast[0] = True
    watcher.poll()
    assert seen == [[3]]
    assert watcher.next_poll == server.ANKI_POLL_MINING
    assert watcher.due is None and watcher.status() == {"state": "off", "last": None, "error": None}


# ------------------------------------------------------------------ the picture

def test_the_stream_is_resolved_once_per_video_and_a_failure_is_kept(rig):
    resolves = []
    clock = SimpleNamespace(t=NOW)

    def resolve(vid):
        resolves.append(vid)
        if vid == OTHER:
            raise RuntimeError("Sign in to confirm")
        return f"url-{vid}"

    frames = server.FrameGrabber(resolve=resolve, decode=lambda url, t: b"\xff\xd8" + url.encode(), clock=lambda: clock.t)
    assert frames.grab(VIDEO, 1.0) == b"\xff\xd8url-" + VIDEO.encode()
    frames.grab(VIDEO, 50.0)
    for _ in range(2):
        with pytest.raises(server.FrameUnavailable, match="did not resolve"):
            frames.grab(OTHER, 1.0)
    assert resolves == [VIDEO, OTHER]
    clock.t += server.FRAME_RETRY + 1  # a failure that was no verdict is tried again
    with pytest.raises(server.FrameUnavailable):
        frames.grab(OTHER, 1.0)
    assert resolves == [VIDEO, OTHER, OTHER]
    clock.t += server.FRAME_STREAM_TTL
    frames.grab(VIDEO, 1.0)
    assert resolves == [VIDEO, OTHER, OTHER, VIDEO]


def test_a_drm_verdict_stands_as_long_as_a_stream_would():
    clock = SimpleNamespace(t=NOW)
    resolves = []

    def resolve(vid):
        resolves.append(vid)
        raise server.FrameUnavailable("the video is DRM-protected")

    frames = server.FrameGrabber(resolve=resolve, decode=lambda url, t: b"", clock=lambda: clock.t)
    for step in (0, server.FRAME_RETRY + 1):
        clock.t += step
        with pytest.raises(server.FrameUnavailable, match="DRM"):
            frames.grab(VIDEO, 1.0)
    assert resolves == [VIDEO]


def test_an_expired_stream_url_is_resolved_again_once(rig):
    clock = SimpleNamespace(t=NOW)
    resolves, opened = [], []

    def decode(url, t):
        opened.append(url)
        if url == "url-1":
            raise server.FrameUnavailable("HTTP 403")
        return b"\xff\xd8"

    frames = server.FrameGrabber(resolve=lambda vid: resolves.append(vid) or f"url-{len(resolves)}", decode=decode, clock=lambda: clock.t)
    with pytest.raises(server.FrameUnavailable):
        frames.grab(VIDEO, 1.0)  # a URL resolved just now that fails is not resolved again
    clock.t += server.FRAME_FRESH + 1
    assert frames.grab(VIDEO, 1.0) == b"\xff\xd8"
    assert opened == ["url-1", "url-1", "url-2"] and len(resolves) == 2


def test_resolve_stream_asks_for_720p_h264_with_the_audio_fetch_cookies(monkeypatch):
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            captured["url"], captured["download"] = url, download
            return captured.get("info", {"url": "https://media.example/v.mp4"})

    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYDL))
    fetcher = SimpleNamespace(ytdlp_options=lambda vid: {"format": "bestaudio", "cookiesfrombrowser": ("firefox",),
                                                         "progress_hooks": [print]})
    frames = server.FrameGrabber(fetcher)
    assert frames.resolve_stream(VIDEO) == "https://media.example/v.mp4"
    assert captured["format"] == server.FRAME_FORMAT and "720" in captured["format"] and "avc1" in captured["format"]
    assert captured["cookiesfrombrowser"] == ("firefox",) and "progress_hooks" not in captured
    assert captured["download"] is False
    captured["info"] = {"url": "x", "has_drm": True}
    with pytest.raises(server.FrameUnavailable, match="DRM"):
        frames.resolve_stream(VIDEO)


def synthetic_video(path, seconds=2, fps=10):
    """A tiny MPEG-TS file whose frame brightness encodes its time: frame n is grey 10*n."""
    av = pytest.importorskip("av")
    with av.open(str(path), "w", format="mpegts") as out:
        stream = out.add_stream("mpeg2video", rate=fps)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for n in range(seconds * fps):
            frame = av.VideoFrame(64, 48, "gray")
            frame.planes[0].update(bytes([min(255, 10 * n)]) * frame.planes[0].buffer_size)
            frame = frame.reformat(format="yuv420p")
            frame.pts = n
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def test_frame_jpeg_decodes_the_frame_at_the_time(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "v.ts"
    synthetic_video(path)
    jpeg = server.frame_jpeg(str(path), 1.0)
    assert jpeg.startswith(b"\xff\xd8")
    with av.open(io.BytesIO(jpeg)) as img:
        frame = next(img.decode(video=0))
        assert (frame.width, frame.height) == (64, 48)
        level = float(frame.to_ndarray(format="gray").mean())
    assert 80 <= level <= 125  # about 1.0 s in (grey 100), not the first frame
    assert server.frame_jpeg(str(path), 99).startswith(b"\xff\xd8")  # past the end: the last frame
    with pytest.raises(server.FrameUnavailable):
        server.frame_jpeg(str(tmp_path / "missing.ts"), 1.0)


def test_frame_time_is_a_little_into_the_line():
    assert server.frame_time(10.0, 12.0) == 10.75
    assert server.frame_time(10.0, 10.5) == 10.25


# ------------------------------------------------------------------ the switches

def test_mining_is_on_by_default_and_off_in_the_container_without_an_anki_url():
    resolve = server.resolve_mining
    args = lambda **kw: SimpleNamespace(**{"mine": None, "anki_url": None, **kw})  # noqa: E731
    host, container = {}, {"SHISUKO_CONTAINER": "1"}
    assert resolve(args(), host) is True
    assert resolve(args(mine="off"), host) is False
    assert resolve(args(), container) is False
    assert resolve(args(anki_url="http://host.docker.internal:8765"), container) is True
    assert resolve(args(mine="auto"), container) is True
    assert resolve(SimpleNamespace(), host) is True


def test_parse_args_takes_the_mining_options():
    args = server.parse_args([])
    assert (args.mine, args.mine_picture) == (None, "on")
    args = server.parse_args(["--mine", "off", "--mine-picture", "off"])
    assert (args.mine, args.mine_picture) == ("off", "off")
    with pytest.raises(SystemExit):
        server.parse_args(["--mine-picture", "maybe"])


def test_start_anki_watcher_starts_the_miner_beside_the_sync(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **kw: started.append(kw["name"]) or SimpleNamespace(start=lambda: None))
    monkeypatch.delenv("SHISUKO_CONTAINER", raising=False)
    app = make_app(monkeypatch, tmp_path)
    args = lambda **kw: SimpleNamespace(**{"anki_sync": None, "anki_url": None, "mine": None, "mine_picture": "on", **kw})  # noqa: E731
    expired = []
    monkeypatch.setattr(server.DeckIndexes, "notes_added", lambda self, ids: expired.append(ids))
    watcher = server.start_anki_watcher(args(anki_sync="off"), app)
    assert started == ["anki-mine", "anki-deck", "anki-sync"] and watcher.sync_on is False
    assert isinstance(app.miner.frames, server.FrameGrabber) and isinstance(app.decks, server.DeckIndexes)
    # A new note reaches both: the miner judges it, and the deck indexes are read again.
    watcher.on_new([5])
    assert app.miner.jobs.get_nowait() == ("auto", 5) and expired == [[5]]
    app.miner = None
    server.start_anki_watcher(args(mine_picture="off"), app)
    assert app.miner.frames is None
    started.clear()
    app.miner = app.decks = None
    # The sync and the mining off: the watcher still runs, for the word colours alone.
    watcher = server.start_anki_watcher(args(anki_sync="off", mine="off"), app)
    assert started == ["anki-deck", "anki-sync"] and app.miner is None and watcher.fast is None
    started.clear()
    app.decks = None
    monkeypatch.setenv("SHISUKO_CONTAINER", "1")
    assert server.start_anki_watcher(args(), app) is None
    assert started == [] and app.miner is None and app.decks is None
    monkeypatch.delenv("SHISUKO_CONTAINER")
    stand_in = SimpleNamespace(exit_code=None)  # what the tests hand main(): no sessions, no miner
    assert server.start_anki_watcher(args(anki_sync="off"), stand_in) is None
    assert not hasattr(stand_in, "miner")

"""The creator's own subtitles: fetched beside the audio, cleaned, kept with the session.

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


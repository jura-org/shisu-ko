# SPDX-License-Identifier: Apache-2.0
"""Pure helpers of portal/portal.py: allowlist, host check, cloze, glossary sanitiser, tokens."""

import http.client
import importlib.util
import json
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "portal.py"
spec = importlib.util.spec_from_file_location("portal", PATH)
portal = importlib.util.module_from_spec(spec)
sys.modules["portal"] = portal  # `from __future__ import annotations` needs the module registered
spec.loader.exec_module(portal)

STATUS = {
    "Self": {"UserID": 1, "TailscaleIPs": ["100.101.1.1", "fd7a:115c:a1e0::11"]},
    "Peer": {
        "a": {"UserID": 1, "TailscaleIPs": ["100.101.1.2", "fd7a:115c:a1e0::12"]},
        "b": {"UserID": 10, "TailscaleIPs": ["100.101.2.1"]},
        "c": {"UserID": 11, "TailscaleIPs": ["100.101.3.1"]},
    },
}


def test_owner_ips_only_same_user():
    owners = portal.owner_ips(STATUS)
    assert owners == {"100.101.1.1", "fd7a:115c:a1e0::11", "100.101.1.2", "fd7a:115c:a1e0::12"}
    assert portal.owner_ips({}) == set()
    assert portal.owner_ips({"Peer": STATUS["Peer"]}) == set()


def test_ip_allowed():
    owners = portal.owner_ips(STATUS)
    assert portal.ip_allowed("127.0.0.1", owners)
    assert portal.ip_allowed("::1", set())
    assert portal.ip_allowed("100.101.1.2", owners)
    assert portal.ip_allowed("::ffff:100.101.1.2", owners)
    assert portal.ip_allowed("fd7a:115c:a1e0:0::12", owners)  # another spelling of the same address
    assert not portal.ip_allowed("100.101.2.1", owners)  # another user's laptop
    assert not portal.ip_allowed("100.101.3.1", owners)
    assert not portal.ip_allowed("192.168.1.5", owners)
    assert not portal.ip_allowed("not an ip", owners)


def test_host_allowed():
    hosts = {"100.101.1.1", "127.0.0.1", "localhost", "::1", "desk"}
    assert portal.host_allowed("100.101.1.1:8791", hosts)
    assert portal.host_allowed("Desk:8791", hosts)
    assert portal.host_allowed("[::1]:8791", hosts)
    assert not portal.host_allowed("evil.example:8791", hosts)
    assert not portal.host_allowed("", hosts)
    assert not portal.host_allowed(None, hosts)


def test_build_cloze_escapes_and_splits():
    assert portal.build_cloze("今日は寿司を食べた", 3, 5) == ("今日は", "寿司", "を食べた")
    assert portal.build_cloze("<a>&b", 0, 3) == ("", "&lt;a&gt;", "&amp;b")
    for start, end in ((2, 2), (-1, 2), (0, 99), (3, 1)):
        with pytest.raises(ValueError):
            portal.build_cloze("abcdef", start, end)
    with pytest.raises(ValueError):
        portal.build_cloze("", 0, 1)


def test_spans_count_utf16_units_as_the_page_does():
    # 𠮟 (U+20B9F) is one Python character and two JavaScript ones. The page's span [3, 6) in
    # 先生に𠮟られた is 𠮟ら: counted in code points, the cloze would bold 𠮟られ.
    text = "先生に𠮟られた"
    assert portal.utf16_length(text) == 8 and len(text) == 7
    assert portal.build_cloze(text, 3, 6) == ("先生に", "𠮟ら", "れた")
    assert portal.build_cloze(text, 3, 5) == ("先生に", "𠮟", "られた")
    assert portal.build_cloze(text, 0, 8)[1] == text
    for start, end in ((3, 4), (4, 6), (0, 9)):  # half of 𠮟, or past the end
        with pytest.raises(ValueError):
            portal.build_cloze(text, start, end)
    # The entry's match is reported in the same units, so the page's span ends after 𠮟る.
    entries = portal.compact_entries({"dictionaryEntries": [_entry("𠮟る", "しかる", "𠮟られた"), _entry("𠮟る", "しかる", "𠮟ら")]})
    assert entries[0]["matched"] == 5
    assert portal.match_length(_entry("𠮟", "しつ", "𠮟")) == 2


def test_render_template_and_markers():
    values = {"cloze-prefix": "a", "cloze-body": "b", "cloze-suffix": "c"}
    assert portal.render_template("{cloze-prefix}<b>{cloze-body}</b>{cloze-suffix}", values) == "a<b>b</b>c"
    assert portal.render_template("{missing}", values) == ""
    markers = portal.template_markers({"Front": "{expression}", "Picture": "{portal-picture}", "Hint": ""})
    assert "expression" in markers and "portal-picture" in markers


def test_sc_to_html_drops_everything_unsafe():
    hostile = {
        "tag": "div",
        "style": {"background": "url(javascript:alert(1))"},
        "data": {"onclick": "x"},
        "content": [
            {"tag": "script", "content": "alert(1)"},
            {"tag": "a", "href": "javascript:alert(1)", "content": "link"},
            {"tag": "img", "path": "x.svg", "title": "verb\"><script>"},
            {"tag": "iframe", "src": "https://evil", "content": "<b>x</b>"},
            {"tag": "td", "colSpan": 2, "rowSpan": "3 onload=x", "content": "cell"},
            "line1\nline2",
        ],
    }
    out = portal.sc_to_html(hostile)
    for bad in ("<script", "href", "src", "style", "onclick", "onload", "url(", "<iframe", "<a", '"><'):
        assert bad not in out, bad
    assert "<span>alert(1)</span>" in out  # an unknown tag keeps its text, as a span
    assert '<td colspan="2">cell</td>' in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert "line1<br>line2" in out
    assert 'class="sc-label">verb&quot;&gt;' in out or 'class="sc-label">verb"&gt;&lt;script&gt;' in out


def test_sc_to_html_keeps_only_safe_markers_as_class():
    ok = portal.sc_to_html({"tag": "ul", "data": {"content": "glossary"}, "content": "x"})
    assert ok == '<ul class="sc-glossary">x</ul>'
    for bad in ('a" onclick="x', "UPPER", "x y", "a;b", 'x"', "-lead"):
        out = portal.sc_to_html({"tag": "ul", "data": {"content": bad}, "content": "x"})
        assert out == "<ul>x</ul>", bad


def test_sc_to_html_depth_limit():
    node = "deep"
    for _ in range(200):
        node = {"tag": "span", "content": node}
    assert "deep" not in portal.sc_to_html(node)


def test_glossary_html_and_first_gloss():
    sc = {"type": "structured-content", "content": {"tag": "ul", "content": [
        {"tag": "li", "content": [{"tag": "img", "title": "Ichidan verb"},
                                  {"tag": "ul", "content": [{"tag": "li", "content": "to eat"}, {"tag": "li", "content": "to live on"}]}]},
    ]}}
    assert portal.first_gloss(sc) == "to eat"
    assert "<li>to eat</li>" in portal.glossary_html(sc)
    assert portal.first_gloss("\n① 口に入れる\n② 暮らす") == "① 口に入れる"
    assert portal.glossary_html("a<b>\nc") == "a&lt;b&gt;<br>c"
    assert portal.glossary_html({"type": "image", "path": "x.png"}) == ""


def test_compact_tokens_keeps_text_and_first_headword():
    content = [
        [{"text": "今日", "reading": "こんにち", "headwords": [[{"term": "今日は"}]]}, {"text": "は", "reading": ""}],
        [{"text": "、", "reading": ""}],
    ]
    tokens = portal.compact_tokens(content)
    assert tokens == [
        {"text": "今日は", "segs": [["今日", "こんにち"], ["は", ""]], "term": "今日は"},
        {"text": "、", "segs": [["、", ""]], "term": None},
    ]


def test_compact_entries_groups_dictionaries():
    data = {"dictionaryEntries": [{
        "headwords": [{"term": "食べる", "reading": "たべる"}],
        "definitions": [
            {"dictionary": "Other", "entries": ["た・べる\n① 食う"]},
            {"dictionary": "Dict EN", "entries": [
                {"type": "structured-content", "content": {"tag": "ul", "content": [{"tag": "li", "content": "to eat"}]}}]},
        ],
        "pronunciations": [{"headwordIndex": 0, "pronunciations": [{"type": "pitch-accent", "positions": 2}]}],
        "frequencies": [{"headwordIndex": 0, "dictionary": "JPDB", "displayValue": "184"},
                        {"headwordIndex": 0, "dictionary": "JPDB", "displayValue": "13390"}],
    }]}
    [entry] = portal.compact_entries(data, default_dictionary="Dict EN")
    assert entry["term"] == "食べる" and entry["reading"] == "たべる"
    assert [d["name"] for d in entry["dicts"]] == ["Other", "Dict EN"]
    assert entry["gloss"] == "to eat"  # the default dictionary wins over the first one
    assert entry["glossFrom"] == "Dict EN"
    assert portal.compact_entries(data)[0]["gloss"] == "た・べる"  # no default dictionary: the first one
    assert entry["pitches"] == [2]
    assert entry["freqs"] == [{"name": "JPDB", "value": "184"}]


def test_valid_video_id_and_frame_time():
    assert portal.valid_video_id("abcDEF_-12")
    assert not portal.valid_video_id("../etc")
    assert not portal.valid_video_id("abc")
    assert not portal.valid_video_id(None)
    assert portal.frame_time(10.0, 11.0) == 10.5
    assert portal.frame_time(10.0, 20.0) == 10.75


def _entry(term, reading, matched):
    return {"headwords": [{"term": term, "reading": reading, "sources": [{"originalText": matched}]}],
            "definitions": [{"dictionary": "D", "entries": [term]}]}


def test_compact_entries_puts_the_longest_matches_first_then_the_shorter():
    data = {"dictionaryEntries": [_entry("天", "てん", "天"), _entry("天気", "てんき", "天気"), _entry("天", "あま", "天")]}
    entries = portal.compact_entries(data)
    # The sheet opens on the longest match; the readings of 天 follow, in Yomitan's order.
    assert [(e["term"], e["reading"], e["matched"]) for e in entries] == [
        ("天気", "てんき", 2), ("天", "てん", 1), ("天", "あま", 1)]


def test_compact_entries_keeps_yomitans_order_within_a_length_and_drops_repeats():
    data = {"dictionaryEntries": [
        _entry("降る", "ふる", "降っています"), _entry("降る", "くだる", "降っています"),
        _entry("降って", "くだって", "降って"), _entry("降", "こう", "降"),
        _entry("降る", "ふる", "降っています"),  # the same headword again (another dictionary's entry)
        _entry("降", "ふり", "降"),
    ]}
    entries = portal.compact_entries(data)
    assert [(e["term"], e["reading"], e["matched"]) for e in entries] == [
        ("降る", "ふる", 6), ("降る", "くだる", 6), ("降って", "くだって", 3), ("降", "こう", 1), ("降", "ふり", 1)]


def test_compact_entries_caps_the_choices():
    data = {"dictionaryEntries": [_entry("天気", "てんき", "天気")]
            + [_entry("天", f"r{i}", "天") for i in range(20)]}
    entries = portal.compact_entries(data)
    assert len(entries) == portal.LOOKUP_ENTRIES
    assert entries[0]["term"] == "天気"
    assert [e["reading"] for e in portal.compact_entries(data, limit=3)] == ["てんき", "r0", "r1"]
    assert portal.compact_entries({}) == [] and portal.compact_entries(None) == []


# ---------------------------------------------------------------- retries


def test_backoff_schedule_and_jitter():
    mid = lambda: 0.5  # noqa: E731 - no jitter
    assert [portal.backoff_delay(n, 1.0, 2.0, 8.0, mid) for n in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert portal.backoff_delay(0, 1.0, 2.0, 8.0, lambda: 0) == pytest.approx(0.8)
    assert portal.backoff_delay(0, 1.0, 2.0, 8.0, lambda: 1) == pytest.approx(1.2)


def test_transient_vs_permanent():
    assert portal.is_transient(portal.UpstreamError("down", 503))
    assert portal.is_transient(portal.UpstreamError("yomitan", 502))
    assert not portal.is_transient(portal.UpstreamError("bad", 400))
    assert not portal.is_transient(portal.UpstreamError("Anki: duplicate", 409))
    assert not portal.is_transient(ValueError("bug"))


def test_retry_stops_on_success_and_on_permanent_errors():
    slept = []
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise portal.UpstreamError("timeout", 503)
        return "ok"

    assert portal.retry(flaky, tries=3, sleep=slept.append, rand=lambda: 0.5) == "ok"
    assert slept == [1.0, 2.0]

    calls.clear()

    def refused():
        calls.append(1)
        raise portal.UpstreamError("Anki: bad field", 422)

    with pytest.raises(portal.UpstreamError):
        portal.retry(refused, tries=3, sleep=slept.append)
    assert len(calls) == 1

    calls.clear()

    def down():
        calls.append(1)
        raise portal.UpstreamError("down", 503)

    with pytest.raises(portal.UpstreamError):
        portal.retry(down, tries=2, sleep=lambda s: None)
    assert len(calls) == 3


class FakeAnki:
    """AnkiConnect enough for addNote, findNotes and notesInfo; can lose the answer to an add."""

    def __init__(self, lose_answers=0, fail_before_add=0):
        self.notes = {}
        self.lose_answers = lose_answers
        self.fail_before_add = fail_before_add
        self.queries = []

    def __call__(self, action, **params):
        if action == "addNote":
            if self.fail_before_add:
                self.fail_before_add -= 1
                raise portal.UpstreamError("unreachable", 503)
            note_id = 1000 + len(self.notes)
            self.notes[note_id] = params["note"]
            if self.lose_answers:
                self.lose_answers -= 1
                raise portal.UpstreamError("timed out after the add", 503)
            return note_id
        if action == "findNotes":
            self.queries.append(params["query"])
            return list(self.notes)
        if action == "notesInfo":
            return [{"noteId": i, "fields": {k: {"value": v} for k, v in self.notes[i]["fields"].items()}} for i in params["notes"]]
        raise AssertionError(action)


NOTE = {"deckName": "Mining", "fields": {"Front": "天気", "Back": "今日は<b>天気</b>がいいですね"}}


def test_add_note_once_never_adds_twice_when_the_answer_was_lost():
    fake = FakeAnki(lose_answers=1)
    assert portal.add_note_once(NOTE, "Front", "Back", call=fake, sleep=lambda s: None) == 1000
    assert len(fake.notes) == 1
    assert fake.queries == ['"deck:Mining" "Front:天気"']


def test_add_note_once_adds_when_the_first_try_never_arrived():
    fake = FakeAnki(fail_before_add=2)
    assert portal.add_note_once(NOTE, "Front", "Back", call=fake, sleep=lambda s: None) == 1000
    assert len(fake.notes) == 1


def test_find_same_note_needs_the_same_sentence():
    fake = FakeAnki()
    fake.notes[7] = {"fields": {"Front": "天気", "Back": "another sentence"}}
    assert portal.find_same_note(NOTE, "Front", "Back", call=fake) is None
    fake.notes[8] = {"fields": dict(NOTE["fields"])}
    assert portal.find_same_note(NOTE, "Front", "Back", call=fake) == 8


def test_search_quote_and_apple_icons():
    assert portal.search_quote('a"b*c_d\\e') == 'a\\"b\\*c\\_d\\\\e'
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/apple-touch-icon-240x240.png",
                 "/apple-touch-icon-240x240-precomposed.png", "/apple-touch-icon-180x180.png"):
        assert portal.APPLE_ICON_RE.match(path), path
    assert not portal.APPLE_ICON_RE.match("/apple-touch-icon.png/../portal.py")


# ---------------------------------------------------------------- HLS proxy, positions

MASTER = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:URI="https://m.example/api/itag/233/x",TYPE=AUDIO,GROUP-ID="233",NAME="Default",DEFAULT=YES
#EXT-X-MEDIA:URI="https://m.example/api/itag/234/x",TYPE=AUDIO,GROUP-ID="234",NAME="Default",DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=197703,CODECS="avc1.4D4015,mp4a.40.5",RESOLUTION=426x240,AUDIO="233"
https://m.example/api/itag/229/a
#EXT-X-STREAM-INF:BANDWIDTH=282067,CODECS="avc1.4D4015,mp4a.40.2",RESOLUTION=426x240,AUDIO="234"
https://m.example/api/itag/229/b
#EXT-X-STREAM-INF:BANDWIDTH=804860,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=1280x720,AUDIO="234"
https://m.example/api/itag/232/c
#EXT-X-STREAM-INF:BANDWIDTH=2646572,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,AUDIO="234"
https://m.example/api/itag/270/d
#EXT-X-STREAM-INF:BANDWIDTH=1106437,CODECS="vp09.00.31.08,mp4a.40.2",RESOLUTION=1280x720,AUDIO="234"
https://m.example/api/itag/609/e
"""


def test_rewrite_master_keeps_h264_aac_lc_up_to_720p_and_hides_upstream():
    text, upstream, heights = portal.rewrite_master(MASTER, "/api/hls/TOKEN/")
    assert "m.example" not in text  # no upstream URL ever reaches the player
    assert heights == {"v229": 240, "v232": 720}  # no 1080p, no VP9
    assert upstream == {"a234": "https://m.example/api/itag/234/x", "v229": "https://m.example/api/itag/229/b",
                        "v232": "https://m.example/api/itag/232/c"}
    assert 'URI="/api/hls/TOKEN/a234.m3u8"' in text and 'GROUP-ID="233"' not in text
    assert "mp4a.40.5" not in text  # HE-AAC variants dropped when AAC-LC ones exist
    assert text.count("#EXT-X-STREAM-INF") == 2
    assert text.index("v232.m3u8") < text.index("v229.m3u8")  # the best variant first: where players start


def test_rewrite_media_and_segment_lookup():
    media = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-TARGETDURATION:7\n#EXTINF:3.5,\nhttps://g.example/s0\n#EXTINF:5.0,\nhttps://g.example/s1\n#EXT-X-KEY:METHOD=AES-128,URI=\"https://evil\"\n#EXTINF:4.0,\nhttps://g.example/s2\n#EXT-X-ENDLIST\n"
    text, urls, durations = portal.rewrite_media(media, "/api/hls/T/v232/")
    assert urls == ["https://g.example/s0", "https://g.example/s1", "https://g.example/s2"]
    assert durations == [3.5, 5.0, 4.0]
    assert "g.example" not in text and "evil" not in text
    assert "/api/hls/T/v232/2.ts" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert portal.rewrite_media(media, "/x/a234/", "aac")[0].count(".aac") == 3
    assert portal.segment_at(durations, 0) == (0, 0)
    assert portal.segment_at(durations, 4.0) == (1, 0.5)
    assert portal.segment_at(durations, 99) == (2, 99 - 8.5)


def test_byte_range():
    assert portal.byte_range(None, 100) is None
    assert portal.byte_range("bytes=0-9", 100) == (0, 9)
    assert portal.byte_range("bytes=90-", 100) == (90, 99)
    assert portal.byte_range("bytes=-10", 100) == (90, 99)
    assert portal.byte_range("bytes=0-999", 100) == (0, 99)
    assert portal.byte_range("bytes=200-", 100) is False
    assert portal.byte_range("items=0-1", 100) is None


def test_atomic_write_json(tmp_path):
    path = tmp_path / "state.json"
    portal.atomic_write_json(path, {"a": 1})
    portal.atomic_write_json(path, {"a": 2, "b": "日本"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "b": "日本"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temporary file left behind


def test_atomic_write_never_leaves_half_a_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    portal.atomic_write_json(path, {"a": 1})

    def boom(fh):
        raise OSError("disk full")

    monkeypatch.setattr(portal.os, "fsync", boom)
    with pytest.raises(OSError):
        portal.atomic_write_json(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}  # the old file, whole


def test_position_store(tmp_path):
    store = portal.PositionStore(tmp_path / "s.json")
    store.put("AAAAAAAAAAA", 118.27, 1033, "first", updated=1000)
    store.put("BBBBBBBBBBB", 5, None, None, updated=2000)
    assert store.get("AAAAAAAAAAA") == {"t": 118.3, "duration": 1033.0, "title": "first", "updated": 1000}
    assert [v["id"] for v in store.all()] == ["BBBBBBBBBBB", "AAAAAAAAAAA"]  # most recent first
    again = portal.PositionStore(tmp_path / "s.json")  # survives a restart
    assert again.get("AAAAAAAAAAA")["t"] == 118.3
    again.delete("AAAAAAAAAAA")
    assert portal.PositionStore(tmp_path / "s.json").get("AAAAAAAAAAA") is None


def test_a_bare_position_keeps_the_title_and_duration(tmp_path):
    # The home list's Mark as unwatched posts a position of 0 with no title: the row keeps its name.
    store = portal.PositionStore(tmp_path / "s.json")
    store.put("AAAAAAAAAAA", 500, 1033, "first", updated=1000)
    entry = store.put("AAAAAAAAAAA", 0)
    assert (entry["t"], entry["duration"], entry["title"]) == (0.0, 1033.0, "first")


def test_keep_order_keeps_the_entry_s_place(tmp_path):
    # Mark as watched / unwatched posts with keep_order: the row neither jumps to the top nor
    # changes which entries the cap drops.
    store = portal.PositionStore(tmp_path / "s.json")
    store.put("AAAAAAAAAAA", 500, 1033, "first", updated=1000)
    store.put("BBBBBBBBBBB", 5, 600, "second", updated=2000)
    entry = store.put("AAAAAAAAAAA", 1033, 1033, keep_order=True)
    assert (entry["t"], entry["updated"]) == (1033.0, 1000)
    assert [v["id"] for v in store.all()] == ["BBBBBBBBBBB", "AAAAAAAAAAA"]
    assert store.put("AAAAAAAAAAA", 0)["updated"] > 2000  # a plain save is a watch: to the top
    assert store.put("CCCCCCCCCCC", 0, keep_order=True)["updated"] > 2000  # nothing to keep: now


def test_api_position_keep_order_is_a_bool(tmp_path, monkeypatch):
    services = make_services(tmp_path, "local")
    services.positions = services.state
    services.state.put("AAAAAAAAAAA", 500, 1033, "first", updated=1000)
    monkeypatch.setattr(portal.Handler, "services", services)
    monkeypatch.setattr(portal.Handler, "allowed_hosts", {"127.0.0.1"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(body):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/api/position", json.dumps(body), {"Host": "127.0.0.1", "Content-Type": "application/json"})
        res = conn.getresponse()
        answer = (res.status, json.loads(res.read()))
        conn.close()
        return answer

    try:
        for bad in ("yes", 1, None):
            assert post({"video_id": "AAAAAAAAAAA", "t": 1033, "keep_order": bad})[0] == 400
        status, answer = post({"video_id": "AAAAAAAAAAA", "t": 1033, "duration": 1033, "keep_order": True})
        assert status == 200 and answer["position"]["updated"] == 1000
        assert post({"video_id": "AAAAAAAAAAA", "t": 0})[1]["position"]["updated"] > 1000
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_validate_card():
    card = portal.validate_card({"deck": "Mining", "note_model": "Basic",
                                 "fields": {"Front": "{expression}", "Back": portal.SENTENCE_CLOZE}})
    assert card["first_field"] == "Front" and card["sentence_field"] == "Back"
    assert card["tags"] == ["shisu-ko-portal"] and card["duplicate_scope"] == "deck"
    assert portal.validate_card({"deck": "x"}, partial=True) == {"deck": "x"}
    basic = portal.validate_card({"deck": "x", "note_model": "Basic", "fields": {"Front": "{expression}", "Back": "{glossary}"}})
    assert basic["sentence_field"] == ""  # no sentence field: still a valid setup
    assert portal.validate_card(basic) == basic  # and it validates again once saved
    for bad in ({"dek": "x"}, {"deck": "x", "note_model": "y"}, {"deck": "", "note_model": "y", "fields": {"F": ""}},
                {"deck": "x", "note_model": "y", "fields": {}}, {"deck": "x", "note_model": "y", "fields": {"F": ""}, "tags": ["a b"]},
                {"deck": "x", "note_model": "y", "fields": {"F": ""}, "first_field": "G"}):
        with pytest.raises(ValueError):
            portal.validate_card(bad)
    with pytest.raises(ValueError):
        portal.validate_card(["not", "an", "object"])


def test_read_card_file(tmp_path):
    assert portal.read_card_file(tmp_path / "missing.json") is None
    (tmp_path / "portal.json").write_text('{"deck": "Mining"}', encoding="utf-8")
    assert portal.read_card_file(tmp_path / "portal.json") == {"deck": "Mining"}
    (tmp_path / "portal.json").write_text('{"dek": "Mining"}', encoding="utf-8")
    with pytest.raises(ValueError):
        portal.read_card_file(tmp_path / "portal.json")


def test_a_leftover_auto_sync_is_accepted_warned_about_and_ignored(tmp_path, caplog):
    # 0.20.0 and 0.21.0 read auto_sync from portal.json; the server syncs Anki now. A file that
    # still holds it, with any value, must not stop the portal: unknown keys do, this one warns.
    path = tmp_path / "portal.json"
    for value in (False, True, "on"):
        path.write_text(json.dumps({"deck": "Fixture deck", "auto_sync": value}), encoding="utf-8")
        caplog.clear()
        with caplog.at_level("WARNING", logger="portal"):
            assert portal.read_card_file(path) == {"deck": "Fixture deck"}
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 1 and "auto_sync" in warnings[0] and "ignored" in warnings[0]
    path.write_text('{"auto_sync": false}', encoding="utf-8")
    assert portal.read_card_file(path) == {}
    path.write_text('{"deck": "Fixture deck"}', encoding="utf-8")
    caplog.clear()
    with caplog.at_level("WARNING", logger="portal"):
        portal.read_card_file(path)
    assert caplog.records == []


def test_health_passes_on_the_servers_anki_sync(tmp_path, monkeypatch):
    services = make_services(tmp_path, "local")
    sync = {"state": "watching", "last": 1_700_000_000_000, "error": None}
    monkeypatch.setattr(portal, "request", lambda *a, **kw: (200, json.dumps({"version": "0.21.1", "anki_sync": sync}).encode()))
    monkeypatch.setattr(portal, "yomitan", lambda *a, **kw: {"version": "1"})
    monkeypatch.setattr(portal, "anki", lambda *a, **kw: 6)
    shisuko = services.health()["services"]["shisuko"]
    assert shisuko == {"ok": True, "version": "0.21.1", "anki_sync": sync}
    # A server from before the move has no anki_sync: None, and Settings shows no line.
    monkeypatch.setattr(portal, "request", lambda *a, **kw: (200, b'{"version": "0.21.0"}'))
    assert services.health()["services"]["shisuko"]["anki_sync"] is None


def test_the_portal_never_calls_ankiconnects_sync():
    source = PATH.read_text(encoding="utf-8")
    assert '"sync"' not in source.replace('"/sync"', "")
    assert "/api/anki/sync" not in source
    for name in ("app.js", "lib.js", "index.html"):
        assert "/api/anki/sync" not in (PATH.parent / "static" / name).read_text(encoding="utf-8")


def test_guess_fields_for_a_rich_note_type():
    names = ["Expression", "Reading", "Furigana", "Meaning", "Definition (JP)", "Sentence", "Sentence Audio",
             "Word Audio", "Image", "Pitch Accent", "Pitch Graph", "Pitch Position", "Selected Definition",
             "Frequency", "Notes"]
    assert portal.guess_fields(names) == {
        "Expression": "{expression}", "Reading": "{reading}", "Furigana": "{furigana}", "Meaning": "{glossary}",
        "Definition (JP)": "{glossary}", "Sentence": portal.SENTENCE_CLOZE, "Sentence Audio": "{portal-sentence-audio}",
        "Word Audio": "{audio}", "Image": "{portal-picture}", "Pitch Accent": "{pitch-accents}",
        "Pitch Graph": "{pitch-accent-graphs}", "Pitch Position": "{pitch-accent-positions}",
        "Selected Definition": "{popup-selection-text}", "Frequency": "{frequency-harmonic-rank}", "Notes": ""}


def test_guess_fields_for_basic_and_other_names():
    assert portal.guess_fields(["Front", "Back"]) == {"Front": "{expression}", "Back": "{glossary}"}
    assert portal.guess_fields(["Expression", "Meaning", "Sentence Audio", "Screenshot"]) == {
        "Expression": "{expression}", "Meaning": "{glossary}", "Sentence Audio": "{portal-sentence-audio}",
        "Screenshot": "{portal-picture}"}
    assert portal.guess_fields(["Vocab-Kana", "image"])["Vocab-Kana"] == "{reading}"
    # Nothing recognisable: the first field still gets the word (Anki checks duplicates on it).
    assert portal.guess_fields(["Feld 1", "Feld 2"]) == {"Feld 1": "{expression}", "Feld 2": ""}


def test_host_names_from_tailscale_status():
    status = {"Self": {"HostName": "desk", "DNSName": "desk.example.ts.net.", "TailscaleIPs": ["100.101.1.1"]}}
    names = portal.host_names(["127.0.0.1"], ["phone-alias"], status)
    assert {"desk", "desk.example.ts.net", "100.101.1.1", "localhost", "127.0.0.1", "phone-alias"} <= names
    assert "evil.example" not in names


# ---------------------------------------------------------------- access modes and pairing


def make_services(tmp_path, mode):
    args = SimpleNamespace(mode=mode, tailscale_bin="/nonexistent/tailscale", config=tmp_path / "portal.json")
    services = portal.Services(args)
    services.state = portal.PortalState(tmp_path / "state.json")
    services.owners_at = time.time()
    services.owners = {"100.101.1.2"}
    return services


def test_authorize_by_mode(tmp_path):
    local = make_services(tmp_path, "local")
    assert local.authorize("127.0.0.1", None) == "ok"
    assert local.authorize("192.168.1.5", None) == "forbidden"
    lan = make_services(tmp_path, "lan")
    assert lan.authorize("192.168.1.5", None) == "unpaired"
    assert lan.authorize("192.168.1.5", "x" * 43) == "unpaired"  # an unknown token
    token = lan.state.add_device("Android · Chrome")
    assert lan.authorize("192.168.1.5", token) == "ok"
    tailnet = make_services(tmp_path, "tailscale")
    assert tailnet.authorize("100.101.1.2", None) == "ok"  # the owner's own device
    assert tailnet.authorize("100.101.2.1", None) == "forbidden"  # another user's
    assert tailnet.authorize("100.101.2.1", token) == "ok"  # unless it holds a paired token


def test_pairing_codes_work_once(tmp_path):
    lan = make_services(tmp_path, "lan")
    lan.new_pairing()
    short = lan.pairing["short"]
    assert lan.pair("00000000" if short != "00000000" else "11111111", "") is None  # wrong code
    token = lan.pair(f"{short[:4]} {short[4:]}", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) Safari/604.1")
    assert token and lan.authorize("192.168.1.5", token) == "ok"
    assert lan.pair(short, "") is None  # used: a new code replaced it
    long = lan.pairing["long"]
    assert lan.pair(long, "") and lan.pair(long, "") is None
    assert [d["name"] for d in lan.state.devices()][0] == "iPhone · Safari"


def test_expired_pairing_code_is_refused(tmp_path):
    lan = make_services(tmp_path, "lan")
    lan.new_pairing()
    short = lan.pairing["short"]
    lan.pairing["until"] = time.time() - 1
    assert lan.pair(short, "") is None
    assert lan.pairing["short"] != short or lan.pairing["until"] > time.time()  # a new one is out


def test_pairing_is_rate_limited(tmp_path):
    lan = make_services(tmp_path, "lan")
    assert not any(lan.pair_limited("192.168.1.5") for _ in range(portal.PAIR_PER_IP))
    assert lan.pair_limited("192.168.1.5")
    assert not lan.pair_limited("192.168.1.6")  # another address has its own budget


def test_devices_are_stored_as_hashes_and_can_be_forgotten(tmp_path):
    state = portal.PortalState(tmp_path / "state.json")
    token = state.add_device("Mac · Firefox")
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert token not in raw  # only the hash is on disk
    device = state.device_for(token)
    assert device and "hash" not in device
    assert state.forget("ffffffff") == 0
    assert state.forget(device["id"]) == 1
    assert state.device_for(token) is None
    state.add_device("a")
    state.add_device("b")
    assert state.forget() == 2


def test_check_binds():
    assert portal.check_binds("local", ["127.0.0.1"]) is None
    assert portal.check_binds("local", ["192.168.1.5"])
    assert portal.check_binds("local", ["0.0.0.0"])
    assert portal.check_binds("tailscale", ["127.0.0.1", "100.101.1.1"]) is None
    assert portal.check_binds("tailscale", ["192.168.1.5"])
    assert portal.check_binds("tailscale", ["::"])
    assert portal.check_binds("lan", ["0.0.0.0"]) is None  # every device must pair anyway
    assert portal.check_binds("lan", ["not-an-ip"])


def test_device_name():
    assert portal.device_name("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36") == "Android · Chrome"
    assert portal.device_name("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0; rv:140.0) Gecko/20100101 Firefox/140.0") == "Mac · Firefox"
    assert portal.device_name("") == "Device · browser"


def test_service_address():
    assert portal.service_address("127.0.0.1:9999", ("x", 1)) == ("127.0.0.1", 9999)
    assert portal.service_address(":9999", ("127.0.0.1", 1)) == ("127.0.0.1", 9999)


# ---------------------------------------------------------------- pictures with PyAV


def synthetic_ts(seconds=2, fps=10):
    """A tiny MPEG-TS clip whose frame brightness encodes its time: frame n is grey level 10*n."""
    av = pytest.importorskip("av")
    import io
    buf = io.BytesIO()
    with av.open(buf, "w", format="mpegts") as out:
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
    return buf.getvalue()


def test_frame_jpeg_from_a_segment():
    av = pytest.importorskip("av")
    import io
    data = synthetic_ts()
    jpeg = portal.frame_jpeg(data, 1.0, relative=True)
    assert jpeg.startswith(b"\xff\xd8")
    with av.open(io.BytesIO(jpeg)) as img:
        frame = next(img.decode(video=0))
        assert (frame.width, frame.height) == (64, 48)
        level = float(frame.to_ndarray(format="gray").mean())
    assert 90 <= level <= 115  # the frame at 1.0 s (n = 10, grey 100), not the first one
    assert portal.frame_jpeg(data, 99, relative=True).startswith(b"\xff\xd8")  # past the end: the last frame


def test_with_start_marks_a_resume_in_master_and_media_playlists():
    master, _, _ = portal.rewrite_master(MASTER, "/api/hls/TOKEN/")
    started = portal.with_start(master, 482)
    lines = started.split("\n")
    assert lines[0] == "#EXTM3U" and lines[1] == "#EXT-X-START:TIME-OFFSET=482.0,PRECISE=YES"
    assert "/api/hls/TOKEN/v232.m3u8?start=482.0" in lines
    assert 'URI="/api/hls/TOKEN/a234.m3u8?start=482.0"' in started
    media = portal.rewrite_media("#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nhttps://g.example/s0\n#EXT-X-ENDLIST\n", "/api/hls/T/v232/")[0]
    assert portal.with_start(media, 482).split("\n")[1].startswith("#EXT-X-START:TIME-OFFSET=482.0")
    assert "/api/hls/T/v232/0.ts" in portal.with_start(media, 482)  # segment URIs untouched
    # No start, or nonsense: the playlist as it was (the cached one is never changed).
    for t in (None, "", "0", "-5", "abc", "1e9"):
        assert portal.with_start(master, t) == master
    assert portal.with_start(started, 90).count("#EXT-X-START") == 1


# ---------------------------------------------------------------- word colours


def test_sync_asks_the_server_for_the_card_setups_deck_only_when_the_page_does(tmp_path, monkeypatch):
    services = make_services(tmp_path, "local")
    sent = []

    def request(base, method, path, body=None, headers=None, timeout=None):
        if path == "/health":
            return 200, b'{"model": "tiny"}'
        sent.append(json.loads(body))
        reply = {"ok": True, "cues": []}
        if "deck" in sent[-1]:
            reply["deck"] = {"token": "e1.1", "status": "ok", "full": True, "notes": [[1, "猫", "", "new", None]], "removed": []}
        return 200, json.dumps(reply).encode()

    monkeypatch.setattr(portal, "request", request)
    plain = services.sync({"video_id": "dQw4w9WgXcQ", "t": 1})
    assert "deck" not in sent[-1] and "deck" not in plain
    monkeypatch.setattr(services, "card", lambda: {"deck": "Fixture deck"})
    answer = services.sync({"video_id": "dQw4w9WgXcQ", "t": 1, "deck": {"since": "e1.0"}})
    assert sent[-1]["deck"] == {"name": "Fixture deck", "since": "e1.0"}
    assert answer["deck"]["notes"] == [[1, "猫", "", "new", None]]
    # The page names a token, never a deck; anything else it sends is dropped.
    services.sync({"video_id": "dQw4w9WgXcQ", "t": 1, "deck": {"since": "bad token!", "name": "Other"}})
    assert sent[-1]["deck"] == {"name": "Fixture deck", "since": None}
    monkeypatch.setattr(services, "card", lambda: None)
    services.sync({"video_id": "dQw4w9WgXcQ", "t": 1, "deck": {}})
    assert sent[-1]["deck"] == {"name": "", "since": None}  # no card setup: the server's automatic deck


def test_the_page_gets_the_extensions_own_words_js():
    assert portal.WORDS_JS == PATH.parent.parent / "addon" / "words.js"
    assert portal.STATIC_NAME_RE.match("words.js")
    assert not (PATH.parent / "static" / "words.js").exists(), "one words.js, served from addon/"
    html = (PATH.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index('src="words.js"') < html.index('src="app.js"')


# ---------------------------------------------------------------- the HLS resolve: anonymous first


def fake_resolver(monkeypatch, tmp_path, outcomes, cookies=True):
    """yt-dlp stand-in: `outcomes` maps signed in (True/False) to an info dict or an exception.
    Returns the list of option dicts each YoutubeDL was made with."""
    monkeypatch.setattr(portal, "SHISUKO_HOME", tmp_path)
    if cookies:
        (tmp_path / "config.json").write_text('{"cookies_from_browser": "firefox"}', encoding="utf-8")
    made = []

    class YoutubeDL:
        def __init__(self, options):
            self.options = options
            made.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            outcome = outcomes["cookiesfrombrowser" in self.options]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(portal, "import_ytdlp", lambda: SimpleNamespace(YoutubeDL=YoutubeDL))
    monkeypatch.setattr(portal, "http_open", lambda url, headers=None, timeout=20.0: SimpleNamespace(read=lambda: b"#EXTM3U"))
    monkeypatch.setattr(portal, "rewrite_master", lambda text, prefix: ("#EXTM3U", {"v1": "u"}, {"v1": 720}))
    return made


HLS_INFO = {"duration": 60, "title": "t", "formats": [{"protocol": "m3u8_native", "manifest_url": "https://example.invalid/m"}]}
NO_HLS_INFO = {"duration": 60, "title": "t", "formats": [{"protocol": "https", "url": "https://example.invalid/v"}]}


def test_resolve_takes_the_anonymous_hls_and_never_reads_the_cookies(tmp_path, monkeypatch):
    made = fake_resolver(monkeypatch, tmp_path, {False: HLS_INFO, True: AssertionError("signed in")})
    video = portal.HlsVideo("dQw4w9WgXcQ")
    portal.HlsProxy().resolve(video)
    assert video.master and len(made) == 1 and "cookiesfrombrowser" not in made[0]


@pytest.mark.parametrize("anonymous", [NO_HLS_INFO, RuntimeError("Sign in to confirm you're not a bot")])
def test_resolve_tries_the_cookies_when_anonymous_gives_no_hls(tmp_path, monkeypatch, anonymous):
    made = fake_resolver(monkeypatch, tmp_path, {False: anonymous, True: HLS_INFO})
    video = portal.HlsVideo("dQw4w9WgXcQ")
    portal.HlsProxy().resolve(video)
    assert video.master and ["cookiesfrombrowser" in o for o in made] == [False, True]
    assert made[1]["cookiesfrombrowser"] == ("firefox",)


def test_resolve_without_cookies_or_with_both_failing_raises(tmp_path, monkeypatch):
    made = fake_resolver(monkeypatch, tmp_path, {False: NO_HLS_INFO, True: HLS_INFO}, cookies=False)
    with pytest.raises(portal.UpstreamError, match="no HLS"):
        portal.HlsProxy().resolve(portal.HlsVideo("dQw4w9WgXcQ"))
    assert len(made) == 1  # no browser chosen: no second try
    # Both tries failing in their own ways: the signed-in error, unless the anonymous try's clean
    # "no HLS" came first, which sends the page to YouTube's player (422) rather than a 502.
    fake_resolver(monkeypatch, tmp_path, {False: NO_HLS_INFO, True: RuntimeError("members only")})
    with pytest.raises(portal.UpstreamError, match="no HLS") as caught:
        portal.HlsProxy().resolve(portal.HlsVideo("dQw4w9WgXcQ"))
    assert caught.value.status == 422
    fake_resolver(monkeypatch, tmp_path, {False: RuntimeError("bot check"), True: RuntimeError("members only")})
    with pytest.raises(RuntimeError, match="members only"):
        portal.HlsProxy().resolve(portal.HlsVideo("dQw4w9WgXcQ"))


def test_a_live_stream_is_not_tried_again_with_cookies(tmp_path, monkeypatch):
    made = fake_resolver(monkeypatch, tmp_path, {False: {**HLS_INFO, "is_live": True}, True: HLS_INFO})
    with pytest.raises(portal.UpstreamError, match="live"):
        portal.HlsProxy().resolve(portal.HlsVideo("dQw4w9WgXcQ"))
    assert len(made) == 1


def test_only_the_hls_resolve_goes_without_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "SHISUKO_HOME", tmp_path)
    (tmp_path / "config.json").write_text('{"cookies_from_browser": "firefox"}', encoding="utf-8")
    assert portal.ydl_options("best")["cookiesfrombrowser"] == ("firefox",)  # the picture's stream URL
    assert "cookiesfrombrowser" not in portal.ydl_options(cookies=False)

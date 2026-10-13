"""Word timings on cues: built with them, kept through merges, cached, and sent only on request.

A cue built from Whisper's words keeps them as `words`, [{start, end, text}], whose texts join to
the cue's text exactly. /sync leaves them out unless the body says "words": true, so the
extension, which never asks, gets the same bytes it always did.
"""
from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace

from _serverlib import load_server

server = load_server()
W = server.Word
VIDEO = "abcdefabcdef"


def words(spec, prob: float = 0.9):
    return [W(text, start, end, prob) for text, start, end in spec]


def joined(cue) -> str:
    return "".join(w["text"] for w in cue["words"])


def assert_settled(cue):
    """Texts spell the cue; times are in order and inside it."""
    assert joined(cue) == cue["text"]
    floor = cue["start"]
    for w in cue["words"]:
        assert floor <= w["start"] <= w["end"] <= cue["end"], (w, cue)
        floor = w["end"]


# --------------------------------------------------------------------------- build_cues

def test_build_cues_keeps_the_words_and_their_text_spells_the_cue():
    ws = words([("今日", 1.0, 1.3), ("は", 1.3, 1.4), ("いい", 1.4, 1.7), ("天気", 1.7, 2.1), ("です。", 2.1, 2.5)])
    cues = server.build_cues(ws, [[0.9, 2.6]], server.CueLimits())
    assert len(cues) == 1
    assert cues[0]["text"] == "今日はいい天気です。"
    assert [w["text"] for w in cues[0]["words"]] == ["今日", "は", "いい", "天気", "です。"]
    assert cues[0]["words"][0]["start"] == 1.0 and cues[0]["words"][-1]["end"] == 2.5


def test_cue_words_strip_what_word_text_strips_and_drop_empty_edges():
    ws = words([(" ", 0.0, 0.1), (" はい", 0.1, 0.4), ("そう ", 0.4, 0.8), ("  ", 0.8, 0.9)])
    out = server.cue_words(ws)
    assert [w["text"] for w in out] == ["はい", "そう"]
    assert "".join(w["text"] for w in out) == server.word_text(ws)
    assert server.cue_words(words([(" ", 0.0, 0.1)])) == []


def test_a_carried_sentence_mark_is_in_the_words_too():
    # trim_words() drops the stretched last word and carry_trailing_mark() moves its 。 onto the
    # one that now ends the cue: the word list must show the same mark the text does.
    ws = words([("そう", 1.0, 1.3), ("なんだ", 1.3, 1.7), ("。", 1.7, 4.5)])
    cues = server.build_cues(ws, [[0.9, 1.8]], server.CueLimits())
    assert cues[0]["text"] == "そうなんだ。"
    assert joined(cues[0]) == cues[0]["text"]


# --------------------------------------------------------------------------- merges

def cue(start, end, text, seg=0, ws=None):
    c = {"start": start, "end": end, "text": text, "seg": seg}
    if ws is not None:
        c["words"] = [{"start": a, "end": b, "text": t} for t, a, b in ws]
    return c


def test_merge_adjacent_concatenates_the_words_in_order():
    a = cue(0.0, 0.5, "ねえ", ws=[("ね", 0.0, 0.2), ("え", 0.2, 0.5)])
    b = cue(0.6, 1.5, "聞いて", ws=[("聞", 0.6, 0.9), ("いて", 0.9, 1.5)])
    out = server.merge_adjacent([a, b], server.CueLimits(), 0.35, only_short=False)
    assert len(out) == 1 and out[0]["text"] == "ねえ聞いて"
    assert [w["text"] for w in out[0]["words"]] == ["ね", "え", "聞", "いて"]
    # The input cues are untouched: a merge builds new word lists.
    assert [w["text"] for w in a["words"]] == ["ね", "え"]


def test_merge_adjacent_puts_the_sentence_seam_on_the_last_word_of_the_row():
    a = cue(0.0, 0.5, "うん。", ws=[("うん。", 0.0, 0.5)])
    b = cue(0.6, 1.5, "そうだね", ws=[("そう", 0.6, 1.0), ("だね", 1.0, 1.5)])
    out = server.merge_adjacent([a, b], server.CueLimits(), 0.35, only_short=False)
    assert out[0]["text"] == "うん。\nそうだね"
    assert [w["text"] for w in out[0]["words"]] == ["うん。\n", "そう", "だね"]
    assert joined(out[0]) == out[0]["text"]
    assert a["words"][0]["text"] == "うん。"


def test_merge_segments_concatenates_across_segments_with_the_seam_it_chose():
    a = cue(0.0, 1.0, "思", seg=0, ws=[("思", 0.0, 1.0)])
    b = cue(1.1, 2.0, "っております", seg=1, ws=[("っ", 1.1, 1.3), ("ております", 1.3, 2.0)])
    out = server.merge_segments([a, b], server.CueLimits())
    assert len(out) == 1 and out[0]["text"] == "思っております"
    assert [w["text"] for w in out[0]["words"]] == ["思", "っ", "ております"]


def test_a_merge_with_a_cue_that_has_no_words_leaves_none():
    # An old cue has none, and a partial list would be a wrong one.
    a = cue(0.0, 0.5, "ねえ", ws=[("ねえ", 0.0, 0.5)])
    b = cue(0.6, 1.5, "聞いて")
    out = server.merge_adjacent([a, b], server.CueLimits(), 0.35, only_short=False)
    assert out[0]["text"] == "ねえ聞いて" and "words" not in out[0]


# --------------------------------------------------------------------------- settle_words

def test_settle_words_rounds_clamps_and_keeps_order():
    c = cue(1.0, 2.0, "あいうえ", ws=[("あ", 0.71234, 1.2), ("い", 1.15, 1.4), ("う", 1.5, 2.6), ("え", 2.7, 3.0)])
    server.settle_words([c])
    assert c["words"] == [
        {"start": 1.0, "end": 1.2, "text": "あ"},     # pulled in to the cue's start
        {"start": 1.2, "end": 1.4, "text": "い"},     # never before the word in front of it ends
        {"start": 1.5, "end": 2.0, "text": "う"},     # cut at the cue's end
        {"start": 2.0, "end": 2.0, "text": "え"},     # past the end: squeezed onto it
    ]
    assert_settled(c)


def test_settle_words_drops_words_that_no_longer_spell_the_text():
    c = cue(0.0, 1.0, "はい。", ws=[("はい", 0.0, 1.0)])
    empty = cue(0.0, 1.0, "はい", ws=[])
    server.settle_words([c, empty])
    assert "words" not in c and "words" not in empty


def test_build_window_cues_settles_every_cue_after_normalise_gaps():
    segs = [
        SimpleNamespace(text="今日はいい天気ですね。", start=0.0, end=2.4, no_speech_prob=0.0, avg_logprob=-0.2,
                        compression_ratio=1.0,
                        words=words([("今日", 0.0, 0.4), ("は", 0.4, 0.5), ("いい", 0.5, 0.9), ("天気", 0.9, 1.4),
                                     ("です", 1.4, 1.9), ("ね。", 1.9, 2.4)])),
        SimpleNamespace(text="散歩に行こうか", start=2.6, end=4.4, no_speech_prob=0.0, avg_logprob=-0.2,
                        compression_ratio=1.0,
                        words=words([("散歩", 2.6, 3.1), ("に", 3.1, 3.2), ("行こう", 3.2, 3.9), ("か", 3.9, 4.4)])),
    ]
    cues, _ = server.build_window_cues(segs, 10.0, [[9.9, 12.5], [12.55, 14.5]], server.CueLimits(), 0)
    assert cues
    for c in cues:
        assert "words" in c
        assert_settled(c)
        assert all(round(w["start"], 2) == w["start"] and round(w["end"], 2) == w["end"] for w in c["words"])


# --------------------------------------------------------------------------- cache

def make_app(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = SimpleNamespace(first_window=20.0, window=30.0, lookahead=900.0, model="large-v3", language="ja",
                           idle_minutes=30, language_patience=60.0, lyrics="auto")
    app = server.App(args, model=None, device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


WORDED = {"start": 1.0, "end": 2.0, "text": "はい", "words": [{"start": 1.0, "end": 1.5, "text": "は"},
                                                              {"start": 1.5, "end": 1.8, "text": "い"}], "seg": 0}
PLAIN = {"start": 3.0, "end": 4.0, "text": "そう", "seg": 1}


def test_words_survive_a_cache_round_trip(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=10.0)
    s.cues = [dict(WORDED, id=0), dict(PLAIN, id=1)]
    s.covered = [[0.0, 10.0]]
    app.save_cache(s)
    assert server.CACHE_FORMAT == 6
    data = json.loads((tmp_path / f"{VIDEO}.cues.json").read_text(encoding="utf-8"))
    assert data["format"] == 6 and data["cues"][0]["words"] == WORDED["words"]
    reloaded = server.Session(video_id=VIDEO, url="u")
    app.load_cache(reloaded)
    assert reloaded.cues[0]["words"] == WORDED["words"]
    assert "words" not in reloaded.cues[1]


def test_an_old_record_without_words_still_loads(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    record = {"video_id": VIDEO, "title": "t", "duration": 10.0, "format": 6, "model": "large-v3",
              "language": "ja", "cues": [PLAIN], "covered": [[0.0, 10.0]], "speech": [[3.0, 4.0]],
              "lyrics": "auto", "language_state": {"foreign_seconds": 0.0, "heard": None, "paused": False}}
    (tmp_path / f"{VIDEO}.cues.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    s = server.Session(video_id=VIDEO, url="u")
    app.load_cache(s)
    assert s.cues == [dict(PLAIN, id=0)] and s.status == "ready"
    assert app.sync(VIDEO, "u", 0.0, 0, words=True)["cues"] == [dict(PLAIN, id=0)]


# --------------------------------------------------------------------------- /sync

def loaded_app(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    s = server.Session(video_id=VIDEO, url="u", duration=10.0)
    s.cues = [dict(WORDED, id=0), dict(PLAIN, id=1)]
    app.sessions[VIDEO] = s
    return app, s


def test_sync_without_the_flag_sends_no_words_and_the_same_cues_as_before(monkeypatch, tmp_path):
    app, s = loaded_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0)
    assert all("words" not in c for c in resp["cues"])
    # Byte for byte what a cue without the field serialises to, key order included.
    assert json.dumps(resp["cues"][0], ensure_ascii=False) == \
        '{"start": 1.0, "end": 2.0, "text": "はい", "seg": 0, "id": 0}'
    assert "words" in s.cues[0]  # the session keeps them for the next client that asks


def test_sync_with_the_flag_sends_the_words(monkeypatch, tmp_path):
    app, _ = loaded_app(monkeypatch, tmp_path)
    resp = app.sync(VIDEO, "u", 0.0, 0, words=True)
    assert resp["cues"][0]["words"] == WORDED["words"]
    assert "words" not in resp["cues"][1]  # absent where there are none, never empty


def post(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/sync", body=json.dumps(body))
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def test_http_sync_sends_words_only_for_a_literal_true(monkeypatch, tmp_path):
    app, _ = loaded_app(monkeypatch, tmp_path)
    server.Handler.app = app
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        base = {"video_id": VIDEO, "t": 0, "since": 0}
        for extra in ({}, {"words": False}, {"words": "true"}, {"words": 1}):
            status, body = post(port, dict(base, **extra))
            assert status == 200 and all("words" not in c for c in body["cues"]), extra
        status, body = post(port, dict(base, words=True))
        assert status == 200 and body["cues"][0]["words"] == WORDED["words"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.app = None

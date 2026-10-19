"""Mining through /sync: the extension's `anki` settings, the viewer's own `mine`, and the
`mined` log the answer carries. The fixtures are test_mining.py's, all invented."""
from __future__ import annotations

import json

import pytest

from _serverlib import load_server
from test_mining import NOW, VIDEO, FakeAnki, fresh_id, note_fields, rig  # noqa: F401 - rig is a fixture
from test_update_endpoint import Served, make_app

server = load_server()


def drain(miner):
    while not miner.jobs.empty():
        job = miner.jobs.get()
        (miner.attach_new if job[0] == "auto" else miner.mine_now)(*job[1:])


def test_mine_request_takes_a_cue_id_and_a_nonce_only():
    assert server.mine_request({"cue_id": 3, "nonce": "a1-B_", "session": "abc"}) == {"cue_id": 3, "nonce": "a1-B_", "session": "abc"}
    for bad in ({"cue_id": -1, "nonce": "n"}, {"cue_id": True, "nonce": "n"}, {"cue_id": "3", "nonce": "n"},
                {"cue_id": 1, "nonce": ""}, {"cue_id": 1, "nonce": "a b"}, {"cue_id": 1, "nonce": "x" * 41},
                {"cue_id": 1, "nonce": "n", "session": 5}, "mine"):
        with pytest.raises(ValueError):
            server.mine_request(bad)


def body(**extra):
    return dict({"video_id": VIDEO, "url": f"https://www.youtube.com/watch?v={VIDEO}", "t": 14.0, "since": 0,
                 "anki": {"picture": "Picture", "audio": "SentenceAudio"}}, **extra)


def sync(app, **extra):
    b = body(**extra)
    return app.sync(b["video_id"], b["url"], b["t"], b["since"], anki=b.get("anki"), mine=b.get("mine"),
                    mined_since=b.get("mined_since"))


def test_a_mine_through_sync_fills_the_newest_note_once(rig):
    s = rig.session()
    rig.anki.add(fresh_id(60), note_fields(Sentence="古い"))
    newest = fresh_id(30)
    rig.anki.add(newest, note_fields())
    first = sync(rig.app, mined_since=None)
    assert first["mined"] == [] and first["mined_next"] == 0
    mine = {"cue_id": 1, "nonce": "abc123", "session": s.token}
    sync(rig.app, mine=mine, mined_since=0)
    sync(rig.app, mine=mine, mined_since=0)  # the answer was lost and the client sent it again
    assert rig.miner.jobs.qsize() == 1
    drain(rig.miner)
    answer = sync(rig.app, mined_since=0)
    assert [(e["nonce"], e["status"], e["note"]) for e in answer["mined"]] == [("abc123", "attached", newest)]
    assert answer["mined_next"] == 1 and sync(rig.app, mined_since=1)["mined"] == []
    assert rig.anki.notes[newest]["SentenceAudio"]["value"].startswith("[sound:")
    # The automatic attach leaves the note the viewer mined into alone.
    assert rig.miner.attach_new(newest) is None


def test_a_mine_answers_why_nothing_was_attached(rig):
    s = rig.session()
    mine = lambda n, cue=1, session=None: {"cue_id": cue, "nonce": n, "session": session}  # noqa: E731
    assert rig.miner.mine_now(s, s.anki, mine("a"))["status"] == "no_note"
    rig.anki.add(fresh_id(), note_fields(Picture="p", SentenceAudio="a"))
    assert rig.miner.mine_now(s, s.anki, mine("b"))["status"] == "filled"
    rig.anki.add(fresh_id(1), {"Front": {"value": "", "order": 0}, "Back": {"value": "", "order": 1}})
    missing = rig.miner.mine_now(s, s.anki, mine("c"))
    assert missing["status"] == "no_fields" and '"Picture" or "SentenceAudio"' in missing["message"]
    assert "Its fields: Front, Back." in missing["message"]
    assert rig.miner.mine_now(s, s.anki, mine("d", cue=99))["status"] == "no_cue"
    assert rig.miner.mine_now(s, s.anki, mine("e", session="old-token"))["status"] == "no_cue"
    rig.anki.down = True
    assert rig.miner.mine_now(s, s.anki, mine("f"))["message"] == "Anki is not running or AnkiConnect is not installed"
    assert [e["nonce"] for e in s.mined] == list("abcdef")


def test_a_mine_with_mining_off_says_so(rig):
    rig.app.miner = None
    s = rig.session()
    sync(rig.app, mine={"cue_id": 1, "nonce": "zz", "session": s.token}, mined_since=0)
    events = sync(rig.app, mined_since=0)["mined"]
    assert [(e["status"], e["ok"]) for e in events] == [("off", False)]


def test_sync_keeps_valid_settings_and_reports_bad_ones(rig):
    s = rig.session(settings={})
    answer = sync(rig.app, anki={"picture": "Bild", "pad_ms": 0})
    assert "anki_error" not in answer and s.anki["picture"] == "Bild" and s.anki["pad_ms"] == 0
    answer = sync(rig.app, anki={"picture": "<img>"})
    assert "anki.picture" in answer["anki_error"] and s.anki["picture"] == "Bild"  # the last good ones stand
    plain = rig.app.sync(VIDEO, "", 14.0, 0)
    assert not {"mined", "mined_next", "anki_error"} & set(plain)  # a body without `anki`: as before


def test_a_cursor_from_before_a_restart_reads_the_whole_log(rig):
    s = rig.session()
    server.Miner.record(s, {"nonce": None, "ok": True, "status": "attached", "message": "m", "note": 1}, NOW)
    assert [e["seq"] for e in sync(rig.app, mined_since=50)["mined"]] == [0]


def test_the_sync_endpoint_validates_mine_and_mined_since(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    served = Served(app)
    try:
        def post(**extra):
            return served.request("POST", "/sync", json.dumps(body(**extra)))

        status, answer = post(mined_since=None)
        assert status == 200 and answer["mined"] == [] and answer["mined_next"] == 0
        assert post(mine={"cue_id": "x", "nonce": "n"})[0] == 400
        assert post(mined_since=-1)[0] == 400
        assert post(mined_since=True)[0] == 400
        status, answer = post(anki={"audio": 'a"b'})
        assert status == 200 and "anki_error" in answer
        status, answer = post(mine={"cue_id": 0, "nonce": "n1"}, mined_since=0)
        assert status == 200  # no miner on a hand-built App: the log says so on the next answer
        assert post(mined_since=0)[1]["mined"][0]["status"] == "off"
    finally:
        served.close()


def test_an_outcome_recorded_at_once_is_not_skipped_by_a_fresh_cursor(rig):
    rig.app.miner = None
    s = rig.session()
    first = sync(rig.app, mine={"cue_id": 1, "nonce": "fresh", "session": s.token}, mined_since=None)
    assert first["mined"] == [] and first["mined_next"] == 0
    events = sync(rig.app, mined_since=first["mined_next"])["mined"]
    assert [(e["nonce"], e["status"]) for e in events] == [("fresh", "off")]

"""The word colours through /sync, and the golden test's server half.

addon/tests/fixtures/deck-golden.json holds invented notes, the buildIndex() entries the
extension's old deck path read them into, and the rows the server reads them into
(addon/tests/deck-golden.test.js checks the old path and turns the rows into entries with the
background's deckEntries()). Here the server's DeckIndex reads the same notes from a fake
AnkiConnect, /sync carries its rows, and they must be the fixture's rows, whose entries
(rows_entries()) are the old path's exactly: the same buildIndex() input, the same marks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _serverlib import load_server
from test_deck_index import Clock, FakeAnki, rows_entries
from test_update_endpoint import Served, make_app

server = load_server()
FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "addon" / "tests" / "fixtures" / "deck-golden.json")
                     .read_text(encoding="utf-8"))
VIDEO = "dQw4w9WgXcQ"


def fixture_anki():
    return FakeAnki({n["noteId"]: {"deck": n["deck"], "status": n["status"], "mod": n["mod"], "edited": False,
                                   "fields": n["fields"]} for n in FIXTURE["notes"]})


@pytest.fixture
def rig(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    clock = Clock()
    anki = fixture_anki()
    app.decks = server.DeckIndexes(anki, path=tmp_path / "anki-deck.json", clock=clock)
    rig = type("Rig", (), {})()
    rig.app, rig.anki, rig.clock = app, anki, clock
    return rig


def sync(app, deck=None):
    extra = {"deck": deck} if deck is not None else {}
    return app.sync(VIDEO, f"https://www.youtube.com/watch?v={VIDEO}", 10.0, 0, **extra)


def deck_body(settings, since=None):
    return {"name": FIXTURE["deck"], "word": settings["ankiWordField"], "pitch": settings["ankiPitchField"], "since": since}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["settings"]["ankiWordField"] or "default")
def test_the_server_gives_the_old_extension_path_s_entries(rig, case):
    first = sync(rig.app, deck_body(case["settings"]))["deck"]
    assert first["status"] == "loading"
    rig.app.decks.step()
    answer = sync(rig.app, deck_body(case["settings"], since=first["token"]))["deck"]
    assert answer["status"] == "ok" and answer["full"] is True
    assert answer["notes"] == case["rows"]  # what deck-golden.test.js turns into entries with deckEntries()
    assert rows_entries(answer["notes"]) == case["entries"]
    # As the client receives it: through JSON.
    assert rows_entries(json.loads(json.dumps(answer, ensure_ascii=False))["notes"]) == case["entries"]


def test_a_sync_without_deck_is_what_it_was(rig, monkeypatch):
    plain = sync(rig.app)
    assert "deck" not in plain
    rig.app.decks = None
    monkeypatch.setattr(server.time, "time", lambda: 1_700_000_000.0)
    without = json.dumps(sync(rig.app), sort_keys=True)
    rig.app.decks = server.DeckIndexes(rig.anki, path=Path("/nonexistent/anki-deck.json"), clock=rig.clock)
    assert json.dumps(sync(rig.app), sort_keys=True) == without
    assert rig.anki.calls == []


def test_the_deck_rides_along_only_when_its_token_moved(rig):
    settings = FIXTURE["cases"][0]["settings"]
    token = sync(rig.app, deck_body(settings))["deck"]["token"]
    assert "deck" not in sync(rig.app, deck_body(settings, since=token))  # still loading: nothing new
    rig.app.decks.step()
    full = sync(rig.app, deck_body(settings, since=token))["deck"]
    assert "deck" not in sync(rig.app, deck_body(settings, since=full["token"]))
    # A review in Anki: the next refresh sends that row alone.
    rig.anki.notes[1013]["status"] = "review"
    rig.clock.t += server.DECK_TTL
    sync(rig.app, deck_body(settings, since=full["token"]))
    rig.app.decks.step()
    delta = sync(rig.app, deck_body(settings, since=full["token"]))["deck"]
    assert delta["full"] is False and delta["removed"] == []
    assert delta["notes"] == [[1013, "魚", "", "learned", "heiban"]]
    assert len(json.dumps(delta)) < len(json.dumps(full)) / 5


def test_a_server_without_anki_says_so_once(rig):
    rig.app.decks = None
    off = sync(rig.app, {"name": "Japanese"})["deck"]
    assert off == {"token": "off", "status": "off", "error": server.DECK_OFF_TEXT}
    assert "deck" not in sync(rig.app, {"name": "Japanese", "since": "off"})


def test_a_malformed_deck_is_answered_not_refused(rig):
    answer = sync(rig.app, {"name": "Japanese", "word": "a:b"})
    assert answer["ok"] is True and answer["deck"]["status"] == "error"
    assert "deck.word" in answer["deck"]["error"]


def test_deck_over_http_and_the_popup_s_deck_list(rig):
    served = Served(rig.app)
    try:
        body = {"video_id": VIDEO, "url": "", "t": 1, "since": 0, "deck": deck_body(FIXTURE["cases"][0]["settings"])}
        status, data = served.request("POST", "/sync", json.dumps(body))
        assert status == 200 and data["deck"]["status"] == "loading"
        status, data = served.request("POST", "/sync", json.dumps({**body, "deck": "Japanese"}))
        assert status == 200 and data["deck"]["error"] == "invalid deck: deck must be an object"
        status, health = served.request("GET", "/health")
        assert "anki_decks" not in health
        status, health = served.request("GET", "/health?decks=1")
        assert health["anki_decks"]["ok"] is True and "Japanese" in health["anki_decks"]["decks"]
        rig.app.decks = None
        status, health = served.request("GET", "/health?decks=1")
        assert health["anki_decks"] == {"ok": False, "error": server.DECK_OFF_TEXT, "seen": None, "offline": False}
    finally:
        served.close()

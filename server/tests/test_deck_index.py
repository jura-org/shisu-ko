"""The word colours' deck index: the server reads one deck through AnkiConnect, keeps what each note
said, and hands clients the rows they turn into words.js buildIndex() entries.

Every test drives DeckIndexes with a fake clock and a fake AnkiConnect (`FakeAnki`) that answers
the searches the index runs by the deck, the status clause and `edited:`; nothing reaches a real
Anki. The note reading itself (wc_plain_word(), wc_pitch_of(), ...) is words.js's: the last test
holds the two equal over random fields and every length limit (Node runs words.js; CI must have
it), and test_deck_golden.py over the
fixture both sides read.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from _serverlib import load_server

server = load_server()
WORDS_JS = Path(__file__).resolve().parents[2] / "addon" / "words.js"


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def note(word, status="new", deck="Japanese", mod=1, **fields):
    """A note of the fake Anki: `Word` at order 0, then any other fields in the order given."""
    values = {"Word": word, **fields}
    return {"deck": deck, "status": status, "mod": mod, "edited": False,
            "fields": {name: {"value": value, "order": i} for i, (name, value) in enumerate(values.items())}}


class FakeAnki:
    """findNotes (deck, status clause, edited:), notesInfo, notesModTime, deckNamesAndIds, deckNames,
    findCards, getDecks. `notes`: id -> note(); `down` makes every call fail as Anki closed would."""

    STATUS = {"is:suspended": {"suspended"}, "-is:suspended": {"new", "learning", "review"},
              "is:new -is:suspended": {"new"}, "is:learn -is:suspended": {"learning"},
              "is:review -is:learn -is:suspended": {"review"}}

    def __init__(self, notes=None):
        self.notes = dict(notes or {})
        self.calls: list = []
        self.down = False
        self.refuse_info = set()  # ids whose notesInfo chunk Anki refuses
        self.decks = {"Default": 1, "Japanese": 2, "Japanese::Sub": 3, "current": 4}

    def actions(self, name=None):
        return [c for c in self.calls if name is None or c[0] == name]

    def in_scope(self, scope, n):
        if scope.startswith("did:"):
            ids = {int(i) for i in scope[4:].split(",")}
            return self.decks.get(n["deck"]) in ids
        name = re.fullmatch(r'"deck:(.*)"', scope).group(1).replace("\\", "")
        return n["deck"] == name or n["deck"].startswith(name + "::")

    def __call__(self, action, params, timeout):
        self.calls.append((action, params))
        if self.down:
            raise server.AnkiUnreachable("[Errno 111] Connection refused")
        if action == "findNotes":
            query = params["query"]
            if query.startswith("did:"):
                scope, _, clause = query.partition(" ")
            else:
                scope, clause = re.fullmatch(r'("deck:(?:[^"\\]|\\.)*") (.*)', query).groups()
            if clause.startswith("edited:"):
                return sorted(i for i, n in self.notes.items() if self.in_scope(scope, n) and n["edited"])
            wanted = self.STATUS[clause]
            return sorted((i for i, n in self.notes.items() if self.in_scope(scope, n) and n["status"] in wanted), reverse=True)
        if action == "notesInfo":
            if set(params["notes"]) & self.refuse_info:
                raise RuntimeError("Note was not found")
            return [{"noteId": i, "fields": self.notes[i]["fields"], "mod": self.notes[i]["mod"]} if i in self.notes else {}
                    for i in params["notes"]]
        if action == "notesModTime":
            return [{"noteId": i, "mod": self.notes[i]["mod"]} for i in params["notes"] if i in self.notes]
        if action == "deckNamesAndIds":
            return dict(self.decks)
        if action == "deckNames":
            return list(self.decks)
        if action == "findCards":
            note_id = int(params["query"][4:])
            return [note_id * 10] if note_id in self.notes else []
        if action == "getDecks":
            return {self.notes[c // 10]["deck"]: [c] for c in params["cards"]}
        raise AssertionError(action)


def request(name="Japanese", since=None, word="", pitch=""):
    return server.deck_request({"name": name, "word": word, "pitch": pitch, "since": since})


def make(notes, tmp_path):
    clock = Clock()
    anki = FakeAnki(notes)
    decks = server.DeckIndexes(anki, path=tmp_path / "anki-deck.json", clock=clock)
    return clock, anki, decks


def settle(decks, req):
    """Ask, let the deck thread read, ask again: the answer a client gets once the read is in."""
    first = decks.ask(req)
    decks.step()
    return first, decks.ask(dict(req, since=first["token"]))


def rows_entries(rows) -> list:
    """The buildIndex() entries a client makes of the rows (deckEntries() in background.js), by id."""
    entries = []
    for _, word, reading, status, pitch in sorted(rows, key=lambda row: row[0]):
        entries.append([word, status, pitch])
        if reading:
            entries.append([reading, status, pitch])
    return entries


def entries_of(answer):
    return rows_entries(answer["notes"])


# ---- reading a deck

def test_first_ask_loads_then_answers_every_row(tmp_path):
    notes = {10: note("食べる", "review", Pitch="[2]", Reading="たべる"), 11: note("猫", "new"),
             12: note("犬", "suspended"), 13: note("鳥", "learning", deck="Other")}
    clock, anki, decks = make(notes, tmp_path)
    first, full = settle(decks, request())
    assert first["status"] == "loading" and first["automatic"] is False
    assert full["status"] == "ok" and full["full"] is True and full["deck"] == "Japanese"
    assert full["notes"] == [[10, "食べる", "", "learned", "nakadaka"], [11, "猫", "", "new", None], [12, "犬", "", "suspended", None]]
    assert entries_of(full) == [["食べる", "learned", "nakadaka"], ["猫", "new", None], ["犬", "suspended", None]]
    # The client holds this now: the same ask is answered with nothing at all.
    assert decks.ask(request(since=full["token"])) is None


def test_a_word_usually_written_in_kana_gets_its_reading_row(tmp_path):
    notes = {5: note("更に", "review", Reading="さらに", Glossary="(adv, uk, JMdict (English)) furthermore")}
    _, _, decks = make(notes, tmp_path)
    _, full = settle(decks, request())
    assert entries_of(full) == [["更に", "learned", None], ["さらに", "learned", None]]


def test_fields_named_by_the_client_are_read(tmp_path):
    notes = {1: note("ignored", "new", Expression="<b>見る</b>", Accent="平板")}
    _, _, decks = make(notes, tmp_path)
    _, full = settle(decks, request(word="expression", pitch="accent"))
    assert full["notes"] == [[1, "見る", "", "new", "heiban"]]


def test_a_refresh_reads_only_new_and_edited_notes_and_sends_a_delta(tmp_path):
    notes = {1: note("猫", "new"), 2: note("犬", "new"), 3: note("鳥", "new")}
    clock, anki, decks = make(notes, tmp_path)
    _, full = settle(decks, request())
    assert [c[1]["notes"] for c in anki.actions("notesInfo")] == [[1, 2, 3]]
    # A review, an edit, a new note and a deletion, half a minute later.
    anki.notes[1]["status"] = "review"
    anki.notes[2].update(edited=True, mod=2)
    anki.notes[2]["fields"]["Word"]["value"] = "子犬"
    anki.notes[4] = note("魚", "learning")
    del anki.notes[3]
    anki.calls.clear()
    clock.t += server.DECK_TTL
    decks.ask(request(since=full["token"]))
    decks.step()
    assert [c[1]["notes"] for c in anki.actions("notesInfo")] == [[2, 4]]
    assert [c[1]["notes"] for c in anki.actions("notesModTime")] == [[2]]
    assert any("edited:2" in c[1]["query"] for c in anki.actions("findNotes"))
    delta = decks.ask(request(since=full["token"]))
    assert delta["full"] is False
    assert delta["notes"] == [[1, "猫", "", "learned", None], [2, "子犬", "", "new", None], [4, "魚", "", "learning", None]]
    assert delta["removed"] == [3]
    # Applied to what the client held, the delta gives what a full answer would.
    held = {row[0]: row for row in full["notes"]}
    held.update({row[0]: row for row in delta["notes"]})
    for gone in delta["removed"]:
        held.pop(gone)
    assert entries_of({"notes": list(held.values())}) == entries_of(decks.ask(request()))


def test_an_unchanged_deck_keeps_its_token(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    _, full = settle(decks, request())
    clock.t += server.DECK_TTL
    decks.ask(request(since=full["token"]))
    decks.step()
    assert len(anki.actions("findNotes")) > 5  # it did read again
    assert decks.ask(request(since=full["token"])) is None


def test_a_token_too_old_or_from_elsewhere_gets_every_row(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    _, full = settle(decks, request())
    stale = full["token"]
    for i in range(server.DECK_HISTORY + 1):
        anki.notes[1]["status"] = "review" if i % 2 == 0 else "new"
        clock.t += server.DECK_TTL
        decks.ask(request(since=stale))
        decks.step()
    assert decks.ask(request(since=stale))["full"] is True
    assert decks.ask(request(since="0123abcd.1"))["full"] is True


def test_no_ask_no_read_and_an_index_nobody_wants_is_dropped(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    settle(decks, request())
    anki.calls.clear()
    clock.t += server.DECK_TTL * 2
    decks.step()
    assert anki.calls == []  # nobody asked lately: Anki is left alone
    clock.t += server.DECK_IDLE
    decks.step()
    assert decks.indexes == {}


def test_a_deck_switch_is_another_index_read_from_scratch(tmp_path):
    notes = {1: note("猫", "new"), 2: note("犬", "review", deck="Other")}
    _, anki, decks = make(notes, tmp_path)
    _, japanese = settle(decks, request())
    _, other = settle(decks, request("Other", since=japanese["token"]))
    assert other["full"] is True and other["notes"] == [[2, "犬", "", "learned", None]]
    assert other["token"].split(".")[0] != japanese["token"].split(".")[0]


def test_subdecks_and_the_keyword_names(tmp_path):
    notes = {1: note("猫", "new", deck="Japanese::Sub"), 2: note("犬", "new", deck="current")}
    _, anki, decks = make(notes, tmp_path)
    _, full = settle(decks, request())
    assert [row[0] for row in full["notes"]] == [1]
    _, current = settle(decks, request("current"))
    assert [row[0] for row in current["notes"]] == [2]
    assert any(c[1]["query"].startswith("did:4 ") for c in anki.actions("findNotes"))


def test_deck_search_escapes_what_anki_reads_in_quotes():
    assert server.deck_search('a"b*c_d\\e') == '"deck:a\\"b\\*c\\_d\\\\e"'


def test_a_new_note_expires_every_index(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    _, full = settle(decks, request())
    anki.notes[2] = note("犬", "new")
    decks.notes_added([2])
    decks.ask(request(since=full["token"]))  # seconds later, well inside DECK_TTL
    decks.step()
    delta = decks.ask(request(since=full["token"]))
    assert delta["notes"] == [[2, "犬", "", "new", None]]


def test_a_new_note_during_a_read_lands_expired(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    decks.ask(request())
    index = next(iter(decks.indexes.values()))
    real = anki.__call__

    def during(action, params, timeout):
        if action == "notesInfo":
            decks.notes_added([9])
        return real(action, params, timeout)

    index.refresh(during)
    assert index.fetched_at == 0.0 and index.status == "ok"


def test_anki_down_with_nothing_held_says_so_and_recovers(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    anki.down = True
    _, down = settle(decks, request())
    assert down["status"] == "offline" and down["error"] == "Anki is not running or AnkiConnect is not installed"
    anki.down = False
    clock.t += server.DECK_TTL
    decks.ask(request(since=down["token"]))
    decks.step()
    up = decks.ask(request(since=down["token"]))
    assert up["status"] == "ok" and up["full"] is True


def test_anki_down_with_rows_held_keeps_them(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    _, full = settle(decks, request())
    anki.down = True
    clock.t += server.DECK_TTL
    decks.ask(request(since=full["token"]))
    decks.step()
    assert decks.ask(request(since=full["token"])) is None
    assert decks.ask(request())["notes"] == full["notes"]


def test_a_refused_notesinfo_chunk_keeps_what_its_notes_said(tmp_path):
    clock, anki, decks = make({1: note("猫", "new")}, tmp_path)
    _, full = settle(decks, request())
    anki.notes[1].update(edited=True, mod=5)
    anki.refuse_info = {1}
    clock.t += server.DECK_TTL
    decks.ask(request(since=full["token"]))
    decks.step()
    assert decks.ask(request(since=full["token"])) is None
    assert next(iter(decks.indexes.values())).notes[1][0] == "猫"


# ---- the automatic deck

def test_no_deck_named_and_nothing_mined_is_no_deck(tmp_path):
    _, anki, decks = make({1: note("猫", "new")}, tmp_path)
    answer = decks.ask(request(""))
    assert answer == {"token": "nodeck", "status": "no_deck", "error": server.DECK_NO_DECK_TEXT, "automatic": True}
    assert decks.ask(request("", since="nodeck")) is None
    decks.step()
    assert anki.calls == []


def test_a_mined_note_names_the_automatic_deck_and_it_survives_a_restart(tmp_path):
    clock, anki, decks = make({7: note("猫", "new", deck="Japanese::Sub")}, tmp_path)
    decks.note_mined(7)
    decks.step()
    assert decks.seen == "Japanese::Sub"
    assert json.loads((tmp_path / "anki-deck.json").read_text())["deck"] == "Japanese::Sub"
    first, full = settle(decks, request(""))
    assert full["automatic"] is True and full["deck"] == "Japanese::Sub" and full["notes"][0][0] == 7
    again = server.DeckIndexes(anki, path=tmp_path / "anki-deck.json", clock=clock)
    assert again.seen == "Japanese::Sub"


def test_the_deck_an_old_extension_remembered_is_adopted_once_and_kept(tmp_path):
    clock, anki, decks = make({7: note("猫", "new", deck="Japanese::Sub"), 8: note("犬", "new")}, tmp_path)
    first, full = settle(decks, server.deck_request({"seen": "Japanese::Sub"}))
    assert first["status"] == "loading" and first["deck"] == "Japanese::Sub" and first["automatic"] is True
    assert [row[0] for row in full["notes"]] == [7]
    assert json.loads((tmp_path / "anki-deck.json").read_text())["deck"] == "Japanese::Sub"
    assert server.DeckIndexes(anki, path=tmp_path / "anki-deck.json", clock=clock).seen == "Japanese::Sub"
    # A server that knows its own automatic deck keeps it.
    decks.ask(server.deck_request({"seen": "Japanese"}))
    assert decks.seen == "Japanese::Sub"
    # A card mined later outranks the hint.
    decks.note_mined(8)
    decks.step()
    assert decks.seen == "Japanese"
    assert json.loads((tmp_path / "anki-deck.json").read_text())["deck"] == "Japanese"


def test_a_named_deck_still_takes_the_hint_for_later(tmp_path):
    _, _, decks = make({1: note("猫", "new")}, tmp_path)
    answer = decks.ask(server.deck_request({"name": "Japanese", "seen": "Other"}))
    assert answer["deck"] == "Japanese" and answer["automatic"] is False
    assert decks.seen == "Other"


def test_a_wake_during_a_read_is_not_lost(tmp_path):
    """A deck switch or a new note while the deck thread reads Anki must not wait out DECK_TTL."""
    _, anki, decks = make({1: note("猫", "new"), 2: note("犬", "new", deck="Other")}, tmp_path)
    decks.ask(request())
    real = anki.__call__

    def during(action, params, timeout):
        if action == "notesInfo" and not decks.indexes.get(("Other", "", "")):
            decks.ask(request("Other"))  # the client switched decks while this read ran
        return real(action, params, timeout)

    decks.call = during
    decks.step()
    # run_forever() waits only while `wake` is unset: the next turn reads the new deck at once.
    assert decks.wake is True
    decks.step()
    assert decks.ask(request("Other", since=None))["status"] == "ok"


def test_the_miner_reports_an_attached_note(tmp_path):
    reported = []
    app = type("App", (), {"decks": type("D", (), {"note_mined": lambda self, n: reported.append(n)})()})()
    miner = server.Miner(app, call=None)
    session = server.Session(video_id="abcdefghijk", url="")
    miner.outcome(session, None, "attached", "Added", 42)
    miner.outcome(session, None, "filled", "nothing", 43)
    assert reported == [42]


def test_deck_names_for_the_popup(tmp_path):
    _, anki, decks = make({}, tmp_path)
    assert decks.deck_names() == {"ok": True, "decks": ["current", "Default", "Japanese", "Japanese::Sub"], "seen": None}
    anki.down = True
    down = decks.deck_names()
    assert down["ok"] is False and down["offline"] is True


# ---- what a client may send

def test_deck_request_validation():
    assert server.deck_request({}) == {"name": "", "word": "", "pitch": "", "since": None, "seen": None}
    # A malformed hint is dropped, not refused: the colours must not fail on what 0.22.0 stored.
    for junk in (3, "", "a\nb", "x" * 201):
        assert server.deck_request({"seen": junk})["seen"] is None
    assert server.deck_request({"seen": "単語帳::Sub"})["seen"] == "単語帳::Sub"
    assert server.deck_request({"name": " 単語帳::Sub ", "word": "Word ", "since": "ab12.3"})["name"] == "単語帳::Sub"
    for bad in ([], {"name": 3}, {"name": "a\nb"}, {"name": "x" * 201}, {"word": "a:b"}, {"pitch": "<b>"},
                {"since": 5}, {"since": "a b"}, {"since": "x" * 65}):
        with pytest.raises(ValueError):
            server.deck_request(bad)


def test_word_colours_are_off_in_the_container_without_an_anki_url():
    args = type("Args", (), {"anki_url": None})()
    assert server.resolve_word_colours(args, environ={}) is True
    assert server.resolve_word_colours(args, environ={"SHISUKO_CONTAINER": "1"}) is False
    args.anki_url = "http://host.docker.internal:8765"
    assert server.resolve_word_colours(args, environ={"SHISUKO_CONTAINER": "1"}) is True


# ---- the note reading is words.js's

PIECES = ["食", "べ", "る", "た", "さら", "更", "に", "ꜜ", "ー", "・", "ｰ", "ゃ", "ア", "ッ", "😀", "𠀋", " ", "　", "﻿",
          " ", "\x1c", "\n", "\t", "<br>", "<BR/>", "</div>", "<p>", "</p>", "<li>", "</li>", "<ol>", "<ruby>", "</ruby>",
          "<rt>", "</rt>", "<rp>(", ")</rp>", "<b>", "</b>", "<", ">", "[", "]", " 食[た]べる", "お 茶[ちゃ]", "&nbsp;", "&AMP;",
          "&lt;", "&#39;", "&quot;", "heiban", "HEIBAN", "平板", "odaka", "Nakadaka", "頭高", "[0]", "[1]", "[2]", "[3]", "12", "0",
          " 3 ", "(uk)", "(adv, uk, JMdict (English))", "(UK)", "(uk-based", "usually written using kana alone",
          "Usually Written Using Kana Alone", '<span style="display:inline-block;position:relative;">',
          '<span style="display: inline-block">', '<span style="border-color:currentColor;border-top-width:2px;">',
          '<span style="border-color:currentColor;border-right-width:2px;">', "</span>", '<svg viewBox="0 0 100 100">',
          "</svg>", '<circle r="15" cx="10" cy="25" style="fill:none">', '<circle r="15" cx="10" cy="75">',
          '<circle r = "5">', '<circle r="10" fill="none">', "<circle r=12 fill=none>", '<path d="M0">', "ſ", "K", "İ", "ı",
          "x" * 50, "あ" * 30, "(" + "a" * 199 + ")", "(" + "😀" * 100 + ",uk)", "(" + "😀" * 99 + ",uk)",
          # Ruby, furigana and entity forms Yomitan and hand-made cards hold.
          "<ruby>食<rt>た</rt></ruby>べる", "<ruby>猫<rp>(</rp><rt>ねこ</rt><rp>)</rp></ruby>",
          "<ruby>漢字<rt>かん</rt><rt>じ</rt></ruby>", "<RUBY>犬<RT>いぬ</RT></RUBY>", "<ruby>未<rt>み", "食べ[た]る",
          "日本[にほん]語[ご]", "[]", "a[b", "&gt;", "&amp;nbsp;", "&#x27;", "&Nbsp;", "&amp;", "&nbsp",
          # Long runs that cross the 320, 1000 and 16,000-unit limits when two or three meet.
          "あ" * 150, "😀" * 80, "x" * 490, "y" * 5300, "😀" * 2700]
NAMES = ["Word", "word", "Expression", "Reading", "reading", "Furigana", "SentenceFurigana", "読み", "例文読み", "Pitch",
         "PitchAccentPosition", "PitchAccentGraph", "アクセント", "Glossary", "Sentence", "0", "12", "Accent"]
# words.js's limits on a word field's line, a reading field and a field's HTML, in UTF-16 units.
LIMITS = (320, 1000, 16000)
NODE_EVAL = """
const W = require(process.argv[1]);
// background.js's noteSummary() word until 0.22.0, which wc_note_word() ports with plainWord().
function noteWord(fields, settings) {
  const wordName = String(settings.ankiWordField || "").trim();
  let raw = "";
  if (wordName) {
    const key = W.fieldKey(fields, wordName);
    raw = key ? String((fields[key] && fields[key].value) || "").trim() : "";
  } else {
    for (const value of Object.values(fields)) {
      if (value && Number(value.order) === 0) { raw = String(value.value || "").trim(); break; }
    }
  }
  const word = W.plainWord(raw);
  return word.length > W.MAX_WORD_LEN ? "" : word;
}
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const out = JSON.parse(input).map((c) => {
    const settings = { ankiWordField: c.word, ankiPitchField: c.pitch };
    return [W.pitchOf(c.fields, settings), W.kanaReadingOf(c.fields, settings), noteWord(c.fields, settings),
            c.values.map((v) => W.plainWord(v)), c.values.map((v) => W.readingOf(v)), W.usuallyKana(c.fields)];
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def random_cases(rnd, n):
    cases = []
    for _ in range(n):
        names = rnd.sample(NAMES, rnd.randint(0, 6))
        orders = rnd.sample(range(len(names)), len(names))
        fields = {name: {"value": "".join(rnd.choice(PIECES) for _ in range(rnd.randint(0, 12))), "order": order}
                  for name, order in zip(names, orders)}
        cases.append({"fields": fields, "word": rnd.choice(["", "", "word", "Reading", " Expression ", "none"]),
                      "pitch": rnd.choice(["", "", "pitch", "PitchAccentGraph", "Glossary"])})
    return cases


def boundary_cases():
    """Every limit, two units either side, with a pair or markup straddling the cut: the word, the
    reading and the pitch fields each carry the value."""
    cases = []
    tails = ["る", "😀る", "<b>る</b>", "[たべ]る", "[2]"]
    for limit in LIMITS:
        for delta in range(-2, 2):
            for fill, units in (("あ", 1), ("😀", 2)):
                for tail in tails:
                    head = "a" * ((limit + delta) % units) + fill * ((limit + delta) // units)
                    for value in (head + tail, "<br>" + head + tail, "<ruby>" + head + "<rt>" + tail):
                        fields = {"Word": {"value": value, "order": 0}, "Reading": {"value": value, "order": 1},
                                  "Pitch": {"value": value, "order": 2}, "Glossary": {"value": value, "order": 3}}
                        cases.append({"fields": fields, "word": "", "pitch": ""})
    return cases


def py_reading(case):
    fields = case["fields"]
    return [server.wc_pitch_of(fields, case["word"], case["pitch"]),
            server.wc_kana_reading_of(fields, case["word"], case["pitch"]), server.wc_note_word(fields, case["word"]),
            [server.wc_plain_word(f["value"]) for f in fields.values()],
            [server.wc_reading_of(f["value"]) for f in fields.values()], server.wc_usually_kana(fields)]


# CI always has Node and must run this; elsewhere a missing Node skips it.
@pytest.mark.skipif(shutil.which("node") is None and not os.environ.get("CI"), reason="needs Node to run words.js")
def test_the_note_reading_matches_words_js_over_random_and_boundary_fields():
    cases = random_cases(random.Random(20261019), 4000) + boundary_cases()
    for case in cases:
        case["values"] = [f["value"] for f in case["fields"].values()]
    done = subprocess.run(["node", "-e", NODE_EVAL, str(WORDS_JS)], input=json.dumps(cases), capture_output=True,
                          text=True, encoding="utf-8", check=True)
    # u16slice() leaves out a pair the cut would split, where JavaScript keeps its first half.
    lone = re.compile(r"\\ud[89a-f][0-9a-f]{2}")
    mismatches = [case for case, js in zip(cases, json.loads(done.stdout))
                  if lone.sub("", json.dumps(py_reading(case))) != lone.sub("", json.dumps(js))]
    assert not mismatches, json.dumps(mismatches[0], ensure_ascii=False)[:2000]

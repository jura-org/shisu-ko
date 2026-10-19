"use strict";

// The word colours' golden test, the extension's half. The fixture's invented notes go through the
// deck path the extension ran until 0.22.0 (oldDeckEntries(), its background page's note reading
// and entry loop, kept here as it was) and must give the fixture's entries, which
// server/tests/test_deck_golden.py holds the server's DeckIndex to. Same entries, same
// buildIndex(), same marks. UPDATE_GOLDEN=1 writes the entries anew from the old path.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const W = require("../words.js");
const { loadBackground } = require("./_loadBackground.js");

const FIXTURE_PATH = path.join(__dirname, "fixtures", "deck-golden.json");
const fixture = JSON.parse(fs.readFileSync(FIXTURE_PATH, "utf8"));

// background.js's noteSummary() word, as it was.
function noteWord(info, settings) {
  const fields = (info && info.fields) || {};
  const wordName = String(settings.ankiWordField || "").trim();
  if (wordName) {
    const key = W.fieldKey(fields, wordName);
    return key ? String((fields[key] && fields[key].value) || "").trim() : "";
  }
  for (const value of Object.values(fields)) {
    if (value && Number(value.order) === 0) return String(value.value || "").trim();
  }
  return "";
}

// The five searches as Anki answers them for a note's cards.
const STATUS_SETS = {
  suspended: ["suspended"],
  unsuspended: ["new", "learning", "review"],
  new: ["new"],
  learning: ["learning"],
  review: ["review"],
};

// background.js's fetchDeckIndex() entry loop over one deck and its subdecks, as it was.
function oldDeckEntries(notes, deck, settings) {
  const inDeck = notes.filter((n) => n.deck === deck || n.deck.startsWith(deck + "::"));
  const sets = {};
  for (const [name, statuses] of Object.entries(STATUS_SETS)) sets[name] = new Set(inDeck.filter((n) => statuses.includes(n.status)).map((n) => n.noteId));
  const ids = [...new Set([...sets.suspended, ...sets.unsuspended])].sort((a, b) => a - b);
  const byId = new Map(inDeck.map((n) => [n.noteId, n]));
  const entries = [];
  for (const id of ids) {
    const info = byId.get(id);
    const word = W.plainWord(noteWord(info, settings));
    const note = {
      word: word.length > W.MAX_WORD_LEN ? "" : word,
      reading: W.kanaReadingOf(info.fields, settings),
      pitch: W.pitchOf(info.fields, settings),
    };
    if (!note.word) continue;
    const status = W.statusOf(sets, id);
    if (!status) continue;
    entries.push([note.word, status, note.pitch]);
    if (note.reading) entries.push([note.reading, status, note.pitch]);
  }
  return entries;
}

if (process.env.UPDATE_GOLDEN) {
  for (const c of fixture.cases) c.entries = oldDeckEntries(fixture.notes, fixture.deck, c.settings);
  fs.writeFileSync(FIXTURE_PATH, JSON.stringify(fixture, null, 1) + "\n");
}

test("the old deck path reads the fixture into the golden entries", () => {
  for (const c of fixture.cases) {
    assert.deepEqual(oldDeckEntries(fixture.notes, fixture.deck, c.settings), c.entries, JSON.stringify(c.settings));
  }
});

test("the golden entries exercise every status, pitch and the kana reading", () => {
  const all = fixture.cases.flatMap((c) => c.entries);
  for (const status of ["new", "learning", "learned", "suspended"]) assert.ok(all.some((e) => e[1] === status), status);
  for (const pitch of [...W.PITCHES, null]) assert.ok(all.some((e) => e[2] === pitch), String(pitch));
  assert.ok(all.some((e) => e[0] === "さらに"), "a word usually written in kana has its reading");
});

// A fake AnkiConnect over the fixture, for the background page's own fetchDeckIndex().
function fixtureAnki() {
  return async (url, init) => {
    const { action, params } = JSON.parse(init.body);
    let result;
    if (action === "requestPermission") result = { permission: "granted" };
    else if (action === "findNotes") {
      const [, deck, clause] = /^"deck:(.*)" (.*)$/.exec(params.query);
      const wanted = { "is:suspended": ["suspended"], "-is:suspended": ["new", "learning", "review"], "is:new -is:suspended": ["new"], "is:learn -is:suspended": ["learning"], "is:review -is:learn -is:suspended": ["review"] }[clause] || [];
      result = fixture.notes.filter((n) => (n.deck === deck || n.deck.startsWith(deck + "::")) && wanted.includes(n.status)).map((n) => n.noteId).reverse();
    } else if (action === "notesInfo") {
      result = params.notes.map((id) => {
        const n = fixture.notes.find((x) => x.noteId === id);
        return { noteId: id, fields: n.fields, mod: n.mod };
      });
    } else throw new Error(action);
    return { json: async () => ({ result, error: null }) };
  };
}

test("the fixture's old path is what the background page's fetchDeckIndex() answered", async () => {
  for (const c of fixture.cases) {
    const bg = loadBackground({ fetch: fixtureAnki() });
    const index = await bg.sandbox.fetchDeckIndex("http://127.0.0.1:8765", fixture.deck, c.settings);
    assert.deepEqual(JSON.parse(JSON.stringify(index.entries)), c.entries, JSON.stringify(c.settings));
  }
});

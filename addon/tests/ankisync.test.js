"use strict";

// The Anki sync after mining (background.js, "Anki sync"): the scheduler's pure steps on a fake
// clock, then the wiring, with a fake AnkiConnect, the alarms the loader records and a clock the
// test moves.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadBackground, makeMemoryStorage } = require("./_loadBackground");

const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise((resolve) => setImmediate(resolve));
};
const SECOND = 1000;

// The scheduler alone. Cards arrive at the given times; a sync takes `duration`; `fail(n)` is
// the failure of sync number n (0-based), null for a sync that went through. Events at one
// instant run in this order: a sync ending, a card, a sync starting. Returns the start times.
function simulate(sb, { cards, duration = 2 * SECOND, until = 3600 * SECOND, fail = () => null }) {
  const timing = { firstMs: sb.ANKI_SYNC_FIRST_MS, repeatMs: sb.ANKI_SYNC_REPEAT_MS, backoffMs: sb.ANKI_SYNC_BACKOFF_MS };
  const queue = [...cards].sort((a, b) => a - b);
  let state = sb.ankiSyncIdle();
  let endAt = null;
  const starts = [];
  for (;;) {
    const card = queue.length ? queue[0] : Infinity;
    const due = !state.running && state.due !== null ? state.due : Infinity;
    const end = endAt === null ? Infinity : endAt;
    const t = Math.min(card, due, end);
    if (t === Infinity || t > until) break;
    if (t === end) {
      state = sb.ankiSyncEnd(state, t, fail(starts.length - 1), timing);
      endAt = null;
    } else if (t === card) {
      queue.shift();
      state = sb.ankiSyncCard(state, t, timing);
    } else {
      assert.equal(endAt, null, "a sync must never start while one runs");
      state = sb.ankiSyncStart(state, t);
      starts.push(t);
      endAt = t + duration;
    }
  }
  return { starts: starts.map((t) => t / SECOND), state };
}

const { sandbox: sb } = loadBackground();

test("the delays are named: a minute for the first card, 30 s between syncs, backoff capped at 5 min", () => {
  assert.equal(sb.ANKI_SYNC_FIRST_MS, 60 * SECOND);
  assert.equal(sb.ANKI_SYNC_REPEAT_MS, 30 * SECOND);
  assert.deepEqual(plain(sb.ANKI_SYNC_BACKOFF_MS), [60 * SECOND, 120 * SECOND, 300 * SECOND]);
});

test("one card: one sync, a minute later", () => {
  assert.deepEqual(simulate(sb, { cards: [0] }).starts, [60]);
});

test("cards in the first minute ride along and do not push the sync back", () => {
  assert.deepEqual(simulate(sb, { cards: [0, 20 * SECOND, 59 * SECOND] }).starts, [60]);
});

test("a card every 10 s for 3 minutes syncs at 60, 90, 120 ... while they come, then stops", () => {
  const cards = Array.from({ length: 18 }, (_, i) => i * 10 * SECOND); // 0 .. 170 s
  const { starts, state } = simulate(sb, { cards });
  assert.deepEqual(starts, [60, 90, 120, 150, 180]);
  assert.equal(state.due, null);
  assert.equal(state.cards, 0);
});

test("a card made during a sync gets a follow-up 30 s after that sync began", () => {
  assert.deepEqual(simulate(sb, { cards: [0, 62 * SECOND], duration: 5 * SECOND }).starts, [60, 90]);
});

test("a card more than 30 s after the last sync began waits its minute again", () => {
  assert.deepEqual(simulate(sb, { cards: [0, 100 * SECOND] }).starts, [60, 160]);
});

test("syncs never overlap: one slower than 30 s is followed at once, not during it", () => {
  const { starts } = simulate(sb, { cards: [0, 61 * SECOND, 70 * SECOND], duration: 45 * SECOND });
  assert.deepEqual(starts, [60, 105]);
});

test("a failed sync is retried after 1, 2, 5, 5 minutes while its cards are owed", () => {
  const { starts, state } = simulate(sb, { cards: [0], duration: 0, until: 1200 * SECOND, fail: () => ({ text: "Anki is not running", halt: false }) });
  assert.deepEqual(starts, [60, 120, 240, 540, 840, 1140]);
  assert.equal(state.cards, 1, "the card is still owed");
});

test("a retry that goes through ends the round and resets the backoff", () => {
  const fail = (n) => (n < 2 ? { text: "Anki is not running", halt: false } : null);
  const { starts, state } = simulate(sb, { cards: [0, 1000 * SECOND], duration: 0, fail });
  assert.deepEqual(starts, [60, 120, 240, 1060]);
  assert.equal(state.failures, 0);
  assert.equal(state.cards, 0);
});

test("not signed in to AnkiWeb stops the retries until the next card", () => {
  const halt = { text: "not signed in to AnkiWeb", halt: true };
  const { starts, state } = simulate(sb, { cards: [0], duration: 0, fail: () => halt });
  assert.deepEqual(starts, [60]);
  assert.equal(state.halted, true);
  assert.equal(state.due, null);
  const again = simulate(sb, { cards: [0, 500 * SECOND], duration: 0, fail: (n) => (n === 0 ? halt : null) });
  assert.deepEqual(again.starts, [60, 560]);
});

test("AnkiConnect's errors sort into retry and stop", () => {
  const network = new TypeError("NetworkError when attempting to fetch resource.");
  assert.deepEqual(plain(sb.ankiSyncFailure(network)), { text: "Anki is not running", halt: false });
  const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
  assert.deepEqual(plain(sb.ankiSyncFailure(abort)), { text: "Anki did not answer in time", halt: false });
  assert.deepEqual(plain(sb.ankiSyncFailure(new Error("sync: auth not configured"))), { text: "not signed in to AnkiWeb", halt: true });
  assert.equal(sb.ankiSyncFailure(new Error("Sync status 2 not one of [0, 1] - see SyncCollectionResponse")).halt, true);
  assert.equal(sb.ankiSyncFailure(new Error("collection is not available")).halt, false);
});

test("off clears what is planned and owed, and keeps the outcome the popup shows", () => {
  const timing = { firstMs: 60, repeatMs: 30, backoffMs: [60] };
  const planned = sb.ankiSyncCard({ ...sb.ankiSyncIdle(), last: { at: 5, ok: true } }, 0, timing);
  const off = sb.ankiSyncOff(planned);
  assert.equal(off.due, null);
  assert.equal(off.cards, 0);
  assert.deepEqual(plain(off.last), { at: 5, ok: true });
  assert.equal(sb.ankiSyncOn({ enabled: true, ankiAutoSync: true }), true);
  assert.equal(sb.ankiSyncOn({ enabled: false, ankiAutoSync: true }), false);
  assert.equal(sb.ankiSyncOn({ enabled: true, ankiAutoSync: false }), false);
});

test("a record left running by a stopped event page owes its cards, due now", () => {
  const record = { ...sb.ankiSyncIdle(), running: true, startedAt: 10, inFlight: 2, cards: 1 };
  const resumed = sb.ankiSyncResume(record, 500);
  assert.equal(resumed.running, false);
  assert.equal(resumed.cards, 3);
  assert.equal(resumed.due, 500);
});

// ------------------------------------------------------------------ the wiring

const T0 = Date.UTC(2026, 0, 1, 12, 0, 0);

const fieldsOf = (sentence) => ({
  Sentence: { value: sentence, order: 0 },
  Picture: { value: "", order: 1 },
  SentenceAudio: { value: "", order: 2 },
});

// The Whisper server's /clip and a fake AnkiConnect; `sync` answers what the test says.
function fakeServices(syncAnswer) {
  const calls = [];
  const fetch = async (url, init) => {
    if (String(url).includes("/clip")) {
      return { ok: true, status: 200, headers: { get: () => "audio/mpeg" }, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer };
    }
    const req = JSON.parse(init.body);
    calls.push({ action: req.action, headers: init.headers });
    const answers = {
      requestPermission: () => ({ permission: "granted" }),
      notesInfo: () => [{ noteId: 101, fields: fieldsOf("テストの文です") }],
      storeMediaFile: () => "stored.jpg",
      updateNoteFields: () => null,
      findCards: () => [],
      getDecks: () => ({}),
      sync: () => (typeof syncAnswer === "function" ? syncAnswer() : null),
    };
    if (!answers[req.action]) throw new Error("unexpected AnkiConnect action " + req.action);
    try {
      const result = await answers[req.action]();
      return { json: async () => ({ result, error: null }) };
    } catch (err) {
      return { json: async () => ({ result: null, error: err.message }) };
    }
  };
  return { fetch, calls, syncs: () => calls.filter((c) => c.action === "sync").length };
}

// Timers recorded, never run: the test fires the alarm itself.
const heldTimers = { setTimeout: () => 0 };

const mineMsg = { type: "mine", videoId: "abc123abc123", cue: { start: 0, end: 2, text: "テストの文です" }, noteId: 101, auto: true,
  imageDataUrl: "data:image/jpeg;base64,AAAA" };

async function mined(opts = {}) {
  const services = fakeServices(opts.sync);
  const storage = makeMemoryStorage(opts.settings ? { settings: opts.settings } : {});
  const bg = loadBackground({ fetch: services.fetch, storage, ...heldTimers });
  bg.setNow(T0);
  const res = await bg.dispatch(mineMsg, 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  await settle();
  return { bg, services };
}

const alarmAt = (bg) => (bg.alarms.get(bg.sandbox.ANKI_SYNC_ALARM) || {}).when;

test("a completed mine schedules a sync a minute later, and the alarm runs it", async () => {
  const { bg, services } = await mined();
  assert.equal(alarmAt(bg), T0 + 60 * SECOND);
  assert.equal(services.syncs(), 0, "no sync before the minute");
  bg.setNow(T0 + 60 * SECOND);
  await bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  await settle();
  assert.equal(services.syncs(), 1);
  const sync = services.calls.find((c) => c.action === "sync");
  assert.equal(sync.headers, undefined, "AnkiConnect requests carry no Content-Type header");
  const status = await bg.dispatch({ type: "ankiSyncStatus" });
  assert.equal(status.on, true);
  assert.deepEqual(plain(status.last), { at: T0 + 60 * SECOND, ok: true });
  assert.equal(alarmAt(bg), undefined, "nothing new: the round is over");
});

test("wake-ups during a sync start no second one, and a card made during it gets the follow-up", async () => {
  let release;
  const { bg, services } = await mined({ sync: () => new Promise((resolve) => (release = () => resolve(null))) });
  bg.setNow(T0 + 60 * SECOND);
  bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  await settle();
  assert.equal(services.syncs(), 1);
  assert.equal((await bg.dispatch({ type: "ankiSyncStatus" })).running, true);
  bg.setNow(T0 + 65 * SECOND);
  assert.equal((await bg.dispatch(mineMsg, 1)).ok, true);
  await settle();
  bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  bg.sandbox.runAnkiSync();
  await settle();
  assert.equal(services.syncs(), 1, "one sync at a time");
  bg.setNow(T0 + 70 * SECOND);
  release();
  await settle();
  assert.equal(alarmAt(bg), T0 + 90 * SECOND, "the card made during the sync goes 30 s after it began");
  bg.setNow(T0 + 90 * SECOND);
  await bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  await settle();
  assert.equal(services.syncs(), 2);
  release();
  await settle();
  assert.equal(alarmAt(bg), undefined);
});

test("a page restarted with a sync owed asks AnkiConnect for permission first, then syncs", async () => {
  const services = fakeServices();
  const session = makeMemoryStorage({ ankiSync: { due: T0, running: false, startedAt: null, cards: 1, inFlight: 0, failures: 0, halted: false, last: null } });
  const bg = loadBackground({ fetch: services.fetch, session, ...heldTimers });
  bg.setNow(T0 + SECOND);
  await bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  await settle();
  assert.deepEqual(services.calls.map((c) => c.action), ["requestPermission", "sync"]);
});

test("not signed in to AnkiWeb: one try, the reason for the popup, no retry planned", async () => {
  const { bg, services } = await mined({ sync: () => { throw new Error("sync: auth not configured"); } });
  bg.setNow(T0 + 60 * SECOND);
  await bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  await settle();
  assert.equal(services.syncs(), 1);
  const status = await bg.dispatch({ type: "ankiSyncStatus" });
  assert.equal(status.halted, true);
  assert.deepEqual(plain(status.last), { at: T0 + 60 * SECOND, ok: false, error: "not signed in to AnkiWeb" });
  assert.equal(alarmAt(bg), undefined);
});

test("Anki closed: the sync is planned again a minute on", async () => {
  const { bg } = await mined({ sync: () => { throw new Error("collection is not available"); } });
  bg.setNow(T0 + 60 * SECOND);
  await bg.fireAlarm(bg.sandbox.ANKI_SYNC_ALARM);
  await settle();
  assert.equal(alarmAt(bg), T0 + 120 * SECOND);
});

test("the master switch off cancels the planned sync, and a mine while off plans none", async () => {
  const { bg, services } = await mined();
  assert.equal(alarmAt(bg), T0 + 60 * SECOND);
  await bg.dispatch({ type: "saveSettings", settings: { enabled: false } });
  await settle();
  assert.equal(alarmAt(bg), undefined, "the alarm is cleared");
  bg.setNow(T0 + 60 * SECOND);
  await bg.sandbox.runAnkiSync();
  await settle();
  assert.equal(services.syncs(), 0);
  assert.equal((await bg.dispatch({ type: "ankiSyncStatus" })).on, false);

  const off = await mined({ settings: { enabled: false } });
  assert.equal(alarmAt(off.bg), undefined);
});

test("the setting off: a mine plans no sync", async () => {
  const { bg, services } = await mined({ settings: { ankiAutoSync: false } });
  assert.equal(alarmAt(bg), undefined);
  bg.setNow(T0 + 120 * SECOND);
  await bg.sandbox.runAnkiSync();
  assert.equal(services.syncs(), 0);
});

test("the setting is on by default", () => {
  assert.equal(sb.DEFAULT_SETTINGS.ankiAutoSync, true);
});

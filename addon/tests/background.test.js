"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadBackground, makeMemoryStorage } = require("./_loadBackground");

// Results returned from the vm sandbox are plain objects from a *different* realm, so
// assert.deepEqual's reference-equality check on prototypes rejects them even when every field
// matches. Round-tripping through JSON compares the same objects in this realm instead.
const plain = (value) => JSON.parse(JSON.stringify(value));

// ------------------------------------------------------------------ normalizeBase

test("normalizeBase strips trailing slashes from a valid http(s) URL", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.normalizeBase("http://127.0.0.1:8790///", "fallback"), "http://127.0.0.1:8790");
  assert.equal(sandbox.normalizeBase("https://example.com/", "fallback"), "https://example.com");
});

test("normalizeBase falls back for missing or non-http(s) values", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.normalizeBase("", "http://127.0.0.1:8790"), "http://127.0.0.1:8790");
  assert.equal(sandbox.normalizeBase(undefined, "http://127.0.0.1:8790"), "http://127.0.0.1:8790");
  assert.equal(sandbox.normalizeBase("javascript:alert(1)", "http://127.0.0.1:8790"), "http://127.0.0.1:8790");
  assert.equal(sandbox.normalizeBase("ftp://example.com", "http://127.0.0.1:8790"), "http://127.0.0.1:8790");
});

// ------------------------------------------------------------------ bytesToBase64

test("bytesToBase64 matches Node's own base64 encoding", () => {
  const { sandbox } = loadBackground();
  const bytes = new Uint8Array([0, 1, 2, 255, 254, 253, 65, 66, 67]);
  const expected = Buffer.from(bytes).toString("base64");
  assert.equal(sandbox.bytesToBase64(bytes.buffer), expected);
});

test("bytesToBase64 handles buffers larger than its 0x8000 chunk size", () => {
  const { sandbox } = loadBackground();
  const bytes = new Uint8Array(0x8000 * 2 + 137);
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256;
  const expected = Buffer.from(bytes).toString("base64");
  assert.equal(sandbox.bytesToBase64(bytes.buffer), expected);
});

// ------------------------------------------------------------------ settings

test("getSettings merges stored values over the defaults", async () => {
  const storage = makeMemoryStorage({ settings: { fontScale: 1.5, mineTarget: "download" } });
  const { sandbox } = loadBackground({ storage });
  const settings = await sandbox.getSettings();
  assert.equal(settings.fontScale, 1.5);
  assert.equal(settings.mineTarget, "download");
  assert.equal(settings.serverUrl, sandbox.DEFAULT_SETTINGS.serverUrl); // untouched default
});

test("saveSettings patches on top of the current settings and persists them", async () => {
  const storage = makeMemoryStorage();
  const { sandbox } = loadBackground({ storage });
  const next = await sandbox.saveSettings({ fontScale: 2 });
  assert.equal(next.fontScale, 2);
  const again = await sandbox.getSettings();
  assert.equal(again.fontScale, 2);
  assert.equal(again.serverUrl, sandbox.DEFAULT_SETTINGS.serverUrl);
});

// The popup's flush and the content script's Alt+Shift+S can land a millisecond apart. A save
// reads the settings, merges its patch and writes; the second, reading during the first's write,
// used to merge onto the settings from before it and write the first patch away.
test("two saves in flight at once both keep their patch", async () => {
  const base = makeMemoryStorage({ settings: { fontScale: 1, enabled: true } });
  // The cross-process write takes its time; the second save arrives inside it.
  const storage = { get: (key) => base.get(key), set: async (patch) => { await new Promise((r) => setTimeout(r, 5)); await base.set(patch); } };
  const bg = loadBackground({ storage });
  const first = bg.dispatch({ type: "saveSettings", settings: { fontScale: 2 } });
  await new Promise((r) => setTimeout(r, 1));
  const second = bg.dispatch({ type: "saveSettings", settings: { enabled: false } }, 7);
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.fontScale, 2);
  assert.equal(a.enabled, true, "the first save answers with what it wrote");
  assert.equal(b.fontScale, 2, "the second save merges onto the first, not onto what it overwrote");
  assert.equal(b.enabled, false);
  const stored = (await base.get("settings")).settings;
  assert.equal(stored.fontScale, 2);
  assert.equal(stored.enabled, false);
  assert.equal((await bg.sandbox.getSettings()).fontScale, 2);
  // A write that fails does not take the saves after it down with it.
  storage.set = async () => {
    throw new Error("quota");
  };
  await assert.rejects(bg.dispatch({ type: "saveSettings", settings: { fontScale: 3 } }), /quota/);
  storage.set = (patch) => base.set(patch);
  assert.equal((await bg.dispatch({ type: "saveSettings", settings: { fontScale: 4 } })).fontScale, 4);
});

test("getSettings reads storage once and serves the rest from memory", async () => {
  let gets = 0;
  const base = makeMemoryStorage({ settings: { fontScale: 1.5 } });
  const storage = { get: (key) => (gets++, base.get(key)), set: (patch) => base.set(patch) };
  const { sandbox } = loadBackground({ storage });
  for (let i = 0; i < 20; i++) await sandbox.getSettings();
  assert.equal(gets, 1);
  assert.equal((await sandbox.getSettings()).fontScale, 1.5);
});

test("a settings change in storage drops the cache", async () => {
  let gets = 0;
  const base = makeMemoryStorage({ settings: { fontScale: 1.5 } });
  const storage = { get: (key) => (gets++, base.get(key)), set: (patch) => base.set(patch) };
  const { sandbox, listeners } = loadBackground({ storage });
  assert.equal((await sandbox.getSettings()).fontScale, 1.5);
  assert.equal(gets, 1);
  // What the popup does: write the new value, then let storage.onChanged tell everyone.
  await base.set({ settings: { fontScale: 3 } });
  for (const fn of listeners.onChanged) fn({ settings: { newValue: { fontScale: 3 } } }, "local");
  assert.equal((await sandbox.getSettings()).fontScale, 3);
  assert.equal(gets, 2);
});

test("a change to something other than the settings leaves the cache alone", async () => {
  let gets = 0;
  const base = makeMemoryStorage({ settings: { fontScale: 1.5 } });
  const storage = { get: (key) => (gets++, base.get(key)), set: (patch) => base.set(patch) };
  const { sandbox, listeners } = loadBackground({ storage });
  await sandbox.getSettings();
  for (const fn of listeners.onChanged) fn({ somethingElse: { newValue: 1 } }, "local");
  for (const fn of listeners.onChanged) fn({ settings: { newValue: {} } }, "sync"); // another area
  await sandbox.getSettings();
  assert.equal(gets, 1);
});

test("saveSettings makes the new value readable without another storage read", async () => {
  let gets = 0;
  const base = makeMemoryStorage();
  const storage = { get: (key) => (gets++, base.get(key)), set: (patch) => base.set(patch) };
  const { sandbox } = loadBackground({ storage });
  await sandbox.saveSettings({ fontScale: 2 });
  const getsAfterSave = gets;
  assert.equal((await sandbox.getSettings()).fontScale, 2);
  assert.equal(gets, getsAfterSave);
});

// ------------------------------------------------------------------ apiRequest

test("apiRequest rejects paths that do not start with /", async () => {
  const { sandbox } = loadBackground();
  const res = await sandbox.apiRequest("health", undefined);
  assert.deepEqual(plain(res), { ok: false, error: "Invalid API path" });
});

test("apiRequest sends GET for no body and returns parsed JSON on success", async () => {
  let seenUrl, seenInit;
  const fetch = async (url, init) => {
    seenUrl = url;
    seenInit = init;
    return { ok: true, text: async () => JSON.stringify({ hello: "world" }) };
  };
  const { sandbox } = loadBackground({ fetch });
  const res = await sandbox.apiRequest("/health", undefined);
  assert.equal(seenUrl, sandbox.DEFAULT_SETTINGS.serverUrl + "/health");
  assert.equal(seenInit.method, "GET");
  assert.deepEqual(plain(res), { ok: true, data: { hello: "world" } });
});

test("apiRequest sends POST with a JSON body when a body is given", async () => {
  let seenInit;
  const fetch = async (_url, init) => {
    seenInit = init;
    return { ok: true, text: async () => "{}" };
  };
  const { sandbox } = loadBackground({ fetch });
  await sandbox.apiRequest("/sync", { video_id: "abc" });
  assert.equal(seenInit.method, "POST");
  assert.equal(seenInit.headers["Content-Type"], "application/json");
  assert.equal(seenInit.body, JSON.stringify({ video_id: "abc" }));
});

test("apiRequest reports a friendly error for a non-JSON response", async () => {
  const fetch = async () => ({ ok: true, text: async () => "<html>not json</html>" });
  const { sandbox } = loadBackground({ fetch });
  const res = await sandbox.apiRequest("/health", undefined);
  assert.deepEqual(plain(res), { ok: false, error: "Server returned a non-JSON response" });
});

test("apiRequest surfaces the server's error field on a non-ok response", async () => {
  const fetch = async () => ({ ok: false, status: 503, text: async () => JSON.stringify({ error: "busy" }) });
  const { sandbox } = loadBackground({ fetch });
  const res = await sandbox.apiRequest("/sync", {});
  assert.equal(res.ok, false);
  assert.equal(res.error, "busy");
});

test("apiRequest reports the server as unreachable when fetch throws", async () => {
  const fetch = async () => {
    throw new TypeError("NetworkError");
  };
  const { sandbox } = loadBackground({ fetch });
  const res = await sandbox.apiRequest("/health", undefined);
  assert.deepEqual(plain(res), { ok: false, offline: true, error: "Server unreachable" });
});

// ------------------------------------------------------------------ mineCue

const instantTimers = { setTimeout: (fn) => { fn(); return 0; } };

test("mineCue reports an error when the message carries no cue", async () => {
  const { sandbox } = loadBackground();
  const res = await sandbox.mineCue({ videoId: "abc123abc123" });
  assert.equal(res.ok, false);
  assert.match(res.error, /no subtitle/i);
});

test("mineCue saves screenshot and audio clip to Downloads when the target is 'download'", async () => {
  const storage = makeMemoryStorage({ settings: { mineTarget: "download" } });
  const fetch = async () => ({
    ok: true,
    status: 200,
    headers: { get: () => "audio/mpeg" },
    arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
  });
  const downloaded = [];
  const download = async (opts) => {
    downloaded.push(opts.filename);
    return {};
  };
  const { sandbox } = loadBackground({ storage, fetch, download, ...instantTimers });

  const res = await sandbox.mineCue({
    videoId: "abc123abc123",
    cue: { start: 10, end: 12 },
    imageDataUrl: "data:image/jpeg;base64,Zm9v",
  });

  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(downloaded.length, 2);
  assert.ok(downloaded.every((name) => name.startsWith("shisu-ko-mining/shisuko_abc123abc123_")));
});

test("mineCue retries the clip endpoint while it returns 503, then succeeds", async () => {
  let calls = 0;
  const fetch = async () => {
    calls += 1;
    if (calls < 3) return { status: 503, ok: false };
    return {
      ok: true,
      status: 200,
      headers: { get: () => "audio/mpeg" },
      arrayBuffer: async () => new Uint8Array([9]).buffer,
    };
  };
  const storage = makeMemoryStorage({ settings: { mineTarget: "download" } });
  const download = async () => ({});
  const { sandbox } = loadBackground({ storage, fetch, download, ...instantTimers });

  const res = await sandbox.mineCue({ videoId: "abc123abc123", cue: { start: 0, end: 1 } });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(calls, 3);
});

test("mineCue falls back to Downloads when AnkiConnect is unreachable and mineFallbackDownload is on", async () => {
  const storage = makeMemoryStorage({ settings: { mineTarget: "anki", mineFallbackDownload: true } });
  const fetch = async (url) => {
    if (String(url).includes("/clip")) {
      return {
        ok: true,
        status: 200,
        headers: { get: () => "audio/mpeg" },
        arrayBuffer: async () => new Uint8Array([1]).buffer,
      };
    }
    // AnkiConnect endpoint: simulate "Anki not running" (a fetch-level TypeError).
    throw new TypeError("fetch failed");
  };
  const downloaded = [];
  const download = async (opts) => {
    downloaded.push(opts.filename);
    return {};
  };
  const { sandbox } = loadBackground({ storage, fetch, download, ...instantTimers });

  const res = await sandbox.mineCue({ videoId: "abc123abc123", cue: { start: 0, end: 1 } });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.warning, true);
  // The outcome first: a toast is cut at 240 characters, and the Anki error after it may run long.
  assert.match(res.message, /^Saved to Downloads instead\. Anki: /);
  assert.equal(downloaded.length, 1);
});

// ------------------------------------------------------------------ AnkiConnect watcher

// AnkiConnect speaks one POST per action; the mock dispatches on the action name and records calls.
function ankiFetch(handlers) {
  const calls = [];
  const fetch = async (_url, init) => {
    const req = JSON.parse(init.body);
    calls.push({ action: req.action, params: req.params });
    const handler = handlers[req.action];
    if (handler === undefined) throw new Error("unexpected AnkiConnect action " + req.action);
    const result = typeof handler === "function" ? handler(req.params) : handler;
    return { json: async () => ({ result, error: null }) };
  };
  return { fetch, calls, actions: () => calls.map((c) => c.action) };
}

const granted = { permission: "granted" };

// Put the previous poll far enough back to clear the throttle but inside the 10 s window
// that keeps the baseline trustworthy.
function allowNextPoll(sandbox) {
  sandbox.ankiWatch.lastPollAt = Date.now() - 1000;
}

test("ankiPoll takes a baseline on the first poll and reports nothing", async () => {
  const anki = ankiFetch({ requestPermission: granted, findNotes: [100, 101] });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
  assert.equal(sandbox.ankiWatch.baseline, 101);
});

test("ankiPoll reports a single note added after the baseline", async () => {
  let ids = [100, 101];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  await sandbox.ankiPoll();
  ids = [100, 101, 102];
  allowNextPoll(sandbox);
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: 102, note: { sentence: "これは<b>猫</b>です。", word: "猫" } });
  // The note is reported once; a poll that finds nothing newer stays quiet.
  allowNextPoll(sandbox);
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
});

// What the content script matches a card against: the two fields that say what it is about. The
// word comes from the first field unless the viewer named one, because that is where every
// Yomitan template puts the expression.
const YOMITAN_NOTE = [
  {
    fields: {
      Expression: { value: "猫", order: 0 },
      Sentence: { value: "これは<b>猫</b>です。", order: 1 },
      Reading: { value: "ねこ", order: 2 },
    },
  },
];

test("ankiPoll reads the new card's sentence and its first field", async () => {
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  await sandbox.ankiPoll();
  ids = [100, 101];
  allowNextPoll(sandbox);
  const res = await sandbox.ankiPoll();
  assert.deepEqual(plain(res.note), { sentence: "これは<b>猫</b>です。", word: "猫" });
  const asked = anki.calls.find((c) => c.action === "notesInfo");
  assert.deepEqual(plain(asked.params.notes), [101]);
});

test("ankiPoll takes the word from the field the viewer named", async () => {
  const storage = makeMemoryStorage({ settings: { ankiWordField: "Reading", ankiSentenceField: "Sentence" } });
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const { sandbox } = loadBackground({ storage, fetch: anki.fetch });
  await sandbox.ankiPoll();
  ids = [100, 101];
  allowNextPoll(sandbox);
  assert.deepEqual(plain((await sandbox.ankiPoll()).note), { sentence: "これは<b>猫</b>です。", word: "ねこ" });
});

test("ankiPoll still reports the card when its fields cannot be read", async () => {
  let ids = [100];
  const anki = ankiFetch({
    requestPermission: granted,
    findNotes: () => ids,
    notesInfo: () => {
      throw new Error("Anki went away");
    },
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  await sandbox.ankiPoll();
  ids = [100, 101];
  allowNextPoll(sandbox);
  // The baseline has moved on all the same: this card is never offered twice.
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: 101, note: null });
  assert.equal(sandbox.ankiWatch.baseline, 101);
});

test("ankiPoll ignores a batch of several new notes", async () => {
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  await sandbox.ankiPoll();
  ids = [100, 101, 102];
  allowNextPoll(sandbox);
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
  assert.equal(sandbox.ankiWatch.baseline, 102);
});

test("ankiPoll throttles polls closer together than the throttle interval", async () => {
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  await sandbox.ankiPoll();
  const before = anki.calls.length;
  ids = [100, 101];
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
  assert.equal(anki.calls.length, before, "the throttled poll must not talk to AnkiConnect");
});

test("ankiPoll re-baselines after a failed poll instead of reporting a stale note", async () => {
  let ids = [100];
  let broken = false;
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids });
  const fetch = async (url, init) => {
    if (broken) throw new TypeError("fetch failed");
    return anki.fetch(url, init);
  };
  const { sandbox } = loadBackground({ fetch });
  await sandbox.ankiPoll();

  broken = true;
  allowNextPoll(sandbox);
  const failed = await sandbox.ankiPoll();
  assert.equal(failed.ok, false);
  assert.equal(failed.offline, true);

  // Notes added while Anki was unreachable must not be attached to.
  broken = false;
  ids = [100, 101];
  allowNextPoll(sandbox);
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
  ids = [100, 101, 102];
  allowNextPoll(sandbox);
  assert.equal((await sandbox.ankiPoll()).newNoteId, 102);
});

// A video is playing, the viewer starts Anki a few seconds later and makes a card: the poll that
// found no Anki must not count as a permission verdict, or the minute's wait for a viewer who
// clicked No would keep the watcher off the network while the card is made. Nor may the very
// next poll ask again: autoMine is on by default, so with Anki closed that is a refused
// connection a second, per tab, for the whole session. A few seconds between attempts.
test("ankiPoll asks again a few seconds after a poll found no Anki, not on the next poll", async () => {
  let up = false;
  let ids = [100, 101];
  let refused = 0;
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const fetch = async (url, init) => {
    if (!up) {
      refused++;
      throw new TypeError("NetworkError when attempting to fetch resource.");
    }
    return anki.fetch(url, init);
  };
  const bg = loadBackground({ fetch });
  const retry = bg.sandbox.ANKI_PERMISSION_RETRY_MS;
  assert.ok(retry >= 2000 && retry <= 10000, "a few seconds, not a poll and not a minute: " + retry);
  const t0 = Date.now();
  bg.setNow(t0);
  const first = await bg.sandbox.ankiPoll();
  assert.equal(first.ok, false);
  assert.equal(first.offline, true);
  assert.equal(refused, 1);
  bg.setNow(t0 + 1000);
  const second = await bg.sandbox.ankiPoll();
  assert.equal(second.ok, false);
  assert.equal(second.offline, true, "still Anki's absence, not a refusal by the viewer");
  assert.equal(refused, 1, "the next poll does not ask again");
  up = true;
  bg.setNow(t0 + retry);
  assert.deepEqual(plain(await bg.sandbox.ankiPoll()), { ok: true, newNoteId: null }, "the baseline, not a minute of 'denied'");
  assert.deepEqual(anki.actions(), ["requestPermission", "findNotes"]);
  ids = [100, 101, 102];
  bg.setNow(t0 + retry + 1000);
  assert.equal((await bg.sandbox.ankiPoll()).newNoteId, 102, "the card made a second later is reported");
});

// The regression: a poll a second with Anki closed sent a requestPermission a second.
test("with Anki closed the watcher asks once per retry interval, not once per poll", async () => {
  let requests = 0;
  const fetch = async () => {
    requests++;
    throw new TypeError("NetworkError when attempting to fetch resource.");
  };
  const bg = loadBackground({ fetch });
  const t0 = Date.now();
  for (let second = 0; second <= 60; second++) {
    bg.setNow(t0 + second * 1000);
    assert.equal((await bg.sandbox.ankiPoll(1)).ok, false);
  }
  const retry = bg.sandbox.ANKI_PERMISSION_RETRY_MS;
  assert.equal(requests, Math.floor(60000 / retry) + 1, `one request per ${retry} ms over a minute of polls, not 61`);
});

test("ankiPoll asks a viewer who clicked No again only after a minute", async () => {
  const anki = ankiFetch({ requestPermission: { permission: "denied" }, findNotes: [100] });
  const bg = loadBackground({ fetch: anki.fetch });
  assert.equal(bg.sandbox.ANKI_PERMISSION_RECHECK_MS, 60000);
  const t0 = Date.now();
  bg.setNow(t0);
  assert.match((await bg.sandbox.ankiPoll()).error, /denied access/);
  bg.setNow(t0 + 1000);
  assert.match((await bg.sandbox.ankiPoll()).error, /denied access/);
  assert.equal(anki.calls.length, 1, "no second dialog within the minute");
  bg.setNow(t0 + 60000);
  await bg.sandbox.ankiPoll();
  assert.equal(anki.calls.length, 2);
});

// The minute counts from the verdict, not from the request: Anki's dialog stays up until the
// viewer clicks, and a No after more than a minute of that used to be followed by a second
// dialog on the very next poll, the stamp set before the request having passed meanwhile.
test("a No clicked after the dialog was up for over a minute still buys the viewer a minute", async () => {
  let answerDialog;
  const dialog = new Promise((resolve) => {
    answerDialog = resolve;
  });
  const anki = ankiFetch({ requestPermission: () => dialog, findNotes: [100] });
  const bg = loadBackground({ fetch: anki.fetch });
  const recheck = bg.sandbox.ANKI_PERMISSION_RECHECK_MS;
  const t0 = Date.now();
  bg.setNow(t0);
  const first = bg.sandbox.ankiPoll();
  await settle();
  assert.equal(anki.calls.length, 1);
  // The viewer clicks No a minute and a half after the dialog came up.
  bg.setNow(t0 + recheck + 30000);
  answerDialog({ permission: "denied" });
  assert.match((await first).error, /denied access/);
  bg.setNow(t0 + recheck + 31000);
  assert.match((await bg.sandbox.ankiPoll()).error, /denied access/);
  assert.equal(anki.calls.length, 1, "no second dialog a second after the No");
  bg.setNow(t0 + recheck + 30000 + recheck);
  await bg.sandbox.ankiPoll();
  assert.equal(anki.calls.length, 2, "asked again a minute after the verdict");
});

// Alt+Shift+M asks Anki itself, with a longer timeout, and the viewer may click Yes in that
// dialog inside the minute after a No to the watcher's: from then on the permission is granted,
// and the watcher, a tab's words and the popup's deck list must see it rather than answer
// "denied" for the rest of the minute, or a card Yomitan makes meanwhile is never mined.
test("a permission granted through a mine's own dialog serves the watcher at once", async () => {
  let verdict = { permission: "denied" };
  const anki = ankiFetch({
    requestPermission: () => verdict,
    findNotes: [100],
    notesInfo: () => noteFields("これは<b>猫</b>です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
    findCards: [],
    getDecks: {},
    deckNames: ["Japanese::Mining"],
  });
  const bg = loadBackground({ fetch: anki.fetch });
  const t0 = Date.now();
  bg.setNow(t0);
  assert.match((await bg.sandbox.ankiPoll()).error, /denied access/);
  // Five seconds on the viewer mines by hand and clicks Yes in the dialog that brings up.
  verdict = granted;
  bg.setNow(t0 + 5000);
  const settings = await bg.sandbox.getSettings();
  const mined = await bg.sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);
  assert.equal(mined.ok, true, JSON.stringify(mined));
  assert.equal(bg.sandbox.ankiWatch.permission, "granted");
  await settle(); // the deck lookup the mine leaves running
  const before = anki.calls.length;
  bg.setNow(t0 + 6000);
  assert.deepEqual(plain(await bg.sandbox.ankiPoll()), { ok: true, newNoteId: null }, "the poll goes through, not 'denied' for the rest of the minute");
  assert.deepEqual(anki.actions().slice(before), ["findNotes"], "straight to the baseline, no third dialog");
  bg.setNow(t0 + 7000);
  assert.deepEqual(plain(await bg.sandbox.ankiDecks()), { ok: true, decks: ["Japanese::Mining"], seen: null });
  assert.equal(anki.actions().filter((action) => action === "requestPermission").length, 2);
});

// Two visible YouTube tabs both poll, and whichever tick lands first used to take the note: the
// tab the card was made in then never heard of it, and the other toasted a mismatch. The note
// now stays on a ledger for every tab that polls within the window.
test("a note found by one tab's poll is reported to the other tab that polls soon after", async () => {
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const bg = loadBackground({ fetch: anki.fetch });
  assert.equal(bg.sandbox.ANKI_REPORT_WINDOW_MS, 60000);
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch({ type: "ankiPoll" }, 1); // the baseline
  ids = [100, 101];
  bg.setNow(t0 + 1000);
  const other = await bg.dispatch({ type: "ankiPoll" }, 2); // the other window's tick lands first
  assert.deepEqual(plain(other), { ok: true, newNoteId: 101, note: { sentence: "これは<b>猫</b>です。", word: "猫" } });
  // The tab the card was made in polls next, inside the throttle even: it gets the same note,
  // without another request, marked as one that may be another tab's.
  bg.setNow(t0 + 1100);
  const requests = anki.calls.length;
  assert.deepEqual(plain(await bg.dispatch({ type: "ankiPoll" }, 1)), {
    ok: true,
    newNoteId: 101,
    note: { sentence: "これは<b>猫</b>です。", word: "猫" },
    replayed: true,
  });
  assert.equal(anki.calls.length, requests);
  // Once per tab: the next ticks of both find nothing new.
  bg.setNow(t0 + 2000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, null);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, null);
  // A tab that first polls once the window has passed was not reading when the card was made.
  bg.setNow(t0 + 1000 + 60001);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 3)).newNoteId, null);
});

// The tab the card was made in was mining the card before (its polls wait for a mine, which
// can take longer than a few seconds) when the other tab found this one: a window as short as
// the old 3 s lost the note to it.
test("the ledger keeps a note for a tab whose next poll comes only after its mine", async () => {
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const bg = loadBackground({ fetch: anki.fetch });
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch({ type: "ankiPoll" }, 1);
  ids = [100, 102];
  bg.setNow(t0 + 2500);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 102);
  bg.setNow(t0 + 6000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 102);
});

// Two words looked up in one line make two cards a second apart, both found by the other tab
// while this one mined the card before: a single remembered note would have lost the first.
test("the ledger holds every note the other tab found, one per poll", async () => {
  let ids = [100];
  const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => YOMITAN_NOTE });
  const bg = loadBackground({ fetch: anki.fetch });
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch({ type: "ankiPoll" }, 1);
  ids = [100, 101];
  bg.setNow(t0 + 1000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 101);
  ids = [100, 101, 102];
  bg.setNow(t0 + 2000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 102);
  bg.setNow(t0 + 4000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 101, "the older card first");
  bg.setNow(t0 + 5000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 102);
  bg.setNow(t0 + 6000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, null);
});

// The other tabs match a card to a line of their own by its sentence. A card with a word alone
// would be attached by every tab whose lines hold the word (a template whose sentence sits in a
// field the popup does not name), and one whose fields could not be read would be attached the
// playhead of every tab, into the same note: such a card goes to the tab that found it only.
test("a note without a sentence to match is not handed to the other tabs", async () => {
  const wordOnly = [{ fields: { Expression: { value: "猫", order: 0 }, Context: { value: "これは猫です。", order: 1 } } }];
  const unreadable = () => {
    throw new Error("Anki went away");
  };
  for (const notesInfo of [() => wordOnly, unreadable]) {
    let ids = [100];
    const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo });
    const bg = loadBackground({ fetch: anki.fetch });
    const t0 = Date.now();
    bg.setNow(t0);
    await bg.dispatch({ type: "ankiPoll" }, 1);
    ids = [100, 101];
    bg.setNow(t0 + 1000);
    assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 101, "the tab that found it");
    bg.setNow(t0 + 1100);
    assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, null, "not the other");
    assert.equal(bg.sandbox.ankiWatch.reports.length, 0);
  }
});

// A card whose sentence is a few characters (はい。, うん, the word alone) is in the lines of
// every video, mid-clause if not as a line: handed to a tab on another video, it was attached
// that video's frame, clip and sentence. Such a card goes to the tab that found it alone.
test("a card whose sentence is a few characters is not handed to the other tabs", async () => {
  assert.equal(loadBackground().sandbox.ANKI_REPORT_MIN_CHARS, 6, "comparable()'s floor in match.js");
  for (const [sentence, shared] of [["<b>はい</b>。", false], ["<b>猫</b>", false], ["嘘でしょ", false], ["これは<b>猫</b>です。", true]]) {
    let ids = [100];
    const anki = ankiFetch({ requestPermission: granted, findNotes: () => ids, notesInfo: () => noteFields(sentence) });
    const bg = loadBackground({ fetch: anki.fetch });
    const t0 = Date.now();
    bg.setNow(t0);
    await bg.dispatch({ type: "ankiPoll" }, 1);
    ids = [100, 101];
    bg.setNow(t0 + 1000);
    const found = await bg.dispatch({ type: "ankiPoll" }, 2);
    assert.equal(found.newNoteId, 101, "the tab that found it: " + sentence);
    assert.equal(found.note.sentence, sentence);
    bg.setNow(t0 + 1100);
    assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, shared ? 101 : null, sentence);
    assert.equal(bg.sandbox.ankiWatch.reports.length, shared ? 1 : 0, sentence);
  }
});

// Once a tab has written into the card, a tab polling later must not be handed it: it would
// match the same line (two tabs on one video) and write the card a second time.
test("a mine that writes into the note takes it off the ledger", async () => {
  let ids = [100];
  const anki = ankiFetch({
    requestPermission: granted,
    findNotes: () => ids,
    notesInfo: () => noteFields("これは<b>猫</b>です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const fetch = async (url, init) => {
    if (String(url).includes("/clip")) {
      return { ok: true, status: 200, headers: { get: () => "audio/mpeg" }, arrayBuffer: async () => new Uint8Array([1]).buffer };
    }
    return anki.fetch(url, init);
  };
  const bg = loadBackground({ fetch, ...instantTimers });
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch({ type: "ankiPoll" }, 1);
  ids = [100, 101];
  bg.setNow(t0 + 1000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 101);
  bg.setNow(t0 + 1100);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 101);
  // A mismatch in one tab leaves the card for the others; a write takes it.
  const mine = { type: "mine", videoId: "abc123abc123", cue: { start: 0, end: 1, text: "全然違う字幕です" }, noteId: 101, auto: true };
  assert.equal((await bg.dispatch(mine, 2)).mismatch, true);
  bg.setNow(t0 + 1200);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 3)).newNoteId, 101);
  const wrote = await bg.dispatch({ ...mine, cue: { start: 0, end: 1, text: "これは猫です。" } }, 1);
  assert.equal(wrote.ok, true, JSON.stringify(wrote));
  bg.setNow(t0 + 1300);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 4)).newNoteId, null);
  assert.deepEqual(plain(bg.sandbox.ankiWatch.reports.map((r) => [r.id, r.written])), [[101, true]]);
});

// An AnkiConnect that writes, and a /clip that answers when the test says so, one per video: a
// mine is in flight between its request and that answer, as it is for seconds in the browser.
function miningAnki(sentence) {
  const state = { ids: [100], clips: {} };
  const anki = ankiFetch({
    requestPermission: granted,
    findNotes: () => state.ids,
    notesInfo: () => noteFields(sentence),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const fetch = (url, init) => {
    const text = String(url);
    if (!text.includes("/clip")) return anki.fetch(url, init);
    const videoId = /video_id=([^&]+)/.exec(text)[1];
    return new Promise((resolve) => {
      state.clips[videoId] = () =>
        resolve({ ok: true, status: 200, headers: { get: () => "audio/mpeg" }, arrayBuffer: async () => new Uint8Array([1]).buffer });
    });
  };
  const bg = loadBackground({ fetch, ...instantTimers });
  const mine = (tabId, videoId, text, noteId) =>
    bg.dispatch({ type: "mine", videoId, cue: { start: 0, end: 1, text }, noteId, auto: true, imageDataUrl: "data:image/jpeg;base64,AAAA" }, tabId);
  const settled = () => new Promise((r) => setImmediate(r));
  const writes = () => anki.calls.filter((c) => c.action === "updateNoteFields").map((c) => c.params.note);
  return { bg, anki, state, mine, settled, writes };
}

// The tab that found the note is mid-mine for seconds (the clip, four Anki round trips) when
// the other tab's own tick lands: handed the note, that tab matched the same line and wrote the
// card a second time, and on two videos sharing a line the card made in one ended up with the
// other's frame and clip. A note a mine is writing into is handed to nobody until the mine is
// over, and given back only when it wrote nothing.
test("a note a tab is mining is not handed to another tab's poll while the mine runs", async () => {
  const { bg, state, mine, settled, writes } = miningAnki("これは<b>猫</b>です。");
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch({ type: "ankiPoll" }, 1);
  state.ids = [100, 101];
  bg.setNow(t0 + 1000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 101);
  const first = mine(1, "videoAAAAAAA", "これは猫です。", 101);
  await settled();
  assert.equal(typeof state.clips.videoAAAAAAA, "function", "the mine is waiting for its clip");
  // The other tab's tick, inside the mine: nothing for it, and nothing once the card is written.
  bg.setNow(t0 + 1100);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, null);
  state.clips.videoAAAAAAA();
  assert.equal((await first).ok, true);
  bg.setNow(t0 + 1200);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, null);
  // A tab that was handed the note before the write, and mines only now, writes nothing either.
  const late = await mine(3, "videoBBBBBBB", "これは猫です。", 101);
  assert.equal(late.ok, true);
  assert.equal(late.warning, true);
  assert.match(late.message, /another tab/);
  assert.equal(state.clips.videoBBBBBBB, undefined, "no clip was even fetched");
  assert.equal(writes().length, 1, "note 101 written once");
  assert.match(writes()[0].fields.Picture, /videoAAAAAAA/);
  assert.match(writes()[0].fields.SentenceAudio, /videoAAAAAAA/);
  // A mine that wrote nothing gives the note back: the next tab's tick is handed it.
  state.ids = [100, 101, 102];
  bg.setNow(t0 + 2000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 102);
  const mismatch = mine(1, "videoAAAAAAA", "全然違う字幕です", 102);
  await settled();
  bg.setNow(t0 + 2100);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, null, "held while the mine runs");
  state.clips.videoAAAAAAA();
  assert.equal((await mismatch).mismatch, true);
  bg.setNow(t0 + 2200);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 102, "given back");
  assert.equal(writes().length, 1);
});

// Both tabs were handed the note before either mine began (the other tab's poll landed first,
// or the tab was paused and seeking back for its frame): the second mine waits for the first
// and writes only when that one wrote nothing.
test("a second mine for a note waits for the first and writes only when that one did not", async () => {
  const { bg, state, mine, settled, writes } = miningAnki("これは<b>猫</b>です。");
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch({ type: "ankiPoll" }, 1);
  state.ids = [100, 101];
  bg.setNow(t0 + 1000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 101);
  bg.setNow(t0 + 1100);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).replayed, true);
  const second = mine(2, "videoBBBBBBB", "これは猫です。", 101);
  await settled();
  const first = mine(1, "videoAAAAAAA", "これは猫です。", 101);
  await settled();
  assert.equal(typeof state.clips.videoBBBBBBB, "function");
  assert.equal(state.clips.videoAAAAAAA, undefined, "the second mine waits before it fetches anything");
  state.clips.videoBBBBBBB();
  assert.equal((await second).ok, true);
  const waited = await first;
  assert.equal(waited.ok, true);
  assert.equal(waited.warning, true);
  assert.equal(state.clips.videoAAAAAAA, undefined);
  assert.equal(writes().length, 1);
  assert.match(writes()[0].fields.Picture, /videoBBBBBBB/);
  // The first mine wrote nothing (its line was not the card's after all): the waiting one writes.
  state.ids = [100, 101, 102];
  bg.setNow(t0 + 3000);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 2)).newNoteId, 102);
  bg.setNow(t0 + 3100);
  assert.equal((await bg.dispatch({ type: "ankiPoll" }, 1)).newNoteId, 102);
  const wrong = mine(2, "videoBBBBBBB", "全然違う字幕です", 102);
  await settled();
  const right = mine(1, "videoAAAAAAA", "これは猫です。", 102);
  await settled();
  state.clips.videoBBBBBBB();
  assert.equal((await wrong).mismatch, true);
  await settled();
  assert.equal(typeof state.clips.videoAAAAAAA, "function", "the waiting mine goes on");
  state.clips.videoAAAAAAA();
  const wrote = await right;
  assert.equal(wrote.ok, true, JSON.stringify(wrote));
  assert.equal(wrote.warning, undefined);
  assert.equal(writes().length, 2);
  assert.equal(writes()[1].id, 102);
  assert.match(writes()[1].fields.Picture, /videoAAAAAAA/);
});

test("ankiPoll stays silent when autoMine is off", async () => {
  const storage = makeMemoryStorage({ settings: { autoMine: false } });
  const { sandbox } = loadBackground({ storage });
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
});

// ------------------------------------------------------------------ normalizeSentence

test("normalizeSentence strips tags, whitespace and punctuation", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.normalizeSentence("これは <b>猫</b> です。"), "これは猫です");
  assert.equal(sandbox.normalizeSentence("a&nbsp;b\n c"), "abc");
  assert.equal(sandbox.normalizeSentence(null), "");
});

// ------------------------------------------------------------------ addToAnki with an explicit note

const MEDIA = {
  image: { filename: "shot.jpg", base64: "Zm9v" },
  audio: { filename: "clip.mp3", base64: "YmFy", mime: "audio/mpeg" },
};

function noteFields(sentence) {
  return [{ fields: { Picture: { value: "" }, SentenceAudio: { value: "" }, Sentence: { value: sentence } } }];
}

test("addToAnki writes to the note it is given without looking up the newest one", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("これは<b>猫</b>です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);

  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.noteId, 555);
  assert.ok(!anki.actions().includes("findNotes"), "an explicit note id needs no findNotes lookup");
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.equal(update.params.note.id, 555);
  assert.equal(update.params.note.fields.Picture, '<img src="shot.jpg">');
  assert.equal(update.params.note.fields.SentenceAudio, "[sound:clip.mp3]");
});

// Eminent's note type spells its fields picture, sentenceAudio and sentence; the settings'
// defaults say Picture, SentenceAudio and Sentence. Every mine used to fail with "The new card has
// none of the fields Picture, SentenceAudio".
function eminentFields(sentence) {
  return [{ fields: {
    wordDictionaryForm: { value: "猫", order: 0 }, sentence: { value: sentence, order: 1 },
    sentenceAudio: { value: "", order: 5 }, picture: { value: "", order: 6 },
  } }];
}

test("addToAnki fills a note type that spells the fields in another case, under its own names", async () => {
  for (const noteId of [555, null]) {
    const anki = ankiFetch({
      requestPermission: granted,
      findNotes: () => [555],
      notesInfo: () => eminentFields("これは<b>猫</b>です。"),
      storeMediaFile: (p) => p.filename,
      updateNoteFields: null,
    });
    const { sandbox } = loadBackground({ fetch: anki.fetch });
    const settings = await sandbox.getSettings();
    const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, noteId);
    assert.equal(res.ok, true, JSON.stringify(res));
    const update = anki.calls.find((c) => c.action === "updateNoteFields");
    assert.deepEqual(Object.keys(update.params.note.fields).sort(), ["picture", "sentenceAudio"], "the note type's own names, nothing new");
    assert.equal(update.params.note.fields.picture, '<img src="shot.jpg">');
    assert.equal(update.params.note.fields.sentenceAudio, "[sound:clip.mp3]");
  }
  // The guard reads the sentence under the note type's name too: a card about something else is
  // refused rather than read as a card without a sentence.
  const other = ankiFetch({
    requestPermission: granted,
    notesInfo: () => eminentFields("まったく別の文です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: other.fetch });
  const res = await sandbox.addToAnki(await sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);
  assert.equal(res.mismatch, true);
  assert.ok(!other.actions().includes("updateNoteFields"));
  // And the watcher's summary of a new note finds its sentence the same way.
  assert.deepEqual(plain(sandbox.noteSummary(eminentFields("これは猫です。")[0], {})), { sentence: "これは猫です。", word: "猫" });
});

test("a card with none of the configured fields says which it has and where the settings are", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    findNotes: () => [555],
    notesInfo: () => [{ fields: {
      Back: { value: "", order: 1 }, Front: { value: "猫", order: 0 }, Screenshot: { value: "", order: 2 }, Clip: { value: "", order: 3 },
    } }],
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  // The note Yomitan just made, found by the watcher (the automatic mine), and so without a sentence
  // field the guard could read: the note's own fields are all there is to go by.
  const res = await sandbox.addToAnki(await sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);
  assert.equal(res.ok, false);
  // Where to change the names before the card's fields, which may run long; the fields that look
  // like a picture or an audio field first.
  assert.equal(res.error, 'The new card has no field "Picture" or "SentenceAudio". '
    + "Check the field names and change them in the settings if they differ: popup > Anki, clips and server. "
    + "Its fields: Screenshot, Clip, Front, Back.");
  assert.ok(!anki.actions().includes("updateNoteFields"));
  assert.ok(!anki.actions().includes("storeMediaFile"), "no media is uploaded for a card that cannot take it");
  // A long note type lists its first fields only, and a mining note type's media fields, which
  // sit near its end (Lapis: SentenceAudio 10th, Picture 11th), are among them.
  const lapis = ["Expression", "ExpressionFurigana", "ExpressionReading", "ExpressionAudio", "SelectionText", "MainDefinition",
    "DefinitionPicture", "Sentence", "SentenceFurigana", "SentenceAudio", "Picture", "Glossary", "Hint", "IsWordAndSentenceCard"];
  const lapisFields = Object.fromEntries(lapis.map((name, order) => [name, { value: "", order }]));
  assert.match(sandbox.missingFieldsError("new", ["Screenshot"], lapisFields),
    /Its fields: ExpressionAudio, DefinitionPicture, SentenceAudio, Picture, Expression, ExpressionFurigana, ExpressionReading, SelectionText, …\.$/);
  assert.equal(sandbox.missingFieldsError("new", ["Picture"], {}), 'The new card has no field "Picture". Check the field names and change them in the settings if they differ: popup > Anki, clips and server.');
  // What the viewer sees, the toast's "Mining failed: " or the fallback's "Saved to Downloads
  // instead. Anki: " in front, keeps the settings hint inside the toast's 240 characters.
  for (const prefix of ["Mining failed: ", "Saved to Downloads instead. Anki: "]) {
    const shown = (prefix + sandbox.missingFieldsError("newest", ["Picture", "SentenceAudio"], lapisFields)).slice(0, 240);
    assert.ok(shown.includes("popup > Anki, clips and server."), shown);
  }
});

// A mine whose frame and clip have nowhere to go is refused, sentence field or not: it used to
// answer success for an extended sentence, with the media lost and no Downloads fallback.
test("a card that can take neither the frame nor the clip is refused even when its sentence could grow", async () => {
  for (const [label, fields] of [
    ["an Eminent-like note, sentence found in another case", { sentence: { value: "<b>猫</b>です", order: 0 }, Image: { value: "", order: 1 }, Audio: { value: "", order: 2 } }],
    ["a note with the default sentence field", { Sentence: { value: "<b>猫</b>です", order: 0 }, Image: { value: "", order: 1 } }],
  ]) {
    const anki = ankiFetch({ requestPermission: granted, notesInfo: () => [{ fields }], storeMediaFile: (p) => p.filename, updateNoteFields: null });
    const { sandbox } = loadBackground({ fetch: anki.fetch });
    const res = await sandbox.addToAnki(await sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);
    assert.equal(res.ok, false, label);
    assert.match(res.error, /^The new card has no field "Picture" or "SentenceAudio"\./, label);
    assert.deepEqual(anki.actions().filter((a) => a === "storeMediaFile" || a === "updateNoteFields"), [], label);
  }
  // One of the two missing is still a partial mine, as before.
  const partial = ankiFetch({
    requestPermission: granted,
    notesInfo: () => [{ fields: { picture: { value: "", order: 0 }, Sentence: { value: "これは猫です。", order: 1 } } }],
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: partial.fetch });
  const res = await sandbox.addToAnki(await sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.match(res.message, /no field named SentenceAudio/);
});

test("a card that is no longer in Anki is said to be gone, not to lack fields", async () => {
  const anki = ankiFetch({ requestPermission: granted, notesInfo: () => [{}], storeMediaFile: (p) => p.filename, updateNoteFields: null });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const res = await sandbox.addToAnki(await sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);
  assert.equal(res.ok, false);
  assert.equal(res.error, "The new card is no longer in Anki; nothing attached");
  assert.deepEqual(anki.actions().filter((a) => a === "storeMediaFile" || a === "updateNoteFields"), []);
});

test("the sentence goes under the note type's spelling, filled or extended, and a field named __proto__ is written", async () => {
  // Filled: the settings name "Sentence", the note spells it "sentence", and it is empty.
  const filled = ankiFetch({ requestPermission: granted, notesInfo: () => eminentFields(""), storeMediaFile: (p) => p.filename, updateNoteFields: null });
  const a = loadBackground({ storage: makeMemoryStorage({ settings: { ankiSentenceField: "Sentence" } }), fetch: filled.fetch });
  assert.equal((await a.sandbox.addToAnki(await a.sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555)).ok, true);
  const f1 = filled.calls.find((c) => c.action === "updateNoteFields").params.note.fields;
  assert.deepEqual(Object.keys(f1).sort(), ["picture", "sentence", "sentenceAudio"]);
  assert.equal(f1.sentence, "これは猫です。");
  // Extended: Yomitan's fragment grown to the spoken sentence, under the note's own spelling.
  const grown = ankiFetch({ requestPermission: granted, notesInfo: () => eminentFields("<b>猫</b>です"), storeMediaFile: (p) => p.filename, updateNoteFields: null });
  const b = loadBackground({ fetch: grown.fetch });
  assert.equal((await b.sandbox.addToAnki(await b.sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555)).ok, true);
  const f2 = grown.calls.find((c) => c.action === "updateNoteFields").params.note.fields;
  assert.deepEqual(Object.keys(f2).sort(), ["picture", "sentence", "sentenceAudio"]);
  assert.equal(f2.sentence, "これは<b>猫</b>です。");
  // The watcher's summary finds a word field named in another case.
  assert.equal(b.sandbox.noteSummary(eminentFields("x")[0], { ankiWordField: "WordDictionaryForm" }).word, "猫");
  // A field literally named __proto__ is an own key of notesInfo's answer and gets its media.
  const proto = ankiFetch({
    requestPermission: granted,
    notesInfo: () => JSON.parse('[{"fields":{"__proto__":{"value":"","order":0},"Sentence":{"value":"これは猫です。","order":1}}}]'),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const c = loadBackground({ storage: makeMemoryStorage({ settings: { ankiImageField: "__proto__" } }), fetch: proto.fetch });
  assert.equal((await c.sandbox.addToAnki(await c.sandbox.getSettings(), { text: "これは猫です。" }, MEDIA.image, null, 555)).ok, true);
  const f3 = proto.calls.find((call) => call.action === "updateNoteFields").params.note.fields;
  assert.ok(Object.prototype.hasOwnProperty.call(f3, "__proto__"), JSON.stringify(f3));
});

test("addToAnki refuses a note whose sentence is about something else", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("まったく別の文です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555);

  assert.equal(res.ok, false);
  assert.equal(res.mismatch, true);
  assert.ok(!anki.actions().includes("updateNoteFields"), "nothing may be written to a mismatched note");
  assert.ok(!anki.actions().includes("storeMediaFile"), "no media may be uploaded for a mismatched note");
});

test("addToAnki accepts a card whose sentence carries furigana and a changed ending", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    // What a Yomitan template with {sentence-furigana} writes into the note.
    notesInfo: () => noteFields(" 私[わたし]は<b> 猫[ねこ]</b>が 好[す]きです。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "私は猫が好きです" }, MEDIA.image, MEDIA.audio, 555);
  assert.equal(res.ok, true, JSON.stringify(res));
});

test("addToAnki refuses a card that only ends like the subtitle", async () => {
  // Both sentences end in 字幕です and share nothing else. Scoring the shared bigrams against the
  // shorter sentence alone put this at exactly the threshold and let it through; the Chrome smoke
  // test caught it writing media into the wrong card.
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("別の字幕です"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これはテスト字幕です" }, MEDIA.image, MEDIA.audio, 202);
  assert.equal(res.mismatch, true, JSON.stringify(res));
  assert.ok(!anki.actions().includes("storeMediaFile"), "no media may reach a mismatched note");
});

test("mineCue does not fall back to Downloads when mining automatically", async () => {
  const storage = makeMemoryStorage({ settings: { mineTarget: "anki", mineFallbackDownload: true } });
  const fetch = async (url) => {
    if (String(url).includes("/clip")) {
      return {
        ok: true,
        status: 200,
        headers: { get: () => "audio/mpeg" },
        arrayBuffer: async () => new Uint8Array([1]).buffer,
      };
    }
    throw new TypeError("fetch failed"); // Anki is not running
  };
  const downloaded = [];
  const download = async (opts) => {
    downloaded.push(opts.filename);
    return {};
  };
  const { sandbox } = loadBackground({ storage, fetch, download, ...instantTimers });

  const res = await sandbox.mineCue({
    videoId: "abc123abc123",
    cue: { start: 0, end: 1, text: "これは猫です。" },
    noteId: 555,
    auto: true,
  });
  assert.equal(res.ok, false);
  assert.equal(downloaded.length, 0, "automatic mining must never write files");
});

test("addToAnki references the filename Anki reports back, not the one it asked for", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields(""),
    storeMediaFile: (p) => p.filename.toLowerCase(), // Anki 26 lowercases media names
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const image = { ...MEDIA.image, filename: "shisuko_SAxvVpdUw24_6380.jpg" };
  const audio = { ...MEDIA.audio, filename: "shisuko_SAxvVpdUw24_6380.mp3" };
  const res = await sandbox.addToAnki(settings, { text: "猫" }, image, audio, 555);

  assert.equal(res.ok, true, JSON.stringify(res));
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.equal(update.params.note.fields.Picture, '<img src="shisuko_saxvvpduw24_6380.jpg">');
  assert.equal(update.params.note.fields.SentenceAudio, "[sound:shisuko_saxvvpduw24_6380.mp3]");
});

// ------------------------------------------------------------------ the whole sentence, not the cue

// The server splits one spoken sentence into several short cues; the content script joins the cues
// sharing a `seg` and sends the result as msg.sentence. Everything below is about that join being
// what reaches Anki: the clip covers it, the guard compares against it, the card keeps it.

test("extendSentenceField grows a fragment into the full sentence and keeps Yomitan's bold", () => {
  const { sandbox } = loadBackground();
  assert.equal(
    sandbox.extendSentenceField("これは<b>猫</b>です。", "これは猫です。とても可愛い。"),
    "これは<b>猫</b>です。とても可愛い。"
  );
});

test("extendSentenceField writes the sentence plain when the bold word cannot be located", () => {
  const { sandbox } = loadBackground();
  // Normalising drops the space inside the bold span, so "thecat" is not in the raw sentence.
  assert.equal(
    sandbox.extendSentenceField("Look at <b>the cat</b>.", "Look at the cat. It sleeps."),
    "Look at the cat. It sleeps."
  );
});

test("extendSentenceField leaves a field that already holds the whole sentence alone", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.extendSentenceField("これは<b>猫</b>です。", "これは猫です。"), null);
  assert.equal(sandbox.extendSentenceField("これは<b>猫</b>です。とても可愛い。", "これは猫です。とても可愛い。"), null);
});

test("extendSentenceField refuses text that is not part of the sentence", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.extendSentenceField("まったく別の文です。", "これは猫です。とても可愛い。"), null);
  assert.equal(sandbox.extendSentenceField("", "これは猫です。"), null);
});

// A field is HTML to Anki, a transcription is text: "1<2" must stay "1<2" on the card, and the
// server, whoever runs it, must not be able to put markup into the collection. The guard cannot
// catch this (normalising strips tags before comparing), so the write itself has to escape.
test("extendSentenceField writes the transcription as text, with only its own <b> as markup", () => {
  const { sandbox } = loadBackground();
  assert.equal(
    sandbox.extendSentenceField("これは<b>猫</b>です。", "これは猫です。<script>alert(1)</script>とても可愛い。"),
    "これは<b>猫</b>です。&lt;script&gt;alert(1)&lt;/script&gt;とても可愛い。"
  );
  assert.equal(sandbox.extendSentenceField("Look at <b>the cat</b>.", "Look at the cat. 1<2 & \"so\" it's"), "Look at the cat. 1&lt;2 &amp; &quot;so&quot; it&#39;s");
});

// The server joins two merged utterances with a newline, and the overlay, Yomitan and match.js all
// read it as a line break. HTML would collapse it into a space, so the card would lose the only
// mark saying where one utterance ended and the next began.
test("escapeHtml keeps the server's line break as a line break on the card", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.escapeHtml("体動かない\n待って!"), "体動かない<br>待って!");
  assert.equal(sandbox.escapeHtml("a\r\nb"), "a<br>b");
  assert.equal(sandbox.escapeHtml("1<2\n&"), "1&lt;2<br>&amp;");
});

// A merged cue can hold two utterances either side of the seam. Yomitan ends its sentence at the
// newline, so growing its fragment past it would put the other speaker's line on the card -- which
// is the one thing the seam exists to prevent.
test("extendSentenceField grows a fragment inside its own row, never across the seam", () => {
  const { sandbox } = loadBackground();
  assert.equal(
    sandbox.extendSentenceField("これは<b>猫</b>です。", "これは猫です。とても可愛い\n待って!"),
    "これは<b>猫</b>です。とても可愛い"
  );
});

test("extendSentenceField leaves a fragment that already holds its whole row alone", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.extendSentenceField("<b>体</b>動かない", "体動かない\n待って!"), null);
});

test("extendSentenceField is unchanged for a cue without a seam", () => {
  const { sandbox } = loadBackground();
  assert.equal(
    sandbox.extendSentenceField("これは<b>猫</b>です。", "これは猫です。とても可愛い。"),
    "これは<b>猫</b>です。とても可愛い。"
  );
});

test("addToAnki escapes the sentence it writes into an empty field", async () => {
  const storage = makeMemoryStorage({ settings: { ankiSentenceField: "Sentence" } });
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields(""),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ storage, fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "1<2 かな" }, MEDIA.image, MEDIA.audio, 555, { start: 10, end: 14, text: "1<2 かな Q&A <img src=x onerror=alert(1)> です。" });
  assert.equal(res.ok, true, JSON.stringify(res));
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.equal(update.params.note.fields.Sentence, "1&lt;2 かな Q&amp;A &lt;img src=x onerror=alert(1)&gt; です。");
});

test("addToAnki rewrites the sentence field to the whole sentence and says so", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("これは<b>猫</b>です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings(); // ankiSentenceField unset: the guard's "Sentence"
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555, {
    start: 10,
    end: 14,
    text: "これは猫です。とても可愛い。",
  });

  assert.equal(res.ok, true, JSON.stringify(res));
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.equal(update.params.note.fields.Sentence, "これは<b>猫</b>です。とても可愛い。");
  assert.match(res.message, /sentence/i);
});

test("addToAnki leaves a sentence field that is already complete untouched", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("これは<b>猫</b>です。とても可愛い。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555, {
    start: 10,
    end: 14,
    text: "これは猫です。とても可愛い。",
  });

  assert.equal(res.ok, true, JSON.stringify(res));
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.ok(!("Sentence" in update.params.note.fields), "a complete sentence must not be rewritten");
  assert.doesNotMatch(res.message, /sentence extended/i);
});

test("addToAnki accepts a note whose sentence matches a neighbouring cue of the same segment", async () => {
  // Yomitan copied its sentence from the cue after the one being mined. Comparing against the cue
  // alone would call this a different card; comparing against the joined sentence gets it right.
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("とても<b>可愛い</b>。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555, {
    start: 10,
    end: 14,
    text: "これは猫です。とても可愛い。",
  });

  assert.equal(res.ok, true, JSON.stringify(res));
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.equal(update.params.note.fields.Sentence, "これは猫です。とても<b>可愛い</b>。");
});

test("addToAnki still refuses an unrelated note, and writes no sentence into it", async () => {
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields("まったく別の文です。"),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555, {
    start: 10,
    end: 14,
    text: "これは猫です。とても可愛い。",
  });

  assert.equal(res.ok, false);
  assert.equal(res.mismatch, true);
  assert.ok(!anki.actions().includes("updateNoteFields"), "nothing may be written to a mismatched note");
});

test("addToAnki fills an empty sentence field with the whole sentence when one is configured", async () => {
  const storage = makeMemoryStorage({ settings: { ankiSentenceField: "Sentence" } });
  const anki = ankiFetch({
    requestPermission: granted,
    notesInfo: () => noteFields(""),
    storeMediaFile: (p) => p.filename,
    updateNoteFields: null,
  });
  const { sandbox } = loadBackground({ storage, fetch: anki.fetch });
  const settings = await sandbox.getSettings();
  const res = await sandbox.addToAnki(settings, { text: "これは猫です。" }, MEDIA.image, MEDIA.audio, 555, {
    start: 10,
    end: 14,
    text: "これは猫です。とても可愛い。",
  });

  assert.equal(res.ok, true, JSON.stringify(res));
  const update = anki.calls.find((c) => c.action === "updateNoteFields");
  assert.equal(update.params.note.fields.Sentence, "これは猫です。とても可愛い。");
});

test("mineCue cuts the clip over the sentence but names the file after the cue", async () => {
  const storage = makeMemoryStorage({ settings: { mineTarget: "download" } }); // clipPaddingMs 200 ms
  const urls = [];
  const fetch = async (url) => {
    urls.push(String(url));
    return {
      ok: true,
      status: 200,
      headers: { get: () => "audio/mpeg" },
      arrayBuffer: async () => new Uint8Array([1]).buffer,
    };
  };
  const downloaded = [];
  const download = async (opts) => {
    downloaded.push(opts.filename);
    return {};
  };
  const { sandbox } = loadBackground({ storage, fetch, download, ...instantTimers });

  const res = await sandbox.mineCue({
    videoId: "abc123abc123",
    cue: { start: 10, end: 12, text: "これは猫です。" },
    sentence: { start: 8, end: 14, text: "これは猫です。とても可愛い。" },
  });

  assert.equal(res.ok, true, JSON.stringify(res));
  assert.match(urls[0], /start=7\.800&end=14\.200/);
  assert.deepEqual(downloaded, ["shisu-ko-mining/shisuko_abc123abc123_10000.mp3"]);
});

test("mineCue falls back to the cue's own range when no sentence is sent", async () => {
  const storage = makeMemoryStorage({ settings: { mineTarget: "download" } });
  const urls = [];
  const fetch = async (url) => {
    urls.push(String(url));
    return {
      ok: true,
      status: 200,
      headers: { get: () => "audio/mpeg" },
      arrayBuffer: async () => new Uint8Array([1]).buffer,
    };
  };
  const { sandbox } = loadBackground({ storage, fetch, download: async () => ({}), ...instantTimers });

  await sandbox.mineCue({ videoId: "abc123abc123", cue: { start: 10, end: 12, text: "これは猫です。" } });
  assert.match(urls[0], /start=9\.800&end=12\.200/);
});

// ------------------------------------------------------------------ pre-mined sentences

const VIDEO = "abc123abc123";
// The background answers a premine before its clip request has come back; a turn of the event
// loop is what the content script gets for free on its next message.
const settle = () => new Promise((resolve) => setImmediate(resolve));

// One mock for both endpoints a mine touches: the Whisper server's /clip and AnkiConnect.
function miningFetch(handlers) {
  const clips = [];
  const calls = [];
  const fetch = async (url, init) => {
    if (String(url).includes("/clip")) {
      clips.push(String(url));
      return {
        ok: true,
        status: 200,
        headers: { get: () => "audio/mpeg" },
        arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
      };
    }
    const req = JSON.parse(init.body);
    calls.push({ action: req.action, params: req.params });
    const handler = (handlers || {})[req.action];
    if (handler === undefined) throw new Error("unexpected AnkiConnect action " + req.action);
    const result = typeof handler === "function" ? handler(req.params) : handler;
    return { json: async () => ({ result, error: null }) };
  };
  return { fetch, clips, calls };
}

const jpeg = (text) => "data:image/jpeg;base64," + Buffer.from(text).toString("base64");

function premine(key, patch) {
  return Object.assign(
    {
      type: "premine",
      videoId: VIDEO,
      key,
      cueIds: [key],
      sentence: { start: key * 10, end: key * 10 + 2, text: `文${key}` },
    },
    patch
  );
}

test("premine keeps the frame and fetches the clip once, and reports what a tab holds", async () => {
  const mock = miningFetch();
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch });
  const res = await dispatch(premine(0, { imageDataUrl: jpeg("frame") }), 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.deepEqual(plain(res.held), [{ key: 0, cueIds: [0], image: true, audio: false }]);
  await settle();
  assert.deepEqual(plain(sandbox.heldFor(1)), [{ key: 0, cueIds: [0], image: true, audio: true }]);
  // The same sentence again must not ask the server for the clip a second time.
  await dispatch(premine(0), 1);
  await settle();
  assert.equal(mock.clips.length, 1);
});

test("premine drops a screenshot too large to be one", async () => {
  const mock = miningFetch();
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch });
  const huge = "data:image/jpeg;base64," + "A".repeat(5 * 1024 * 1024);
  await dispatch(premine(0, { imageDataUrl: huge }), 1);
  await settle();
  // The oversize frame is thrown away; everything else about the sentence still proceeds.
  assert.deepEqual(plain(sandbox.heldFor(1)), [{ key: 0, cueIds: [0], image: false, audio: true }]);
});

test("premine keeps five sentences per tab and drops the oldest", async () => {
  const mock = miningFetch();
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch });
  for (let key = 0; key < 7; key++) await dispatch(premine(key, { imageDataUrl: jpeg("f") }), 1);
  const keys = sandbox.heldFor(1).map((e) => e.key).sort((a, b) => a - b);
  assert.deepEqual(plain(keys), [2, 3, 4, 5, 6]);
});

test("premine keeps the hovered sentence even when older ones are dropped", async () => {
  const mock = miningFetch();
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch });
  await dispatch(premine(0, { imageDataUrl: jpeg("read"), hover: true }), 1);
  for (let key = 1; key < 7; key++) await dispatch(premine(key, { imageDataUrl: jpeg("f") }), 1);
  const keys = sandbox.heldFor(1).map((e) => e.key).sort((a, b) => a - b);
  assert.equal(keys.length, 5);
  assert.ok(keys.includes(0), "the sentence being looked up must survive: " + keys.join(","));
});

// The content script sends the line on screen with its frame, then the next line's clip request
// with `ahead`. The order it gets back is its tie-break between two identical lines, so the
// line that has not played yet must not come first merely for being the newest message.
test("a sentence prepared ahead of its turn ranks behind the one on screen", async () => {
  const mock = miningFetch();
  const bg = loadBackground({ fetch: mock.fetch });
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.dispatch(premine(0, { imageDataUrl: jpeg("frame") }), 1);
  bg.setNow(t0 + 1);
  const res = await bg.dispatch(premine(1, { ahead: true }), 1);
  assert.deepEqual(plain(res.held.map((e) => e.key)), [0, 1]);
  // Its clip was still fetched: a lookup on it is paid for.
  await settle();
  assert.deepEqual(plain(bg.sandbox.heldFor(1)), [{ key: 0, cueIds: [0], image: true, audio: true }, { key: 1, cueIds: [1], image: false, audio: true }]);
  // Once it plays it is the line read last.
  bg.setNow(t0 + 2000);
  await bg.dispatch(premine(1, { imageDataUrl: jpeg("frame") }), 1);
  assert.deepEqual(plain(bg.sandbox.heldFor(1).map((e) => e.key)), [1, 0]);
  // Hovering the earlier line brings it back to the front.
  bg.setNow(t0 + 3000);
  await bg.dispatch(premine(0, { imageDataUrl: jpeg("read"), hover: true }), 1);
  assert.deepEqual(plain(bg.sandbox.heldFor(1).map((e) => e.key)), [0, 1]);
});

test("premine holds ten sentences across every tab", async () => {
  const mock = miningFetch();
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch });
  for (let key = 0; key < 5; key++) await dispatch(premine(key), 1);
  for (let key = 0; key < 5; key++) await dispatch(premine(key), 2);
  assert.equal(sandbox.premined.size, 10);
  await dispatch(premine(0), 3);
  assert.equal(sandbox.premined.size, 10);
  assert.equal(sandbox.heldFor(1).length, 4, "the oldest tab gives up the oldest sentence");
  assert.equal(sandbox.heldFor(2).length, 5);
  assert.equal(sandbox.heldFor(3).length, 1);
});

test("a tab that navigates away or closes leaves nothing behind", async () => {
  const mock = miningFetch();
  const { sandbox, dispatch, closeTab } = loadBackground({ fetch: mock.fetch });
  await dispatch(premine(0), 1);
  await dispatch(premine(1), 2);
  await dispatch({ type: "premineReset" }, 1);
  assert.deepEqual(plain(sandbox.heldFor(1)), []);
  assert.equal(sandbox.heldFor(2).length, 1);
  closeTab(2);
  assert.equal(sandbox.premined.size, 0);
});

// ------------------------------------------------------------------ mining from the cache

function mineMsg(key, patch) {
  return Object.assign(
    {
      type: "mine",
      videoId: VIDEO,
      key,
      cue: { start: key * 10, end: key * 10 + 2, text: `文${key}` },
      sentence: { start: key * 10, end: key * 10 + 2, text: `文${key}` },
      noteId: 555,
    },
    patch
  );
}

const ankiOk = {
  requestPermission: granted,
  notesInfo: () => noteFields("文0"),
  storeMediaFile: (p) => p.filename,
  updateNoteFields: null,
};

test("mining a pre-mined sentence asks the server for nothing and uses the frame it kept", async () => {
  const mock = miningFetch(ankiOk);
  const { dispatch } = loadBackground({ fetch: mock.fetch, ...instantTimers });
  await dispatch(premine(0, { imageDataUrl: jpeg("the frame that was read") }), 1);
  await settle();
  assert.equal(mock.clips.length, 1);

  const res = await dispatch(mineMsg(0), 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(mock.clips.length, 1, "the clip was already there");
  const image = mock.calls.find((c) => c.action === "storeMediaFile" && c.params.filename.endsWith(".jpg"));
  assert.equal(Buffer.from(image.params.data, "base64").toString(), "the frame that was read");
});

test("a frame sent with the mine beats the pre-mined one", async () => {
  const mock = miningFetch(ankiOk);
  const { dispatch } = loadBackground({ fetch: mock.fetch, ...instantTimers });
  await dispatch(premine(0, { imageDataUrl: jpeg("prepared") }), 1);
  await settle();
  const res = await dispatch(mineMsg(0, { imageDataUrl: jpeg("captured now") }), 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  const image = mock.calls.find((c) => c.action === "storeMediaFile" && c.params.filename.endsWith(".jpg"));
  assert.equal(Buffer.from(image.params.data, "base64").toString(), "captured now");
});

test("a clip cut with the old padding is fetched again, not reused", async () => {
  const mock = miningFetch(ankiOk);
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch, ...instantTimers });
  await dispatch(premine(0), 1);
  await settle();
  assert.match(mock.clips[0], /start=0\.000&end=2\.200/);

  await sandbox.saveSettings({ clipPaddingMs: 500 });
  const res = await dispatch(mineMsg(0), 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(mock.clips.length, 2, "the held clip is the wrong length now");
  assert.match(mock.clips[1], /start=0\.000&end=2\.500/);
});

test("a sentence survives being mined: two words from one line make two cards", async () => {
  const mock = miningFetch(ankiOk);
  const { sandbox, dispatch } = loadBackground({ fetch: mock.fetch, ...instantTimers });
  await dispatch(premine(0, { imageDataUrl: jpeg("frame") }), 1);
  await settle();
  assert.equal((await dispatch(mineMsg(0), 1)).ok, true);
  assert.equal((await dispatch(mineMsg(0), 1)).ok, true);
  assert.equal(mock.clips.length, 1, "the second card reuses the same clip");
  assert.deepEqual(plain(sandbox.heldFor(1)), [{ key: 0, cueIds: [0], image: true, audio: true }]);
});

// ------------------------------------------------------------------ mining deadlines

// The content script holds its mining flag until the mine's answer arrives, so a request that
// hangs (a container paused, a laptop asleep mid-request, a permission dialog nobody sees) used to
// end manual mining, automatic mining and the Anki watch for that tab until a reload. The mock
// below answers a hanging request only through its abort signal, and fires the background's
// timers on the next turn instead of after their delay, keeping the delays for the assertions.
function hangingFetch(handlers, hangs) {
  const inner = miningFetch(handlers);
  const delays = [];
  const setTimeout = (fn, ms) => {
    delays.push(ms);
    return globalThis.setTimeout(fn, 0);
  };
  const fetch = (url, init) => {
    const action = String(url).includes("/clip") ? "clip" : JSON.parse(init.body).action;
    if (!hangs(action)) return inner.fetch(url, init);
    if (action === "clip") inner.clips.push(String(url));
    return new Promise((_resolve, reject) => {
      const signal = init && init.signal;
      if (!signal) return; // no deadline: this connection never answers
      const abort = () => reject(Object.assign(new Error("The operation was aborted."), { name: "AbortError" }));
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    });
  };
  return { fetch, setTimeout, delays, clips: inner.clips, calls: inner.calls };
}

const settles = (promise) => Promise.race([promise, new Promise((resolve) => globalThis.setTimeout(() => resolve("hung"), 500).unref())]);

test("a mine whose clip request hangs ends at the clip deadline", async () => {
  const mock = hangingFetch(ankiOk, (action) => action === "clip");
  const bg = loadBackground({ fetch: mock.fetch, setTimeout: mock.setTimeout });
  assert.equal(bg.sandbox.CLIP_TIMEOUT_MS, 30000);
  const res = await settles(bg.dispatch(mineMsg(0), 1));
  assert.notEqual(res, "hung");
  assert.equal(res.ok, false);
  assert.match(res.error, /Whisper server timed out/);
  assert.ok(mock.delays.includes(30000), "the clip deadline: " + mock.delays.join(","));
});

test("a mine whose AnkiConnect request hangs ends at that request's deadline", async () => {
  // Automatic mines: a manual one would fall back to Downloads on the failure and answer ok.
  const cases = [
    ["requestPermission", 60000, mineMsg(0, { auto: true })], // the viewer may be looking for Anki's dialog
    ["findNotes", 30000, mineMsg(0, { noteId: null, auto: true })],
    ["notesInfo", 30000, mineMsg(0, { auto: true })],
    ["storeMediaFile", 30000, mineMsg(0, { auto: true })],
    ["updateNoteFields", 30000, mineMsg(0, { auto: true })],
  ];
  for (const [action, deadline, msg] of cases) {
    const mock = hangingFetch({ ...ankiOk, findNotes: [555] }, (a) => a === action);
    const bg = loadBackground({ fetch: mock.fetch, setTimeout: mock.setTimeout });
    const res = await settles(bg.dispatch(msg, 1));
    assert.notEqual(res, "hung", action);
    assert.equal(res.ok, false, action);
    assert.match(res.error, /did not answer in time/, action);
    assert.ok(mock.delays.includes(deadline), `${action}: ${mock.delays.join(",")}`);
  }
  const { sandbox } = loadBackground();
  assert.equal(sandbox.ANKI_PERMISSION_TIMEOUT_MS, 60000);
  assert.equal(sandbox.ANKI_REQUEST_TIMEOUT_MS, 30000);
});

test("a mine that waits on a pre-mined clip still under way ends when that request does", async () => {
  const mock = hangingFetch(ankiOk, (action) => action === "clip");
  const bg = loadBackground({ fetch: mock.fetch, setTimeout: mock.setTimeout });
  await bg.dispatch(premine(0, { imageDataUrl: jpeg("frame") }), 1);
  assert.equal(mock.clips.length, 1);
  // Dispatched before the pre-mine's request has come back: the mine waits on that request.
  const res = await settles(bg.dispatch(mineMsg(0), 1));
  assert.notEqual(res, "hung");
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.match(res.message, /no audio \(Whisper server timed out\)/);
});

// ------------------------------------------------------------------ downloads fallback

test("downloadFiles hands Firefox object URLs, never data: URLs", async () => {
  const seen = [];
  const revoked = [];
  const download = async (options) => {
    seen.push(options);
    return seen.length;
  };
  const { sandbox } = loadBackground({
    download,
    createObjectURL: (blob) => `blob:moz-extension://test/${blob.type}`,
    revokeObjectURL: (url) => revoked.push(url),
    setTimeout: (fn, ms) => setTimeout(fn, ms).unref(), // the revoke timer must not keep the test runner alive
  });
  const image = { base64: Buffer.from("jpeg bytes").toString("base64"), filename: "a.jpg" };
  const audio = { base64: Buffer.from("mp3 bytes").toString("base64"), filename: "a.mp3", mime: "audio/mpeg" };
  const res = await sandbox.downloadFiles(image, audio);
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.deepEqual(seen.map((o) => o.url), ["blob:moz-extension://test/image/jpeg", "blob:moz-extension://test/audio/mpeg"]);
  assert.deepEqual(seen.map((o) => o.filename), ["shisu-ko-mining/a.jpg", "shisu-ko-mining/a.mp3"]);
  assert.ok(seen.every((o) => !o.url.startsWith("data:")));
  assert.equal(revoked.length, 0, "the object URL must live until the download has finished");
});

test("downloadFiles uses Chrome data URLs in the service worker", async () => {
  const seen = [];
  const { sandbox } = loadBackground({
    runtimeURL: "chrome-extension://test/",
    download: async (options) => { seen.push(options); return seen.length; },
    createObjectURL: () => { throw new Error("service workers have no object URLs"); },
  });
  const imageBytes = Buffer.from([0, 255, 65]);
  const res = await sandbox.downloadFiles({ base64: imageBytes.toString("base64"), filename: "a.jpg" }, null);
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(seen.length, 1);
  assert.match(seen[0].url, /^data:image\/jpeg;base64,/);
  assert.deepEqual(Buffer.from(seen[0].url.split(",", 2)[1], "base64"), imageBytes);
});

test("downloadFiles revokes the object URL when the download is refused", async () => {
  const revoked = [];
  const { sandbox } = loadBackground({
    download: async () => { throw new Error("Access denied for URL"); },
    revokeObjectURL: (url) => revoked.push(url),
    setTimeout: (fn, ms) => setTimeout(fn, ms).unref(),
  });
  const res = await sandbox.downloadFiles({ base64: Buffer.from("x").toString("base64"), filename: "a.jpg" }, null);
  assert.equal(res.ok, false);
  assert.match(res.error, /Download failed: Access denied/);
  assert.equal(revoked.length, 1);
});

test("base64ToBlob decodes the bytes and keeps the mime type", async () => {
  const { sandbox } = loadBackground();
  const blob = sandbox.base64ToBlob(Buffer.from([0, 255, 65]).toString("base64"), "audio/mpeg");
  assert.equal(blob.type, "audio/mpeg");
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [0, 255, 65]);
});

// ------------------------------------------------------------------ startServer (native host)

// The native host answers one message and exits; the stub records what it was asked.
function nativeHost(answer) {
  const calls = [];
  const sendNativeMessage = async (application, message) => {
    calls.push({ application, message });
    if (answer instanceof Error) throw answer;
    return typeof answer === "function" ? answer() : answer;
  };
  return { calls, sendNativeMessage };
}

// A launch's answer carries the deadline of its window (START_WINDOW_MS from the moment the host
// answered); the launch itself is compared without it.
function launched(res, expected) {
  assert.ok(Number.isFinite(res.deadline) && res.deadline > Date.now() + 80000 && res.deadline <= Date.now() + 90000, `deadline ${res.deadline}`);
  const { deadline, ...rest } = plain(res);
  assert.deepEqual(rest, expected);
}

test("startServer asks the shisuko host to start and resolves its answer", async () => {
  const host = nativeHost({ ok: true, started: true, log: null });
  const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  assert.equal(sandbox.START_WINDOW_MS, 90000);
  launched(res, { ok: true, started: true, already: false, loading: false, log: null });
  assert.deepEqual(plain(host.calls), [{ application: "shisuko", message: { cmd: "start" } }]);
});

test("startServer treats an already running server as started, with the log path when there is one", async () => {
  const host = nativeHost({ ok: true, already: true, log: "/home/x/.shisu-ko/server.log" });
  const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  launched(res, { ok: true, started: false, already: true, loading: false, log: "/home/x/.shisu-ko/server.log" });
});

// The host's `starting`: /health is silent but server.py holds its instance lock, so a server is
// loading its model (minutes on a first use). The popup must wait for it as for its own launch,
// not take it for a server at another address; `loading` here, since `starting` is the name of
// startStatus's own answer.
test("startServer passes the host's 'starting' on as 'loading'", async () => {
  const host = nativeHost({ ok: true, already: true, starting: true });
  const { sandbox, dispatch } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  launched(res, { ok: true, started: false, already: true, loading: true, log: null });
  const status = await dispatch({ type: "startServerStatus" });
  assert.deepEqual(plain(status), { starting: true, already: true, loading: true, log: null, deadline: res.deadline });
});

test("startServer maps Firefox's missing-host error to 'launcher not registered' with the setup hint", async () => {
  const host = nativeHost(new Error("No such native application shisuko"));
  const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  assert.equal(res.ok, false);
  assert.equal(res.error, "launcher not registered");
  assert.equal(res.hint, sandbox.LAUNCHER_HINT);
  assert.match(res.hint, /setup\.cmd/);
  assert.match(res.hint, /setup\.sh/);
});

test("startServer maps Chrome's two registration errors the same way", async () => {
  for (const text of ["Specified native messaging host not found.", "Access to the specified native messaging host is forbidden."]) {
    const host = nativeHost(new Error(text));
    const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
    const res = await sandbox.startServer();
    assert.equal(res.error, "launcher not registered", text);
    assert.equal(res.hint, sandbox.LAUNCHER_HINT);
  }
});

test("startServer reports a missing nativeMessaging permission instead of throwing", async () => {
  const { sandbox } = loadBackground(); // no sendNativeMessage at all: the permission was never granted
  const res = await sandbox.startServer();
  assert.equal(res.ok, false);
  assert.equal(res.error, "permission missing");
  const denied = nativeHost(new Error("Access to this API is denied: the nativeMessaging permission is missing"));
  const other = loadBackground({ sendNativeMessage: denied.sendNativeMessage });
  assert.equal((await other.sandbox.startServer()).error, "permission missing");
});

test("startServer passes the host's own refusal through", async () => {
  const host = nativeHost({ ok: false, error: "run.cmd is missing" });
  const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  assert.deepEqual(plain(res), { ok: false, error: "run.cmd is missing" });
});

test("startServer gives up on a host that never answers", async () => {
  const host = nativeHost(() => new Promise(() => {}));
  // The deadline timer fires at once, but the delay it was given is kept: the 15 s the popup's
  // 90 s is built on, and a slip to 15 ms would fail every click before the host's Python is up.
  const delays = [];
  const setTimeout = (fn, ms) => { delays.push(ms); fn(); return 0; };
  const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage, setTimeout });
  assert.equal(sandbox.NATIVE_TIMEOUT_MS, 15000);
  const res = await sandbox.startServer();
  assert.deepEqual(plain(res), { ok: false, error: "the launcher did not answer" });
  assert.deepEqual(delays, [sandbox.NATIVE_TIMEOUT_MS]);
});

test("startServer refuses an answer that is not an object", async () => {
  const host = nativeHost(() => "yes");
  const { sandbox } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  assert.deepEqual(plain(res), { ok: false, error: "the launcher gave no answer" });
});

test("a startServer message from the popup reaches the native host", async () => {
  const host = nativeHost({ ok: true, started: true });
  const { dispatch } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await dispatch({ type: "startServer" });
  launched(res, { ok: true, started: true, already: false, loading: false, log: null });
  assert.equal(host.calls.length, 1);
});

// The popup document dies with every click outside it. A reopened one asks whether a start is
// under way and must be told so for as long as the server can still be loading: the host only
// checks /health, which a loading server does not answer, so a second request would start a
// second server.
test("a second startServer request while the launch is under way is answered without the host", async () => {
  const host = nativeHost({ ok: true, started: true, log: "/home/x/.shisu-ko/server.log" });
  const { sandbox, dispatch } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const first = await sandbox.startServer();
  const again = await dispatch({ type: "startServer" });
  assert.deepEqual(plain(again), plain(first));
  assert.equal(host.calls.length, 1);
  const status = await dispatch({ type: "startServerStatus" });
  assert.deepEqual(plain(status), { starting: true, already: false, loading: false, log: "/home/x/.shisu-ko/server.log", deadline: first.deadline });
});

test("startServerStatus reports no launch when none was requested or the last one failed", async () => {
  const { dispatch } = loadBackground();
  assert.deepEqual(plain(await dispatch({ type: "startServerStatus" })), { starting: false });
  const host = nativeHost({ ok: false, error: "run.cmd is missing" });
  const failed = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  await failed.sandbox.startServer();
  assert.deepEqual(plain(await failed.dispatch({ type: "startServerStatus" })), { starting: false });
  // Nothing to answer from: the next request goes to the host again.
  await failed.sandbox.startServer();
  assert.equal(host.calls.length, 2);
});

test("the launch outlives the event page: a restarted background answers from storage.session", async () => {
  const host = nativeHost({ ok: true, started: true });
  const first = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await first.sandbox.startServer();
  const restarted = loadBackground({ sendNativeMessage: host.sendNativeMessage, session: first.session });
  const status = await restarted.dispatch({ type: "startServerStatus" });
  assert.deepEqual(plain(status), { starting: true, already: false, loading: false, log: null, deadline: res.deadline });
  assert.deepEqual(plain(await restarted.sandbox.startServer()), plain(res));
  assert.equal(host.calls.length, 1);
});

test("a launch is remembered in memory alone on a browser without storage.session", async () => {
  const host = nativeHost({ ok: true, started: true });
  const { sandbox, dispatch } = loadBackground({ sendNativeMessage: host.sendNativeMessage, session: null });
  const res = await sandbox.startServer();
  assert.deepEqual(plain(await sandbox.startServer()), plain(res));
  assert.equal(host.calls.length, 1);
  assert.equal((await dispatch({ type: "startServerStatus" })).starting, true);
});

test("once the launch's window has passed the host is asked again", async () => {
  const host = nativeHost({ ok: true, started: true });
  const { sandbox, session } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const res = await sandbox.startServer();
  await session.set({ startServer: { ...plain(res), deadline: Date.now() - 1 } });
  assert.deepEqual(plain(await sandbox.startStatus()), { starting: false });
  await sandbox.startServer();
  assert.equal(host.calls.length, 2);
});

test("a server that answers /health ends the launch, so the next request reaches the host", async () => {
  const host = nativeHost({ ok: true, started: true });
  const fetch = async () => ({ ok: true, status: 200, text: async () => JSON.stringify({ model: "large-v3" }) });
  const { sandbox, session } = loadBackground({ sendNativeMessage: host.sendNativeMessage, fetch });
  await sandbox.startServer();
  assert.equal((await sandbox.apiRequest("/health")).ok, true);
  assert.deepEqual(plain(await sandbox.startStatus()), { starting: false });
  assert.equal((await session.get("startServer")).startServer, null);
  await sandbox.startServer();
  assert.equal(host.calls.length, 2);
});

test("requests that overlap share the host's one answer", async () => {
  let answer = null;
  const host = nativeHost(() => new Promise((resolve) => { answer = resolve; }));
  const { sandbox, dispatch } = loadBackground({ sendNativeMessage: host.sendNativeMessage });
  const one = sandbox.startServer();
  const two = dispatch({ type: "startServer" });
  const status = dispatch({ type: "startServerStatus" }); // waits for the verdict too
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.calls.length, 1);
  answer({ ok: true, started: true });
  const [a, b] = await Promise.all([one, two]);
  assert.deepEqual(plain(a), plain(b));
  assert.equal((await status).starting, true);
  assert.equal(host.calls.length, 1);
});

// ------------------------------------------------------------------ updates

// What api.github.com/repos/<owner>/<repo>/releases/latest answers, cut to the fields the
// background reads plus a few it must ignore.
const RELEASE = {
  url: "https://api.github.com/repos/Multysquid/shisu-ko/releases/1",
  html_url: "https://github.com/Multysquid/shisu-ko/releases/tag/v0.9.0",
  tag_name: "v0.9.0",
  name: "Shisu-ko 0.9.0",
  draft: false,
  prerelease: false,
  assets: [
    { name: "shisu-ko-0.9.0-chrome.zip", browser_download_url: "https://github.com/Multysquid/shisu-ko/releases/download/v0.9.0/shisu-ko-0.9.0-chrome.zip", content_type: "application/zip", size: 1 },
    { name: "shisu_ko-0.9.0.xpi", browser_download_url: "https://github.com/Multysquid/shisu-ko/releases/download/v0.9.0/shisu_ko-0.9.0.xpi", content_type: "application/x-xpinstall", size: 1 },
  ],
};
const LATEST = { version: "0.9.0", tag: "v0.9.0", url: RELEASE.html_url, xpi: RELEASE.assets[1].browser_download_url };
const HOUR = 60 * 60 * 1000;

function jsonResponse(status, body) {
  const text = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
  return { ok: status >= 200 && status < 300, status, text: async () => text, json: async () => JSON.parse(text) };
}

// One fetch for the three addresses an update touches: GitHub, /health and /update. Each handler
// is a response, a function returning one, or an Error to throw; the calls are recorded by path.
function updateFetch(handlers) {
  const calls = [];
  const fetch = async (url, init) => {
    const address = String(url);
    const which = address.includes("api.github.com") ? "github" : address.endsWith("/health") ? "health" : address.endsWith("/update") ? "update" : "other";
    calls.push({ which, method: (init && init.method) || "GET", headers: (init && init.headers) || {} });
    const handler = handlers[which];
    if (handler === undefined) throw new TypeError(`NetworkError: ${address}`);
    const res = typeof handler === "function" ? handler() : handler;
    if (res instanceof Error) throw res;
    return res;
  };
  return { fetch, calls, count: (which) => calls.filter((c) => c.which === which).length };
}

const healthOf = (version, launcher) => jsonResponse(200, { ok: true, version, launcher, model: "large-v3", device: "cuda", compute_type: "float16" });
const github = () => jsonResponse(200, RELEASE);
// A check made 25 hours ago: stale by the day's rule.
const staleCheck = () => ({ updateCheck: { checkedAt: Date.now() - 25 * HOUR, latest: { version: "0.8.5", tag: "v0.8.5", url: null, xpi: null }, error: null } });
const freshCheck = () => ({ updateCheck: { checkedAt: Date.now() - HOUR, latest: LATEST, error: null } });

test("parseVersion reads three numbers and shrugs at the rest", () => {
  const { sandbox } = loadBackground();
  const cases = [
    ["0.9.0", [0, 9, 0]], ["v0.9.0", [0, 9, 0]], ["V1.2.3", [1, 2, 3]], ["0.10.0", [0, 10, 0]], ["0.9", [0, 9, 0]], ["2", [2, 0, 0]],
    ["", [0, 0, 0]], [undefined, [0, 0, 0]], [null, [0, 0, 0]], ["garbage", [0, 0, 0]], ["0.9.0-rc1", [0, 9, 0]], ["a.b.c", [0, 0, 0]],
    [" 1.2.3 ", [1, 2, 3]], ["1.2.3.4", [1, 2, 3]], ["-1.2.3", [0, 2, 3]],
  ];
  for (const [text, expected] of cases) assert.deepEqual(plain(sandbox.parseVersion(text)), expected, String(text));
});

test("compareVersions orders releases numerically, whatever the spelling", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.compareVersions("0.9.0", "0.8.0"), 1);
  assert.equal(sandbox.compareVersions("0.8.0", "0.9.0"), -1);
  assert.equal(sandbox.compareVersions("0.9.0", "v0.9.0"), 0);
  assert.equal(sandbox.compareVersions("0.10.0", "0.9.0"), 1, "numeric, not lexical");
  assert.equal(sandbox.compareVersions("1.0.0", "0.99.99"), 1);
  assert.equal(sandbox.compareVersions("0.9", "0.9.0"), 0);
  assert.equal(sandbox.compareVersions("0.9.1", "0.9"), 1);
  assert.equal(sandbox.compareVersions("garbage", "0.0.1"), -1);
  assert.equal(sandbox.compareVersions("", ""), 0);
  assert.equal(sandbox.compareVersions("0.9.0.1", "0.9.0"), 0, "a listed build is its release");
});

test("decideUpdate tells the server's case and the extension's apart", () => {
  const { sandbox } = loadBackground();
  const decide = (input) => plain(sandbox.decideUpdate(input));
  const latest = { version: "0.9.0" };
  assert.deepEqual(decide({ latest, serverVersion: "0.8.0", serverLauncher: true, extensionVersion: "0.9.0" }), { server: "newer", extension: "current" });
  assert.deepEqual(decide({ latest, serverVersion: "0.8.0", serverLauncher: false, extensionVersion: "0.9.0" }), { server: "cannot", extension: "current" });
  assert.deepEqual(decide({ latest, serverVersion: "0.9.0", serverLauncher: true, extensionVersion: "0.9.0" }), { server: "current", extension: "current" });
  assert.deepEqual(decide({ latest, serverVersion: "0.9.1", serverLauncher: false, extensionVersion: "0.9.0" }), { server: "current", extension: "current" }, "a server ahead of the release is current");
  assert.deepEqual(decide({ latest, serverVersion: null, serverLauncher: null, extensionVersion: "0.8.0" }), { server: "offline", extension: "newer" });
  assert.deepEqual(decide({ latest, serverVersion: "0.8.0", serverLauncher: true, extensionVersion: "0.8.0" }), { server: "newer", extension: "newer" });
  // The smoke fixture: online, but neither a version nor the launcher flag.
  assert.deepEqual(decide({ latest, serverVersion: null, serverLauncher: null, extensionVersion: "0.9.0", serverOnline: true }), { server: "unknown", extension: "current" });
  // A server from before the flag (0.8.0): behind the release, and no telling whether it can update itself.
  assert.deepEqual(decide({ latest, serverVersion: "0.8.0", serverLauncher: null, extensionVersion: "0.9.0" }), { server: "behind", extension: "current" });
  assert.deepEqual(decide({ latest, serverVersion: "0.9.0", serverLauncher: null, extensionVersion: "0.9.0" }), { server: "current", extension: "current" });
  // Nothing known about the newest release: nothing to say.
  assert.deepEqual(decide({ latest: null, serverVersion: "0.8.0", serverLauncher: true, extensionVersion: "0.8.0" }), { server: "unknown", extension: "current" });
  assert.deepEqual(decide({ latest: { version: "" }, serverVersion: "0.8.0", serverLauncher: true }), { server: "unknown", extension: "current" });
  assert.deepEqual(decide(undefined), { server: "unknown", extension: "current" });
  assert.deepEqual(decide({ latest: { version: "v0.10.0" }, serverVersion: "0.9.0", serverLauncher: true, extensionVersion: "0.9.0" }), { server: "newer", extension: "newer" });
  // The listing's build of a release is its number plus ".1" (scripts/amo-xpi.mjs listing): the
  // same release as the GitHub one, and behind the next.
  assert.deepEqual(decide({ latest, serverVersion: "0.9.0", serverLauncher: true, extensionVersion: "0.9.0.1" }), { server: "current", extension: "current" });
  assert.deepEqual(decide({ latest: { version: "0.9.1" }, serverVersion: "0.9.1", serverLauncher: true, extensionVersion: "0.9.0.1" }), { server: "current", extension: "newer" });
});

test("releaseFromApi reads the version, the page and the xpi off GitHub's answer", () => {
  const { sandbox } = loadBackground();
  assert.deepEqual(plain(sandbox.releaseFromApi(RELEASE)), LATEST);
  const noAssets = plain(sandbox.releaseFromApi({ tag_name: "0.9.1", html_url: RELEASE.html_url }));
  assert.deepEqual(noAssets, { version: "0.9.1", tag: "0.9.1", url: RELEASE.html_url, xpi: null });
  assert.equal(plain(sandbox.releaseFromApi({ tag_name: "v0.9.0", assets: [{ name: "only.zip", browser_download_url: "https://x/only.zip" }] })).xpi, null);
  // The unsigned stand-in of a release AMO has not signed yet is not the .xpi Firefox can install.
  const unsigned = { name: "shisu-ko-0.9.0-firefox-unsigned.xpi", browser_download_url: "https://x/shisu-ko-0.9.0-firefox-unsigned.xpi" };
  const signed = { name: "shisu_ko-0.9.0.xpi", browser_download_url: "https://x/shisu_ko-0.9.0.xpi" };
  assert.equal(plain(sandbox.releaseFromApi({ tag_name: "v0.9.0", assets: [unsigned] })).xpi, null);
  assert.equal(plain(sandbox.releaseFromApi({ tag_name: "v0.9.0", assets: [unsigned, signed] })).xpi, signed.browser_download_url);
  // No tag, no release; and a page that is not https is no page to open.
  assert.equal(sandbox.releaseFromApi({ html_url: RELEASE.html_url }), null);
  assert.equal(sandbox.releaseFromApi({ tag_name: "  " }), null);
  assert.equal(sandbox.releaseFromApi(null), null);
  assert.equal(sandbox.releaseFromApi("v0.9.0"), null);
  assert.equal(plain(sandbox.releaseFromApi({ tag_name: "v0.9.0", html_url: "javascript:alert(1)" })).url, null);
  assert.equal(plain(sandbox.releaseFromApi({ tag_name: "v0.9.0", assets: [{ name: "a.xpi", browser_download_url: "http://x/a.xpi" }] })).xpi, null);
});

test("checkForUpdate answers from a fresh store without asking GitHub", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ github });
  const { sandbox } = loadBackground({ storage, fetch: mock.fetch });
  const res = await sandbox.checkForUpdate();
  assert.deepEqual(plain(res.latest), LATEST);
  assert.equal(res.error, null);
  assert.equal(mock.count("github"), 0);
});

test("checkForUpdate asks GitHub when the store is a day old, or when forced, and stores the answer", async () => {
  const storage = makeMemoryStorage(staleCheck());
  const mock = updateFetch({ github });
  const { sandbox } = loadBackground({ storage, fetch: mock.fetch });
  assert.equal(sandbox.UPDATE_CHECK_MAX_AGE_MS, 24 * HOUR);
  const before = Date.now();
  const res = await sandbox.checkForUpdate();
  assert.equal(mock.count("github"), 1);
  assert.deepEqual(plain(mock.calls[0].headers), { Accept: "application/vnd.github+json" });
  assert.deepEqual(plain(res.latest), LATEST);
  assert.ok(res.checkedAt >= before && res.checkedAt <= Date.now());
  assert.deepEqual(plain((await storage.get("updateCheck")).updateCheck), plain(res));
  // Fresh now: the next call is served from the store, a forced one is not.
  await sandbox.checkForUpdate();
  assert.equal(mock.count("github"), 1);
  await sandbox.checkForUpdate({ force: true });
  assert.equal(mock.count("github"), 2);
  assert.equal(sandbox.GITHUB_LATEST_URL, "https://api.github.com/repos/Multysquid/shisu-ko/releases/latest");
});

test("checkForUpdate stores a failure as an error, keeps the last release seen, and never throws", async () => {
  const cases = [
    ["offline", new TypeError("NetworkError when attempting to fetch resource."), /could not reach GitHub/],
    ["rate limit", jsonResponse(403, { message: "API rate limit exceeded" }), /rate limit/],
    ["server error", jsonResponse(500, "boom"), /HTTP 500/],
    ["not JSON", jsonResponse(200, "<html>not json</html>"), /non-JSON/],
    ["no tag", jsonResponse(200, { message: "Moved Permanently" }), /names no release/],
  ];
  for (const [name, answer, pattern] of cases) {
    const storage = makeMemoryStorage(staleCheck());
    const mock = updateFetch({ github: answer });
    const { sandbox } = loadBackground({ storage, fetch: mock.fetch });
    const res = await sandbox.checkForUpdate();
    assert.match(res.error, pattern, name);
    assert.equal(res.latest.version, "0.8.5", `${name}: the last release seen stays`);
    assert.equal((await storage.get("updateCheck")).updateCheck.error, res.error, name);
    // A failed check is no check: the next call tries again.
    await sandbox.checkForUpdate();
    assert.equal(mock.count("github"), 2, name);
  }
});

test("checkForUpdate takes a 404 for no release published yet", async () => {
  const storage = makeMemoryStorage(staleCheck());
  const mock = updateFetch({ github: jsonResponse(404, { message: "Not Found" }) });
  const { sandbox } = loadBackground({ storage, fetch: mock.fetch });
  const res = await sandbox.checkForUpdate();
  assert.equal(res.error, null);
  assert.equal(res.latest, null);
});

test("checks that overlap share one request", async () => {
  const storage = makeMemoryStorage();
  const mock = updateFetch({ github });
  const { sandbox, dispatch } = loadBackground({ storage, fetch: mock.fetch });
  const [a, b] = await Promise.all([sandbox.checkForUpdate({ force: true }), dispatch({ type: "checkForUpdate", force: true })]);
  assert.deepEqual(plain(a), plain(b));
  assert.equal(mock.count("github"), 1);
});

test("the browser starting checks, sets the badge and notifies once per release", async () => {
  const storage = makeMemoryStorage(staleCheck());
  const mock = updateFetch({ github, health: healthOf("0.8.0", true) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await bg.startup();
  assert.equal(mock.count("github"), 1);
  assert.equal(mock.count("health"), 1);
  assert.deepEqual(plain(bg.badge), [["color", "#5b6fb8"], ["text", "1"]]);
  assert.equal(bg.notifications.length, 1);
  const shown = bg.notifications[0];
  assert.equal(shown.id, "shisuko-update");
  assert.equal(shown.type, "basic");
  assert.equal(shown.title, "Shisu-ko 0.9.0 is available");
  assert.equal(shown.message, "The server runs 0.8.0. Click to update it now.");
  assert.equal(shown.iconUrl, "moz-extension://test/icons/icon-128.png");
  // Once per release and browser session: the extension installed again, or the check run again,
  // says nothing more; a restarted event page in the same session neither.
  await bg.startup(true);
  await bg.startup();
  assert.equal(bg.notifications.length, 1);
  assert.equal(mock.count("github"), 1, "the day's check is served from the store");
  const restarted = loadBackground({ storage, session: bg.session, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await restarted.startup();
  assert.equal(restarted.notifications.length, 0);
  assert.deepEqual(plain(restarted.badge), [["color", "#5b6fb8"], ["text", "1"]]);
});

test("a server that cannot update itself, or none, gets the badge and no notification", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const docker = loadBackground({ storage, fetch: updateFetch({ health: healthOf("0.8.0", false) }).fetch, extensionVersion: "0.9.0" });
  await docker.startup();
  assert.deepEqual(plain(docker.badge.at(-1)), ["text", "1"]);
  assert.equal(docker.notifications.length, 0);
  // Offline: the launcher updates it at the next start, and the extension is current.
  const offline = loadBackground({ storage, fetch: updateFetch({}).fetch, extensionVersion: "0.9.0" });
  await offline.startup();
  assert.deepEqual(plain(offline.badge), [["text", ""]]);
  assert.equal(offline.notifications.length, 0);
  // Only the extension is behind: the badge, and nothing to click, since its update is AMO's.
  const extension = loadBackground({ storage, fetch: updateFetch({}).fetch, extensionVersion: "0.8.0" });
  await extension.startup();
  assert.deepEqual(plain(extension.badge.at(-1)), ["text", "1"]);
  assert.equal(extension.notifications.length, 0);
  // A 0.8.0 server, which reports its version but no launcher flag: the badge, and no
  // notification, since nothing says whether /update would work.
  const older = loadBackground({ storage, fetch: updateFetch({ health: jsonResponse(200, { ok: true, version: "0.8.0", model: "large-v3", device: "cuda", compute_type: "float16" }) }).fetch, extensionVersion: "0.9.0" });
  await older.startup();
  assert.deepEqual(plain(older.badge.at(-1)), ["text", "1"]);
  assert.equal(older.notifications.length, 0);
  assert.deepEqual(plain((await older.dispatch({ type: "updateStatus", health: { version: "0.8.0" } })).decision), { server: "behind", extension: "current" });
  // Nothing newer: the badge is cleared, in case an earlier check set it.
  const current = loadBackground({ storage, fetch: updateFetch({ health: healthOf("0.9.0", true) }).fetch, extensionVersion: "0.9.0" });
  await current.startup();
  assert.deepEqual(plain(current.badge), [["text", ""]]);
});

test("a profile that has never checked makes no request at install or start; the popup's check is its first", async () => {
  // A fresh profile, as the Chrome smoke test loads one: nothing may leave for api.github.com,
  // and nothing for the server either, until the popup asks.
  const storage = makeMemoryStorage();
  const mock = updateFetch({ github, health: healthOf("0.8.0", true) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await bg.startup(true);
  await bg.startup();
  assert.deepEqual(mock.calls, []);
  assert.deepEqual(plain(bg.badge), []);
  assert.equal(bg.notifications.length, 0);
  assert.equal((await storage.get("updateCheck")).updateCheck, undefined);
  // The popup's first question checks; from then on the browser's start does too.
  const opened = await bg.dispatch({ type: "updateStatus", health: null, check: true });
  assert.equal(mock.count("github"), 1);
  assert.equal(opened.latest.version, "0.9.0");
  const later = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await later.startup();
  assert.equal(mock.count("health"), 1);
  assert.deepEqual(plain(later.badge), [["color", "#5b6fb8"], ["text", "1"]]);
  assert.equal(later.notifications.length, 1);
  // A check the popup seeded (the smoke test's way of staying offline) counts as a check.
  const seeded = makeMemoryStorage({ updateCheck: { checkedAt: Date.now(), latest: null, error: null } });
  const quiet = loadBackground({ storage: seeded, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await quiet.startup(true);
  assert.equal(mock.count("github"), 1);
  assert.deepEqual(plain(quiet.badge), [["text", ""]]);
});

test("the start-up check survives a browser without the badge or the notification API", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ github, health: healthOf("0.8.0", true) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0", action: null, notifications: null });
  await bg.startup();
  const status = await bg.dispatch({ type: "updateStatus" });
  assert.deepEqual(plain(status.decision), { server: "newer", extension: "current" });
  assert.deepEqual(plain(bg.badge), []);
  assert.deepEqual(plain(bg.notifications), []);
});

test("updateStatus answers the popup with the release, the server, the verdict and the flags", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ github, health: healthOf("0.8.0", true) });
  const { dispatch } = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  // With the /health answer in the message the server is not asked again.
  const given = await dispatch({ type: "updateStatus", health: { version: "0.8.0", launcher: true }, check: true });
  assert.equal(mock.count("health"), 0);
  assert.equal(mock.count("github"), 0, "the store is fresh");
  assert.deepEqual(plain(given.latest), LATEST);
  assert.equal(typeof given.checkedAt, "number");
  assert.equal(given.error, null);
  assert.deepEqual(plain(given.server), { version: "0.8.0", launcher: true });
  assert.deepEqual(plain(given.decision), { server: "newer", extension: "current" });
  assert.equal(given.snoozed, null);
  assert.equal(given.updating, null);
  assert.equal(given.extensionVersion, "0.9.0");
  // Without one, /health is called; the smoke fixture's answer (no version, no flag) is unknown.
  const asked = await dispatch({ type: "updateStatus" });
  assert.equal(mock.count("health"), 1);
  assert.deepEqual(plain(asked.decision), { server: "newer", extension: "current" });
  const fixture = await dispatch({ type: "updateStatus", health: { model: "smoke", device: "cpu", compute_type: "test" } });
  assert.deepEqual(plain(fixture.server), { version: null, launcher: null });
  assert.deepEqual(plain(fixture.decision), { server: "unknown", extension: "current" });
  const offline = await dispatch({ type: "updateStatus", health: null });
  assert.equal(offline.server, null);
  assert.deepEqual(plain(offline.decision), { server: "offline", extension: "current" });
});

test("updateStatus runs the day's check when the popup opens and the store is stale", async () => {
  const storage = makeMemoryStorage(staleCheck());
  const mock = updateFetch({ github });
  const { dispatch } = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  const without = await dispatch({ type: "updateStatus", health: null });
  assert.equal(mock.count("github"), 0);
  assert.equal(without.latest.version, "0.8.5");
  const opened = await dispatch({ type: "updateStatus", health: null, check: true });
  assert.equal(mock.count("github"), 1);
  assert.equal(opened.latest.version, "0.9.0");
});

test("snoozeUpdate remembers the release for the browser session", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const { dispatch, session } = loadBackground({ storage, fetch: updateFetch({}).fetch });
  assert.deepEqual(plain(await dispatch({ type: "snoozeUpdate", version: "0.9.0" })), { ok: true });
  assert.equal((await session.get("updateSnoozed")).updateSnoozed, "0.9.0");
  assert.equal((await dispatch({ type: "updateStatus", health: null })).snoozed, "0.9.0");
  assert.equal((await dispatch({ type: "snoozeUpdate" })).ok, false);
});

test("updateServer posts /update, remembers the request for the popup and answers with the versions", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ health: healthOf("0.8.0", true), update: jsonResponse(200, { ok: true, restarting: true, version: "0.8.0" }) });
  const { sandbox, dispatch, session } = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  assert.equal(sandbox.UPDATE_WINDOW_MS, 120000);
  const before = Date.now();
  const res = await dispatch({ type: "updateServer" });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.restarting, true);
  assert.equal(res.from, "0.8.0");
  assert.equal(res.to, "0.9.0");
  assert.ok(res.requestedAt >= before && res.deadline === res.requestedAt + 120000);
  const posted = mock.calls.find((c) => c.which === "update");
  assert.equal(posted.method, "POST");
  const stored = (await session.get("serverUpdate")).serverUpdate;
  assert.deepEqual(plain(stored), { requestedAt: res.requestedAt, from: "0.8.0", to: "0.9.0", deadline: res.deadline, down: false });
  assert.deepEqual(plain((await dispatch({ type: "updateStatus", health: null })).updating), plain(stored));
  // A second request while the restart is under way is answered from the record, not posted.
  const again = await sandbox.updateServer();
  assert.equal(again.already, true);
  assert.equal(again.deadline, res.deadline);
  assert.equal(mock.count("update"), 1);
});

test("updateServer passes the server's refusal through, and says when it is unreachable", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const refused = updateFetch({ health: healthOf("0.8.0", false), update: jsonResponse(409, { ok: false, error: "not started by run.cmd / run.sh" }) });
  const bg = loadBackground({ storage, fetch: refused.fetch, extensionVersion: "0.9.0" });
  const res = await bg.sandbox.updateServer();
  assert.deepEqual(plain(res), { ok: false, error: "not started by run.cmd / run.sh", refused: true, offline: false });
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate, undefined, "a refused request leaves no record");
  const gone = loadBackground({ storage, fetch: updateFetch({}).fetch, extensionVersion: "0.9.0" });
  const offline = await gone.sandbox.updateServer();
  assert.equal(offline.ok, false);
  assert.equal(offline.offline, true);
  assert.equal(offline.refused, false);
  // A server that already runs the release is not restarted for nothing.
  const done = loadBackground({ storage, fetch: updateFetch({ health: healthOf("0.9.0", true), update: jsonResponse(200, { ok: true, restarting: true }) }).fetch });
  const skip = await done.sandbox.updateServer();
  assert.equal(skip.ok, false);
  assert.match(skip.error, /already runs 0\.9\.0/);
});

test("the popup's /health polls tell the background how the update went", async () => {
  const storage = makeMemoryStorage(freshCheck());
  let health = healthOf("0.8.0", true);
  const mock = updateFetch({ health: () => health, update: jsonResponse(200, { ok: true, restarting: true }) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await bg.sandbox.updateServer();
  bg.badge.length = 0;
  // The old server still answers for a moment: nothing changes.
  await bg.sandbox.apiRequest("/health");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate.down, false);
  // Then nobody does: the restart is under way.
  health = new TypeError("NetworkError");
  await bg.sandbox.apiRequest("/health");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate.down, true);
  // The new version answers: the record ends, the badge goes, and the viewer is told.
  health = healthOf("0.9.0", true);
  await bg.sandbox.apiRequest("/health");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate, null);
  assert.deepEqual(plain(bg.badge), [["text", ""]]);
  const told = bg.notifications.find((n) => n.id === "shisuko-updated");
  assert.equal(told.title, "Shisu-ko updated to 0.9.0");
  assert.equal((await bg.dispatch({ type: "updateStatus", health: { version: "0.9.0", launcher: true } })).updating, null);
});

test("the old version back after the restart ends the record without a word", async () => {
  const storage = makeMemoryStorage(freshCheck());
  let health = healthOf("0.8.0", true);
  const mock = updateFetch({ health: () => health, update: jsonResponse(200, { ok: true, restarting: true }) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await bg.sandbox.updateServer();
  health = new TypeError("NetworkError");
  await bg.sandbox.apiRequest("/health");
  health = healthOf("0.8.0", true);
  await bg.sandbox.apiRequest("/health");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate, null);
  assert.ok(!bg.notifications.some((n) => n.id === "shisuko-updated"));
  // The banner is back: the next request is posted again.
  await bg.sandbox.updateServer();
  assert.equal(mock.count("update"), 2);
});

test("the old version long after the request ends the record even when no poll saw the server down", async () => {
  // The popup closed with the click, so nobody polled while the server restarted; update.py could
  // not update and run.cmd brought 0.8.0 back. The first poll after that is a reopened popup's.
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ health: healthOf("0.8.0", true), update: jsonResponse(200, { ok: true, restarting: true }) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  const t0 = Date.now();
  bg.setNow(t0);
  await bg.sandbox.updateServer();
  // Within the shutdown allowance the old server may still be answering: the record stays, and a
  // second request is answered from it.
  bg.setNow(t0 + 5000);
  await bg.sandbox.apiRequest("/health");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate.down, false);
  assert.equal((await bg.sandbox.updateServer()).already, true);
  assert.equal(mock.count("update"), 1);
  // Past it the old version is the restarted server: the record ends without a word, the popup
  // is not told to resume anything, and its Update click is posted again.
  bg.setNow(t0 + 11000);
  await bg.sandbox.apiRequest("/health");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate, null);
  assert.ok(!bg.notifications.some((n) => n.id === "shisuko-updated"));
  assert.equal((await bg.dispatch({ type: "updateStatus", health: healthOf("0.8.0", true).data })).updating, null);
  const again = await bg.sandbox.updateServer();
  assert.equal(again.already, undefined);
  assert.equal(again.restarting, true);
  assert.equal(mock.count("update"), 2);
  // The notification path polls nothing between clicks: a click alone, long after, posts too.
  bg.setNow(t0 + 40000);
  assert.equal((await bg.sandbox.updateServer()).already, undefined);
  assert.equal(mock.count("update"), 3);
});

test("the update record outlives the event page", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ health: healthOf("0.8.0", true), update: jsonResponse(200, { ok: true, restarting: true }) });
  const first = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  const res = await first.sandbox.updateServer();
  const restarted = loadBackground({ storage, session: first.session, fetch: mock.fetch, extensionVersion: "0.9.0" });
  const status = await restarted.dispatch({ type: "updateStatus", health: null });
  assert.equal(status.updating.deadline, res.deadline);
  assert.equal((await restarted.sandbox.updateServer()).already, true);
  assert.equal(mock.count("update"), 1);
  // Past its window the record is spent.
  await first.session.set({ serverUpdate: { ...plain(res), deadline: Date.now() - 1 } });
  const later = loadBackground({ storage, session: first.session, fetch: mock.fetch, extensionVersion: "0.9.0" });
  assert.equal((await later.dispatch({ type: "updateStatus", health: null })).updating, null);
});

test("clicking the notification updates the server and the background follows it to the end", async () => {
  const storage = makeMemoryStorage(freshCheck());
  let health = healthOf("0.8.0", true);
  const mock = updateFetch({ health: () => health, update: () => {
    health = healthOf("0.9.0", true); // the launcher's restart, seen by the very next poll
    return jsonResponse(200, { ok: true, restarting: true });
  } });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0", ...instantTimers });
  await bg.clickNotification("some-other-notification");
  assert.equal(mock.count("update"), 0);
  await bg.clickNotification("shisuko-update");
  assert.equal(mock.count("update"), 1);
  await settle();
  assert.ok(bg.notifications.some((n) => n.id === "shisuko-update" && n.cleared), "the clicked notification is cleared");
  assert.ok(bg.notifications.some((n) => n.id === "shisuko-updated"), "the background polled /health and saw the new version");
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate, null);
});

// The click asked for something: unlike a check, a request that did not get through must not
// vanish without a word, since no popup is open to show the result.
test("a notification click whose request fails says so in a notification of its own", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const cases = [
    // The server stopped between the notification and the click.
    { handlers: {}, error: "Server unreachable" },
    // Restarted by hand without the launcher since the notification.
    { handlers: { health: healthOf("0.8.0", false), update: jsonResponse(409, { ok: false, error: "the server was not started by run.cmd / run.sh" }) }, error: "the server was not started by run.cmd / run.sh" },
  ];
  for (const { handlers, error } of cases) {
    const mock = updateFetch(handlers);
    const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0", ...instantTimers });
    await bg.clickNotification("shisuko-update");
    await settle();
    const failed = bg.notifications.filter((n) => n.id === "shisuko-update-failed");
    assert.equal(failed.length, 1, error);
    assert.equal(failed[0].title, "Shisu-ko could not update the server");
    assert.equal(failed[0].message, error);
    assert.ok(!bg.notifications.some((n) => n.id === "shisuko-updated"));
    assert.equal((await bg.session.get("serverUpdate")).serverUpdate, undefined);
    // Its own id: a click on the failure posts nothing.
    const posted = mock.count("update");
    await bg.clickNotification("shisuko-update-failed");
    assert.equal(mock.count("update"), posted);
  }
  // The popup's request reports the same failure on its own line; no notification for it.
  const popup = loadBackground({ storage, fetch: updateFetch({}).fetch, extensionVersion: "0.9.0" });
  assert.equal((await popup.dispatch({ type: "updateServer" })).ok, false);
  assert.equal(popup.notifications.length, 0);
});

// The viewer restarted run.cmd by hand (which updates) while the notification sat there, then
// clicked it: nothing was posted and nothing failed, so "could not update" would be a lie.
test("a notification click on a server updated another way clears it and says nothing", async () => {
  const storage = makeMemoryStorage(freshCheck());
  const mock = updateFetch({ health: healthOf("0.9.0", true), update: jsonResponse(200, { ok: true, restarting: true }) });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0", ...instantTimers });
  await bg.clickNotification("shisuko-update");
  await settle();
  assert.equal(mock.count("update"), 0, "nothing to post");
  assert.ok(bg.notifications.some((n) => n.id === "shisuko-update" && n.cleared), "the stale notification is cleared");
  assert.ok(!bg.notifications.some((n) => n.id === "shisuko-update-failed"), "nothing failed");
  assert.ok(!bg.notifications.some((n) => n.id === "shisuko-updated"));
  assert.equal((await bg.session.get("serverUpdate")).serverUpdate, undefined);
  // The popup's own request is told the same, marked so the answer is not a refusal.
  const res = await bg.dispatch({ type: "updateServer" });
  assert.deepEqual(plain(res), { ok: false, upToDate: true, error: "the server already runs 0.9.0" });
});

test("the popup's status clears the notification once the server is seen at the release", async () => {
  const storage = makeMemoryStorage(staleCheck());
  let health = healthOf("0.8.0", true);
  const mock = updateFetch({ github, health: () => health });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  await bg.startup();
  assert.equal(bg.notifications.filter((n) => n.id === "shisuko-update").length, 1);
  // The server is still behind: the offer stands.
  await bg.dispatch({ type: "updateStatus", health: { version: "0.8.0", launcher: true } });
  assert.ok(!bg.notifications.some((n) => n.cleared));
  // Offline: it may come back at the old version, so the offer stands too.
  await bg.dispatch({ type: "updateStatus", health: null });
  assert.ok(!bg.notifications.some((n) => n.cleared));
  // Restarted by hand at the release: the offer is withdrawn.
  await bg.dispatch({ type: "updateStatus", health: { version: "0.9.0", launcher: true } });
  assert.ok(bg.notifications.some((n) => n.id === "shisuko-update" && n.cleared));
});

// Between the old server's exit and the new one's port the native host sees neither /health nor
// the instance lock, and would launch run.cmd a second time: two update.py runs on one folder.
test("startServer is refused while an update is under way, and startServerStatus carries the record", async () => {
  const storage = makeMemoryStorage(freshCheck());
  let health = healthOf("0.8.0", true);
  const mock = updateFetch({ health: () => health, update: jsonResponse(200, { ok: true, restarting: true }) });
  const host = nativeHost({ ok: true, started: true, log: null });
  const bg = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0", sendNativeMessage: host.sendNativeMessage });
  const res = await bg.sandbox.updateServer();
  assert.equal(res.restarting, true);
  const record = (await bg.session.get("serverUpdate")).serverUpdate;
  assert.deepEqual(plain(await bg.dispatch({ type: "startServerStatus" })), { starting: false, updating: plain(record) });
  const refused = await bg.dispatch({ type: "startServer" });
  assert.equal(refused.ok, false);
  assert.equal(refused.error, "an update is under way");
  assert.match(refused.hint, /launcher restarts the server itself/);
  assert.equal(host.calls.length, 0, "the host was not asked");
  // A restarted event page knows the record from storage.session and refuses too.
  const restarted = loadBackground({ storage, session: bg.session, fetch: mock.fetch, extensionVersion: "0.9.0", sendNativeMessage: host.sendNativeMessage });
  assert.equal((await restarted.dispatch({ type: "startServer" })).error, "an update is under way");
  assert.equal(host.calls.length, 0);
  // The new version ends the record: a start reaches the host again, and the status says nothing of an update.
  health = healthOf("0.9.0", true);
  await bg.sandbox.apiRequest("/health");
  assert.deepEqual(plain(await bg.dispatch({ type: "startServerStatus" })), { starting: false });
  assert.equal((await bg.dispatch({ type: "startServer" })).ok, true);
  assert.equal(host.calls.length, 1);
});

test("the message switch routes the four update messages", async () => {
  const storage = makeMemoryStorage(staleCheck());
  const mock = updateFetch({ github, health: healthOf("0.8.0", true), update: jsonResponse(200, { ok: true, restarting: true }) });
  const { dispatch } = loadBackground({ storage, fetch: mock.fetch, extensionVersion: "0.9.0" });
  assert.equal((await dispatch({ type: "checkForUpdate", force: true })).latest.version, "0.9.0");
  assert.equal(mock.count("github"), 1);
  assert.deepEqual(plain((await dispatch({ type: "updateStatus", health: null })).decision), { server: "offline", extension: "current" });
  assert.equal((await dispatch({ type: "snoozeUpdate", version: "0.9.0" })).ok, true);
  assert.equal((await dispatch({ type: "updateServer" })).restarting, true);
  assert.equal(mock.count("update"), 1);
});

// ------------------------------------------------------------------ word colours

const SHISUKO_WORDS = require("../words");

// The five searches the deck index runs, by the clause after the deck; a fake AnkiConnect answers
// each with the note ids a test puts in that set. A refresh adds a sixth, `edited:<days>`,
// answered from `sets.edited`.
const STATUS_CLAUSES = {
  "is:suspended": "suspended",
  "-is:suspended": "unsuspended",
  "is:new -is:suspended": "new",
  "is:learn -is:suspended": "learning",
  "is:review -is:learn -is:suspended": "review",
};

// A note as notesInfo lists it, with the fields of a Yomitan card and a modification time.
function note(id, word, extra, mod) {
  const fields = { Expression: { value: word, order: 0 }, Sentence: { value: `${word}です`, order: 1 } };
  let order = 2;
  for (const [name, value] of Object.entries(extra || {})) fields[name] = { value, order: order++ };
  return { noteId: id, fields, mod: mod === undefined ? 1700000000 + id : mod };
}

// One deck as AnkiConnect sees it: `sets` holds the note ids each search lists (suspended and
// unsuspended together being the deck), `notes` what notesInfo says about each id, and
// notesModTime what each note's `mod` is.
function deckHandlers(deck, sets, notes, extra) {
  return {
    requestPermission: granted,
    findNotes: (p) => {
      const found = /^"deck:(.+)" (.+)$/.exec(p.query);
      if (!found || found[1] !== deck) return [];
      if (/^edited:\d+$/.test(found[2])) return sets.edited || [];
      return found[2] in STATUS_CLAUSES ? sets[STATUS_CLAUSES[found[2]]] || [] : [];
    },
    notesInfo: (p) => p.notes.map((id) => (notes[id] ? notes[id] : {})),
    notesModTime: (p) => p.notes.map((id) => (notes[id] ? { noteId: id, mod: notes[id].mod } : {})),
    ...extra,
  };
}

const queriesAsked = (anki) => anki.calls.filter((c) => c.action === "findNotes").map((c) => c.params.query);
const notesAsked = (anki) => anki.calls.filter((c) => c.action === "notesInfo").map((c) => plain(c.params.notes));
const modTimesAsked = (anki) => anki.calls.filter((c) => c.action === "notesModTime").map((c) => plain(c.params.notes));
const sortedEntries = (entries) => plain(entries).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));

// A deck with a note in every state, a duplicate word and an empty one.
const DECK = "Japanese::Mining";
const DECK_SETS = { suspended: [5], unsuspended: [1, 2, 3, 4, 6, 8, 9], new: [2, 3, 9], learning: [4], review: [1] };
const DECK_NOTES = {
  1: note(1, "日本語"),
  2: note(2, "猫", { Reading: "ねこ", PitchAccent: "[1]" }),
  3: note(3, "橋", { Reading: "はし", PitchAccent: "[2]" }),
  4: note(4, "字幕"),
  5: note(5, "食べる"),
  6: note(6, "走る"), // unsuspended yet in none of the three sets: statusOf's fallback
  8: note(8, ""),
  9: note(9, "日本語"), // the same word as note 1, on a new card
};
const DECK_ENTRIES = sortedEntries([
  ["日本語", "learned", null],
  ["猫", "new", "atamadaka"],
  ["橋", "new", "odaka"],
  ["字幕", "learning", null],
  ["食べる", "suspended", null],
  ["走る", "learning", null],
  ["日本語", "new", null],
]);

const colourSettings = (patch) => makeMemoryStorage({ settings: { cardStatus: true, cardStatusDeck: DECK, ...patch } });

test("deckSearch quotes the deck name whole and escapes what Anki reads as syntax", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.deckSearch("My Deck::Sub"), '"deck:My Deck::Sub"');
  assert.equal(sandbox.deckSearch('a"b*c_d\\e'), '"deck:a\\"b\\*c\\_d\\\\e"');
});

test("wordColoursOn is either feature", () => {
  const { sandbox } = loadBackground();
  assert.equal(sandbox.wordColoursOn({ cardStatus: false, pitchAccent: false }), false);
  assert.equal(sandbox.wordColoursOn({ cardStatus: true, pitchAccent: false }), true);
  assert.equal(sandbox.wordColoursOn({ cardStatus: false, pitchAccent: true }), true);
});

test("resolveDeck takes the popup's deck, else the last mined card's, else none", async () => {
  const seen = { ankiDeckSeen: { deck: "Seen::Deck", at: 1, noteId: 7 } };
  const manual = loadBackground({ storage: makeMemoryStorage({ settings: { cardStatusDeck: " Mine " }, ...seen }) });
  assert.deepEqual(plain(await manual.sandbox.resolveDeck(await manual.sandbox.getSettings())), { deck: "Mine", automatic: false });
  const automatic = loadBackground({ storage: makeMemoryStorage(seen) });
  assert.deepEqual(plain(await automatic.sandbox.resolveDeck(await automatic.sandbox.getSettings())), { deck: "Seen::Deck", automatic: true });
  const none = loadBackground();
  assert.deepEqual(plain(await none.sandbox.resolveDeck(await none.sandbox.getSettings())), { deck: null, automatic: true });
});

test("cardStatus answers 'off' without touching Anki while both features are off", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const { sandbox } = loadBackground({ fetch: anki.fetch });
  assert.deepEqual(plain(await sandbox.cardStatus({})), { ok: false, reason: "off" });
  assert.equal(anki.calls.length, 0);
});

test("cardStatus asks for no deck when nothing was mined and none is chosen", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const { sandbox } = loadBackground({ storage: makeMemoryStorage({ settings: { pitchAccent: true } }), fetch: anki.fetch });
  const res = await sandbox.cardStatus({});
  assert.equal(res.ok, false);
  assert.equal(res.reason, "noDeck");
  assert.match(res.error, /pick a deck/);
  assert.equal(anki.calls.length, 0);
});

test("cardStatus reports Anki's refusal and Anki being away", async () => {
  const denied = ankiFetch({ requestPermission: { permission: "denied" } });
  const refused = loadBackground({ storage: colourSettings(), fetch: denied.fetch });
  assert.deepEqual(plain(await refused.sandbox.cardStatus({})), { ok: false, reason: "denied", error: "AnkiConnect denied access. Click Yes in Anki's permission dialog." });
  assert.deepEqual(denied.actions(), ["requestPermission"]);

  const away = loadBackground({
    storage: colourSettings(),
    fetch: async () => {
      throw new TypeError("fetch failed");
    },
  });
  assert.deepEqual(plain(await away.sandbox.cardStatus({})), { ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed" });

  const broken = ankiFetch({
    requestPermission: granted,
    findNotes: () => {
      throw new Error("collection is not available");
    },
  });
  const failed = loadBackground({ storage: colourSettings(), fetch: broken.fetch });
  assert.deepEqual(plain(await failed.sandbox.cardStatus({})), { ok: false, reason: "error", error: "collection is not available" });
});

test("cardStatus runs the five searches on the deck and reads every note once", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const res = await sandbox.cardStatus({ since: 0 });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.deck, DECK);
  assert.equal(res.automatic, false);
  assert.equal(typeof res.at, "number");
  assert.deepEqual(queriesAsked(anki), [
    '"deck:Japanese::Mining" is:suspended',
    '"deck:Japanese::Mining" -is:suspended',
    '"deck:Japanese::Mining" is:new -is:suspended',
    '"deck:Japanese::Mining" is:learn -is:suspended',
    '"deck:Japanese::Mining" is:review -is:learn -is:suspended',
  ]);
  // Every note of the deck, in one call, none twice, and by id whatever order Anki listed them in.
  assert.deepEqual(notesAsked(anki), [[1, 2, 3, 4, 5, 6, 8, 9]]);
  assert.deepEqual(sortedEntries(res.entries), DECK_ENTRIES);
  // The content script merges the two cards of 日本語 to the one with the least progress.
  const index = SHISUKO_WORDS.buildIndex(plain(res.entries));
  assert.equal(index.exact.get("日本語").status, "new");
  assert.equal(index.exact.get("橋").pitch, "odaka");
});

test("cardStatus reads the word and the pitch from the fields the viewer named", async () => {
  // The named pitch field wins over the one whose name says "Accent"; [2] on a two-mora reading is odaka.
  const notes = { 1: note(1, "<b>猫</b>", { Reading: "ねこ", Accent: "[0]", Pitch: "[2]" }) };
  const anki = ankiFetch(deckHandlers(DECK, { suspended: [], unsuspended: [1], new: [1], learning: [], review: [] }, notes));
  const { sandbox } = loadBackground({ storage: colourSettings({ ankiWordField: "Reading", ankiPitchField: "Pitch" }), fetch: anki.fetch });
  const res = await sandbox.cardStatus({});
  assert.deepEqual(plain(res.entries), [["ねこ", "new", "odaka"]]);
});

test("cardStatus answers a second ask from memory, and the same index as 'unchanged'", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({ since: 0 });
  const calls = anki.calls.length;
  const again = await sandbox.cardStatus({});
  assert.equal(again.at, first.at);
  assert.deepEqual(plain(again.entries), plain(first.entries));
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.equal(anki.calls.length, calls, "an index this fresh asks Anki nothing");
});

test("a refresh after the time to live runs the searches again but asks only about new notes", async () => {
  const sets = { suspended: [5], unsuspended: [1, 2, 3, 4, 6, 8, 9], new: [2, 3, 9], learning: [4], review: [1] };
  const notes = { ...DECK_NOTES, 10: note(10, "学校") };
  const anki = ankiFetch(deckHandlers(DECK, sets, notes));
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({});
  // The viewer reviewed 字幕 to learned, suspended 日本語's new card, and added 学校 meanwhile.
  sets.learning = [];
  sets.review = [1, 4];
  sets.suspended = [5, 9];
  sets.unsuspended = [1, 2, 3, 4, 6, 8, 10];
  sets.new = [2, 3, 10];
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS);
  const res = await sandbox.cardStatus({ since: first.at });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.notEqual(res.at, first.at);
  // The five searches again, and a sixth for the notes edited since the last look: one day
  // more than the half minute elapsed, since Anki counts days back from the coming rollover.
  assert.equal(queriesAsked(anki).length, 11);
  assert.equal(queriesAsked(anki)[10], '"deck:Japanese::Mining" edited:2');
  assert.deepEqual(notesAsked(anki), [[1, 2, 3, 4, 5, 6, 8, 9], [10]]);
  assert.deepEqual(modTimesAsked(anki), [], "nothing was edited, so no modification time was asked for");
  assert.deepEqual(
    sortedEntries(res.entries),
    sortedEntries([
      ["日本語", "learned", null],
      ["猫", "new", "atamadaka"],
      ["橋", "new", "odaka"],
      ["字幕", "learned", null],
      ["食べる", "suspended", null],
      ["走る", "learning", null],
      ["日本語", "suspended", null],
      ["学校", "new", null],
    ])
  );
});

test("notes are read in chunks, and a chunk Anki complains about costs those notes alone", async () => {
  const ids = [];
  const notes = {};
  for (let id = 1; id <= 450; id++) {
    ids.push(id);
    notes[id] = note(id, `語${id}`);
  }
  const handlers = deckHandlers(DECK, { suspended: [], unsuspended: ids, new: ids, learning: [], review: [] }, notes);
  const anki = ankiFetch({
    ...handlers,
    notesInfo: (p) => {
      if (p.notes.includes(201)) throw new Error("note was not found: 201");
      return handlers.notesInfo(p);
    },
  });
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const res = await sandbox.cardStatus({});
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.deepEqual(notesAsked(anki).map((chunk) => chunk.length), [200, 200, 50]);
  assert.equal(res.entries.length, 250);
});

test("a refresh that fails answers the old index as stale, and nothing when there is none", async () => {
  let away = false;
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const fetch = async (url, init) => {
    if (away) throw new TypeError("fetch failed");
    return anki.fetch(url, init);
  };
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch });
  const first = await sandbox.cardStatus({});
  away = true;
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS + 1);
  const stale = await sandbox.cardStatus({});
  assert.equal(stale.ok, true, JSON.stringify(stale));
  assert.equal(stale.stale, true);
  assert.equal(stale.at, first.at);
  assert.deepEqual(plain(stale.entries), plain(first.entries));
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK, stale: true });
  // Anki back: the next ask is a full refresh again, which finds the deck as it was, so the tab
  // holding that index hears "unchanged", no longer stale.
  away = false;
  const searches = queriesAsked(anki).length;
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.equal(queriesAsked(anki).length, searches + 6, "the searches ran again");

  // An empty deck leaves nothing worth answering: the failure it is.
  const empty = ankiFetch(deckHandlers("Empty", { suspended: [], unsuspended: [], new: [], learning: [], review: [] }, {}));
  let gone = false;
  const bare = loadBackground({
    storage: colourSettings({ cardStatusDeck: "Empty" }),
    fetch: async (url, init) => {
      if (gone) throw new TypeError("fetch failed");
      return empty.fetch(url, init);
    },
  });
  const none = await bare.sandbox.cardStatus({});
  assert.deepEqual(plain(none.entries), []);
  gone = true;
  bare.setNow(none.at + bare.sandbox.CARD_STATUS_TTL_MS + 1);
  assert.deepEqual(plain(await bare.sandbox.cardStatus({})), { ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed" });
});

test("asks that overlap share one fetch", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  sandbox.ankiWatch.permission = "granted"; // the first permission check is one request at a time; this is about the index
  const [a, b] = await Promise.all([sandbox.cardStatus({}), sandbox.cardStatus({})]);
  assert.equal(a.at, b.at);
  assert.equal(queriesAsked(anki).length, 5);
  assert.equal(notesAsked(anki).length, 1);
});

test("another deck, or another field to read, drops the index; other settings leave it", async () => {
  const other = { suspended: [], unsuspended: [20], new: [], learning: [], review: [20] };
  const mine = deckHandlers(DECK, DECK_SETS, DECK_NOTES);
  const theirs = deckHandlers("Other", other, { 20: note(20, "本") });
  const anki = ankiFetch({
    ...mine,
    findNotes: (p) => (p.query.startsWith('"deck:Other"') ? theirs.findNotes(p) : mine.findNotes(p)),
    notesInfo: (p) => (p.notes.includes(20) ? theirs.notesInfo(p) : mine.notesInfo(p)),
  });
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({});
  assert.equal(first.deck, DECK);

  await sandbox.saveSettings({ fontScale: 1.2 });
  assert.equal((await sandbox.cardStatus({})).at, first.at, "a setting the index does not depend on");
  assert.equal(queriesAsked(anki).length, 5);
  // The viewer's known words and the katakana switch are the content script's: the index is the
  // deck's alone, and every tab folds the list in on its own.
  await sandbox.saveSettings({ knownWords: "日本語", katakanaKnown: true });
  assert.equal((await sandbox.cardStatus({})).at, first.at);
  assert.equal(queriesAsked(anki).length, 5);

  await sandbox.saveSettings({ cardStatusDeck: "Other" });
  const switched = await sandbox.cardStatus({});
  assert.equal(switched.deck, "Other");
  assert.deepEqual(plain(switched.entries), [["本", "learned", null]]);
  assert.equal(queriesAsked(anki).length, 10);
  assert.ok(queriesAsked(anki).slice(5).every((q) => q.startsWith('"deck:Other"')));

  await sandbox.saveSettings({ ankiWordField: "Reading" });
  await sandbox.cardStatus({});
  assert.equal(queriesAsked(anki).length, 15, "a new word field means every note is read again");
  assert.deepEqual(notesAsked(anki).slice(-1), [[20]]);

  await sandbox.saveSettings({ ankiPitchField: "Accent" });
  await sandbox.cardStatus({});
  assert.equal(queriesAsked(anki).length, 20);
});

test("a mine remembers the deck the card went to and expires the index, so the card shows at once", async () => {
  const sets = { ...DECK_SETS };
  const mock = miningFetch({
    ...ankiOk,
    ...deckHandlers(DECK, sets, DECK_NOTES, { notesInfo: (p) => (p.notes.includes(555) ? noteFields("文0") : p.notes.map((id) => DECK_NOTES[id] || {})) }),
    findCards: (p) => (p.query === "nid:555" ? [9001, 9002, 9003] : []),
    getDecks: { Default: [9003], "Japanese::Mining": [9001, 9002] },
  });
  const storage = makeMemoryStorage({ settings: { cardStatus: true } });
  const { sandbox, storage: store, dispatch } = loadBackground({ storage, fetch: mock.fetch, ...instantTimers });
  assert.equal((await sandbox.cardStatus({})).reason, "noDeck");

  const res = await dispatch(mineMsg(0), 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  await settle();
  const seen = store._dump()[sandbox.DECK_SEEN_KEY];
  assert.equal(seen.deck, "Japanese::Mining");
  assert.equal(seen.noteId, 555);
  assert.equal(typeof seen.at, "number");
  const findCards = mock.calls.find((c) => c.action === "findCards");
  assert.deepEqual(plain(findCards.params), { query: "nid:555" });
  const getDecks = mock.calls.find((c) => c.action === "getDecks");
  assert.deepEqual(plain(getDecks.params), { cards: [9001, 9002, 9003] });

  // Automatic now means that deck; the card just filled is looked up straight away.
  const coloured = await sandbox.cardStatus({});
  assert.equal(coloured.ok, true, JSON.stringify(coloured));
  assert.equal(coloured.deck, "Japanese::Mining");
  assert.equal(coloured.automatic, true);
  const before = mock.calls.filter((c) => c.action === "findNotes").length;
  // Yomitan's new card, the next mine's target, is in the deck by the time the index is asked again.
  sets.unsuspended = [...DECK_SETS.unsuspended, 555];
  sets.new = [...DECK_SETS.new, 555];
  assert.equal((await dispatch(mineMsg(0), 1)).ok, true);
  await settle();
  const read = mock.calls.filter((c) => c.action === "notesInfo").length; // the mine's own read of the card
  await sandbox.cardStatus({});
  assert.equal(mock.calls.filter((c) => c.action === "findNotes").length, before + 6, "the searches ran again after the mine");
  // Expired, not forgotten: only the card just made is read, not the whole deck again.
  const after = mock.calls.filter((c) => c.action === "notesInfo").slice(read);
  assert.deepEqual(after.map((c) => plain(c.params.notes)), [[555]]);
});

test("a mine whose deck cannot be told still succeeds and remembers nothing", async () => {
  const mock = miningFetch({
    ...ankiOk,
    findCards: () => {
      throw new Error("collection is not available");
    },
  });
  const { storage, dispatch } = loadBackground({ fetch: mock.fetch, ...instantTimers });
  const res = await dispatch(mineMsg(0), 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  await settle();
  assert.equal(storage._dump().ankiDeckSeen, undefined);
});

test("ankiDecks lists the decks sorted with the last mined card's, and says why it cannot", async () => {
  const seen = { ankiDeckSeen: { deck: "Japanese::Mining", at: 1, noteId: 555 } };
  const anki = ankiFetch({ requestPermission: granted, deckNames: ["Japanese::Mining", "Default", "English"] });
  const listed = loadBackground({ storage: makeMemoryStorage(seen), fetch: anki.fetch });
  assert.deepEqual(plain(await listed.sandbox.ankiDecks()), { ok: true, decks: ["Default", "English", "Japanese::Mining"], seen: "Japanese::Mining" });

  const none = loadBackground({ fetch: anki.fetch });
  assert.deepEqual(plain(await none.sandbox.ankiDecks()), { ok: true, decks: ["Default", "English", "Japanese::Mining"], seen: null });

  const away = loadBackground({
    storage: makeMemoryStorage(seen),
    fetch: async () => {
      throw new TypeError("fetch failed");
    },
  });
  assert.deepEqual(plain(await away.sandbox.ankiDecks()), { ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed", seen: "Japanese::Mining" });

  const denied = ankiFetch({ requestPermission: { permission: "denied" } });
  const refused = loadBackground({ fetch: denied.fetch });
  const res = await refused.sandbox.ankiDecks();
  assert.equal(res.ok, false);
  assert.equal(res.reason, "denied");
  assert.equal(res.seen, null);
});

test("one permission dialog serves every caller that arrives while it is up", async () => {
  // Anki holds its dialog until the viewer clicks: the popup asks for the deck list, and a tab
  // asks for the deck's words meanwhile. Both wait for the one answer; nobody gets "denied" from
  // the minute's recheck and no second dialog is requested.
  let answerDialog;
  const dialog = new Promise((resolve) => {
    answerDialog = resolve;
  });
  // ankiFetch answers at once; the dialog is the one request that waits.
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES, { deckNames: ["Japanese::Mining"] }));
  const calls = [];
  const fetch = async (url, init) => {
    const action = JSON.parse(init.body).action;
    calls.push(action);
    if (action === "requestPermission") return { json: async () => ({ result: await dialog, error: null }) };
    return anki.fetch(url, init);
  };
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch });
  const first = sandbox.ankiDecks();
  const second = sandbox.ankiDecks();
  const words = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.deepEqual(calls, ["requestPermission"], "the dialog is requested once, and nothing else runs before it is answered");
  assert.ok(sandbox.ankiWatch.permissionPending, "the request under way is shared");
  answerDialog(granted);
  const [a, b, c] = await Promise.all([first, second, words]);
  assert.equal(a.ok, true, JSON.stringify(a));
  assert.equal(b.ok, true, JSON.stringify(b));
  assert.deepEqual(a.decks, ["Japanese::Mining"]);
  assert.deepEqual(b.decks, ["Japanese::Mining"]);
  assert.equal(c.ok, true, JSON.stringify(c));
  assert.equal(c.entries.length, DECK_ENTRIES.length);
  assert.equal(calls.filter((action) => action === "requestPermission").length, 1);
  assert.equal(sandbox.ankiWatch.permission, "granted");
  assert.equal(sandbox.ankiWatch.permissionPending, null);
  // Granted once, no caller asks Anki again.
  await sandbox.ankiDecks();
  assert.equal(calls.filter((action) => action === "requestPermission").length, 1);
});

test("a permission request Anki never answers fails every waiting caller and counts for nothing", async () => {
  let failDialog;
  const dialog = new Promise((_resolve, reject) => {
    failDialog = reject;
  });
  let asked = 0;
  const fetch = async (_url, init) => {
    const req = JSON.parse(init.body);
    if (req.action === "requestPermission") {
      asked++;
      await dialog;
    }
    throw new TypeError("fetch failed");
  };
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch });
  const first = sandbox.ankiDecks();
  const second = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.equal(asked, 1);
  failDialog(new TypeError("fetch failed"));
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.reason, "offline");
  assert.equal(b.reason, "offline");
  assert.equal(sandbox.ankiWatch.permissionPending, null);
  assert.equal(sandbox.ankiWatch.permissionAskAt, 0, "a dialog nobody saw is no refusal");
  // The next caller asks again at once rather than waiting out the minute.
  const again = await sandbox.ankiDecks();
  assert.equal(again.reason, "offline");
  assert.equal(asked, 2);
});

test("the message switch routes cardStatus and ankiDecks", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES, { deckNames: [DECK] }));
  const off = loadBackground({ fetch: anki.fetch });
  assert.deepEqual(plain(await off.dispatch({ type: "cardStatus", since: 0 })), { ok: false, reason: "off" });
  const on = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const res = await on.dispatch({ type: "cardStatus", since: 0 });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.entries.length, DECK_ENTRIES.length);
  assert.deepEqual(plain(await on.dispatch({ type: "ankiDecks" })), { ok: true, decks: [DECK], seen: null });
});

test("Anki away is 'offline' on every ask, never a refusal nobody saw", async () => {
  let calls = 0;
  const fetch = async () => {
    calls++;
    throw new TypeError("fetch failed");
  };
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch });
  const offline = { ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed" };
  setNow(1000000);
  assert.deepEqual(plain(await sandbox.cardStatus({})), offline);
  // Five seconds on, well inside the minute a refusal would hold: still no Anki, still offline.
  setNow(1005000);
  assert.deepEqual(plain(await sandbox.cardStatus({})), offline);
  assert.deepEqual(plain(await sandbox.ankiDecks()), { ...offline, seen: null });
  assert.equal(calls, 3, "every ask tried Anki again");
});

test("the poll leaves a closed Anki alone between retries; a tab's ask and the popup try at once", async () => {
  // The poll is a timer, once a second per tab: with Anki closed it must not knock every second
  // (one request per ANKI_PERMISSION_RETRY_MS, a few seconds: a minute would keep the watcher
  // from a card made right after Anki was started), while the viewer's own asks (a tab's words,
  // the popup's deck list) try at once and, once one of them finds Anki, the poll follows.
  let up = false;
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES, { deckNames: [DECK] }));
  const attempts = [];
  const fetch = async (url, init) => {
    attempts.push(JSON.parse(init.body).action);
    if (!up) throw new TypeError("fetch failed");
    return anki.fetch(url, init);
  };
  const storage = makeMemoryStorage({ settings: { autoMine: true, mineTarget: "anki", cardStatus: true, cardStatusDeck: DECK } });
  const { sandbox, setNow } = loadBackground({ storage, fetch });
  const offline = { ok: false, offline: true, error: "Anki is not running or AnkiConnect is not installed" };
  const retry = sandbox.ANKI_PERMISSION_RETRY_MS;
  assert.ok(retry >= 2000 && retry <= 10000, "a few seconds, not a poll and not a minute: " + retry);
  const t = 1000000;
  for (let ms = 0; ms < retry; ms += 1000) {
    setNow(t + ms);
    assert.deepEqual(plain(await sandbox.ankiPoll()), offline);
  }
  assert.deepEqual(attempts, ["requestPermission"], "one knock in a retry interval of polls");
  setNow(t + retry);
  assert.deepEqual(plain(await sandbox.ankiPoll()), offline);
  assert.equal(attempts.length, 2, "the interval over, the poll tries once more");
  // The viewer's asks are not held back by the poll's failure.
  setNow(t + retry + 1000);
  assert.equal((await sandbox.cardStatus({ since: 0 })).reason, "offline");
  setNow(t + retry + 1500);
  assert.equal((await sandbox.ankiDecks()).reason, "offline");
  assert.equal(attempts.length, 4, "the tab's ask and the popup each tried Anki");
  // Their failure holds the poll off again.
  setNow(t + retry + 2000);
  assert.deepEqual(plain(await sandbox.ankiPoll()), offline);
  assert.equal(attempts.length, 4);
  // Anki started: the tab's ask finds it, and the poll goes through the permission it got.
  up = true;
  setNow(t + retry + 2500);
  const words = await sandbox.cardStatus({ since: 0 });
  assert.equal(words.ok, true, JSON.stringify(words));
  assert.equal(sandbox.ankiWatch.permission, "granted");
  assert.equal(sandbox.ankiWatch.permissionFailed, null);
  setNow(t + retry + 3000);
  assert.deepEqual(plain(await sandbox.ankiPoll()), { ok: true, newNoteId: null });
  assert.equal(attempts.slice(-1)[0], "findNotes", "the poll searched for new notes");
});

test("a deck found unchanged after the time to live keeps its stamp, so the tab holding it hears 'unchanged'", async () => {
  const sets = { ...DECK_SETS };
  const anki = ankiFetch(deckHandlers(DECK, sets, DECK_NOTES));
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({ since: 0 });
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS + 1);
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.equal(queriesAsked(anki).length, 11, "the searches ran again all the same");
  // A tab holding nothing yet gets the entries, under the same stamp.
  const other = await sandbox.cardStatus({ since: 0 });
  assert.equal(other.at, first.at);
  assert.deepEqual(sortedEntries(other.entries), DECK_ENTRIES);
  // The time to live runs from the refetch, not from the stamp: no search for another half minute.
  const calls = anki.calls.length;
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS + 2);
  await sandbox.cardStatus({ since: first.at });
  assert.equal(anki.calls.length, calls);
  // A review changes the entries: a new stamp, and the entries again.
  sets.learning = [];
  sets.review = [1, 4];
  setNow(first.at + 2 * sandbox.CARD_STATUS_TTL_MS + 2);
  const changed = await sandbox.cardStatus({ since: first.at });
  assert.notEqual(changed.at, first.at);
  assert.ok(plain(changed.entries).some((e) => e[0] === "字幕" && e[1] === "learned"), JSON.stringify(changed.entries));
});

test("a note edited since the last look is read again, told by its modification time", async () => {
  const sets = { ...DECK_SETS };
  const notes = { ...DECK_NOTES };
  const handlers = deckHandlers(DECK, sets, notes);
  const anki = ankiFetch(handlers);
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({});
  // Two and a half days on, Anki lists 猫 and 橋 as edited within the days that reach back to the
  // last look; 猫's pitch was corrected (a new modification time), 橋 was edited before that look.
  sets.edited = [2, 3];
  notes[2] = note(2, "猫", { Reading: "ねこ", PitchAccent: "[0]" }, 1800000000);
  setNow(first.at + 2.5 * 86400000);
  const res = await sandbox.cardStatus({ since: first.at });
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(queriesAsked(anki)[10], '"deck:Japanese::Mining" edited:4');
  assert.deepEqual(modTimesAsked(anki), [[2, 3]]);
  assert.deepEqual(notesAsked(anki), [[1, 2, 3, 4, 5, 6, 8, 9], [2]], "only the note whose time moved is read again");
  assert.ok(plain(res.entries).some((e) => e[0] === "猫" && e[2] === "heiban"), JSON.stringify(res.entries));
  assert.ok(!plain(res.entries).some((e) => e[2] === "atamadaka"));
  assert.notEqual(res.at, first.at);
  // An AnkiConnect without notesModTime, or a note gone between the two calls: every edited note is read again.
  handlers.notesModTime = () => {
    throw new Error("unsupported action");
  };
  setNow(first.at + 2.5 * 86400000 + sandbox.CARD_STATUS_TTL_MS);
  await sandbox.cardStatus({});
  assert.deepEqual(notesAsked(anki).slice(-1), [[2, 3]]);
});

test("a note read again for an edit that changed nothing it says keeps the entries' stamp", async () => {
  const sets = { ...DECK_SETS };
  const notes = { ...DECK_NOTES };
  const anki = ankiFetch(deckHandlers(DECK, sets, notes));
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({ since: 0 });
  // The mine wrote the sentence and the audio into 猫's card: a new modification time, the same
  // word, status and pitch. The note is read again, and must not move within the entries, or
  // they would compare as changed now and once more when it is known again.
  notes[2] = note(2, "猫", { Reading: "ねこ", PitchAccent: "[1]" }, 1800000000);
  sets.edited = [2];
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS + 1);
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.deepEqual(notesAsked(anki).slice(-1), [[2]], "the note was read again all the same");
  sets.edited = [];
  setNow(first.at + 2 * sandbox.CARD_STATUS_TTL_MS + 2);
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  // A tab holding nothing gets them as the first fetch had them.
  const other = await sandbox.cardStatus({ since: 0 });
  assert.deepEqual(plain(other.entries), plain(first.entries));
});

test("a fetch dropped for another deck stops at its next request, and the ask waiting on it is answered for the new deck", async () => {
  const other = { suspended: [], unsuspended: [20], new: [], learning: [], review: [20] };
  const mine = deckHandlers(DECK, DECK_SETS, DECK_NOTES);
  const theirs = deckHandlers("Other", other, { 20: note(20, "本") });
  const anki = ankiFetch({
    ...mine,
    findNotes: (p) => (p.query.startsWith('"deck:Other"') ? theirs.findNotes(p) : mine.findNotes(p)),
    notesInfo: (p) => (p.notes.includes(20) ? theirs.notesInfo(p) : mine.notesInfo(p)),
  });
  // The first search waits at the gate: the deck is switched while it is under way.
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let gated = false;
  const fetch = async (url, init) => {
    if (JSON.parse(init.body).action === "findNotes" && !gated) {
      gated = true;
      await gate;
    }
    return anki.fetch(url, init);
  };
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch });
  sandbox.ankiWatch.permission = "granted";
  const asked = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.equal(gated, true);
  await sandbox.saveSettings({ cardStatusDeck: "Other" });
  release();
  const res = await asked;
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.deck, "Other");
  assert.deepEqual(plain(res.entries), [["本", "learned", null]]);
  const queries = queriesAsked(anki);
  assert.equal(queries.filter((q) => q.startsWith('"deck:Japanese')).length, 1, "the old deck's fetch ended after the search it was in");
  assert.equal(queries.filter((q) => q.startsWith('"deck:Other"')).length, 5);
  assert.deepEqual(notesAsked(anki), [[20]], "no note of the old deck was read");
});

// Two decks for the automatic case: the last mined card's deck, and the one the next card goes to.
function twoDeckHandlers() {
  const other = { suspended: [], unsuspended: [20], new: [], learning: [], review: [20] };
  const mine = deckHandlers(DECK, DECK_SETS, DECK_NOTES);
  const theirs = deckHandlers("Other", other, { 20: note(20, "本") });
  return {
    ...mine,
    findNotes: (p) => (p.query.startsWith('"deck:Other"') ? theirs.findNotes(p) : mine.findNotes(p)),
    notesInfo: (p) => (p.notes.includes(20) ? theirs.notesInfo(p) : mine.notesInfo(p)),
    findCards: [9001],
    getDecks: { Other: [9001] },
  };
}

const automaticDeck = () => makeMemoryStorage({ settings: { cardStatus: true }, ankiDeckSeen: { deck: DECK, at: 1, noteId: 1 } });

test("the last mined card's deck asked for ends the fetch of the deck before it, and the tab that waited is answered for the new deck", async () => {
  // Automatic deck: a tab asks while the last card went to Japanese::Mining, and the next card
  // goes to Other before that deck's searches are done. The old walk must not go on to read
  // every note of a deck nobody asks about any more, and the tab must not be coloured by it.
  const anki = ankiFetch(twoDeckHandlers());
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let searches = 0;
  const fetch = async (url, init) => {
    if (JSON.parse(init.body).action === "findNotes" && ++searches === 2) await gate;
    return anki.fetch(url, init);
  };
  const { sandbox } = loadBackground({ storage: automaticDeck(), fetch });
  sandbox.ankiWatch.permission = "granted";
  const waiting = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.equal(searches, 2);
  await sandbox.rememberDeck("http://127.0.0.1:8765", 20);
  const next = await sandbox.cardStatus({ since: 0 });
  assert.equal(next.ok, true, JSON.stringify(next));
  assert.equal(next.deck, "Other");
  release();
  const res = await waiting;
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.deck, "Other");
  assert.deepEqual(plain(res.entries), [["本", "learned", null]]);
  const queries = queriesAsked(anki);
  assert.equal(queries.filter((q) => q.startsWith('"deck:Japanese')).length, 2, "the old deck's walk ended after the search it was in");
  assert.equal(queries.filter((q) => q.startsWith('"deck:Other"')).length, 5);
  assert.deepEqual(notesAsked(anki), [[20]], "no note of the old deck was read");
});

test("a fetch dropped during its last request answers nobody", async () => {
  // The old deck's fetch is reading its one chunk of notes when the deck changes: there is no
  // next request to stop at, and what it read is still about a deck no longer asked for.
  const anki = ankiFetch(twoDeckHandlers());
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let gated = false;
  const fetch = async (url, init) => {
    const req = JSON.parse(init.body);
    if (req.action === "notesInfo" && !req.params.notes.includes(20) && !gated) {
      gated = true;
      await gate;
    }
    return anki.fetch(url, init);
  };
  const { sandbox } = loadBackground({ storage: automaticDeck(), fetch });
  sandbox.ankiWatch.permission = "granted";
  const waiting = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.equal(gated, true);
  await sandbox.rememberDeck("http://127.0.0.1:8765", 20);
  const next = await sandbox.cardStatus({ since: 0 });
  assert.equal(next.deck, "Other");
  release();
  const res = await waiting;
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.deck, "Other");
  assert.deepEqual(plain(res.entries), [["本", "learned", null]]);
  // The index in memory is the new deck's; the old deck's, read to the end, went nowhere.
  assert.deepEqual(plain(await sandbox.cardStatus({ since: next.at })), { ok: true, unchanged: true, at: next.at, deck: "Other" });
});

test("a mine while the index is being fetched expires that fetch when it lands", async () => {
  const sets = { ...DECK_SETS };
  const mock = miningFetch({
    ...ankiOk,
    ...deckHandlers(DECK, sets, DECK_NOTES, { notesInfo: (p) => (p.notes.includes(555) ? noteFields("文0") : p.notes.map((id) => DECK_NOTES[id] || {})) }),
    findCards: (p) => (p.query === "nid:555" ? [9001, 9002] : []),
    getDecks: { "Japanese::Mining": [9001, 9002] },
  });
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let gated = false;
  const fetch = async (url, init) => {
    if (!String(url).includes("/clip") && JSON.parse(init.body).action === "findNotes" && !gated) {
      gated = true;
      await gate;
    }
    return mock.fetch(url, init);
  };
  const { sandbox, dispatch } = loadBackground({ storage: colourSettings(), fetch, ...instantTimers });
  sandbox.ankiWatch.permission = "granted";
  const asked = sandbox.cardStatus({});
  await settle();
  assert.equal(gated, true);
  assert.equal((await dispatch(mineMsg(0), 1)).ok, true);
  await settle();
  release();
  const first = await asked;
  assert.equal(first.ok, true, JSON.stringify(first));
  const searches = mock.calls.filter((c) => c.action === "findNotes").length;
  assert.equal(searches, 5);
  // The fetch may have searched before the card existed: the very next ask searches again.
  sets.unsuspended = [...DECK_SETS.unsuspended, 555];
  sets.new = [...DECK_SETS.new, 555];
  await sandbox.cardStatus({ since: first.at });
  assert.equal(mock.calls.filter((c) => c.action === "findNotes").length, searches + 6);
  assert.deepEqual(plain(mock.calls.filter((c) => c.action === "notesInfo").slice(-1)[0].params.notes), [555]);
});

test("the notes read outlive the event page, and a record for other fields does not", async () => {
  const anki = ankiFetch(deckHandlers(DECK, DECK_SETS, DECK_NOTES));
  const storage = colourSettings();
  const first = loadBackground({ storage, fetch: anki.fetch });
  const res = await first.sandbox.cardStatus({});
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(notesAsked(anki).length, 1);
  const record = first.session._dump()[first.sandbox.DECK_NOTES_KEY];
  assert.equal(record.deck, DECK);
  assert.deepEqual([record.wordField, record.pitchField], ["", ""]);
  assert.equal(typeof record.checkedAt, "number");
  assert.equal(record.notes.length, 8);
  assert.deepEqual(plain(record.notes.find((row) => row[0] === 3)), [3, "橋", "odaka", 1700000003, ""]);
  assert.equal(record.format, first.sandbox.DECK_NOTES_FORMAT);
  assert.equal(record.at, res.at);
  assert.deepEqual(sortedEntries(record.entries), DECK_ENTRIES);
  // The page ended; the next one searches the deck but reads no note it already knows, and
  // finds the entries as the last page left them: the tab holding them hears "unchanged"
  // under the stamp it holds, rather than getting them all again.
  const second = loadBackground({ storage, session: first.session, fetch: anki.fetch });
  second.setNow(res.at + 5000);
  assert.deepEqual(plain(await second.sandbox.cardStatus({ since: res.at })), { ok: true, unchanged: true, at: res.at, deck: DECK });
  const again = await second.sandbox.cardStatus({ since: 0 });
  assert.equal(again.ok, true, JSON.stringify(again));
  assert.equal(again.at, res.at);
  assert.deepEqual(sortedEntries(again.entries), DECK_ENTRIES);
  assert.equal(notesAsked(anki).length, 1, "no note was read again");
  assert.equal(queriesAsked(anki).length, 11);
  assert.match(queriesAsked(anki)[10], /^"deck:Japanese::Mining" edited:\d+$/);
  // The record is written once more with this page's look, so the next page's edit search
  // reaches back to it and not to the first page's.
  const rewritten = second.session._dump()[second.sandbox.DECK_NOTES_KEY];
  assert.equal(rewritten.checkedAt, res.at + 5000);
  assert.equal(rewritten.at, res.at);
  assert.equal(rewritten.notes.length, 8);
  // A page with another word field finds the record worthless and reads every note.
  const third = loadBackground({ storage: colourSettings({ ankiWordField: "Reading" }), session: first.session, fetch: anki.fetch });
  await third.sandbox.cardStatus({});
  assert.equal(notesAsked(anki).length, 2);
  assert.equal(third.session._dump()[third.sandbox.DECK_NOTES_KEY].wordField, "Reading");
  // Another deck chosen forgets the record along with the index.
  await third.sandbox.saveSettings({ cardStatusDeck: "Other" });
  assert.equal(third.session._dump()[third.sandbox.DECK_NOTES_KEY], null);
});

test("the searches answering the same notes in another order keep the entries' stamp", async () => {
  // Anki's search has no order: a review that moves a card's due moves its note in the answer.
  // The deck is the same, so every tab holding its entries must hear "unchanged".
  const sets = { ...DECK_SETS };
  const anki = ankiFetch(deckHandlers(DECK, sets, DECK_NOTES));
  const { sandbox, setNow } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const first = await sandbox.cardStatus({ since: 0 });
  sets.unsuspended = [9, 8, 6, 4, 3, 2, 1];
  sets.new = [9, 3, 2];
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS + 1);
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.equal(queriesAsked(anki).length, 11, "the searches ran again all the same");
  // A tab holding nothing gets them as the first fetch had them.
  const other = await sandbox.cardStatus({ since: 0 });
  assert.equal(other.at, first.at);
  assert.deepEqual(plain(other.entries), plain(first.entries));
});

test("a field named while Anki's permission dialog is up is the one the deck is read with", async () => {
  // The tab asks the moment the feature is turned on; Anki's dialog is up for as long as the
  // viewer takes, and the popup's field is typed meanwhile. The ask read its settings before
  // the dialog, so it reads them again after it, rather than build the index from the old field.
  const notes = { 1: note(1, "古い", { Word: "新しい" }) };
  const anki = ankiFetch(deckHandlers(DECK, { suspended: [], unsuspended: [1], new: [1], learning: [], review: [] }, notes));
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let gated = false;
  const fetch = async (url, init) => {
    if (JSON.parse(init.body).action === "requestPermission") {
      gated = true;
      await gate;
    }
    return anki.fetch(url, init);
  };
  const { sandbox, session } = loadBackground({ storage: colourSettings(), fetch });
  const asked = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.equal(gated, true);
  await sandbox.saveSettings({ ankiWordField: "Word" });
  release();
  const res = await asked;
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.deepEqual(plain(res.entries), [["新しい", "new", null]]);
  assert.equal(anki.actions().filter((a) => a === "requestPermission").length, 1);
  assert.equal(notesAsked(anki).length, 1, "the deck was read once, with the field set now");
  assert.equal(session._dump()[sandbox.DECK_NOTES_KEY].wordField, "Word");
  // Nothing changed under an ask that waited: no second reading of the deck.
  assert.deepEqual(plain(await sandbox.cardStatus({ since: res.at })), { ok: true, unchanged: true, at: res.at, deck: DECK });
  assert.equal(notesAsked(anki).length, 1);
});

test("an index read with other fields than the ones set now is never answered, nor built on", async () => {
  // Automatic deck. A pitch-field change drops the first fetch and the ask starts over; while
  // that second run reads the last mined card's deck, the word field changes too. It is the
  // last time the ask reads its settings, so the index it lands is the old field's: the tab
  // throws that answer away (its generation moved) and asks again, and that ask must read the
  // deck with the field set now, not be answered from an index that is fresh but about
  // something else.
  const notes = { 1: note(1, "古い", { Word: "新しい" }) };
  const sets = { suspended: [], unsuspended: [1], new: [1], learning: [], review: [] };
  const anki = ankiFetch(deckHandlers(DECK, sets, notes));
  const base = makeMemoryStorage({ settings: { cardStatus: true }, ankiDeckSeen: { deck: DECK, at: 1, noteId: 1 } });
  let seenGate = null;
  const storage = {
    get: async (key) => {
      if (key === "ankiDeckSeen" && seenGate) await seenGate;
      return base.get(key);
    },
    set: (patch) => base.set(patch),
    _dump: () => base._dump(),
  };
  let releaseSearch;
  const searchGate = new Promise((resolve) => {
    releaseSearch = resolve;
  });
  let gated = false;
  const fetch = async (url, init) => {
    if (JSON.parse(init.body).action === "findNotes" && !gated) {
      gated = true;
      await searchGate;
    }
    return anki.fetch(url, init);
  };
  const { sandbox, session } = loadBackground({ storage, fetch });
  sandbox.ankiWatch.permission = "granted";
  const asked = sandbox.cardStatus({ since: 0 });
  await settle();
  assert.equal(gated, true);
  let releaseSeen;
  seenGate = new Promise((resolve) => {
    releaseSeen = resolve;
  });
  await sandbox.saveSettings({ ankiPitchField: "Accent" }); // drops the index and the fetch under way
  releaseSearch();
  await settle();
  // The ask started over and is reading which deck the last card went to.
  await sandbox.saveSettings({ ankiWordField: "Word" });
  releaseSeen();
  const first = await asked;
  assert.equal(first.ok, true, JSON.stringify(first));
  assert.equal(notesAsked(anki).length, 1);
  const again = await sandbox.cardStatus({ since: 0 });
  assert.equal(again.ok, true, JSON.stringify(again));
  assert.deepEqual(plain(again.entries), [["新しい", "new", null]]);
  assert.equal(notesAsked(anki).length, 2, "the deck was read again, with the field set now");
  assert.equal(session._dump()[sandbox.DECK_NOTES_KEY].wordField, "Word");
  // Two asks for the same deck under different fields never share a fetch either.
  const url = "http://127.0.0.1:8765";
  const settings = await sandbox.getSettings();
  const one = sandbox.refreshCardIndex(url, DECK, { ...settings, ankiWordField: "Word" });
  const two = sandbox.refreshCardIndex(url, DECK, { ...settings, ankiWordField: "" });
  await assert.rejects(one, (err) => err.dropped === true);
  assert.deepEqual(plain((await two).entries), [["古い", "new", null]]);
});

test("a note read again whose chunk fails keeps what it said, and a chunk failing for good is asked again without a word written", async () => {
  const notes = { 1: note(1, "犬"), 2: note(2, "猫", { Reading: "ねこ", PitchAccent: "[1]" }) };
  const sets = { suspended: [], unsuspended: [1, 2], new: [1, 2], learning: [], review: [] };
  const handlers = deckHandlers(DECK, sets, notes);
  let broken = [];
  const anki = ankiFetch({
    ...handlers,
    notesInfo: (p) => {
      if (p.notes.some((id) => broken.includes(id))) throw new Error("note was not found: " + p.notes.join(","));
      return handlers.notesInfo(p);
    },
  });
  const { sandbox, session, setNow } = loadBackground({ storage: colourSettings({ pitchAccent: true }), fetch: anki.fetch });
  const first = await sandbox.cardStatus({ since: 0 });
  const both = sortedEntries([["犬", "new", null], ["猫", "new", "atamadaka"]]);
  assert.deepEqual(sortedEntries(first.entries), both);
  const record = session._dump()[sandbox.DECK_NOTES_KEY];
  // 猫's pitch was corrected, and Anki cannot serialise the note when it is read again: the
  // note keeps its colour and its old pitch, and the record is not written without it.
  notes[2] = note(2, "猫", { Reading: "ねこ", PitchAccent: "[0]" }, 1800000000);
  sets.edited = [2];
  broken = [2];
  setNow(first.at + sandbox.CARD_STATUS_TTL_MS + 1);
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.deepEqual(notesAsked(anki).slice(-1), [[2]]);
  assert.equal(session._dump()[sandbox.DECK_NOTES_KEY], record, "nothing changed, so the record was not written");
  // A note added whose chunk fails too: not coloured, and no reason to write the record either.
  sets.unsuspended = [1, 2, 3];
  sets.new = [1, 2, 3];
  broken = [2, 3];
  setNow(first.at + 2 * sandbox.CARD_STATUS_TTL_MS + 2);
  assert.deepEqual(plain(await sandbox.cardStatus({ since: first.at })), { ok: true, unchanged: true, at: first.at, deck: DECK });
  assert.deepEqual(notesAsked(anki).slice(-1), [[2, 3]], "both are asked for again");
  assert.equal(session._dump()[sandbox.DECK_NOTES_KEY], record);
  // Anki answers at last: the corrected pitch and the new note arrive, and the record with them.
  notes[3] = note(3, "鳥");
  broken = [];
  setNow(first.at + 3 * sandbox.CARD_STATUS_TTL_MS + 3);
  const res = await sandbox.cardStatus({ since: first.at });
  assert.notEqual(res.at, first.at);
  assert.deepEqual(sortedEntries(res.entries), sortedEntries([["犬", "new", null], ["猫", "new", "heiban"], ["鳥", "new", null]]));
  const written = session._dump()[sandbox.DECK_NOTES_KEY];
  assert.notEqual(written, record);
  assert.deepEqual(plain(written.notes.find((row) => row[0] === 2)), [2, "猫", "heiban", 1800000000, ""]);
  assert.equal(written.notes.length, 3);
});

// Jitendex's glossary tag on a note of a word usually written in kana, as Yomitan writes it.
const KANA_TAG = '<span title="word usually written using kana alone">kana</span> further; furthermore';
const KANA_NOTES = {
  1: note(1, "更に", { Reading: "さらに", PitchAccent: "[1]", SecondaryDef: KANA_TAG }),
  2: note(2, "勝手", { Reading: "かって", PitchAccent: "[0]" }),
};
const KANA_SETS = { suspended: [], unsuspended: [1, 2], new: [2], learning: [], review: [1] };
const KANA_ENTRIES = sortedEntries([
  ["更に", "learned", "atamadaka"],
  ["さらに", "learned", "atamadaka"],
  ["勝手", "new", "heiban"],
]);

test("a word usually written in kana is indexed by its reading too, with its status and pitch", async () => {
  const anki = ankiFetch(deckHandlers(DECK, KANA_SETS, KANA_NOTES));
  const { sandbox, session } = loadBackground({ storage: colourSettings({ pitchAccent: true }), fetch: anki.fetch });
  const res = await sandbox.cardStatus({});
  assert.equal(res.ok, true, JSON.stringify(res));
  // 勝手's reading is not indexed: かって is in every 向かって.
  assert.deepEqual(sortedEntries(res.entries), KANA_ENTRIES);
  const record = session._dump()[sandbox.DECK_NOTES_KEY];
  assert.equal(record.format, sandbox.DECK_NOTES_FORMAT);
  assert.deepEqual(plain(record.notes.find((row) => row[0] === 1)), [1, "更に", "atamadaka", 1700000001, "さらに"]);
  assert.deepEqual(plain(record.notes.find((row) => row[0] === 2)), [2, "勝手", "heiban", 1700000002, ""]);
  // The next page restores the reading from the record and reads no note again.
  const second = loadBackground({ storage: colourSettings({ pitchAccent: true }), session, fetch: anki.fetch });
  const again = await second.sandbox.cardStatus({ since: 0 });
  assert.equal(again.ok, true, JSON.stringify(again));
  assert.deepEqual(sortedEntries(again.entries), KANA_ENTRIES);
  assert.equal(again.at, res.at);
  assert.equal(notesAsked(anki).length, 1);
});

test("a notes record of an older format is ignored and every note read again", async () => {
  const anki = ankiFetch(deckHandlers(DECK, KANA_SETS, KANA_NOTES));
  const first = loadBackground({ storage: colourSettings({ pitchAccent: true }), fetch: anki.fetch });
  const res = await first.sandbox.cardStatus({});
  const record = first.session._dump()[first.sandbox.DECK_NOTES_KEY];
  // What the page before readings wrote: no format, four columns, no reading entry.
  const old = {
    ...plain(record),
    notes: plain(record.notes).map((row) => row.slice(0, 4)),
    entries: plain(record.entries).filter((entry) => entry[0] !== "さらに"),
  };
  delete old.format;
  await first.session.set({ [first.sandbox.DECK_NOTES_KEY]: old });
  const second = loadBackground({ storage: colourSettings({ pitchAccent: true }), session: first.session, fetch: anki.fetch });
  const again = await second.sandbox.cardStatus({ since: res.at });
  assert.equal(again.ok, true, JSON.stringify(again));
  assert.deepEqual(sortedEntries(again.entries), KANA_ENTRIES);
  assert.deepEqual(notesAsked(anki), [[1, 2], [1, 2]]);
  assert.equal(second.session._dump()[second.sandbox.DECK_NOTES_KEY].format, second.sandbox.DECK_NOTES_FORMAT);
});

test("a deck named as one of Anki's keywords is searched by id, its subdecks included", async () => {
  const sets = { suspended: [], unsuspended: [1, 2], new: [1], learning: [], review: [2] };
  const handlers = deckHandlers("current", sets, { 1: note(1, "猫"), 2: note(2, "犬") });
  const anki = ankiFetch({
    ...handlers,
    deckNamesAndIds: { Default: 1, current: 1700000000001, "current::Sub": 1700000000002, Current: 5, currently: 6 },
    findNotes: (p) => {
      const found = /^did:([\d,]+) (.+)$/.exec(p.query);
      if (!found || found[1] !== "1700000000001,1700000000002") return [];
      return sets[STATUS_CLAUSES[found[2]]] || [];
    },
  });
  const { sandbox } = loadBackground({ storage: colourSettings({ cardStatusDeck: "current" }), fetch: anki.fetch });
  const res = await sandbox.cardStatus({});
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.deepEqual(sortedEntries(res.entries), sortedEntries([["猫", "new", null], ["犬", "learned", null]]));
  assert.deepEqual(queriesAsked(anki).slice(0, 2), ["did:1700000000001,1700000000002 is:suspended", "did:1700000000001,1700000000002 -is:suspended"]);
  assert.ok(queriesAsked(anki).every((q) => !q.includes("deck:")));
  // No deck of that name: nothing is searched, as for any other deck that does not exist.
  const none = loadBackground({ storage: colourSettings({ cardStatusDeck: "filtered" }), fetch: anki.fetch });
  const empty = await none.sandbox.cardStatus({});
  assert.equal(empty.ok, true, JSON.stringify(empty));
  assert.deepEqual(plain(empty.entries), []);
  assert.equal(queriesAsked(anki).length, 5, "the first deck's searches only");
});

test("a word field holding a sentence is no word: nothing is kept or sent for it", async () => {
  assert.equal(typeof SHISUKO_WORDS.MAX_WORD_LEN, "number", "words.js exports the cap its index applies");
  const cap = SHISUKO_WORDS.MAX_WORD_LEN;
  const notes = { 1: note(1, "あ".repeat(cap + 1)), 2: note(2, "い".repeat(cap)) };
  const anki = ankiFetch(deckHandlers(DECK, { suspended: [], unsuspended: [1, 2], new: [1, 2], learning: [], review: [] }, notes));
  const { sandbox } = loadBackground({ storage: colourSettings(), fetch: anki.fetch });
  const res = await sandbox.cardStatus({});
  assert.deepEqual(plain(res.entries), [["い".repeat(cap), "new", null]]);
});

test("a note whose fields are a hundred kilobytes of '<' costs the deck only itself, and no time to speak of", async () => {
  // Third-party content: a shared deck may hold a field that is nothing but unclosed tags. The
  // bounds in words.js (16,000 characters read, a tag never running across a "<") keep the
  // read linear, so the four ordinary notes beside it still make their entries at once.
  const hostile = "<".repeat(100 * 1024);
  const notes = {
    1: note(1, "日本語"),
    2: note(2, "猫", { Reading: "ねこ", PitchAccent: "[1]" }),
    3: note(3, hostile, { Reading: hostile, PitchAccent: hostile }),
    4: note(4, "字幕"),
    5: note(5, "橋", { Reading: "はし", PitchAccent: "[2]" }),
  };
  const sets = { suspended: [], unsuspended: [1, 2, 3, 4, 5], new: [1, 2, 3, 4, 5], learning: [], review: [] };
  const anki = ankiFetch(deckHandlers(DECK, sets, notes));
  const { sandbox } = loadBackground({ storage: colourSettings({ pitchAccent: true }), fetch: anki.fetch });
  const started = performance.now();
  const res = await sandbox.cardStatus({});
  const took = performance.now() - started;
  assert.equal(res.ok, true, JSON.stringify(res).slice(0, 200));
  assert.deepEqual(sortedEntries(res.entries), sortedEntries([
    ["日本語", "new", null],
    ["猫", "new", "atamadaka"],
    ["字幕", "new", null],
    ["橋", "new", "odaka"],
  ]));
  // A tag pattern running across "<" costs seconds on this field alone; the bounded one a millisecond.
  assert.ok(took < 1000, `the ask took ${Math.round(took)} ms`);
});

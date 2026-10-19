// SPDX-License-Identifier: Apache-2.0
"use strict";
// node --test portal/tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../static/lib.js");

test("parseVideoInput takes every YouTube link shape and bare ids", () => {
  const id = "AbCdEfGhIjK";
  for (const input of [
    id,
    `https://www.youtube.com/watch?v=${id}`,
    `https://m.youtube.com/watch?feature=share&v=${id}`,
    `youtube.com/watch?v=${id}`,
    `https://youtu.be/${id}`,
    `https://youtu.be/${id}?si=xyz`,
    `https://www.youtube.com/shorts/${id}`,
    `https://www.youtube.com/live/${id}?feature=share`,
    `https://www.youtube.com/embed/${id}`,
    `https://music.youtube.com/watch?v=${id}&list=RD`,
  ]) {
    assert.equal(L.parseVideoInput(input)?.id, id, input);
  }
  assert.equal(L.parseVideoInput(`https://youtu.be/${id}?t=95`).t, 95);
  assert.equal(L.parseVideoInput(`https://www.youtube.com/watch?v=${id}&t=1m30s`).t, 90);
  for (const bad of ["", "hello world", `https://evil.com/watch?v=${id}`, "https://www.youtube.com/watch?v=a/b", "https://youtube.com/"]) {
    assert.equal(L.parseVideoInput(bad), null, bad);
  }
});

test("activeCueIndex keeps the last started cue up through a gap", () => {
  const cues = [{ start: 1, end: 2 }, { start: 3, end: 4 }, { start: 6, end: 7 }];
  assert.equal(L.activeCueIndex(cues, 0.5), -1);
  assert.equal(L.activeCueIndex(cues, 1.5), 0);
  assert.equal(L.activeCueIndex(cues, 4.5), 1);
  assert.equal(L.activeCueIndex(cues, 99), 2);
  assert.equal(L.activeCueIndex([], 5), -1);
});

test("tokenOffsets index the original line", () => {
  const out = L.tokenOffsets([{ text: "今日は" }, { text: "、" }, { text: "寿司" }]);
  assert.deepEqual(out.map((t) => [t.start, t.end]), [[0, 3], [3, 4], [4, 6]]);
});

test("pitchPattern follows the Tokyo accent types", () => {
  const hl = (r, p) => L.pitchPattern(r, p).map((m) => (m.high ? "H" : "L")).join("");
  assert.equal(hl("たべる", 2), "LHL");
  assert.equal(hl("さくら", 0), "LHH");
  assert.equal(hl("いのち", 1), "HLL");
  assert.equal(hl("おとうと", 4), "LHHH");
  assert.deepEqual(L.morae("きょうと"), ["きょ", "う", "と"]);
  assert.equal(L.pitchPattern("たべる", 2)[1].drop, true);
});

test("splitFurigana puts ruby on the kanji only", () => {
  assert.deepEqual(L.splitFurigana("食べる", "たべる"), [["食", "た"], ["べる", ""]]);
  assert.deepEqual(L.splitFurigana("お茶", "おちゃ"), [["お", ""], ["茶", "ちゃ"]]);
  assert.deepEqual(L.splitFurigana("寿司", "すし"), [["寿司", "すし"]]);
  assert.deepEqual(L.splitFurigana("ゲーム", "げーむ"), [["ゲーム", ""]]);
});

test("statusText speaks plainly", () => {
  assert.match(L.statusText({ offline: true }).text, /Reconnecting/);
  assert.match(L.statusText({ data: { status: "downloading" } }).text, /Fetching audio/);
  assert.match(L.statusText({ data: { language_paused: true, heard: "en" } }).text, /not in the subtitle language \(hearing en\)/);
  const ready = { status: "ready", covered: [[0, 100]], duration: 600 };
  assert.match(L.statusText({ data: ready, t: 10, cueCount: 5 }).text, /ready to 1:40/);
  assert.match(L.statusText({ data: ready, t: 95, cueCount: 5 }).text, /Transcribing… \(ready to 1:40\)/);
  assert.match(L.statusText({ data: { ...ready, covered: [[0, 600]] }, t: 10, cueCount: 5 }).text, /to the end/);
  assert.equal(L.statusText({ data: { status: "error", error: "boom" } }).error, true);
  assert.match(L.statusText({ data: { status: "error", error: "HTTP 403", retrying: false } }).text, /gave up\): HTTP 403/);
});

test("formatTime", () => {
  assert.equal(L.formatTime(65), "1:05");
  assert.equal(L.formatTime(3725), "1:02:05");
});

test("watchStatus is silent when healthy", () => {
  const ready = { status: "ready", covered: [[0, 100]], duration: 600 };
  assert.equal(L.watchStatus({ data: ready, t: 50 }), null);
  assert.equal(L.watchStatus({ data: { ...ready, covered: [[0, 600]] }, t: 599.5 }), null);
  assert.deepEqual(L.watchStatus({ data: ready, t: 150 }), { text: "Transcribing…", busy: true });
  assert.deepEqual(L.watchStatus({ data: { status: "downloading" }, t: 0 }), { text: "Transcribing…", busy: true });
  assert.deepEqual(L.watchStatus({ offline: true }), { text: "Reconnecting…", busy: true });
  assert.match(L.watchStatus({ data: { ...ready, language_paused: true }, t: 50 }).text, /subtitle language/);
});

test("freqLabel", () => {
  assert.equal(L.freqLabel("2214㋕"), "#2214");
  assert.equal(L.freqLabel("734"), "#734");
  assert.equal(L.freqLabel(""), null);
});

test("moraCount: small kana join, っ and ー count", () => {
  assert.equal(L.moraCount("きょう"), 2);
  assert.equal(L.moraCount("がっこう"), 4);
  assert.equal(L.moraCount("コーヒー"), 4);
  assert.equal(L.moraCount("しゃしん"), 3);
});

test("estimateUnitTimes spreads a cue by mora and pauses", () => {
  const units = L.cueUnits("今日は、寿司", [
    { text: "今日は", segs: [["今日", "きょう"], ["は", ""]], start: 0, end: 3, term: "今日は" },
    { text: "、", segs: [["、", ""]], start: 3, end: 4, term: null },
    { text: "寿司", segs: [["寿司", "すし"]], start: 4, end: 6, term: "寿司" },
  ]);
  // weights: きょう+は = 3, 、 = 1.5, すし = 2; total 6.5 over 6.5 s
  const times = L.estimateUnitTimes(units, 10, 16.5);
  assert.deepEqual(times.map(([a]) => a), [10, 13, 14.5]);
  assert.equal(times[2][1], 16.5);
  assert.equal(L.spokenCount(times, 9), 0);
  assert.equal(L.spokenCount(times, 10), 1);
  assert.equal(L.spokenCount(times, 14.6), 3);
});

test("cueUnits falls back to characters", () => {
  const units = L.cueUnits("あ。い。", null);
  assert.deepEqual(units.map((u) => [u.text, u.start]), [["あ", 0], ["。", 1], ["い", 2], ["。", 3]]);
  // 1 + 3 + 1, and the closing 。 takes no time inside the cue
  const times = L.estimateUnitTimes(units, 0, 5);
  assert.deepEqual(times.map(([a]) => a), [0, 1, 4, 5]);
});

test("real word times win over the estimate", () => {
  const units = L.cueUnits("今日は寿司", [
    { text: "今日は", segs: [["今日", "きょう"], ["は", ""]], start: 0, end: 3 },
    { text: "寿司", segs: [["寿司", "すし"]], start: 3, end: 5 },
  ]);
  const cue = { start: 10, end: 13, text: "今日は寿司", words: [{ start: 10.2, end: 11, text: "今日は" }, { start: 11.8, end: 12.6, text: " 寿司" }] };
  const times = L.unitTimes(cue, units);
  assert.deepEqual(times, [[10.2, 11.8], [11.8, 13]]);
});

test("a stray word does not derail the word mapping", () => {
  const units = L.cueUnits("あしたはあめ", [
    { text: "あした", segs: [["あした", ""]], start: 0, end: 3 },
    { text: "は", segs: [["は", ""]], start: 3, end: 4 },
    { text: "あめ", segs: [["あめ", ""]], start: 4, end: 6 },
  ]);
  // "あめ" from the cue before must not jump the cursor to the end of this one.
  const words = [{ start: 9.8, end: 10, text: "あめ" }, { start: 10, end: 10.5, text: "あした" }, { start: 10.5, end: 11, text: "は" }, { start: 11, end: 11.4, text: "あめ" }];
  const times = L.unitTimesFromWords(units, "あしたはあめ", words, 10, 12);
  assert.deepEqual(times.map(([a]) => a), [10, 10.5, 11]);
});

test("backoff schedules: 1, 2, 4, 8 … 15 s for sync; 300, 900, 2700 ms for lookups", () => {
  const mid = () => 0.5; // no jitter
  assert.deepEqual([0, 1, 2, 3, 4, 5].map((n) => L.backoffDelay(n, L.SCHEDULES.sync, mid)), [1000, 2000, 4000, 8000, 15000, 15000]);
  assert.deepEqual([0, 1, 2].map((n) => L.backoffDelay(n, L.SCHEDULES.lookup, mid)), [300, 900, 2700]);
  const mine = [0, 1, 2].map((n) => L.backoffDelay(n, L.SCHEDULES.mine, mid));
  assert.deepEqual(mine, [3000, 7500, 18750]);
  assert.ok(mine.reduce((a, b) => a + b) <= 30000);
  // jitter stays within ±20%
  assert.equal(L.backoffDelay(0, L.SCHEDULES.sync, () => 0), 800);
  assert.equal(L.backoffDelay(0, L.SCHEDULES.sync, () => 1), 1200);
});

test("transient vs permanent", () => {
  for (const s of [0, null, 500, 502, 503, 504]) assert.equal(L.isTransient(s, null), true, String(s));
  for (const s of [400, 403, 404, 409, 413, 415]) assert.equal(L.isTransient(s, null), false, String(s));
  assert.equal(L.isTransient(409, { duplicate: true }), false);
  assert.equal(L.isTransient(502, { duplicate: true }), false);
});

test("withRetry retries transient failures, stops on success or a permanent one", async () => {
  const slept = [];
  const sleep = async (ms) => { slept.push(ms); };
  const answers = [{ status: 0 }, { status: 502, data: { ok: false } }, { status: 200, data: { ok: true, v: 1 } }];
  let calls = 0;
  const res = await L.withRetry(async () => answers[calls++], L.SCHEDULES.lookup, { sleep, rand: () => 0.5 });
  assert.equal(res.data.v, 1);
  assert.equal(calls, 3);
  assert.deepEqual(slept, [300, 900]);

  calls = 0;
  const bad = await L.withRetry(async () => { calls++; return { status: 400, data: { ok: false } }; }, L.SCHEDULES.lookup, { sleep });
  assert.equal(bad.status, 400);
  assert.equal(calls, 1);

  calls = 0;
  const gone = await L.withRetry(async () => { calls++; throw new Error("network"); }, L.SCHEDULES.lookup, { sleep });
  assert.equal(gone.status, 0);
  assert.equal(calls, 4); // the first try and three retries
});

test("retry wording: countdown, unknown, final", () => {
  const err = { status: "error", error: "HTTP 403" };
  assert.deepEqual(L.watchStatus({ data: { ...err, retrying: true, retry_in: 8 }, receivedAt: 1000, now: 3500 }), { text: "Retrying in 6 s…", busy: true });
  assert.deepEqual(L.watchStatus({ data: err }), { text: "Retrying…", busy: true });
  assert.deepEqual(L.watchStatus({ data: { ...err, retrying: false } }), { text: "Subtitles unavailable", busy: false });
});

test("resumeTime: ?t= wins, near start and near end start over", () => {
  assert.equal(L.resumeTime({ saved: { t: 120, duration: 1000 }, urlT: 45 }), 45);
  assert.equal(L.resumeTime({ saved: { t: 120, duration: 1000 }, urlT: 0 }), 0); // an explicit t=0 also wins
  assert.equal(L.resumeTime({ saved: { t: 120, duration: 1000 }, urlT: null }), 118);
  assert.equal(L.resumeTime({ saved: { t: 9.5, duration: 1000 }, urlT: null }), 0);
  assert.equal(L.resumeTime({ saved: { t: 975, duration: 1000 }, urlT: null }), 0);
  assert.equal(L.resumeTime({ saved: { t: 969, duration: 1000 }, urlT: null }), 967);
  assert.equal(L.resumeTime({ saved: { t: 975 }, duration: 1000, urlT: null }), 0); // duration from the player
  assert.equal(L.resumeTime({ saved: null, urlT: null }), 0);
});

test("watchedFraction", () => {
  assert.equal(L.watchedFraction({ t: 250, duration: 1000 }), 0.25);
  assert.equal(L.watchedFraction({ t: 990, duration: 1000 }), 1);
  assert.equal(L.watchedFraction({ t: 50 }), 0);
});

test("other languages: even timing by letters, words from Intl.Segmenter", () => {
  const units = L.cueUnits("Hello, big world", L.tokenOffsets(L.segmentWords("Hello, big world", "en")));
  assert.deepEqual(units.filter((u) => u.term).map((u) => u.text), ["Hello", "big", "world"]);
  const times = L.unitTimes({ start: 0, end: 13, text: "Hello, big world" }, units, "en");
  // 5 + 3 + 5 letters over 13 s; the comma and spaces take no time
  assert.deepEqual(units.map((u, i) => [u.text, times[i][0]]).filter(([t]) => /\w/.test(t)), [["Hello", 0], ["big", 5], ["world", 8]]);
  assert.equal(L.isJapanese("ja"), true);
  assert.equal(L.isJapanese(null), true); // unknown: as before
  assert.equal(L.isJapanese("en"), false);
  assert.equal(L.tokensUseful([{ text: "a", term: null }]), false);
  assert.equal(L.tokensUseful([{ text: "a", term: "a" }]), true);
});

test("scrubber clock and spoken time", () => {
  assert.equal(L.formatClock(37, 600), "0:37");
  assert.equal(L.formatClock(601, 900), "10:01");
  assert.equal(L.formatClock(37, 3601), "0:00:37"); // an hour-long video: h:mm:ss throughout
  assert.equal(L.formatClock(3725, 7200), "1:02:05");
  assert.equal(L.spokenTime(725), "12 minutes 5 seconds");
  assert.equal(L.spokenTime(3720), "1 hour 2 minutes");
  assert.equal(L.spokenTime(0), "0 seconds");
  assert.equal(L.spokenTime(61), "1 minute 1 second");
});

test("anchoring: cues inserted above the active one do not move it, and do not glide", () => {
  // A list laid out like the page: each row's height, 22 px apart; the track is placed so the
  // active row's centre sits at the centre point.
  const layout = (cues, heightOf) => {
    let y = 0;
    return cues.map((c) => { const top = y; y += heightOf(c) + 22; return { id: c.id, top, centre: top + heightOf(c) / 2 }; });
  };
  const centre = 260;
  const height = (c) => (c.id % 3 === 0 ? 78 : 39);
  let cues = Array.from({ length: 10 }, (_, i) => ({ id: 100 + i, start: 480 + i * 3, end: 482 + i * 3 }));
  const t = 492;
  let idx = L.activeCueIndex(cues, t);
  let id = L.focusIdentity(cues, idx);
  const screenBefore = (centre - layout(cues, height)[idx].centre) + layout(cues, height)[idx].centre;
  // The server fills in 20 earlier cues; the list is sorted again.
  cues = [...Array.from({ length: 20 }, (_, i) => ({ id: 200 + i, start: i * 3, end: i * 3 + 2 })), ...cues].sort((a, b) => a.start - b.start);
  idx = L.activeCueIndex(cues, t);
  const next = L.focusIdentity(cues, idx);
  assert.equal(next, id); // the same cue is active
  assert.equal(L.focusMotion(id, next), "instant"); // so the list re-anchors, it does not glide
  const rows = layout(cues, height);
  const trackBase = centre - rows[idx].centre; // placeTrack(): centre - anchor
  assert.equal(trackBase + rows[idx].centre, screenBefore); // and the active row has not moved on screen
  assert.equal(L.focusMotion(id, L.focusIdentity(cues, idx + 1)), "glide"); // a real transition still glides
});

test("active cue is stable in gaps and at boundaries", () => {
  const cues = [{ id: 1, start: 0, end: 4 }, { id: 2, start: 10, end: 14 }];
  // In the gap the last started cue stays active.
  for (const t of [4.5, 6, 9.9]) assert.equal(L.activeCueIndex(cues, t), 0);
  // A clock that wobbles a few ms backwards at the boundary (cue 2 counts from 9.95) does not flip back.
  let clock = 9.9;
  const seen = [];
  for (const raw of [9.9, 9.93, 9.96, 9.94, 9.95, 9.98]) {
    clock = L.steadyTime(clock, raw, true);
    seen.push(L.activeCueIndex(cues, clock));
  }
  assert.deepEqual(seen, [0, 0, 1, 1, 1, 1]);
  // A seek back (more than 0.3 s) is taken as it is, and so is any time while paused.
  assert.equal(L.steadyTime(30, 12, true), 12);
  assert.equal(L.steadyTime(10.02, 10.0, false), 10.0);
  assert.equal(L.focusIdentity(cues, 0.5), "1+");
  assert.equal(L.focusIdentity(cues, -1), "start");
});

test("the position saver waits for a resume to land", () => {
  // Resuming at 600 s: the player still at 0-5 s (its early seek dropped) must not be saved.
  for (const t of [0, 0.4, 5.4]) assert.equal(L.saveAllowed(t, 600, false), false);
  assert.equal(L.saveAllowed(599, 600, false), true); // there: saving is fine
  assert.equal(L.saveAllowed(5.4, 600, true), true); // landed (or the viewer seeked): anything goes
  assert.equal(L.saveAllowed(5.4, null, false), true); // no resume at all
});

test("closing the dictionary sheet never does anything else", () => {
  const open = { sheetOpen: true, inSheet: false, sheetCue: 7, sinceCloseMs: 99999 };
  assert.equal(L.dismissAction({ ...open, wordCue: 8 }), "dismiss"); // (a) the next cue's text
  assert.equal(L.dismissAction({ ...open, wordCue: 6 }), "dismiss"); // (b) the previous cue
  assert.equal(L.dismissAction({ ...open, wordCue: null }), "dismiss"); // (c) empty space, (d) the video
  assert.equal(L.dismissAction({ ...open, wordCue: 7 }), "pass"); // another word of the same sentence: look it up
  assert.equal(L.dismissAction({ ...open, wordCue: null, inSheet: true }), "pass"); // inside the sheet
  // Closed a moment ago (a tap outside, a swipe down, Mine): the late synthetic click is swallowed.
  const closed = { sheetOpen: false, inSheet: false, sheetCue: null };
  assert.equal(L.dismissAction({ ...closed, wordCue: 8, sinceCloseMs: 120 }), "swallow");
  assert.equal(L.dismissAction({ ...closed, wordCue: null, sinceCloseMs: 350 }), "swallow");
  assert.equal(L.dismissAction({ ...closed, wordCue: 8, sinceCloseMs: 600 }), "pass"); // later: an ordinary tap
});

test("ankiSyncText shows the server's Anki sync: off, syncing, the error, the last sync, or waiting", () => {
  const clock = () => "12:04";
  assert.equal(L.ankiSyncText(null, clock), "", "a server from before the sync moved there says nothing");
  assert.equal(L.ankiSyncText({}, clock), "");
  assert.equal(L.ankiSyncText({ state: "off", last: null, error: null }, clock), "Anki sync: off on the server");
  assert.equal(L.ankiSyncText({ state: "syncing", last: 1, error: null }, clock), "Anki sync: syncing…");
  assert.equal(L.ankiSyncText({ state: "watching", last: 1, error: null }, clock), "Anki sync: last 12:04");
  assert.equal(L.ankiSyncText({ state: "watching", last: null, error: null }, clock), "Anki sync: on, after the next new card");
  assert.equal(L.ankiSyncText({ state: "halted", last: 1, error: "not signed in to AnkiWeb" }, clock),
    "Anki sync failed: not signed in to AnkiWeb");
  assert.equal(L.ankiSyncText({ state: "unreachable", last: null, error: "AnkiConnect is not answering" }, clock),
    "Anki sync failed: AnkiConnect is not answering");
});

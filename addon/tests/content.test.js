"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadContent } = require("./_loadContent");
const fs = require("node:fs");
const path = require("node:path");

const ADDON = path.join(__dirname, "..");
// A numeric constant as the source declares it, for the cadences two files have to keep in step.
function constant(file, name) {
  const source = fs.readFileSync(path.join(ADDON, file), "utf8");
  const m = source.match(new RegExp(`^\\s*const ${name} = (\\d+);`, "m"));
  if (!m) throw new Error(`${file} no longer declares ${name} as a number`);
  return Number(m[1]);
}

// Arrays built inside the vm sandbox belong to another realm, so deepEqual rejects them on
// prototype identity alone; a JSON round trip compares them in this one.
const plain = (value) => JSON.parse(JSON.stringify(value));

const NOW = 1_000_000;

// A paused viewer on a video the server has fully transcribed around the playhead.
function idle(patch) {
  return Object.assign(
    { paused: true, t: 100, status: "ready", covered: [[0, 1200]], duration: 1200, lastSyncAt: NOW - 1000 },
    patch
  );
}

// ------------------------------------------------------------------ shouldSync

test("shouldSync keeps the one second cadence while the video plays", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle({ paused: false }), NOW), true);
});

test("shouldSync skips the request when a paused video needs nothing", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle(), NOW), false);
});

test("shouldSync keeps asking while the server still has work around the playhead", () => {
  const { api } = loadContent();
  // Covered only to 200 s of a 1200 s video, and the server transcribes 900 s ahead of 100 s.
  assert.equal(api.shouldSync(idle({ covered: [[0, 200]] }), NOW), true);
  // The playhead is not inside any covered range at all.
  assert.equal(api.shouldSync(idle({ t: 900, covered: [[0, 200]] }), NOW), true);
});

test("shouldSync keeps asking while the server is not ready", () => {
  const { api } = loadContent();
  for (const status of ["connecting", "pending", "downloading", "decoding", "error", "offline"]) {
    assert.equal(api.shouldSync(idle({ status }), NOW), true, status);
  }
});

test("shouldSync still beats every five seconds while paused", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle({ lastSyncAt: NOW - 4999 }), NOW), false);
  assert.equal(api.shouldSync(idle({ lastSyncAt: NOW - 5000 }), NOW), true);
  assert.equal(api.shouldSync(idle({ lastSyncAt: 0 }), NOW), true);
});

test("shouldSync asks while the duration is still unknown", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle({ duration: 0 }), NOW), true);
});

test("shouldSync stops once the covered range reaches the end of a short video", () => {
  const { api } = loadContent();
  // Shorter than the server's lookahead: the end of the video is the target, not playhead + 900 s.
  assert.equal(api.shouldSync(idle({ t: 30, duration: 60, covered: [[0, 60]] }), NOW), false);
  assert.equal(api.shouldSync(idle({ t: 30, duration: 60, covered: [[0, 45]] }), NOW), true);
});

// ------------------------------------------------------------------ coveredEnd

test("coveredEnd returns the end of the range holding the playhead, else null", () => {
  const { api } = loadContent();
  assert.equal(api.coveredEnd([[0, 40], [60, 90]], 20), 40);
  assert.equal(api.coveredEnd([[0, 40], [60, 90]], 70), 90);
  assert.equal(api.coveredEnd([[0, 40], [60, 90]], 50), null);
  assert.equal(api.coveredEnd(null, 10), null);
});

// ------------------------------------------------------------------ cue bookkeeping

test("mergeCues indexes cues by id, ignores repeats and keeps them in order", () => {
  const { api } = loadContent();
  api.mergeCues([
    { id: 0, start: 0, end: 1, text: "いち" },
    { id: 1, start: 1, end: 2, text: "に" },
  ]);
  api.mergeCues([
    { id: 1, start: 1, end: 2, text: "に" }, // already known
    { id: 2, start: 2, end: 3, text: "さん" },
  ]);
  assert.deepEqual(plain(api.state.cues.map((c) => c.id)), [0, 1, 2]);
  assert.equal(api.cueById(2).text, "さん");
  assert.equal(api.cueById(0).text, "いち"); // id 0 is a real cue, not "no cue"
  assert.equal(api.cueById(99), null);
});

test("mergeCues sorts a cue that arrives out of order", () => {
  const { api } = loadContent();
  api.mergeCues([{ id: 0, start: 10, end: 11, text: "あと" }]);
  api.mergeCues([{ id: 1, start: 2, end: 3, text: "さき" }]);
  assert.deepEqual(plain(api.state.cues.map((c) => c.start)), [2, 10]);
});

test("mergeCues drops a cue whose times are not finite, or run backwards", () => {
  const { api } = loadContent();
  // JSON.parse turns 1e999 into Infinity; only a broken or hostile server sends it.
  api.mergeCues(JSON.parse('[{"id":0,"start":0,"end":1e999,"text":"ends never"},{"id":1,"start":1e999,"end":1e999,"text":"seeks nowhere"},{"id":2,"start":-1e999,"end":2,"text":"before time"},{"id":3,"start":5,"end":4,"text":"backwards"},{"id":4,"start":5,"end":6,"text":"fine"},{"id":5,"start":7,"end":7,"text":"empty span"}]'));
  assert.deepEqual(plain(api.state.cues.map((c) => c.id)), [4, 5]);
  assert.equal(api.findActiveCue(1e6), null);
  assert.equal(api.jumpTarget(api.state.cues, 6, 1), 6.85);
});

test("findActiveCue picks the cue at the playhead and lingers past the last one", () => {
  const { api } = loadContent();
  api.mergeCues([
    { id: 0, start: 0, end: 2, text: "いち" },
    { id: 1, start: 5, end: 7, text: "に" },
  ]);
  assert.equal(api.findActiveCue(1).id, 0);
  assert.equal(api.findActiveCue(6).id, 1);
  assert.equal(api.findActiveCue(4), null); // the blank between them is long enough to be a blank
  assert.equal(api.findActiveCue(7.2).id, 1); // lingerSeconds keeps the last cue up for a moment
  assert.equal(api.findActiveCue(9), null);
});

// ------------------------------------------------------------------ jumpTarget

// Three lines with a gap between the second and the third.
const JUMP_CUES = [
  { id: 0, start: 0, end: 2, text: "いち" },
  { id: 1, start: 5, end: 7, text: "に" },
  { id: 2, start: 20, end: 22, text: "さん" },
];

test("jumpTarget steps back a line however far into the current one the viewer is", () => {
  const { api } = loadContent();
  // Left used to replay the current line past a second in. A line runs three to six seconds, so
  // that was nearly always, and Left restarted what was already playing instead of going back.
  assert.equal(api.jumpTarget(JUMP_CUES, 5.5, -1), 0); // 0.5 s into cue 1
  assert.equal(api.jumpTarget(JUMP_CUES, 6, -1), 0); // 1.0 s in
  assert.equal(api.jumpTarget(JUMP_CUES, 6.5, -1), 0); // 1.5 s in: still the line before, not a replay
  assert.equal(api.jumpTarget(JUMP_CUES, 20.5, -1), 4.85); // 5 - the 0.15 s lead-in
});

test("jumpTarget in the gap after a line steps back to that line, the last thing heard", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 9, -1), 4.85); // cue 1 ended at 7: back to its own start
  assert.equal(api.jumpTarget(JUMP_CUES, 3, -1), 0); // cue 0 ended at 2
});

test("the lead-in eats silence only, never the tail of the line before", () => {
  const { api } = loadContent();
  // What the server actually emits: normalise_gaps closes every gap under 0.5 s to 0.1 s, which
  // is shorter than the 0.15 s lead-in. Seeking to start - 0.15 landed inside the previous line,
  // so the viewer saw its last frames and was swept straight back into the line they left.
  const tight = [
    { id: 0, start: 65.08, end: 71.0, text: "a" },
    { id: 1, start: 71.1, end: 74.1, text: "b" },
    { id: 2, start: 74.76, end: 79.1, text: "c" },
  ];
  assert.equal(api.jumpTarget(tight, 76, -1), 71.1); // the line before, landed on exactly: 70.95 is cue 0
  assert.equal(api.jumpTarget(tight, 73, 1), 74.61); // 0.66 s of silence ahead: the lead-in fits
  // 65.08 - 0.15 in binary floating point; cue 0 has no neighbour behind it to clamp against.
  assert.ok(Math.abs(api.jumpTarget(tight, 73, -1) - 64.93) < 1e-9);
});

test("jumpTarget lands on the start of the video before the first line", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 0.5, -1), 0); // inside the first cue, less than a second
  assert.equal(api.jumpTarget(JUMP_CUES, 1.5, -1), 0); // replaying cue 0 clamps to 0 as well
  assert.equal(api.jumpTarget(JUMP_CUES, -1, -1), 0); // before every cue
});

test("jumpTarget moves to the next line, or reports none ahead", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 0, 1), 4.85);
  assert.equal(api.jumpTarget(JUMP_CUES, 6, 1), 19.85);
  assert.equal(api.jumpTarget(JUMP_CUES, 10, 1), 19.85); // in the gap: the next line still counts
  assert.equal(api.jumpTarget(JUMP_CUES, 20, 1), null); // on the last line, nothing ahead
  assert.equal(api.jumpTarget(JUMP_CUES, 60, 1), null);
});

test("jumpTarget with no cues leaves both keys to YouTube", () => {
  const { api } = loadContent();
  // The server offline, or the first window still out: Left used to restart the video.
  assert.equal(api.jumpTarget([], 42, -1), null);
  assert.equal(api.jumpTarget([], 42, 1), null);
});

test("jumpTarget only jumps within the covered range around the playhead", () => {
  const { api } = loadContent();
  const cues = [{ id: 0, start: 600, end: 602, text: "いち" }, { id: 1, start: 640, end: 642, text: "に" }];
  // Seeked far past the last known line into an untranscribed stretch: 19 minutes back is not "previous".
  assert.equal(api.jumpTarget(cues, 1800, -1, [[590, 660]]), null);
  assert.equal(api.jumpTarget(cues, 1800, -1), 639.85); // without the ranges, as before
  assert.equal(api.jumpTarget(cues, 650, -1, [[590, 660]]), 639.85); // inside the range: the line before
  assert.equal(api.jumpTarget(cues, 640.5, -1, [[590, 660]]), 599.85);
  assert.equal(api.jumpTarget(cues, 600.2, -1, [[590, 660]]), null); // before the first line, and 0 is outside the range
  assert.equal(api.jumpTarget(cues, 600.2, -1, [[0, 660]]), 0); // but within one range the start of the video still counts
  // The range ahead is another one: the next known line is not the next line.
  assert.equal(api.jumpTarget(cues, 300, 1, [[0, 320], [590, 660]]), null);
  assert.equal(api.jumpTarget(cues, 300, 1, [[0, 660]]), 599.85);
  assert.equal(api.jumpTarget(cues, 300, 1, []), null); // nothing covered at all
});

test("jumpTarget past the covered end still replays the line just heard: the server is catching up", () => {
  const { api } = loadContent();
  // A live stream's covered end trails the playhead by the LIVE_MIN_WINDOW the server waits for and
  // the transcription; a plain video's, whenever a window takes longer to transcribe than to play.
  const cues = [{ id: 0, start: 990, end: 993, text: "いち" }, { id: 1, start: 995, end: 998, text: "に" }];
  assert.equal(api.jumpTarget(cues, 1000.3, -1, [[0, 1000]]), 994.85); // used to be null a hundredth past the end
  assert.equal(api.jumpTarget(cues, 1012, -1, [[0, 1000]]), 994.85);
  assert.equal(api.jumpTarget(cues, 1040, -1, [[0, 1000]]), 994.85); // COVERED_LAG_S: the server's own window
  assert.equal(api.jumpTarget(cues, 1040.5, -1, [[0, 1000]]), null); // further is a seek into an untranscribed stretch
  assert.equal(api.jumpTarget(cues, 1012, 1, [[0, 1000]]), null); // nothing ahead, as before
  // The playhead's range is the one whose end lies nearest behind it, unless it sits in one.
  assert.equal(api.jumpTarget(cues, 1012, -1, [[0, 900], [950, 1000]]), 994.85);
  assert.equal(api.jumpTarget(cues, 1012, -1, [[0, 1000], [1005, 1100]]), null); // its own range is the later one
  assert.equal(api.jumpTarget(cues, 1012, -1, [[0, 1000], [1020, 1100]]), 994.85); // a range ahead changes nothing
});

// ------------------------------------------------------------------ Anki polling

test("ankiPollAllowed polls a playing video and stops on a hidden tab", () => {
  const { api, sandbox } = loadContent();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  api.state.videoId = "abcdef1234";
  assert.equal(api.ankiPollAllowed(), true);
  sandbox.document.visibilityState = "hidden";
  assert.equal(api.ankiPollAllowed(), false);
});

test("ankiPollAllowed gives up on a video left paused, and resumes for a reader", () => {
  const { api } = loadContent();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  api.state.videoId = "abcdef1234";
  api.state.pausedSince = Date.now() - 10_000;
  assert.equal(api.ankiPollAllowed(), true); // ten seconds: the viewer is probably still looking
  api.state.pausedSince = Date.now() - 180_000;
  assert.equal(api.ankiPollAllowed(), false);
  api.state.hoverPaused = true; // the pointer is on the subtitle: a lookup is in progress
  api.state.hoverSeenAt = Date.now() - 10_000;
  api.state.lastAnkiPollAt = Date.now() - 300;
  assert.equal(api.ankiPollAllowed(), true);
  // The pointer has not been seen for as long: the slow cadence, not the hover rate (the viewer
  // may still be reading the popup, whose pointer the page never sees).
  api.state.hoverSeenAt = Date.now() - 180_000;
  assert.equal(api.ankiPollAllowed(), false);
  api.state.lastAnkiPollAt = Date.now() - 5000;
  assert.equal(api.ankiPollAllowed(), true);
});

test("a hover pause whose pointer left for good drops to the slow cadence after two minutes", () => {
  const { api } = loadContent();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 1, paused: false, ended: false, pause() { this.paused = true; }, play: async () => {} };
  api.onSubtitleEnter({ isTrusted: true }); // pauses the video
  assert.equal(api.state.hoverPaused, true);
  api.onSubtitleLeave({ isTrusted: true, relatedTarget: null }); // off to the popup, or the sidebar
  assert.equal(api.state.awaitingPlayerMove, true);
  api.state.lastAnkiPollAt = Date.now() - 300;
  assert.equal(api.ankiPollAllowed(), true); // the hover rate
  // Three hours later the flags still stand (nothing but the pointer clears them), and the poll
  // used to be allowed at that rate for all of them: four AnkiConnect requests a second from a tab
  // nobody reads. One every PAUSE_POLL_SLOW_MS now: the viewer may still be reading the popup.
  api.state.pausedSince -= 3 * 3600_000;
  api.state.hoverSeenAt -= 3 * 3600_000;
  assert.equal(api.state.hoverPaused && api.state.awaitingPlayerMove, true);
  assert.equal(api.ankiPollAllowed(), false);
  api.state.lastAnkiPollAt = Date.now() - 5000;
  assert.equal(api.ankiPollAllowed(), true);
  // The pointer back over the player is the viewer back, at the hover rate.
  api.onPlayerMouseMove({ clientX: 1, clientY: 1, target: {} });
  api.state.lastAnkiPollAt = Date.now() - 300;
  assert.equal(api.ankiPollAllowed(), true);
  api.state.hoverSeenAt -= 3 * 3600_000;
  assert.equal(api.ankiPollAllowed(), false);
  api.onSubtitleEnter({ isTrusted: true });
  assert.equal(api.ankiPollAllowed(), true);
});

test("a reader still in the dictionary popup after two minutes is polled for every few seconds, under the background's ten", async () => {
  const { api, sent } = loadContent();
  await settled();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 1, paused: false, ended: false, pause() { this.paused = true; }, play: async () => {} };
  api.onSubtitleEnter({ isTrusted: true });
  api.onSubtitleLeave({ isTrusted: true, relatedTarget: { tagName: "IFRAME" } }); // into Yomitan's popup
  assert.equal(api.state.awaitingPlayerMove, true);
  // The popup is an iframe: the page sees no pointer event while the viewer reads a long entry.
  api.state.hoverSeenAt -= 150_000;
  api.state.pausedSince -= 150_000;
  const polls = () => sent.filter((m) => m.type === "ankiPoll").length;
  await api.pollForNewCard();
  assert.equal(polls(), 1); // used to be none: the card made now met a baseline the background no longer trusted
  await api.pollForNewCard();
  assert.equal(polls(), 1); // not the hover rate
  api.state.lastAnkiPollAt -= 5000;
  await api.pollForNewCard();
  assert.equal(polls(), 2);
  // The cadence stays under the background's ANKI_BASELINE_MAX_AGE_MS: a longer gap makes the
  // poll after it move the baseline past the card instead of reporting it.
  assert.ok(constant("content.js", "PAUSE_POLL_SLOW_MS") < constant("background.js", "ANKI_BASELINE_MAX_AGE_MS"));
});

test("ankiPollAllowed stays quiet with nothing to attach or the feature off", () => {
  const { api } = loadContent();
  api.state.videoId = "abcdef1234";
  assert.equal(api.ankiPollAllowed(), false); // no cues yet
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  assert.equal(api.ankiPollAllowed(), true);
  api.state.settings.autoMine = false;
  assert.equal(api.ankiPollAllowed(), false);
  api.state.settings.autoMine = true;
  api.state.offline = true;
  assert.equal(api.ankiPollAllowed(), false);
  api.state.offline = false;
  api.state.settings.mineTarget = "download"; // nothing is mined by itself there: no message a second
  assert.equal(api.ankiPollAllowed(), false);
  api.state.settings.mineTarget = "anki";
  assert.equal(api.ankiPollAllowed(), true);
});

// ------------------------------------------------------------------ live streams

// A player element the way Firefox shows it to a content script: the page's API sits behind
// wrappedJSObject, and the video element restarts its own clock at an arbitrary point.
function livePlayer(current, isLive = true) {
  return {
    removeEventListener: () => {}, // discover() lets go of it once the test's fake page is gone
    wrappedJSObject: { getVideoData: () => ({ isLive }), getProgressState: () => ({ current }) },
  };
}

test("liveClock reads the stream clock from a live player and nothing from a video", () => {
  const { api } = loadContent();
  assert.equal(api.liveClock(livePlayer(100490.5)), 100490.5);
  assert.equal(api.liveClock(livePlayer(100490.5, false)), null);
  assert.equal(api.liveClock({ wrappedJSObject: {} }), null);
  assert.equal(api.liveClock(null), null);
  assert.equal(api.liveClock({ wrappedJSObject: { getVideoData: () => { throw new Error("gone"); }, getProgressState: () => ({}) } }), null);
});

test("playhead runs on the stream clock for a live stream and on video time otherwise", () => {
  const { api } = loadContent();
  api.state.video = { currentTime: 46810.4 };
  api.state.player = livePlayer(100490.4);
  api.updateLiveClock();
  assert.equal(api.state.live, true);
  assert.equal(api.playhead().toFixed(3), "100490.400");
  api.state.video.currentTime = 46812.4; // two seconds later, between syncs
  assert.equal(api.playhead().toFixed(3), "100492.400");
  api.seekPlayhead(100400.0);
  assert.equal(api.state.video.currentTime.toFixed(3), "46720.000"); // back on the element's clock

  api.state.player = livePlayer(0, false);
  api.updateLiveClock();
  assert.equal(api.state.live, false);
  assert.equal(api.playhead().toFixed(3), "46720.000");
});

// ------------------------------------------------------------------ master switch

// An arrow key the way onKeyDown sees it; count() says whether YouTube's own seek was stopped.
function arrowKey(key) {
  let stopped = 0;
  return { key, target: { closest: () => null }, preventDefault: () => { stopped++; }, stopImmediatePropagation: () => { stopped++; }, count: () => stopped };
}

test("the master switch also silences the arrow keys", () => {
  const { api } = loadContent();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }, { id: 1, start: 5, end: 7, text: "に" }]);
  api.state.videoId = "abcdef1234";
  api.state.covered = [[0, 30]];
  api.state.video = { currentTime: 0, paused: true };
  const on = arrowKey("ArrowRight");
  api.onKeyDown(on);
  assert.equal(on.count(), 2);
  assert.equal(api.state.video.currentTime.toFixed(2), "4.85");
  api.state.settings.enabled = false;
  const off = arrowKey("ArrowRight");
  api.onKeyDown(off);
  assert.equal(off.count(), 0); // YouTube keeps its own 5 s seek
});

test("Left with no cues, or off a watch page, leaves YouTube's five second seek alone", () => {
  const { api } = loadContent();
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 300, paused: false, play: async () => {} };
  for (const status of ["offline", "transcribing", "error"]) {
    api.state.serverStatus = status;
    const left = arrowKey("ArrowLeft");
    api.onKeyDown(left);
    assert.equal(left.count(), 0, status);
    assert.equal(api.state.video.currentTime, 300, status); // used to restart the video
  }
  // Cues far behind an untranscribed stretch: not the previous line either.
  api.mergeCues([{ id: 0, start: 100, end: 102, text: "いち" }]);
  api.state.covered = [[90, 130]];
  const far = arrowKey("ArrowLeft");
  api.onKeyDown(far);
  assert.equal(far.count(), 0);
  assert.equal(api.state.video.currentTime, 300);
  api.state.covered = [[90, 400]];
  const near = arrowKey("ArrowLeft");
  api.onKeyDown(near);
  assert.equal(near.count(), 2);
  assert.equal(api.state.video.currentTime.toFixed(2), "99.85");
  // The playhead a few seconds past the covered end, the normal state of a live stream and of a
  // video the transcription lags: the line just heard is still the previous line.
  api.state.video.currentTime = 300;
  api.state.covered = [[90, 288]];
  const edge = arrowKey("ArrowLeft");
  api.onKeyDown(edge);
  assert.equal(edge.count(), 2); // used to leave the key to YouTube's 5 s seek
  assert.equal(api.state.video.currentTime.toFixed(2), "99.85");
  // The home page keeps a hidden player around: no video id, no jumps.
  api.state.videoId = null;
  api.state.video.currentTime = 300;
  const home = arrowKey("ArrowLeft");
  api.onKeyDown(home);
  assert.equal(home.count(), 0);
  assert.equal(api.state.video.currentTime, 300);
});

// ------------------------------------------------------------------ sentences

// Four cues: two of one segment a fifth of a second apart, that segment again after 13 s of
// music, then another segment. The first two are the shape that used to fuse into one card.
const SENTENCE_CUES = [
  { id: 0, seg: 7, start: 0, end: 1, text: "あ" },
  { id: 1, seg: 7, start: 1.2, end: 2, text: "い" },
  { id: 2, seg: 7, start: 15, end: 16, text: "う" },
  { id: 3, seg: 8, start: 16.1, end: 17, text: "え" },
];

test("sentenceForCue is the line on screen, never the Whisper segment around it", () => {
  const { api } = loadContent();
  const sentenceForCue = api.sentenceForCue;
  // Cues 0 and 1 are one segment 0.2 s apart: a card mined off either used to get both, and a
  // clip of 0 -> 2. Whisper's segment is not a sentence, so neither reaches past its own line.
  assert.deepEqual(plain(sentenceForCue(SENTENCE_CUES[0])), { start: 0, end: 1, text: "あ", cueIds: [0] });
  assert.deepEqual(plain(sentenceForCue(SENTENCE_CUES[1])), { start: 1.2, end: 2, text: "い", cueIds: [1] });
  assert.deepEqual(plain(sentenceForCue(SENTENCE_CUES[3])), { start: 16.1, end: 17, text: "え", cueIds: [3] });
  assert.equal(sentenceForCue(null), null);
});

test("nextSentence steps to the cue after the one it is given", () => {
  const { api } = loadContent();
  const first = api.sentenceForCue(SENTENCE_CUES[0]);
  assert.deepEqual(plain(api.nextSentence(SENTENCE_CUES, first)), { start: 1.2, end: 2, text: "い", cueIds: [1] });
  const third = api.sentenceForCue(SENTENCE_CUES[2]);
  assert.deepEqual(plain(api.nextSentence(SENTENCE_CUES, third)), { start: 16.1, end: 17, text: "え", cueIds: [3] });
  // Nothing after the last one, and nothing to step from without cue ids.
  const last = api.sentenceForCue(SENTENCE_CUES[3]);
  assert.equal(api.nextSentence(SENTENCE_CUES, last), null);
  assert.equal(api.nextSentence(SENTENCE_CUES, null), null);
  assert.equal(api.nextSentence(SENTENCE_CUES, { start: 0, end: 1, text: "x", cueIds: [99] }), null);
});

// ------------------------------------------------------------------ pre-mining

test("rankOfCue prefers the sentences the background is already holding, newest first", () => {
  const { api } = loadContent();
  const held = [
    { key: 4, cueIds: [4, 5], image: true, audio: true },
    { key: 2, cueIds: [2], image: true, audio: false },
  ];
  assert.equal(api.rankOfCue(held, { id: 5 }), 0);
  assert.equal(api.rankOfCue(held, { id: 2 }), 1);
  assert.equal(api.rankOfCue(held, { id: 9 }), Infinity);
  assert.equal(api.rankOfCue([], { id: 4 }), Infinity);
});

test("resetPremine tells the background only when the tab holds something, or has asked for it", async () => {
  const { api, sent } = loadContent();
  const before = sent.length;
  api.resetPremine();
  await settled();
  assert.equal(sent.length, before, "nothing held, nothing to drop");
  api.state.premined = [{ key: 4, cueIds: [4], image: true, audio: true }];
  api.resetPremine();
  await settled();
  assert.equal(sent[sent.length - 1].type, "premineReset");
  assert.deepEqual(plain(api.state.premined), []);
  // A premine still out will file its sentence after this: the reset must go out behind it.
  api.state.premineInFlight = 1;
  api.resetPremine();
  await settled();
  assert.equal(sent.filter((m) => m.type === "premineReset").length, 2);
});

test("premineAllowed stops on a hidden tab, an offline server and the master switch", () => {
  const { api, sandbox } = loadContent();
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 0, paused: false };
  assert.equal(api.premineAllowed(), true);
  sandbox.document.visibilityState = "hidden";
  assert.equal(api.premineAllowed(), false);
  sandbox.document.visibilityState = "visible";
  api.state.offline = true;
  assert.equal(api.premineAllowed(), false);
  api.state.offline = false;
  api.state.settings.enabled = false;
  assert.equal(api.premineAllowed(), false);
});

// ------------------------------------------------------------------ model setting

test("modelForSync sends the trimmed model name and nothing for the server default", () => {
  const { api } = loadContent();
  assert.equal(api.modelForSync({ model: " kotoba-tech/kotoba-whisper-v2.0-faster " }), "kotoba-tech/kotoba-whisper-v2.0-faster");
  assert.equal(api.modelForSync({ model: "large-v3" }), "large-v3");
  assert.equal(api.modelForSync({ model: "" }), "");
  assert.equal(api.modelForSync({ model: "   " }), "");
  assert.equal(api.modelForSync({ model: 42 }), ""); // storage holds whatever was put there
  assert.equal(api.modelForSync({}), "");
  assert.equal(api.modelForSync(null), "");
});

// A sync round trip against a fake server answer; the sandbox has no player or overlay, so only
// the request body and the state matter here.
async function syncWith(answer, model) {
  const loaded = loadContent();
  const { api, sandbox, sent } = loaded;
  sandbox.browser.runtime.sendMessage = async (msg) => {
    sent.push(msg);
    return msg.path === "/sync" ? { ok: true, data: answer } : { ok: true };
  };
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 12, paused: true };
  api.state.settings.model = model;
  await api.sync();
  return { api, body: sent.find((m) => m.path === "/sync").body };
}

test("sync asks the server for the model in the settings, trimmed, or for its default", async () => {
  assert.equal((await syncWith({ status: "ready" }, " large-v3-turbo ")).body.model, "large-v3-turbo");
  assert.equal((await syncWith({ status: "ready" }, "")).body.model, "");
});

test("sync remembers what the server is loading and what it refused, and forgets both again", async () => {
  const loading = await syncWith({ status: "ready", session: "s1", model_loading: "medium", model_error: null }, "medium");
  assert.equal(loading.api.state.modelLoading, "medium");
  assert.equal(loading.api.state.modelError, null);
  const refused = await syncWith({ status: "ready", session: "s1", model_loading: null, model_error: "not a model name" }, "../x");
  assert.equal(refused.api.state.modelLoading, null);
  assert.equal(refused.api.state.modelError, "not a model name");
  const older = await syncWith({ status: "ready", session: "s1" }, ""); // a server from before the setting
  assert.equal(older.api.state.modelLoading, null);
  assert.equal(older.api.state.modelError, null);
});

// A sequence of /sync answers, each handed out once; the request bodies are kept for inspection.
// Every other message still goes to the loader's stub, which records it in `sent`. An answer with
// `ok: false` is one of the background's own refusals (a 4xx/5xx as apiRequest passes it on, or
// unreachable) and goes out as it is; anything else is the server's data.
function serverAnswering(sandbox, answers) {
  const bodies = [];
  const other = sandbox.browser.runtime.sendMessage;
  sandbox.browser.runtime.sendMessage = async (msg) => {
    if (msg.path !== "/sync") return other(msg);
    bodies.push(msg.body);
    if (!answers.length) throw new Error("the fake server ran out of answers");
    const answer = answers.shift();
    return answer && answer.ok === false ? answer : { ok: true, data: answer };
  };
  return bodies;
}

const cue = (id, text) => ({ id, start: id * 2, end: id * 2 + 1.5, text, seg: id });

// content.js looks for the player once its settings have loaded, a few microtasks after loadContent();
// a fake video installed before that is let go of again as not on the page. Wait that out first.
const settled = () => new Promise((resolve) => setImmediate(resolve));

test("sync starts over on a new session token, so the new session's cue ids never meet the old ones", async () => {
  const { api, sandbox, sent } = loadContent();
  await settled();
  const bodies = serverAnswering(sandbox, [
    { status: "ready", session: "a", cues: [cue(0, "旧一"), cue(1, "旧二"), cue(2, "旧三")], next: 3, covered: [[0, 30]] },
    // The model switch: a fresh session whose ids begin at 0 again, answered for the old `since`.
    { status: "pending", session: "b", model_loading: "large-v3", cues: [], next: 3 },
    { status: "ready", session: "b", cues: [cue(0, "新一"), cue(1, "新二"), cue(2, "新三"), cue(3, "新四")], next: 4, covered: [[0, 40]] },
  ]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 0.5, paused: true };
  await api.sync();
  assert.equal(bodies[0].since, 0);
  assert.equal(api.cueById(0).text, "旧一");
  api.state.activeCueId = 2;
  api.state.premined = [{ key: 2, cueIds: [2], image: true }];

  await api.sync();
  assert.equal(bodies[1].since, 3);
  assert.equal(api.state.serverSession, "b");
  assert.deepEqual(plain(api.state.cues), []);
  assert.equal(api.cueById(0), null); // the old text is gone with the old ids
  assert.equal(api.state.cueById.size, 0);
  assert.equal(api.state.since, 0);
  assert.deepEqual(plain(api.state.covered), []);
  assert.equal(api.state.activeCueId, null);
  assert.deepEqual(plain(api.state.premined), []); // keyed by a cue id of the old session
  assert.ok(sent.some((m) => m.type === "premineReset"), "the background drops the held sentence too");
  assert.equal(api.state.modelLoading, "large-v3");
  assert.equal(api.state.serverStatus, "pending");

  await api.sync();
  assert.equal(bodies[2].since, 0);
  assert.deepEqual(plain(api.state.cues.map((c) => c.text)), ["新一", "新二", "新三", "新四"]);
  assert.equal(api.cueById(0).text, "新一");
  assert.equal(api.findActiveCue(0.5).text, "新一");
  assert.equal(api.findActiveCue(6.5).text, "新四");
  assert.equal(api.state.since, 4);
});

test("a changed model name drops the old name's verdict and asks the server at once", () => {
  const { api, sent, onSettingsChanged } = loadContent();
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 12, paused: true };
  api.state.settings.model = "nope/../x";
  api.state.modelError = "not a model name";
  api.state.modelLoading = "medium";
  const syncs = () => sent.filter((m) => m.path === "/sync");

  onSettingsChanged({ settings: { newValue: { model: "nope/../x", showStatus: false } } }, "local");
  assert.equal(api.state.modelError, "not a model name"); // another setting: the verdict stands
  assert.equal(api.state.modelLoading, "medium");
  assert.equal(api.state.settings.showStatus, false);
  assert.equal(syncs().length, 0);

  onSettingsChanged({ settings: { newValue: { model: " large-v3 " } } }, "local");
  assert.equal(api.state.settings.model, " large-v3 ");
  assert.equal(api.state.modelError, null);
  assert.equal(api.state.modelLoading, null);
  assert.equal(syncs().length, 1);
  assert.equal(syncs()[0].body.model, "large-v3");

  onSettingsChanged({ settings: { newValue: { model: "large-v3" } } }, "sync"); // not our area
  assert.equal(syncs().length, 1);
});

test("a model changed while a request is out: that answer's verdict is left out and the new name asked about", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  const bodies = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  sandbox.browser.runtime.sendMessage = async (msg) => {
    if (msg.path !== "/sync") return { ok: true };
    bodies.push(msg.body);
    if (bodies.length === 1) {
      await gate; // the first request stays out until the test lets it back
      return { ok: true, data: { status: "ready", session: "s1", model_loading: null, model_error: "not a model name", next: 3 } };
    }
    return { ok: true, data: { status: "ready", session: "s1", model_loading: "large-v3", model_error: null, next: 3 } };
  };
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 12, paused: true };
  api.state.settings.model = "../x";
  const first = api.sync();
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].model, "../x");

  onSettingsChanged({ settings: { newValue: { model: "large-v3" } } }, "local");
  assert.equal(bodies.length, 1); // in flight: nothing more went out
  assert.equal(api.state.modelError, null);
  release();
  await first;
  assert.equal(api.state.modelError, null); // the old name's verdict, not put back
  assert.equal(api.state.modelLoading, null);
  assert.equal(api.state.since, 3); // the rest of the answer still counts
  assert.equal(bodies.length, 2); // and the new name went out right after it
  assert.equal(bodies[1].model, "large-v3");
  assert.equal(bodies[1].since, 3);
  await settled();
  assert.equal(api.state.modelLoading, "large-v3");
  assert.equal(bodies.length, 2);

  // Changed and changed back while a request is out: its answer is about the current name.
  api.state.settings.model = "large-v3";
  const third = api.sync();
  onSettingsChanged({ settings: { newValue: { model: "small" } } }, "local");
  onSettingsChanged({ settings: { newValue: { model: " large-v3 " } } }, "local");
  await third;
  assert.equal(api.state.modelLoading, "large-v3");
  assert.equal(bodies.length, 3);
});

function statusElement() {
  const el = { textContent: "", classes: new Set() };
  el.classList = {
    add: (c) => el.classes.add(c),
    remove: (c) => el.classes.delete(c),
    contains: (c) => el.classes.has(c),
    toggle: (c, on) => (on ? el.classes.add(c) : el.classes.delete(c)),
  };
  return el;
}

test("updateStatus caps the server's model error like a toast", () => {
  const { api } = loadContent();
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.serverStatus = "ready";
  api.state.settings.model = "large-v3";
  api.state.modelError = "e".repeat(160);
  api.updateStatus();
  assert.equal(el.textContent, `Shisu-ko: model large-v3: ${"e".repeat(160)}`); // exactly the cap: untouched
  api.state.modelError = "e".repeat(161);
  api.updateStatus();
  assert.equal(el.textContent, `Shisu-ko: model large-v3: ${"e".repeat(159)}…`);
  api.state.settings.model = "x".repeat(300); // the server refuses it; the line still fits
  api.updateStatus();
  assert.equal(el.textContent, `Shisu-ko: model ${"x".repeat(99)}…: ${"e".repeat(159)}…`);
});

// Alt+Shift+H, or the popup's switch: the badge goes, whatever it would say, errors included.
test("statusText and updateStatus show nothing with the status badge off, the red badge included", () => {
  const { api } = loadContent();
  for (const patch of [
    { offline: true },
    { status: "error", error: "boom" },
    { modelError: "not a model name", model: "x" },
    { standby: true },
    { languagePaused: true, heard: "en" },
    { status: "connecting" },
    { ahead: 105 },
  ]) {
    assert.notEqual(says(api, patch).text, null, JSON.stringify(patch));
    assert.equal(says(api, { ...patch, statusBadge: false }).text, null, JSON.stringify(patch));
    assert.deepEqual(says(api, { ...patch, statusBadge: true }), says(api, patch), "on is the default");
  }
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.offline = true;
  api.updateStatus();
  assert.equal(el.textContent, "Shisu-ko server offline. Start it with server/run.cmd or docker/up.cmd");
  assert.ok(el.classes.has("shisuko-status-error") && !el.classes.has("shisuko-hidden"));
  api.state.settings.statusBadge = false;
  api.updateStatus();
  assert.ok(el.classes.has("shisuko-hidden"), "the red badge is hidden");
  api.state.settings.statusBadge = true;
  api.updateStatus();
  assert.ok(!el.classes.has("shisuko-hidden"), "and back");
});

test("updateStatus shows a refused model even with progress messages off, and a load in progress", () => {
  const { api } = loadContent();
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.serverStatus = "ready";
  api.state.settings.showStatus = false;
  api.state.settings.model = "nope/../x";
  api.state.modelError = "not a model name";
  api.updateStatus();
  assert.equal(el.textContent, "Shisu-ko: model nope/../x: not a model name");
  assert.ok(el.classes.has("shisuko-status-error"));
  assert.ok(!el.classes.has("shisuko-hidden"));

  api.state.modelError = null;
  api.state.modelLoading = "large-v3-turbo";
  api.updateStatus();
  assert.ok(el.classes.has("shisuko-hidden")); // a plain progress message obeys the setting
  api.state.settings.showStatus = true;
  api.updateStatus();
  assert.equal(el.textContent, "Loading model large-v3-turbo… (a first use downloads it)");
  assert.ok(!el.classes.has("shisuko-status-error"));
});

// The audio fetch and the model load run on their own threads on the server: a members-only video
// fails within seconds while a first large-v3 downloads for minutes, and both come in one answer.
test("updateStatus keeps a failed session in front of a model load, even with progress messages off", () => {
  const { api } = loadContent();
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.serverStatus = "error";
  api.state.serverError = "This video is only available to members";
  api.state.modelLoading = "large-v3";
  api.state.settings.showStatus = false;
  api.updateStatus();
  assert.equal(el.textContent, "Shisu-ko: This video is only available to members");
  assert.ok(el.classes.has("shisuko-status-error"));
  assert.ok(!el.classes.has("shisuko-hidden"));

  api.state.settings.model = "large-v3";
  api.state.modelError = "no such model large-v3"; // the model's own verdict still comes first
  api.updateStatus();
  assert.equal(el.textContent, "Shisu-ko: model large-v3: no such model large-v3");

  api.state.modelError = null;
  api.state.serverStatus = "downloading"; // the retry: now the load is what transcription waits for
  api.state.settings.showStatus = true;
  api.updateStatus();
  assert.equal(el.textContent, "Loading model large-v3… (a first use downloads it)");
  assert.ok(!el.classes.has("shisuko-status-error"));
});

// ------------------------------------------------------------------ font family

const GOTHIC = '"Noto Sans JP", "Noto Sans CJK JP", "Yu Gothic UI", "Yu Gothic", "Meiryo", "Hiragino Sans", sans-serif';

test("fontStack puts an installed family in front of the preset's stack", () => {
  const { api } = loadContent();
  assert.equal(api.fontStack("default", "Yu Gothic UI"), `"Yu Gothic UI", ${GOTHIC}`);
  assert.equal(api.fontStack("default", "  游ゴシック  "), `"游ゴシック", ${GOTHIC}`); // trimmed, Japanese names allowed
  assert.equal(api.fontStack("gothic-bold", "UD Digi Kyokasho NP-R"), `"UD Digi Kyokasho NP-R", ${GOTHIC}`);
  assert.equal(api.fontStack("default", "M PLUS 1p"), `"M PLUS 1p", ${GOTHIC}`);
  assert.equal(api.fontStack("default", "Noto.Sans_JP"), `"Noto.Sans_JP", ${GOTHIC}`);
  assert.ok(api.fontStack("mincho", "Klee").startsWith('"Klee", "Noto Serif JP"'));
});

test("fontStack falls back to the preset alone for an empty or unusable family name", () => {
  const { api } = loadContent();
  assert.equal(api.fontStack("default", ""), GOTHIC);
  assert.equal(api.fontStack("default", undefined), GOTHIC);
  assert.equal(api.fontStack("default", 7), GOTHIC);
  assert.equal(api.fontStack("nonsense", ""), GOTHIC); // an unknown preset is the default one
  // Written as JS literals: "a\\b" holds a backslash, "a\bb" a backspace, "a\nb" a newline.
  for (const bad of ['Yu "Gothic"', "Yu; color: red", "a { b }", "a\\b", "a\bb", "a\nb", "Yu\tGothic", "url(https://evil.example/x)", "-leading", "x".repeat(101)]) {
    assert.equal(api.fontStack("default", bad), GOTHIC, JSON.stringify(bad));
  }
  assert.ok(!api.fontStack("default", "a\\b").includes("\\")); // a backslash would escape the closing quote
  assert.equal(api.fontStack("default", "x".repeat(100)), `"${"x".repeat(100)}", ${GOTHIC}`);
});

// ------------------------------------------------------------------ word colours

const words = require("../words");

// A deck as the background hands it over, [word, status, pitch], and a line holding both words.
const DECK = [["日本語", "learned", null], ["字幕", "new", "heiban"]];
const LINE = "これは日本語の字幕です";

// The lines of these tests show the deck's words alone, which is what they are about: the
// particle switch, off by default, stays off unless a test is about it ("the particle switch" below),
// so what they expect holds whatever the matcher makes of a particle.
const WORDS_ALONE = Object.freeze({ particlesKnown: false });

// What renderText put into an element: a text node as its text, a span as class{marks}:text.
function nodes(el) {
  return el.childNodes.map((node) => {
    if (node.nodeType === 3) return node.textContent;
    const marks = Object.entries(node.dataset).map(([key, value]) => `${key}=${value}`).join(",");
    return `${node.className}{${marks}}:${node.textContent}`;
  });
}

// An index put in the way pollWordIndex() does it: every index held, and none, has a serial of
// its own, which is what dates a cue's look (a look never holds the index it was found under).
function setIndex(api, index) {
  api.state.wordIndex = index;
  api.state.wordIndexSerial++;
}

// The deck's index as a poll would have left it. An async test gives it only after settled():
// the settings loaded at start-up replace whatever was put in state.settings before.
function giveIndex(api, settings) {
  Object.assign(api.state.settings, WORDS_ALONE, settings);
  setIndex(api, words.buildIndex(DECK));
  api.state.wordIndexAt = 1000;
  api.state.wordIndexKey = JSON.stringify(DECK);
}

function withIndex(settings) {
  const loaded = loadContent();
  giveIndex(loaded.api, settings);
  return loaded;
}

// A subtitle box and its text span, as buildOverlay() would have made them.
function subtitleBox(api, sandbox) {
  api.state.subBox = sandbox.document.createElement("div");
  api.state.subText = sandbox.document.createElement("span");
}

// A watch page whose player discover() has found: the only kind of tab the deck index is asked from.
function watching(api) {
  api.state.video = { currentTime: 0, paused: true };
  api.state.videoId = "abcdef1234";
}

// The overlay parts applySettings() and a shown transcript touch, and a count of the panel's rebuilds.
function overlay(api, sandbox) {
  subtitleBox(api, sandbox);
  api.state.root = sandbox.document.createElement("div");
  api.state.transcriptEl = sandbox.document.createElement("div");
  const list = sandbox.document.createElement("div");
  api.state.transcriptList = list;
  const rebuilds = { count: 0 };
  const replace = list.replaceChildren;
  list.replaceChildren = (...nodes) => {
    rebuilds.count++;
    return replace(...nodes);
  };
  return rebuilds;
}

// The background's answers to the cardStatus asks, handed out in order and the asks kept; every
// other message still goes to the loader's stub.
function backgroundAnswering(sandbox, answers) {
  const asks = [];
  const other = sandbox.browser.runtime.sendMessage;
  sandbox.browser.runtime.sendMessage = async (msg) => {
    if (msg.type !== "cardStatus") return other(msg);
    asks.push(msg);
    if (!answers.length) throw new Error("the fake background ran out of answers");
    return answers.shift();
  };
  return asks;
}

// How often content.js asks the matcher from now on: markWords() once per line matched against
// the index, wordStarts() once per line segmented. The harness leaves words.js's export writable
// for this wrapper; content.js reads the global at every call.
function countingWords(sandbox) {
  const real = sandbox.SHISUKO_WORDS;
  const counts = { matched: 0, segmented: 0 };
  sandbox.SHISUKO_WORDS = Object.assign({}, real, {
    wordStarts: (...args) => {
      counts.segmented++;
      return real.wordStarts(...args);
    },
    markWords: (...args) => {
      counts.matched++;
      return real.markWords(...args);
    },
  });
  return counts;
}

test("wordColoursOn needs the master switch and one of the two colours", () => {
  const { api } = loadContent();
  assert.equal(api.wordColoursOn(), false);
  api.state.settings.cardStatus = true;
  assert.equal(api.wordColoursOn(), true);
  api.state.settings.cardStatus = false;
  api.state.settings.pitchAccent = true;
  assert.equal(api.wordColoursOn(), true);
  api.state.settings.enabled = false;
  assert.equal(api.wordColoursOn(), false);
});

test("renderText writes plain text while both colours are off, or without an index", () => {
  const { api, sandbox } = withIndex({});
  const el = sandbox.document.createElement("span");
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), [LINE]);
  api.state.settings.cardStatus = true;
  api.state.settings.enabled = false;
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), [LINE]);
  api.state.settings.enabled = true;
  setIndex(api, null);
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), [LINE]);
});

test("renderText marks the card's state only with cardStatus on, in spans holding text nodes", () => {
  const { api, sandbox } = withIndex({ cardStatus: true });
  const el = sandbox.document.createElement("span");
  api.renderText(el, cue(0, LINE));
  // の and です have no card of their own, so they stay plain text between the coloured words.
  assert.deepEqual(nodes(el), ["これは", "shisuko-word{status=learned}:日本語", "の", "shisuko-word{status=new}:字幕", "です"]);
  assert.equal(el.textContent, LINE); // the DOM text is the line, for Yomitan
  assert.equal(el.childNodes[1].childNodes[0].nodeType, 3);
  api.renderText(el, cue(1, "字幕")); // drawn again: the old children go
  assert.deepEqual(nodes(el), ["shisuko-word{status=new}:字幕"]);
});

test("renderText marks the pitch only with pitchAccent on, and joins the text around it", () => {
  const { api, sandbox } = withIndex({ pitchAccent: true });
  const el = sandbox.document.createElement("span");
  api.renderText(el, cue(0, LINE));
  // 日本語 has a card but no pitch: with the state not shown it is text like the rest.
  assert.deepEqual(nodes(el), ["これは日本語の", "shisuko-word{pitch=heiban}:字幕", "です"]);
  assert.equal(el.textContent, LINE);
});

test("renderText marks both with both colours on", () => {
  const { api, sandbox } = withIndex({ cardStatus: true, pitchAccent: true });
  const el = sandbox.document.createElement("span");
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), ["これは", "shisuko-word{status=learned}:日本語", "の", "shisuko-word{status=new,pitch=heiban}:字幕", "です"]);
  api.renderText(el, cue(1, ""));
  assert.deepEqual(nodes(el), []);
});

test("setSubtitle and transcriptLine draw their text through renderText", () => {
  const { api, sandbox } = withIndex({ cardStatus: true });
  subtitleBox(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  assert.equal(api.state.activeCueId, 0);
  assert.ok(!api.state.subBox.classList.contains("shisuko-hidden"));
  assert.equal(nodes(api.state.subText)[1], "shisuko-word{status=learned}:日本語");
  assert.equal(api.state.subText.textContent, LINE);

  const line = api.transcriptLine(api.cueById(0));
  assert.equal(line.className, "shisuko-line");
  assert.equal(line.dataset.id, "0");
  const text = line.childNodes[1];
  assert.equal(text.className, "shisuko-linetext");
  assert.deepEqual(nodes(text), ["これは", "shisuko-word{status=learned}:日本語", "の", "shisuko-word{status=new}:字幕", "です"]);
  assert.equal(api.state.lineById.get(0), line);

  api.setSubtitle(null);
  assert.equal(api.state.subText.textContent, "");
  assert.ok(api.state.subBox.classList.contains("shisuko-hidden"));
});

test("refreshWordMarks redraws the line on screen and, in a transcript that is up, only the lines that changed", () => {
  const { api, sandbox } = loadContent();
  Object.assign(api.state.settings, WORDS_ALONE, { cardStatus: true });
  api.state.settings.showTranscript = true;
  const rebuilds = overlay(api, sandbox);
  const list = api.state.transcriptList;
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "字幕" }, { id: 2, start: 5, end: 6, text: "はい" }]);
  api.setSubtitle(api.cueById(0));
  assert.equal(rebuilds.count, 1);
  const lines = [0, 1, 2].map((id) => api.state.lineById.get(id));
  const textOf = (i) => lines[i].childNodes[1];
  assert.deepEqual(nodes(api.state.subText), [LINE]); // no index yet
  assert.deepEqual(nodes(textOf(0)), [LINE]);
  const plain = lines.map((line, i) => textOf(i).childNodes[0]);
  list.scrollTop = 123;

  setIndex(api, words.buildIndex(DECK));
  api.refreshWordMarks();
  assert.equal(nodes(api.state.subText).length, 5);
  assert.equal(rebuilds.count, 1); // the lines stay: only their text is drawn again
  assert.equal(list.childNodes.length, 3);
  for (const [i, line] of lines.entries()) assert.equal(api.state.lineById.get(i), line);
  assert.equal(api.state.activeLineEl, lines[0]);
  assert.equal(list.scrollTop, 123); // nothing re-centres the panel
  assert.equal(nodes(textOf(0)).length, 5);
  assert.deepEqual(nodes(textOf(1)), ["shisuko-word{status=new}:字幕"]);
  assert.equal(textOf(2).childNodes[0], plain[2]); // no word of the deck in it: not touched
  assert.equal(api.state.transcriptDirty, false);
  assert.equal(api.state.transcriptAppendFrom, null);

  // The same look again: nothing is touched. One card reviewed: the line on screen and the
  // lines with that word are drawn again, the others keep their nodes.
  const drawn = [0, 1, 2].map((i) => textOf(i).childNodes[0]);
  api.refreshWordMarks();
  assert.deepEqual([0, 1, 2].map((i) => textOf(i).childNodes[0]), drawn);
  setIndex(api, words.buildIndex([["日本語", "learned", null], ["字幕", "learning", "heiban"]]));
  api.refreshWordMarks();
  assert.equal(nodes(api.state.subText)[3], "shisuko-word{status=learning}:字幕");
  assert.notEqual(textOf(0).childNodes[0], drawn[0]);
  assert.deepEqual(nodes(textOf(1)), ["shisuko-word{status=learning}:字幕"]);
  assert.equal(textOf(2).childNodes[0], drawn[2]);
  assert.equal(rebuilds.count, 1);

  // Lines still pending (they came while the panel was hidden) go in first, drawn under the
  // index of now; the lines up keep their nodes: never a rebuild.
  api.state.settings.showTranscript = false;
  api.mergeCues([{ id: 3, start: 7, end: 8, text: "字幕" }]);
  assert.equal(api.state.transcriptDirty, true);
  api.state.settings.showTranscript = true;
  api.refreshWordMarks();
  assert.equal(rebuilds.count, 1);
  assert.equal(api.state.transcriptDirty, false);
  assert.equal(list.childNodes.length, 4);
  for (const [i, line] of lines.entries()) assert.equal(api.state.lineById.get(i), line);
  assert.deepEqual(nodes(api.state.lineById.get(3).childNodes[1]), ["shisuko-word{status=learning}:字幕"]);
  assert.equal(textOf(2).childNodes[0], drawn[2]);

  api.state.settings.showTranscript = false;
  setIndex(api, null);
  api.refreshWordMarks();
  assert.deepEqual(nodes(api.state.subText), [LINE]);
  assert.equal(api.state.lineById.get(0), lines[0]); // a hidden transcript is left for applySettings()
  assert.deepEqual(nodes(textOf(1)), ["shisuko-word{status=learning}:字幕"]); // as it was: nobody sees it
  assert.equal(api.state.transcriptDirty, false);
});

test("a panel shown again catches up in place on the index that changed while it was hidden", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true, showTranscript: true });
  const rebuilds = overlay(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "字幕" }, { id: 2, start: 5, end: 6, text: "はい" }]);
  api.setSubtitle(api.cueById(0));
  const lines = [0, 1, 2].map((id) => api.state.lineById.get(id));
  const drawn = lines.map((line) => line.childNodes[1].childNodes[0]);
  const base = Object.assign({}, api.state.settings);
  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { showTranscript: false }) } }, "local");
  // A card reviewed while the panel was hidden: the line on screen is drawn again, the panel is
  // left alone (nobody sees it).
  setIndex(api, words.buildIndex([["日本語", "learned", null], ["字幕", "learning", "heiban"]]));
  api.refreshWordMarks();
  assert.equal(nodes(api.state.subText)[3], "shisuko-word{status=learning}:字幕");
  assert.deepEqual(lines.map((line) => line.childNodes[1].childNodes[0]), drawn);
  const counts = countingWords(sandbox);

  onSettingsChanged({ settings: { newValue: Object.assign({}, base) } }, "local");
  assert.equal(rebuilds.count, 1); // never a rebuild: the lines catch up where they are
  for (const [i, line] of lines.entries()) assert.equal(api.state.lineById.get(i), line);
  assert.equal(nodes(lines[0].childNodes[1])[3], "shisuko-word{status=learning}:字幕");
  assert.deepEqual(nodes(lines[1].childNodes[1]), ["shisuko-word{status=learning}:字幕"]);
  assert.equal(lines[2].childNodes[1].childNodes[0], drawn[2]); // looks the same: its node stays
  // Matched against the index of now (the first line's look was found for the line on screen
  // already), never segmented again.
  assert.deepEqual(counts, { matched: 2, segmented: 0 });
  await settled();
});

test("a word setting change takes the colours off a shown transcript's lines in place, without a rebuild", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true, showTranscript: true });
  const rebuilds = overlay(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "字幕" }, { id: 2, start: 5, end: 6, text: "はい" }]);
  api.setSubtitle(api.cueById(0));
  assert.equal(rebuilds.count, 1);
  assert.equal(nodes(api.state.subText).length, 5);
  const lines = [0, 1, 2].map((id) => api.state.lineById.get(id));
  const plainNode = lines[2].childNodes[1].childNodes[0];
  backgroundAnswering(sandbox, [{ ok: true, unchanged: true }]);

  onSettingsChanged({ settings: { newValue: { cardStatus: true, showTranscript: true, cardStatusDeck: "Vocab" } } }, "local");
  assert.equal(rebuilds.count, 1); // nothing is built again: the lines that had a colour lose it where they are
  for (const [i, line] of lines.entries()) assert.equal(api.state.lineById.get(i), line);
  assert.deepEqual(nodes(api.state.subText), [LINE]);
  assert.deepEqual(nodes(lines[0].childNodes[1]), [LINE]);
  assert.deepEqual(nodes(lines[1].childNodes[1]), ["字幕"]);
  assert.equal(lines[2].childNodes[1].childNodes[0], plainNode); // never had a colour: not touched
  await settled();
});

test("a style setting written leaves a shown transcript's lines and the line on screen as they are", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true, showTranscript: true });
  const rebuilds = overlay(api, sandbox);
  const styled = [];
  api.state.root.style.setProperty = (name, value) => styled.push([name, value]);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "はい" }]);
  api.setSubtitle(api.cueById(0));
  assert.equal(rebuilds.count, 1);
  const lines = [0, 1].map((id) => api.state.lineById.get(id));
  const drawn = lines.map((line) => line.childNodes[1].childNodes[0]);
  const shown = api.state.subText.childNodes[0];
  const counts = countingWords(sandbox);
  const base = Object.assign({}, api.state.settings);

  // A slider dragged in the popup writes the settings several times a second.
  for (const opacity of [40, 50, 60]) {
    onSettingsChanged({ settings: { newValue: Object.assign({}, base, { subBackgroundOpacity: opacity }) } }, "local");
  }
  assert.equal(rebuilds.count, 1);
  for (const [i, line] of lines.entries()) assert.equal(api.state.lineById.get(i), line);
  assert.deepEqual(lines.map((line) => line.childNodes[1].childNodes[0]), drawn);
  assert.equal(api.state.subText.childNodes[0], shown);
  assert.deepEqual(counts, { matched: 0, segmented: 0 });
  assert.ok(styled.some(([name, value]) => name === "--shisuko-sub-bg" && value === "rgba(0, 0, 0, 0.6)")); // the style itself landed

  // The master switch decides a line's look too (wordColoursOn), and costs nothing either way:
  // off, the lines keep their colours under the hidden root (nobody sees them, and the listener
  // runs in every tab showing a transcript); on again, the refresh finds every line still drawn
  // under the index it holds and touches none, so the switch costs no matching and no rewrite.
  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { enabled: false }) } }, "local");
  assert.equal(rebuilds.count, 1);
  assert.ok(api.state.root.classList.contains("shisuko-hidden"));
  for (const [i, line] of lines.entries()) assert.equal(api.state.lineById.get(i), line);
  assert.deepEqual(lines.map((line) => line.childNodes[1].childNodes[0]), drawn); // not rewritten
  assert.equal(nodes(lines[0].childNodes[1]).length, 5);
  onSettingsChanged({ settings: { newValue: Object.assign({}, base) } }, "local");
  assert.equal(rebuilds.count, 1);
  assert.ok(!api.state.root.classList.contains("shisuko-hidden"));
  assert.deepEqual(lines.map((line) => line.childNodes[1].childNodes[0]), drawn); // found unchanged
  assert.equal(nodes(lines[0].childNodes[1]).length, 5);
  assert.deepEqual(counts, { matched: 0, segmented: 0 });
  // Hidden, the lines are left alone; shown again with nothing changed meanwhile, they stay.
  const redrawn = lines.map((line) => line.childNodes[1].childNodes[0]);
  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { showTranscript: false }) } }, "local");
  assert.equal(rebuilds.count, 1); // hidden: nothing to build
  onSettingsChanged({ settings: { newValue: Object.assign({}, base) } }, "local");
  assert.equal(rebuilds.count, 1);
  assert.deepEqual(lines.map((line) => line.childNodes[1].childNodes[0]), redrawn);
  assert.deepEqual(counts, { matched: 0, segmented: 0 });
  await settled();
});

test("a transcript built anew and the line on screen draw the runs of the last draw: nothing is matched or segmented again", () => {
  const { api, sandbox } = withIndex({ cardStatus: true, showTranscript: true });
  const rebuilds = overlay(api, sandbox);
  const counts = countingWords(sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "字幕" }, { id: 2, start: 5, end: 6, text: "はい" }]);
  assert.equal(rebuilds.count, 1);
  assert.deepEqual(counts, { matched: 3, segmented: 3 });
  const look = ["これは", "shisuko-word{status=learned}:日本語", "の", "shisuko-word{status=new}:字幕", "です"];
  assert.deepEqual(nodes(api.state.lineById.get(0).childNodes[1]), look);
  api.setSubtitle(api.cueById(0)); // the cue of a line already drawn
  assert.deepEqual(nodes(api.state.subText), look);
  assert.deepEqual(counts, { matched: 3, segmented: 3 });

  // The panel built anew (YouTube replaced the player, and buildOverlay() asks for every line
  // again, the way dropCues() does): the lines are drawn from what the last draw found.
  api.state.transcriptRebuild = true;
  api.state.transcriptDirty = true;
  api.refreshWordMarks();
  assert.equal(rebuilds.count, 2);
  assert.deepEqual(nodes(api.state.lineById.get(0).childNodes[1]), look);
  assert.deepEqual(counts, { matched: 3, segmented: 3 });

  // Another colour on: the runs differ, the word boundaries do not.
  api.state.settings.pitchAccent = true;
  api.refreshWordMarks();
  assert.equal(rebuilds.count, 2);
  assert.equal(nodes(api.state.lineById.get(0).childNodes[1])[3], "shisuko-word{status=new,pitch=heiban}:字幕");
  assert.deepEqual(counts, { matched: 6, segmented: 3 });

  // A new index: the lines holding a word the two indexes differ on are matched again (the third,
  // はい, is not), segmented never.
  setIndex(api, words.buildIndex([["字幕", "learning", null]]));
  api.refreshWordMarks();
  assert.equal(rebuilds.count, 2);
  assert.deepEqual(nodes(api.state.lineById.get(0).childNodes[1]), ["これは日本語の", "shisuko-word{status=learning}:字幕", "です"]);
  assert.deepEqual(nodes(api.state.subText), ["これは日本語の", "shisuko-word{status=learning}:字幕", "です"]);
  assert.deepEqual(counts, { matched: 8, segmented: 3 });
});

test("one card reviewed: only the lines holding that word, or a form of it, are matched again", () => {
  const { api, sandbox } = loadContent();
  Object.assign(api.state.settings, WORDS_ALONE, { cardStatus: true });
  api.state.settings.showTranscript = true;
  const rebuilds = overlay(api, sandbox);
  const deck = [["日本語", "learned", null], ["字幕", "new", "heiban"], ["食べる", "new", null]];
  const texts = [LINE, "字幕", "はい", "昨日食べた", "食事です"];
  api.mergeCues(texts.map((text, id) => ({ id, start: id * 2, end: id * 2 + 1, text })));
  api.setSubtitle(api.cueById(2));
  setIndex(api, words.buildIndex(deck));
  api.refreshWordMarks();
  const textOf = (id) => api.state.lineById.get(id).childNodes[1];
  assert.deepEqual(nodes(textOf(3)), ["昨日", "shisuko-word{status=new}:食べた"]); // found by its stem
  assert.deepEqual(nodes(textOf(4)), ["食事です"]);
  const drawn = texts.map((text, id) => textOf(id).childNodes[0]);
  const counts = countingWords(sandbox);
  const reindex = (entries) => {
    counts.matched = 0;
    setIndex(api, words.buildIndex(entries));
    api.refreshWordMarks();
  };

  // 字幕 reviewed: the two lines holding it.
  reindex([["日本語", "learned", null], ["字幕", "learning", "heiban"], ["食べる", "new", null]]);
  assert.deepEqual(counts, { matched: 2, segmented: 0 });
  assert.deepEqual(nodes(textOf(1)), ["shisuko-word{status=learning}:字幕"]);
  assert.equal(nodes(textOf(0))[3], "shisuko-word{status=learning}:字幕");
  assert.deepEqual([2, 3, 4].map((id) => textOf(id).childNodes[0]), [drawn[2], drawn[3], drawn[4]]);

  // 食べる learned: the line holding 食べた, by the stem; the one holding 食事 is not looked at.
  reindex([["日本語", "learned", null], ["字幕", "learning", "heiban"], ["食べる", "learned", null]]);
  assert.deepEqual(counts, { matched: 1, segmented: 0 });
  assert.deepEqual(nodes(textOf(3)), ["昨日", "shisuko-word{status=learned}:食べた"]);
  assert.equal(textOf(4).childNodes[0], drawn[4]);

  // A card deleted and one added: the lines holding either word, the line on screen among them.
  reindex([["日本語", "learned", null], ["食べる", "learned", null], ["はい", "new", null]]);
  assert.deepEqual(counts, { matched: 3, segmented: 0 });
  assert.deepEqual(nodes(textOf(1)), ["字幕"]);
  assert.deepEqual(nodes(textOf(2)), ["shisuko-word{status=new}:はい"]);
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=new}:はい"]);
  assert.equal(textOf(4).childNodes[0], drawn[4]);

  // The same words again: no line is looked at.
  reindex([["日本語", "learned", null], ["食べる", "learned", null], ["はい", "new", null]]);
  assert.deepEqual(counts, { matched: 0, segmented: 0 });

  // More words changed than are worth looking for in every line: every line is matched again.
  const many = [];
  for (let i = 0; i < 65; i++) many.push([`語${i}`, "new", null]);
  reindex([["日本語", "learned", null], ["食べる", "learned", null], ["はい", "new", null]].concat(many));
  assert.deepEqual(counts, { matched: 5, segmented: 0 });
  assert.deepEqual(nodes(textOf(2)), ["shisuko-word{status=new}:はい"]);
  assert.equal(rebuilds.count, 1); // never a rebuild
});

test("a cue drawn while the transcript is hidden keeps nothing of the index it was drawn under", () => {
  // The transcript hidden, as by default: a refresh visits the line on screen and no other cue,
  // so what the other cues' looks hold stays held until the video changes. With a card reviewed
  // in Anki every half minute, a look holding its index would pin one deck index per review.
  const { api, sandbox } = loadContent();
  Object.assign(api.state.settings, WORDS_ALONE, { cardStatus: true });
  subtitleBox(api, sandbox);
  const texts = ["字幕です", "日本語です", "はい", "昨日食べた", "食事"];
  api.mergeCues(texts.map((text, id) => ({ id, start: id * 2, end: id * 2 + 1, text })));
  const indexes = [];
  for (const [i] of texts.entries()) {
    // One review per index, and one cue shown under each.
    const status = i % 2 ? "learning" : "new";
    setIndex(api, words.buildIndex([["字幕", status, null], ["日本語", "learned", null], [`語${i}`, "new", null]]));
    indexes.push(api.state.wordIndex);
    api.refreshWordMarks();
    api.setSubtitle(api.cueById(i));
  }
  const stale = indexes.slice(0, -1);
  for (const cue of api.state.cues) {
    const look = api.state.cueLooks.get(cue);
    assert.ok(look, cue.text);
    for (const value of Object.values(look)) assert.ok(!stale.includes(value), `${cue.text} keeps an index of before`);
  }
  assert.equal(api.state.wordIndexDrawn, indexes[indexes.length - 1]); // the index of now is held where it is used

  // A look from an index of before is not taken for the look of now: the cue shown again is
  // matched against the index of now, and the look it gets is good until the next index.
  const counts = countingWords(sandbox);
  api.setSubtitle(api.cueById(0));
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=new}:字幕", "です"]);
  assert.deepEqual(counts, { matched: 1, segmented: 0 });
  api.setSubtitle(api.cueById(0));
  assert.deepEqual(counts, { matched: 1, segmented: 0 });
  setIndex(api, words.buildIndex([["字幕", "learned", null]]));
  api.refreshWordMarks(); // the line on screen holds 字幕: drawn again under the index of now
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=learned}:字幕", "です"]);
  assert.deepEqual(counts, { matched: 2, segmented: 0 });
  setIndex(api, null);
  api.refreshWordMarks();
  assert.deepEqual(nodes(api.state.subText), [texts[0]]);
  setIndex(api, words.buildIndex([["字幕", "learned", null]]));
  api.refreshWordMarks(); // a new index after none: the look from two indexes ago is not reused
  assert.deepEqual(counts, { matched: 3, segmented: 0 });
});

test("pollWordIndex asks only with a colour on, a video, the tab visible and the interval past, one ask at a time", async () => {
  const { api, sandbox } = loadContent();
  const asks = backgroundAnswering(sandbox, [{ ok: true, unchanged: true }, { ok: true, unchanged: true }]);
  watching(api);
  await api.pollWordIndex();
  assert.equal(asks.length, 0); // both colours off
  api.state.settings.pitchAccent = true;
  sandbox.document.visibilityState = "hidden";
  await api.pollWordIndex();
  assert.equal(asks.length, 0);
  sandbox.document.visibilityState = "visible";
  // The home page, and a watch page's player kept in the DOM after leaving it: nothing to colour.
  api.state.video = null;
  await api.pollWordIndex();
  assert.equal(asks.length, 0);
  watching(api);
  api.state.videoId = null;
  await api.pollWordIndex();
  assert.equal(asks.length, 0);
  watching(api);
  // Standing by for another tab (the one-tab election, a rule about the server): the index is
  // the deck's, from Anki through the background's cache, and the lines this tab keeps want it.
  api.state.standby = true;
  await api.pollWordIndex();
  assert.equal(asks.length, 1);
  assert.deepEqual(plain(asks[0]), { type: "cardStatus", since: 0 });
  api.state.standby = false;
  await api.pollWordIndex();
  assert.equal(asks.length, 1); // asked a moment ago
  api.state.wordIndexAskedAt = Date.now() - 30_000;
  api.state.wordIndexAt = 1000;
  await api.pollWordIndex();
  assert.equal(asks.length, 2);
  assert.equal(asks[1].since, 1000);
  api.state.wordIndexAskedAt = 0;
  api.state.settings.enabled = false;
  await api.pollWordIndex();
  assert.equal(asks.length, 2); // the master switch: nothing goes out

  // An ask still out: the next tick waits for it instead of sending a second one.
  api.state.settings.enabled = true;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  sandbox.browser.runtime.sendMessage = async (msg) => {
    asks.push(msg);
    await gate;
    return { ok: true, unchanged: true };
  };
  const pending = api.pollWordIndex();
  assert.equal(asks.length, 3);
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(asks.length, 3);
  release();
  await pending;
  assert.equal(api.state.wordIndexInFlight, false);
});

test("pollWordIndex keeps an unchanged index, moves the stamp for the same words and rebuilds on new ones", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  watching(api);
  Object.assign(api.state.settings, WORDS_ALONE, { cardStatus: true });
  subtitleBox(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  const asks = backgroundAnswering(sandbox, [
    { ok: true, deck: "Mining", automatic: true, at: 1000, entries: DECK },
    { ok: true, unchanged: true, at: 1000, deck: "Mining" },
    { ok: true, deck: "Mining", automatic: true, at: 2000, entries: DECK, stale: true },
    { ok: true, deck: "Mining", automatic: true, at: 3000, entries: [["日本語", "learning", null]] },
  ]);
  await api.pollWordIndex();
  assert.equal(api.state.wordIndexAt, 1000);
  assert.equal(api.state.wordIndex.size, 2);
  assert.equal(nodes(api.state.subText)[1], "shisuko-word{status=learned}:日本語");
  const built = api.state.wordIndex;

  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(asks[1].since, 1000);
  assert.equal(api.state.wordIndex, built);

  // Refetched by the background and stamped anew, the same words: nothing is redrawn.
  const drawn = api.state.subText.childNodes[1];
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(api.state.wordIndexAt, 2000);
  assert.equal(api.state.wordIndex, built);
  assert.equal(api.state.subText.childNodes[1], drawn);

  // A card reviewed since: a new index, and the line drawn again.
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(asks[3].since, 2000);
  assert.equal(api.state.wordIndexAt, 3000);
  assert.equal(api.state.wordIndex.size, 1);
  assert.deepEqual(nodes(api.state.subText), ["これは", "shisuko-word{status=learning}:日本語", "の字幕です"]);
});

test("pollWordIndex drops the index when the background says off, and logs other failures once a minute", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true });
  subtitleBox(api, sandbox);
  api.state.toastEl = sandbox.document.createElement("div");
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  assert.equal(nodes(api.state.subText).length, 5);
  const logs = [];
  sandbox.console = { debug: (...args) => logs.push(args.join(" ")), log: () => {}, warn: () => {}, error: () => {} };
  backgroundAnswering(sandbox, [
    { ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed" },
    { ok: false, reason: "noDeck", error: "No card mined yet; pick a deck in the popup" },
    { ok: false, reason: "off" },
  ]);
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(logs.length, 1);
  assert.match(logs[0], /word colours: Anki is not running/);
  assert.equal(api.state.wordIndex.size, 2); // a failure keeps what there is
  assert.equal(nodes(api.state.subText).length, 5);
  assert.equal(api.state.toastEl.textContent, ""); // never a toast

  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(logs.length, 1); // within the minute
  api.state.lastWordIndexLog = Date.now() - 60_000;
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(api.state.wordIndex, null);
  assert.equal(api.state.wordIndexAt, 0);
  assert.equal(logs.length, 1); // "off" is not a failure
  assert.deepEqual(nodes(api.state.subText), [LINE]);
  assert.equal(api.state.toastEl.textContent, "");
});

// Nothing mined yet and no deck chosen, or Anki closed since the page loaded: the background has
// no deck to answer with, but the viewer's known words, the particles and the katakana words need
// none, and used to wait for one.
const NO_DECK_LINE = "猫はコーヒーが好き";
const NO_DECK_LOOK = [
  "shisuko-word{status=learned}:猫",
  "shisuko-word{status=learned}:は",
  "shisuko-word{status=learned}:コーヒー",
  "shisuko-word{status=learned}:が",
  "好き",
];

test("with no deck to colour by, the known words, the particles and the katakana words are drawn all the same", async () => {
  for (const failure of [
    { ok: false, reason: "noDeck", error: "No card mined yet; pick a deck in the popup" },
    { ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed" },
  ]) {
    const { api, sandbox } = loadContent();
    await settled();
    watching(api);
    Object.assign(api.state.settings, { cardStatus: true, knownWords: "猫", katakanaKnown: true, particlesKnown: true });
    subtitleBox(api, sandbox);
    sandbox.console = { debug: () => {}, log: () => {}, warn: () => {}, error: () => {} };
    api.mergeCues([{ id: 0, start: 0, end: 2, text: NO_DECK_LINE }]);
    api.setSubtitle(api.cueById(0));
    assert.deepEqual(nodes(api.state.subText), [NO_DECK_LINE]); // no answer yet
    backgroundAnswering(sandbox, [failure]);
    await api.pollWordIndex();
    assert.deepEqual(nodes(api.state.subText), NO_DECK_LOOK, failure.reason);
    // Still no deck: the stamp and the key are unset, so the first deck answer replaces it.
    assert.equal(api.state.wordEntries, null);
    assert.equal(api.state.wordIndexAt, 0);
    assert.equal(api.state.wordIndexKey, "");
  }
});

test("a known word changed with no deck in hand colours in place and asks nothing; a deck answer then replaces the list-only index, and off leaves nothing", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  Object.assign(api.state.settings, { cardStatus: true, katakanaKnown: true, particlesKnown: true });
  subtitleBox(api, sandbox);
  sandbox.console = { debug: () => {}, log: () => {}, warn: () => {}, error: () => {} };
  api.mergeCues([{ id: 0, start: 0, end: 2, text: NO_DECK_LINE }]);
  api.setSubtitle(api.cueById(0));
  const asks = backgroundAnswering(sandbox, [
    { ok: false, reason: "noDeck", error: "No card mined yet; pick a deck in the popup" },
    { ok: false, reason: "noDeck", error: "No card mined yet; pick a deck in the popup" },
    { ok: true, deck: "Mining", automatic: true, at: 1000, entries: [["好き", "new", null]] },
    { ok: false, reason: "off" },
  ]);
  await api.pollWordIndex();
  assert.deepEqual(nodes(api.state.subText), ["猫", ...NO_DECK_LOOK.slice(1)]); // no known word yet
  const index = api.state.wordIndex;
  // The list edited: the index of the list alone is built again, the line drawn in place.
  onSettingsChanged({ settings: { newValue: Object.assign({}, api.state.settings, { knownWords: "猫" }) } }, "local");
  assert.equal(asks.length, 1);
  assert.notEqual(api.state.wordIndex, index);
  assert.deepEqual(nodes(api.state.subText), NO_DECK_LOOK);
  // Another failure keeps it.
  const kept = api.state.wordIndex;
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(api.state.wordIndex, kept);
  // The first deck answer replaces it, the list going in with the deck.
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(api.state.wordIndexAt, 1000);
  assert.deepEqual(nodes(api.state.subText), [...NO_DECK_LOOK.slice(0, 4), "shisuko-word{status=new}:好き"]);
  // Turned off: plain text, and no list-only index put back.
  api.state.wordIndexAskedAt = 0;
  await api.pollWordIndex();
  assert.equal(api.state.wordIndex, null);
  assert.deepEqual(nodes(api.state.subText), [NO_DECK_LINE]);
});

test("a changed deck or colour starts the index over and asks for the new one at once, never while off", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true });
  subtitleBox(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  api.state.wordIndexAskedAt = Date.now();
  const asks = backgroundAnswering(sandbox, [{ ok: true, unchanged: true }, { ok: true, unchanged: true }, { ok: true, unchanged: true }]);

  onSettingsChanged({ settings: { newValue: { cardStatus: true, showStatus: false } } }, "local");
  assert.equal(api.state.wordIndex.size, 2); // another setting: the index stands
  assert.equal(api.state.wordIndexAt, 1000);
  assert.equal(asks.length, 0);

  onSettingsChanged({ settings: { newValue: { cardStatus: true, cardStatusDeck: "Vocab" } } }, "local");
  assert.equal(api.state.wordIndex, null);
  assert.equal(api.state.wordIndexAt, 0);
  assert.equal(api.state.wordIndexKey, "");
  assert.deepEqual(nodes(api.state.subText), [LINE]); // plain again at once
  assert.equal(asks.length, 1);
  assert.equal(asks[0].since, 0);
  await settled(); // the answer is back

  setIndex(api, words.buildIndex(DECK));
  onSettingsChanged({ settings: { newValue: { cardStatus: false, cardStatusDeck: "Vocab" } } }, "local");
  assert.equal(api.state.wordIndex, null); // turned off: dropped, nothing asked
  assert.equal(asks.length, 1);

  onSettingsChanged({ settings: { newValue: { enabled: false, cardStatus: true, cardStatusDeck: "Other" } } }, "local");
  assert.equal(asks.length, 1); // the master switch off: a deck change asks nothing either

  onSettingsChanged({ settings: { newValue: { pitchAccent: true, ankiPitchField: "Pitch" } } }, "local");
  assert.equal(asks.length, 2);
  await settled();
  setIndex(api, words.buildIndex(DECK));
  api.state.wordIndexAskedAt = Date.now();
  onSettingsChanged({ settings: { newValue: { pitchAccent: true, ankiPitchField: "Pitch", ankiWordField: "Word" } } }, "local");
  assert.equal(api.state.wordIndex, null); // the words were read through the old field
  assert.equal(asks.length, 3);
  await settled();

  // The same change reaches every YouTube tab; one without a video has nothing to colour.
  api.state.video = null;
  onSettingsChanged({ settings: { newValue: { pitchAccent: true, ankiPitchField: "Accent" } } }, "local");
  assert.equal(asks.length, 3);
});

test("an answer to an ask from before a deck change is thrown away, and the new deck asked for", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  api.state.settings.cardStatus = true;
  subtitleBox(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const asks = [];
  sandbox.browser.runtime.sendMessage = async (msg) => {
    if (msg.type !== "cardStatus") return { ok: true };
    asks.push(msg);
    if (asks.length > 1) return { ok: true, unchanged: true };
    await gate; // the background is fetching a large deck
    return { ok: true, deck: "Old", automatic: false, at: 1000, entries: DECK };
  };
  const pending = api.pollWordIndex();
  assert.equal(asks.length, 1);
  // The viewer picks another deck while that one is still being fetched.
  onSettingsChanged({ settings: { newValue: { cardStatus: true, cardStatusDeck: "New" } } }, "local");
  assert.equal(asks.length, 1); // an ask is out: no second one
  release();
  await pending;
  assert.equal(api.state.wordIndex, null); // the old deck's words never colour the new deck's lines
  assert.equal(api.state.wordIndexAt, 0);
  assert.deepEqual(nodes(api.state.subText), [LINE]);
  assert.equal(api.state.wordIndexInFlight, false);
  await api.pollWordIndex(); // the next tick asks for the new deck from the start
  assert.equal(asks.length, 2);
  assert.equal(asks[1].since, 0);
});

test("a mined card makes the next ask go out soon, not at the interval", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  watching(api);
  api.state.settings.cardStatus = true;
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  let clock = Date.now();
  sandbox.Date = { now: () => clock };
  const asks = [];
  let mined = { ok: true, target: "anki", noteId: 1, message: "Added Picture to the newest Anki card" };
  sandbox.browser.runtime.sendMessage = async (msg) => {
    if (msg.type === "mine") return mined;
    if (msg.type !== "cardStatus") return { ok: true };
    asks.push(msg);
    return { ok: true, unchanged: true };
  };
  await api.pollWordIndex();
  assert.equal(asks.length, 1);
  clock += 1000;
  await api.mineCue(api.cueById(0), { auto: true, noteId: 1 });
  await api.pollWordIndex();
  assert.equal(asks.length, 1); // Anki has yet to tell the background the card's deck
  clock += 1500;
  await api.pollWordIndex();
  assert.equal(asks.length, 2); // asked 2.5 s after the last one, not 30

  // A mine that failed made no card: the interval stands.
  mined = { ok: false, error: "no card" };
  clock += 1000;
  await api.mineCue(api.cueById(0), { auto: true, noteId: 1 });
  clock += 5000;
  await api.pollWordIndex();
  assert.equal(asks.length, 2);
});

test("the master switch off: syncTick sends nothing, the deck index included", async () => {
  const { api, sent } = loadContent();
  await settled();
  api.state.video = { currentTime: 0, paused: false };
  api.state.settings.cardStatus = true;
  api.state.settings.pitchAccent = true;
  api.state.settings.enabled = false;
  const before = sent.length;
  api.syncTick();
  assert.equal(sent.length, before);
  api.state.settings.enabled = true;
  api.syncTick();
  assert.deepEqual(plain(sent.slice(before).map((m) => m.type)), ["cardStatus", "api"]);
  assert.equal(sent[before].since, 0);
});

test("a new server session drops the cues but not the deck index", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  giveIndex(api, { cardStatus: true });
  serverAnswering(sandbox, [
    { status: "ready", session: "a", cues: [cue(0, "旧一")], next: 1 },
    { status: "ready", session: "b", cues: [], next: 1 },
  ]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 0.5, paused: true };
  await api.sync();
  await api.sync();
  assert.deepEqual(plain(api.state.cues), []);
  assert.equal(api.state.wordIndex.size, 2);
  assert.equal(api.state.wordIndexAt, 1000);
});

// ------------------------------------------------------------------ known words

// What content.js hands the matcher: the entries and the known list buildIndex() gets, and the
// options markWords() gets; both delegate to the real functions.
function recordingWords(sandbox) {
  const real = sandbox.SHISUKO_WORDS;
  const calls = { built: [], marked: [] };
  sandbox.SHISUKO_WORDS = Object.assign({}, real, {
    buildIndex: (entries, known) => {
      calls.built.push({ entries: plain(entries), known: plain(known) });
      return real.buildIndex(entries, known);
    },
    markWords: (text, index, starts, opts) => {
      calls.marked.push({ text, opts: plain(opts) });
      return real.markWords(text, index, starts, opts);
    },
  });
  return calls;
}

test("knownList reads the setting one word per line, trimmed, blank lines out, each once", () => {
  const { api } = loadContent();
  assert.deepEqual(plain(api.knownList({ knownWords: "" })), []);
  assert.deepEqual(plain(api.knownList({})), []);
  assert.deepEqual(plain(api.knownList({ knownWords: "  食べる  \n\n東京駅\n 食べる\n   \n日本語" })), ["食べる", "東京駅", "日本語"]);
  assert.deepEqual(plain(api.knownList({ knownWords: 42 })), []);
});

test("the deck index is built with the known list, and the katakana and particle switches reach the matcher", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  watching(api);
  api.state.settings.cardStatus = true;
  api.state.settings.particlesKnown = true;
  api.state.settings.knownWords = "テスト\nはい";
  subtitleBox(api, sandbox);
  const calls = recordingWords(sandbox);
  backgroundAnswering(sandbox, [{ ok: true, deck: "Mining", automatic: true, at: 1000, entries: DECK }]);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  await api.pollWordIndex();
  assert.deepEqual(calls.built, [{ entries: DECK, known: ["テスト", "はい"] }]);
  assert.deepEqual(plain(api.state.wordEntries), DECK); // kept, for a list that changes
  // Katakana as the deck says (the default), particles counted as known (switched on above), the
  // names plain (the default).
  assert.deepEqual(calls.marked, [{ text: LINE, opts: { katakana: false, particles: true, names: false } }]);
  // Each switch is part of a look: flipped, the line is matched again with it, and once only.
  api.state.settings.katakanaKnown = true;
  api.refreshWordMarks();
  assert.deepEqual(calls.marked.slice(1), [{ text: LINE, opts: { katakana: true, particles: true, names: false } }]);
  api.refreshWordMarks();
  assert.equal(calls.marked.length, 2);
  api.state.settings.particlesKnown = false;
  api.refreshWordMarks();
  assert.deepEqual(calls.marked.slice(2), [{ text: LINE, opts: { katakana: true, particles: false, names: false } }]);
  api.refreshWordMarks();
  assert.equal(calls.marked.length, 3);
  api.renderText(sandbox.document.createElement("span"), api.cueById(0));
  assert.equal(calls.marked.length, 3); // the look under the switches of now is on record
});

test("a known list that changed builds the index again from the deck in hand, asks nothing, and redraws the lines it changes", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true, showTranscript: true });
  api.state.wordEntries = plain(DECK);
  const rebuilds = overlay(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "はい" }, { id: 2, start: 5, end: 6, text: "字幕" }]);
  api.setSubtitle(api.cueById(1));
  api.refreshWordMarks(); // the index on record as drawn, so the next refresh looks for what changed
  const lines = [0, 1, 2].map((id) => api.state.lineById.get(id).childNodes[1]);
  const drawn = lines.map((text) => text.childNodes[0]);
  assert.deepEqual(nodes(lines[1]), ["はい"]);
  const asks = backgroundAnswering(sandbox, []);
  const calls = recordingWords(sandbox);
  const base = Object.assign({}, api.state.settings);
  const serial = api.state.wordIndexSerial;

  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { knownWords: "はい" }) } }, "local");
  assert.deepEqual(calls.built, [{ entries: DECK, known: ["はい"] }]);
  assert.equal(asks.length, 0); // the deck is the same: nothing asked
  assert.equal(api.state.wordIndexAt, 1000); // the stamp stands, the index moved on
  assert.equal(api.state.wordIndexSerial, serial + 1);
  assert.equal(rebuilds.count, 1); // in place, never a rebuild
  // Once the matcher takes the list, the line holding the word is drawn again (matched once: the
  // line on screen and its transcript line share the cue's look) and the others keep their
  // nodes: only the lines the two indexes disagree on are matched.
  const known = api.state.wordIndex.exact.get("はい");
  if (known) {
    assert.equal(known.status, "learned");
    assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=learned}:はい"]);
    assert.deepEqual(nodes(lines[1]), ["shisuko-word{status=learned}:はい"]);
    assert.deepEqual(calls.marked.map((c) => c.text), ["はい"]);
  }
  assert.equal(lines[0].childNodes[0], drawn[0]);
  assert.equal(lines[2].childNodes[0], drawn[2]);

  // The katakana switch: no new index, every cue looked at again (once: the line on screen and
  // its transcript line share the cue's look) in place, none rebuilt.
  calls.marked.length = 0;
  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { knownWords: "はい", katakanaKnown: true }) } }, "local");
  assert.equal(calls.built.length, 1);
  assert.equal(asks.length, 0);
  const opts = { katakana: true, particles: false, names: false };
  assert.deepEqual(calls.marked.map((c) => c.opts), [opts, opts, opts]);
  assert.equal(rebuilds.count, 1);
  assert.equal(lines[0].childNodes[0], drawn[0]); // no katakana in it: the same nodes
  assert.equal(lines[2].childNodes[0], drawn[2]);

  // A deck change still drops the index and asks; the list goes into the index the answer builds.
  backgroundAnswering(sandbox, [{ ok: true, deck: "Vocab", automatic: false, at: 2000, entries: DECK }]);
  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { knownWords: "はい", katakanaKnown: true, cardStatusDeck: "Vocab" }) } }, "local");
  assert.equal(api.state.wordIndex, null);
  assert.equal(api.state.wordEntries, null);
  await settled();
  assert.deepEqual(calls.built[1], { entries: DECK, known: ["はい"] });
  await settled();
});

// ------------------------------------------------------------------ the particle switch

// Off by default. Switched on, a particle counts as known and is drawn green wherever it stands, between the
// deck's words; off, a line colours the deck's words alone. It is an option of the matcher, not
// part of the index, so a change of it asks the background nothing and keeps the index.
const PARTICLES_KNOWN = [
  "shisuko-word{status=learned}:これ",
  "shisuko-word{status=learned}:は",
  "shisuko-word{status=learned}:日本語",
  "shisuko-word{status=learned}:の",
  "shisuko-word{status=new}:字幕",
  "shisuko-word{status=learned}:です",
];
const WORDS_ONLY = ["これは", "shisuko-word{status=learned}:日本語", "の", "shisuko-word{status=new}:字幕", "です"];

test("particles count as known once switched on: green between the deck's words, and only with the card colours on", () => {
  const { api, sandbox } = loadContent();
  assert.equal(api.state.settings.particlesKnown, false);
  giveIndex(api, { cardStatus: true, particlesKnown: true });
  const el = sandbox.document.createElement("span");
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), PARTICLES_KNOWN);
  assert.equal(el.textContent, LINE);
  // A particle has a state and no pitch: with the pitch alone it is text like the rest.
  Object.assign(api.state.settings, { cardStatus: false, pitchAccent: true });
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), ["これは日本語の", "shisuko-word{pitch=heiban}:字幕", "です"]);
  // Off: the words alone, as 0.12.0 drew them.
  Object.assign(api.state.settings, { cardStatus: true, pitchAccent: false, particlesKnown: false });
  api.renderText(el, cue(0, LINE));
  assert.deepEqual(nodes(el), WORDS_ONLY);
});

test("the particle switch redraws in place: no ask, the index kept, new nodes only for the lines holding a particle", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true, showTranscript: true, particlesKnown: true });
  api.state.wordEntries = plain(DECK);
  const rebuilds = overlay(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: LINE }, { id: 1, start: 3, end: 4, text: "はい" }, { id: 2, start: 5, end: 6, text: "字幕" }]);
  api.setSubtitle(api.cueById(0));
  api.refreshWordMarks(); // the index on record as drawn
  const lines = [0, 1, 2].map((id) => api.state.lineById.get(id).childNodes[1]);
  const drawn = lines.map((text) => text.childNodes[0]);
  assert.deepEqual(nodes(api.state.subText), PARTICLES_KNOWN);
  assert.deepEqual(nodes(lines[0]), PARTICLES_KNOWN);
  assert.deepEqual(nodes(lines[1]), ["はい"]); // no particle: one word ICU keeps whole
  const asks = backgroundAnswering(sandbox, []);
  const calls = recordingWords(sandbox);
  const index = api.state.wordIndex;
  const serial = api.state.wordIndexSerial;
  const base = Object.assign({}, api.state.settings);

  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { particlesKnown: false }) } }, "local");
  assert.equal(asks.length, 0); // the deck is the same: nothing asked
  assert.equal(calls.built.length, 0); // nor built again
  assert.equal(api.state.wordIndex, index);
  assert.equal(api.state.wordIndexSerial, serial);
  assert.equal(api.state.wordIndexAt, 1000);
  assert.equal(rebuilds.count, 1); // in place, never a rebuild
  // Every cue matched again, once (the line on screen and its transcript line share the look).
  const off = { katakana: false, particles: false, names: false };
  assert.deepEqual(calls.marked.map((c) => c.opts), [off, off, off]);
  assert.deepEqual(nodes(api.state.subText), WORDS_ONLY);
  assert.deepEqual(nodes(lines[0]), WORDS_ONLY);
  assert.equal(lines[1].childNodes[0], drawn[1]); // no particle in them: the same nodes
  assert.equal(lines[2].childNodes[0], drawn[2]);

  // On again: the particles come back, the index still the same.
  calls.marked.length = 0;
  onSettingsChanged({ settings: { newValue: Object.assign({}, base) } }, "local");
  assert.equal(calls.marked.length, 3);
  assert.equal(calls.built.length, 0);
  assert.equal(asks.length, 0);
  assert.equal(api.state.wordIndex, index);
  assert.deepEqual(nodes(api.state.subText), PARTICLES_KNOWN);
  assert.deepEqual(nodes(lines[0]), PARTICLES_KNOWN);
  assert.equal(lines[1].childNodes[0], drawn[1]);
  assert.equal(rebuilds.count, 1);
});

// ------------------------------------------------------------------ the name switch

// Off by default: no card stands behind a name or Latin text, so it is drawn as the text around it.
// On, it is blue ("proper"). Like the particle switch it is an option of the matcher: a change asks
// the background nothing, keeps the index and redraws only the lines holding a name.
const NAME_LINE = "OKよ。";

test("the name switch: names plain by default, blue once switched on, redrawn in place with no ask", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  assert.equal(api.state.settings.properNames, false);
  giveIndex(api, { cardStatus: true, showTranscript: true });
  api.state.wordEntries = plain(DECK);
  const rebuilds = overlay(api, sandbox);
  api.mergeCues([{ id: 0, start: 0, end: 2, text: NAME_LINE }, { id: 1, start: 3, end: 4, text: LINE }]);
  api.setSubtitle(api.cueById(0));
  api.refreshWordMarks(); // the index on record as drawn
  const lines = [0, 1].map((id) => api.state.lineById.get(id).childNodes[1]);
  const drawn = lines.map((text) => text.childNodes[0]);
  assert.deepEqual(nodes(api.state.subText), [NAME_LINE]);
  assert.deepEqual(nodes(lines[0]), [NAME_LINE]);
  assert.deepEqual(nodes(lines[1]), WORDS_ONLY);
  const asks = backgroundAnswering(sandbox, []);
  const calls = recordingWords(sandbox);
  const index = api.state.wordIndex;
  const serial = api.state.wordIndexSerial;
  const base = Object.assign({}, api.state.settings);

  onSettingsChanged({ settings: { newValue: Object.assign({}, base, { properNames: true }) } }, "local");
  assert.equal(asks.length, 0);
  assert.equal(calls.built.length, 0);
  assert.equal(api.state.wordIndex, index);
  assert.equal(api.state.wordIndexSerial, serial);
  assert.equal(rebuilds.count, 1); // in place, never a rebuild
  const on = { katakana: false, particles: false, names: true };
  assert.deepEqual(calls.marked.map((c) => c.opts), [on, on]);
  const blue = ["shisuko-word{status=proper}:OK", "よ。"];
  assert.deepEqual(nodes(api.state.subText), blue);
  assert.deepEqual(nodes(lines[0]), blue);
  assert.equal(lines[1].childNodes[0], drawn[1]); // no name in it: the same nodes

  // Blue needs the card colours: with the pitch alone a name is text like the rest.
  const el = sandbox.document.createElement("span");
  Object.assign(api.state.settings, { cardStatus: false, pitchAccent: true });
  api.renderText(el, api.cueById(0));
  assert.deepEqual(nodes(el), [NAME_LINE]);
  Object.assign(api.state.settings, { cardStatus: true, pitchAccent: false });

  // Off again: plain, still nothing asked.
  onSettingsChanged({ settings: { newValue: Object.assign({}, base) } }, "local");
  assert.equal(asks.length, 0);
  assert.equal(calls.built.length, 0);
  assert.deepEqual(nodes(api.state.subText), [NAME_LINE]);
  assert.deepEqual(nodes(lines[0]), [NAME_LINE]);
  assert.equal(rebuilds.count, 1);
});

// Off, the lines keep their colours under the hidden root. A change of the list or a switch while
// off used to draw every coloured line of the hidden transcript as plain text, in every tab, and
// every one of them again once the add-on was back on.
test("the known list and the switches changed while off redraw nothing until the add-on is on, and then only the lines they changed", async () => {
  const { api, sandbox, onSettingsChanged } = loadContent();
  await settled();
  watching(api);
  giveIndex(api, { cardStatus: true, showTranscript: true });
  api.state.wordEntries = plain(DECK);
  overlay(api, sandbox);
  api.mergeCues([
    { id: 0, start: 0, end: 2, text: LINE },
    { id: 1, start: 3, end: 4, text: "はい" },
    { id: 2, start: 5, end: 6, text: "字幕" },
    { id: 3, start: 7, end: 8, text: "コーヒー" },
  ]);
  api.setSubtitle(api.cueById(2));
  api.refreshWordMarks(); // the index on record as drawn
  const lines = [0, 1, 2, 3].map((id) => api.state.lineById.get(id).childNodes[1]);
  const before = lines.map((text) => [...text.childNodes]);
  const looks = lines.map((text) => nodes(text));
  assert.deepEqual(looks[0], WORDS_ONLY);
  const calls = recordingWords(sandbox);
  const off = Object.assign({}, api.state.settings, { enabled: false });
  onSettingsChanged({ settings: { newValue: off } }, "local");
  for (const patch of [{ particlesKnown: true }, { katakanaKnown: true }, { knownWords: "はい" }]) {
    Object.assign(off, patch);
    onSettingsChanged({ settings: { newValue: Object.assign({}, off) } }, "local");
    assert.equal(calls.marked.length, 0, JSON.stringify(patch));
    lines.forEach((text, i) => assert.deepEqual([...text.childNodes], before[i], JSON.stringify(patch)));
  }
  assert.deepEqual(calls.built.map((c) => c.known), [["はい"]]); // the index is ready for the switch
  // On again: the lines holding a particle, the known word or a katakana word are drawn anew; the
  // line holding none keeps its nodes, matched again for the switches and found unchanged.
  onSettingsChanged({ settings: { newValue: Object.assign({}, off, { enabled: true }) } }, "local");
  assert.deepEqual(nodes(lines[0]), PARTICLES_KNOWN);
  assert.deepEqual(nodes(lines[1]), ["shisuko-word{status=learned}:はい"]);
  assert.deepEqual(nodes(lines[3]), ["shisuko-word{status=learned}:コーヒー"]);
  assert.deepEqual([...lines[2].childNodes], before[2]);
  assert.equal(calls.marked.length, 4); // each line once
});

// The overlay as the shortcut finds it: a subtitle on screen and a transcript line, both drawn
// through renderText, and the pointer's caret put where a test says (Firefox's API; Chrome's is
// read the same way).
function pointing(loaded, text, settings) {
  const { api, sandbox } = loaded;
  Object.assign(api.state.settings, settings || {});
  api.state.toastEl = sandbox.document.createElement("div");
  api.state.transcriptList = sandbox.document.createElement("div");
  api.state.subBox = sandbox.document.createElement("div");
  api.state.subText = sandbox.document.createElement("span");
  api.state.subText.className = "shisuko-subtext";
  api.state.subBox.appendChild(api.state.subText);
  api.mergeCues([{ id: 0, start: 0, end: 2, text }]);
  api.setSubtitle(api.cueById(0));
  api.onPlayerMouseMove({ clientX: 100, clientY: 200, target: api.state.subText }); // over the player
  const caret = (node, offset) => {
    sandbox.document.caretPositionFromPoint = (x, y) => {
      assert.deepEqual([x, y], [100, 200]);
      return { offsetNode: node, offset };
    };
  };
  const saves = () => loaded.sent.filter((m) => m.type === "saveSettings").map((m) => plain(m.settings));
  const toast = () => api.state.toastEl.textContent;
  // A child of the line on screen (or of `el`) by its text, read live: a render replaces the
  // child list, and which index a word sits at depends on the matcher's rules for the words
  // around it, not on the test.
  const nodeOf = (text, el = api.state.subText) => {
    const node = el.childNodes.find((n) => n.textContent === text);
    assert.ok(node, `no node "${text}" in ${JSON.stringify(nodes(el))}`);
    return node;
  };
  // The characters' boxes, for a page that has them (the harness has none until this is called):
  // every character of what is drawn in `el` 20 px wide on one row, the one at `index` holding
  // the pointer (x 100) in its right half, 85 to 105.
  const boxes = (index, el = api.state.subText) => {
    sandbox.document.createRange = () => {
      let at = NaN;
      return {
        setStart: (node, offset) => {
          at = drawnIndex(el, node, offset);
        },
        setEnd: () => {},
        getBoundingClientRect: () => {
          const left = 85 + (at - index) * 20;
          return { left, right: left + 20, top: 190, bottom: 210 };
        },
      };
    };
  };
  return { caret, boxes, saves, toast, nodeOf };
}

// The index in what is drawn in `el` of the position `offset` in `node`, a text node of it or of
// one of its word spans.
function drawnIndex(el, node, offset) {
  let at = 0;
  for (const child of el.childNodes) {
    if (child === node || (child.nodeType === 1 && child.childNodes[0] === node)) return at + offset;
    at += child.textContent.length;
  }
  throw new Error("a range in a node the line does not hold");
}

test("Alt+Shift+K marks the word under the pointer as known: a word span, plain text, a verb by its stem", async () => {
  const loaded = withIndex({ cardStatus: true });
  const { api, sandbox, onCommand } = loaded;
  const { caret, saves, toast, nodeOf } = pointing(loaded, LINE);
  const word = nodeOf("字幕");
  assert.equal(word.className, "shisuko-word");
  caret(word.childNodes[0], 1); // in the span's text node
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "字幕" }]);
  assert.equal(toast(), "字幕 marked as known");
  assert.equal(api.state.toastEl.className, "shisuko-toast shisuko-toast-ok");
  // Plain text: the ICU segment under the caret (これ|は). The line's first node is text
  // (これ, with は as it is drawn now); a caret at its end is the position after it.
  const head = api.state.subText.childNodes[0];
  assert.equal(head.nodeType, 3);
  assert.ok(head.textContent.startsWith("これ"));
  caret(head, 1);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[1], { knownWords: "これ" });
  // A particle is no word for the list: the index drops it (as it drops a card for one), so it
  // would colour nothing, and the toast says what does decide its colour instead of claiming it.
  caret(head, 2);
  onCommand({ type: "command", name: "mark-known" });
  assert.equal(saves().length, 2);
  assert.equal(toast(), 'は is a particle: the "Particles count as known" switch decides its colour');
  assert.equal(api.state.toastEl.className, "shisuko-toast shisuko-toast-warn");
  // A caret on the span itself, or the element (between two children), counts its children.
  caret(nodeOf("日本語"), 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[2], { knownWords: "日本語" });
  caret(api.state.subText, api.state.subText.childNodes.indexOf(nodeOf("日本語")));
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[3], { knownWords: "日本語" });
  // A conjugated form found by its stem goes on the list as the deck's word.
  setIndex(api, words.buildIndex([["食べる", "new", null]]));
  api.mergeCues([{ id: 1, start: 3, end: 5, text: "昨日食べた" }]);
  api.setSubtitle(api.cueById(1));
  assert.deepEqual(nodes(api.state.subText), ["昨日", "shisuko-word{status=new}:食べた"]);
  caret(api.state.subText.childNodes[1].childNodes[0], 2);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[4], { knownWords: "食べる" });
  assert.equal(toast(), "食べる marked as known");
  // Plain text after a kanji ICU cut off its okurigana: the hiragana segments after it are joined
  // (走|っ|た), and the deck's word is used when it knows the form.
  api.mergeCues([{ id: 2, start: 6, end: 8, text: "今日は走った" }]);
  api.setSubtitle(api.cueById(2));
  assert.deepEqual(nodes(api.state.subText), ["今日は走った"]);
  caret(api.state.subText.childNodes[0], 3);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[5], { knownWords: "走った" });
  setIndex(api, words.buildIndex([["走る", "new", null], ["走", "learned", null]]));
  api.refreshWordMarks();
  caret(api.state.subText.childNodes[1].childNodes[0], 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[6], { knownWords: "走る" }); // the verb's span, not the one-kanji word's
  assert.equal(sandbox.document.caretPositionFromPoint.length, 2);
  await settled();
});

// The one-kanji word is the one the shortcut is most for (a noun the deck lacks), and the
// hiragana after it is its particle as often as a verb's okurigana.
test("Alt+Shift+K on a one-kanji word in plain text marks the kanji, not the particles and words after it", async () => {
  const loaded = withIndex({ cardStatus: true });
  const { api, onCommand } = loaded;
  const { caret, saves, toast } = pointing(loaded, "私はこれが好き");
  assert.deepEqual(nodes(api.state.subText), ["私はこれが好き"]);
  caret(api.state.subText.childNodes[0], 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "私" }]); // was 私はこれが
  assert.equal(toast(), "私 marked as known");
  const lines = [
    ["猫がいる", "猫"],
    ["家にいます", "家"],
    ["本を読んだ", "本"],
    ["前から", "前"],
    ["食べてから", "食べて"], // the okurigana and its て, up to the particle
    ["聞いてね", "聞いて"],
  ];
  lines.forEach(([text, word], i) => {
    api.mergeCues([{ id: i + 1, start: 3 * (i + 1), end: 3 * (i + 1) + 2, text }]);
    api.setSubtitle(api.cueById(i + 1));
    caret(api.state.subText.childNodes[0], 0);
    onCommand({ type: "command", name: "mark-known" });
    assert.deepEqual(saves()[i + 1], { knownWords: word }, text);
  });
  await settled();
});

// caretPositionFromPoint and caretRangeFromPoint answer the gap between two characters nearest
// the point, the gap after a character over its right half. Read as the character after the gap,
// the right half of 走 was っ, which went on the list and turned every っ ICU cuts off green.
test("Alt+Shift+K reads the character the pointer is on: its right half is not the gap after it", async () => {
  const loaded = withIndex({ cardStatus: true });
  const { api, onCommand } = loaded;
  const { caret, boxes, saves, toast, nodeOf } = pointing(loaded, "今日は走った");
  setIndex(api, words.buildIndex([["今日", "learned", null], ["好き", "learned", null]]));
  api.refreshWordMarks();
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=learned}:今日", "は走った"]);
  const tail = nodeOf("は走った");
  // The right half of 走: the caret API answers the gap after it, and 走's box holds the point.
  boxes(3);
  caret(tail, 2);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "走った" }]); // was っ
  // The left half: the gap before it, and the character after the gap is the one.
  caret(tail, 1);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[1], { knownWords: "走った" });
  // The right half of a span's last character is the span.
  boxes(1);
  caret(nodeOf("今日").childNodes[0], 2);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[2], { knownWords: "今日" });
  // Neither box holds the point (between two rows, past the end): the gap read as before.
  boxes(99);
  caret(tail, 1);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[3], { knownWords: "走った" });

  // The gap at a span's end, answered in the span's node (Chrome may answer either node) for the
  // left half of the plain character after it: that character, not the span.
  api.mergeCues([{ id: 1, start: 3, end: 5, text: "今日走った" }]);
  api.setSubtitle(api.cueById(1));
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=learned}:今日", "走った"]);
  boxes(2);
  caret(nodeOf("今日").childNodes[0], 2);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[4], { knownWords: "走った" }); // was 今日
  // The right half of a plain word before a particle's span: the word, not the particle.
  api.state.settings.particlesKnown = true;
  api.mergeCues([{ id: 2, start: 6, end: 8, text: "猫が好き" }]);
  api.setSubtitle(api.cueById(2));
  assert.deepEqual(nodes(api.state.subText), ["猫", "shisuko-word{status=learned}:が", "shisuko-word{status=learned}:好き"]);
  boxes(0);
  caret(nodeOf("猫"), 1);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[5], { knownWords: "猫" });
  assert.equal(toast(), "猫 marked as known");
  await settled();
});

// お|風呂 is drawn [お][風呂], one word to the eye: pointed at, the prefix means the word. A
// particle drawn green goes nowhere, and one already on the list (typed in the popup) comes off.
test("Alt+Shift+K on an honorific prefix marks the word it fronts, and a particle span is refused", async () => {
  const loaded = withIndex({ cardStatus: true });
  const { api, onCommand } = loaded;
  const { caret, saves, toast, nodeOf } = pointing(loaded, "お風呂に入る");
  setIndex(api, words.buildIndex([["風呂", "new", null]]));
  api.refreshWordMarks();
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=new}:お", "shisuko-word{status=new}:風呂", "に入る"]);
  caret(nodeOf("お").childNodes[0], 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "風呂" }]); // was お
  assert.equal(toast(), "風呂 marked as known");
  // Plain text, the deck without the word: the segment after the prefix.
  setIndex(api, words.buildIndex(DECK));
  api.refreshWordMarks();
  assert.deepEqual(nodes(api.state.subText), ["お風呂に入る"]);
  caret(api.state.subText.childNodes[0], 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[1], { knownWords: "風呂" });

  // A particle counted as known, drawn green: nothing saved, and the toast says why.
  api.state.settings.particlesKnown = true;
  setIndex(api, words.buildIndex([["今日", "learned", null]]));
  api.mergeCues([{ id: 1, start: 3, end: 5, text: "今日は晴れ" }]);
  api.setSubtitle(api.cueById(1));
  assert.deepEqual(nodes(api.state.subText), ["shisuko-word{status=learned}:今日", "shisuko-word{status=learned}:は", "晴れ"]);
  caret(nodeOf("は").childNodes[0], 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.equal(saves().length, 2);
  assert.equal(toast(), 'は is a particle: the "Particles count as known" switch decides its colour');
  assert.equal(api.state.toastEl.className, "shisuko-toast shisuko-toast-warn");
  // On the list already: it comes off.
  api.state.settings.knownWords = "は\n猫";
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[2], { knownWords: "猫" });
  assert.equal(toast(), "は is no longer marked as known");
  await settled();
});

// The transcript sits inside the player, so the last move before the pointer leaves is often over
// a line; the list scrolls on without it and the subtitle moves on, and what is at that place
// then is no word the viewer pointed at.
test("Alt+Shift+K with the pointer gone from the player marks nothing, unless a hover pause keeps the line where it was", async () => {
  const loaded = withIndex({ cardStatus: true });
  const { api, sandbox, onCommand } = loaded;
  const { caret, saves, toast, nodeOf } = pointing(loaded, LINE);
  caret(nodeOf("字幕").childNodes[0], 0);
  api.onPlayerMouseLeave({});
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), []);
  assert.equal(toast(), "No word under the pointer");
  // A selection in the subtitle still counts: Yomitan's, with the pointer in its popup.
  sandbox.document.getSelection = () => ({ isCollapsed: false, rangeCount: 1, getRangeAt: () => ({ commonAncestorContainer: nodeOf("日本語") }), toString: () => "日本語" });
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "日本語" }]);
  sandbox.document.getSelection = () => null;
  // The video paused by the hover, waiting for the pointer gone to a dictionary popup: the line
  // is where it was, and so is the word.
  api.state.hoverPaused = true;
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[1], { knownWords: "字幕" });
  api.state.hoverPaused = false;
  // Back over the player: the pointer counts again.
  api.onPlayerMouseMove({ clientX: 100, clientY: 200, target: api.state.subText });
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[2], { knownWords: "字幕" });
  await settled();
});

test("Alt+Shift+K takes a word selected in the subtitle or the transcript first, and a word already on the list comes off it", async () => {
  const loaded = withIndex({ cardStatus: true, knownWords: "東京駅\n字幕" });
  const { api, sandbox, onCommand } = loaded;
  const { caret, saves, toast, nodeOf } = pointing(loaded, LINE);
  caret(nodeOf("日本語").childNodes[0], 0); // the pointer is on 日本語
  const select = (text, node) => {
    sandbox.document.getSelection = () => ({ isCollapsed: false, rangeCount: 1, getRangeAt: () => ({ commonAncestorContainer: node }), toString: () => text });
  };
  // Trimmed, and the deck's own word for it, as under the pointer.
  select("  日本語の  ", nodeOf("日本語"));
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "東京駅\n字幕\n日本語" }]);
  assert.equal(toast(), "日本語 marked as known");
  // A selection outside the overlay, over two lines, or too long, is not it: the pointer is.
  select("日本語", sandbox.document.createElement("div"));
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[1], { knownWords: "東京駅\n字幕\n日本語" });
  const line = api.transcriptLine(api.cueById(0));
  api.state.transcriptList.appendChild(line);
  select("日本語\n字幕", line.childNodes[1]);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[2], { knownWords: "東京駅\n字幕\n日本語" });
  select("あ".repeat(41), line.childNodes[1]);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[3], { knownWords: "東京駅\n字幕\n日本語" });
  select("。", line.childNodes[1]);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[4], { knownWords: "東京駅\n字幕\n日本語" });
  // On the list already: off it goes, the rest of the list as it was.
  sandbox.document.getSelection = () => null;
  caret(nodeOf("字幕").childNodes[0], 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[5], { knownWords: "東京駅" });
  assert.equal(toast(), "字幕 is no longer marked as known");
  // The pointer over a transcript line's text finds the line's cue.
  caret(nodeOf("日本語", line.childNodes[1]).childNodes[0], 1); // 日本語 in the line
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[6], { knownWords: "東京駅\n字幕\n日本語" });
  // Yomitan selects the text it scanned while its popup is up, the form the line holds: the
  // deck's word for it goes on the list, which colours every form (食べた alone would colour
  // only 食べた), and a word on the list selected comes off it.
  setIndex(api, words.buildIndex([["食べる", "new", null], ["字幕", "new", null]]));
  api.mergeCues([{ id: 1, start: 3, end: 5, text: "昨日食べた" }]);
  api.setSubtitle(api.cueById(1));
  select("食べた", api.state.subText);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[7], { knownWords: "東京駅\n字幕\n食べる" });
  assert.equal(toast(), "食べる marked as known");
  select("字幕", api.state.subText);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[8], { knownWords: "東京駅" });
  // A selected particle is refused like one under the pointer.
  select("は", api.state.subText);
  onCommand({ type: "command", name: "mark-known" });
  assert.equal(saves().length, 9);
  assert.match(toast(), /^は is a particle/);
  await settled();
});

test("Alt+Shift+K with nothing under the pointer says so, and a switched-off add-on ignores it", async () => {
  const loaded = withIndex({ cardStatus: true });
  const { api, sandbox, onCommand } = loaded;
  const { caret, saves, toast, nodeOf } = pointing(loaded, LINE);
  // The pointer over YouTube's own text, a time stamp, or nowhere.
  caret(sandbox.document.createElement("div"), 0);
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), []);
  assert.equal(toast(), "No word under the pointer");
  assert.equal(api.state.toastEl.className, "shisuko-toast shisuko-toast-warn");
  const line = api.transcriptLine(api.cueById(0));
  caret(line.childNodes[0].childNodes[0], 0); // the time stamp
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), []);
  sandbox.document.caretPositionFromPoint = () => null;
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), []);
  delete sandbox.document.caretPositionFromPoint;
  onCommand({ type: "command", name: "mark-known" }); // no caret API at all
  assert.deepEqual(saves(), []);
  // Chrome's API is read the same way.
  sandbox.document.caretRangeFromPoint = () => ({ startContainer: nodeOf("字幕").childNodes[0], startOffset: 0 });
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves(), [{ knownWords: "字幕" }]);
  // Nothing on screen: no subtitle, so no cue under the pointer.
  api.setSubtitle(null);
  sandbox.document.caretRangeFromPoint = () => ({ startContainer: api.state.subText, startOffset: 0 });
  onCommand({ type: "command", name: "mark-known" });
  assert.equal(saves().length, 1);
  assert.equal(toast(), "No word under the pointer");
  // The master switch: the command returns before anything is looked at. The line is drawn anew
  // (new nodes), and the same pointer saves once the switch is back on, so it is the switch
  // that stopped it.
  api.setSubtitle(api.cueById(0));
  sandbox.document.caretRangeFromPoint = () => ({ startContainer: nodeOf("字幕").childNodes[0], startOffset: 0 });
  api.state.toastEl.textContent = "";
  api.state.settings.enabled = false;
  onCommand({ type: "command", name: "mark-known" });
  assert.equal(saves().length, 1);
  assert.equal(toast(), "");
  api.state.settings.enabled = true;
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[1], { knownWords: "字幕" });
  assert.equal(toast(), "字幕 marked as known");
  await settled();
});

test("segmentAt and entryWordFor: the piece under a position, and the deck's word for it", () => {
  const { api } = withIndex({ cardStatus: true });
  const starts = words.wordStarts("今日は走った");
  assert.deepEqual(plain(api.segmentAt("今日は走った", starts, 0)), { start: 0, end: 2, text: "今日" });
  assert.deepEqual(plain(api.segmentAt("今日は走った", starts, 2)), { start: 2, end: 3, text: "は" });
  assert.deepEqual(plain(api.segmentAt("今日は走った", starts, 3)), { start: 3, end: 6, text: "走った" });
  assert.deepEqual(plain(api.segmentAt("今日は走った", starts, 5)), { start: 5, end: 6, text: "た" });
  assert.deepEqual(plain(api.segmentAt("今日は走った", starts, 99)), { start: 5, end: 6, text: "た" }); // past the end: the last
  assert.deepEqual(plain(api.segmentAt("走", new Set([0]), 0)), { start: 0, end: 1, text: "走" });
  assert.equal(api.segmentAt("", new Set([0]), 0), null);
  // A kanji joins the hiragana after it up to a particle, never the kanji or the punctuation.
  const at0 = (text) => plain(api.segmentAt(text, words.wordStarts(text), 0)).text;
  assert.equal(at0("見に行く"), "見"); // the に is 見's particle here (見に行く: to go and see)
  assert.deepEqual(plain(api.segmentAt("走。", new Set([0, 1]), 0)), { start: 0, end: 1, text: "走" });
  assert.equal(at0("私はこれが好き"), "私"); // was 私はこれが
  assert.equal(at0("猫がいる"), "猫"); // was 猫がいる
  assert.equal(at0("家にいます"), "家");
  assert.equal(at0("本を読んだ"), "本");
  assert.equal(at0("前から"), "前");
  assert.equal(at0("食べて"), "食べて"); // the okurigana, and the inflection after it
  assert.equal(at0("食べているのが"), "食べている");
  assert.equal(at0("走った"), "走った");
  assert.equal(at0("強くない"), "強くない");
  assert.deepEqual(plain(api.segmentAt("本を読んだ", words.wordStarts("本を読んだ"), 2)), { start: 2, end: 5, text: "読んだ" });

  setIndex(api, words.buildIndex([["食べる", "new", null], ["食う", "new", null], ["勉強", "learned", null], ["勉強する", "new", null], ["日本", "new", null], ["食", "learned", null]]));
  assert.equal(api.entryWordFor("食べた"), "食べる");
  assert.equal(api.entryWordFor("食べる"), "食べる");
  assert.equal(api.entryWordFor("食事"), null); // 事 continues no verb: not 食う, not 食
  assert.equal(api.entryWordFor("食って"), "食う");
  assert.equal(api.entryWordFor("勉強して"), "勉強する"); // the stem's span (3) beats the noun (2)
  assert.equal(api.entryWordFor("勉強"), "勉強");
  assert.equal(api.entryWordFor("日本語"), null); // 語 is no continuation of 日本
  assert.equal(api.entryWordFor("東京駅"), null);
  assert.equal(api.entryWordFor("の"), null);
  setIndex(api, null);
  assert.equal(api.entryWordFor("食べた"), null);
});

// ------------------------------------------------------------------ statusText

// Everything updateStatus() reads, with nothing wrong and nothing in progress.
function view(patch) {
  return Object.assign(
    {
      enabled: true,
      showStatus: true,
      model: "",
      videoId: "abcdef1234",
      status: "ready",
      error: null,
      offline: false,
      standby: false,
      languagePaused: false,
      heard: null,
      modelLoading: null,
      modelError: null,
      duration: 1200,
      t: 100,
      ahead: null,
      coveredFrom: null,
      cueCount: 40,
      live: false,
    },
    patch
  );
}

// The verdict for one such view, in this realm: statusText builds its object inside the sandbox.
const says = (api, patch) => plain(api.statusText(view(patch)));

// The cases the refactor could most easily have broken, pinned before the new ones.
test("statusText still says what upstream said about the work in progress", () => {
  const { api } = loadContent();
  assert.deepEqual(says(api, {}), { text: "Transcribing…", isError: false });
  assert.deepEqual(says(api, { ahead: 105 }), { text: "Transcribing… (ready to 1:45)", isError: false });
  assert.deepEqual(says(api, { ahead: 1199.5 }), { text: null, isError: false }); // caught up to the end
  assert.deepEqual(says(api, { ahead: 400 }), { text: null, isError: false }); // minutes ahead: nothing to say
  assert.deepEqual(says(api, { status: "error", error: "This video is only available to members" }), {
    text: "Shisu-ko: This video is only available to members",
    isError: true,
  });
  assert.deepEqual(says(api, { status: "error", error: null }), { text: "Shisu-ko: error", isError: true });
  // An error is shown with progress messages off; ordinary chatter is not.
  assert.equal(says(api, { showStatus: false, status: "error", error: "boom" }).text, "Shisu-ko: boom");
  assert.equal(says(api, { showStatus: false, status: "connecting" }).text, null);
  assert.equal(says(api, { enabled: false, status: "connecting" }).text, null);
  assert.equal(says(api, { videoId: null, status: "connecting" }).text, null);
});

test("statusText explains a standby tab, even with progress messages off, and calls it no error", () => {
  const { api } = loadContent();
  const standby = { text: "Shisu-ko: subtitles are running in another tab", isError: false };
  assert.deepEqual(says(api, { standby: true }), standby);
  assert.deepEqual(says(api, { standby: true, showStatus: false }), standby);
  // It is the answer to "why is nothing appearing?", so it outranks every progress message.
  assert.deepEqual(says(api, { standby: true, status: "connecting" }), standby);
  assert.deepEqual(says(api, { standby: true, modelLoading: "large-v3" }), standby);
});

test("statusText explains a video the server stopped transcribing, with the language it hears", () => {
  const { api } = loadContent();
  const paused = "Shisu-ko paused: the speech is not in the subtitle language";
  assert.deepEqual(says(api, { languagePaused: true }), { text: paused, isError: false });
  assert.deepEqual(says(api, { languagePaused: true, heard: "en" }), { text: `${paused} (hearing en)`, isError: false });
  assert.deepEqual(says(api, { languagePaused: true, heard: "" }), { text: paused, isError: false });
  assert.deepEqual(says(api, { languagePaused: true, heard: 7 }), { text: paused, isError: false });
  assert.equal(says(api, { languagePaused: true, heard: "en", showStatus: false }).text, `${paused} (hearing en)`);
  // Not nested in the "ready" case: a server reporting the pause beside any other status still says so.
  assert.equal(says(api, { languagePaused: true, status: "downloading" }).text, paused);
  assert.equal(says(api, { languagePaused: true, status: "connecting" }).text, paused);
});

test("statusText ranks the verdicts about the server above the local ones", () => {
  const { api } = loadContent();
  const offline = "Shisu-ko server offline. Start it with server/run.cmd or docker/up.cmd";
  // A tab that cannot see the server has worse news than a tab waiting for its turn, and the
  // model it asked for being unusable is a verdict about the server it did reach.
  assert.deepEqual(says(api, { offline: true, standby: true, languagePaused: true }), { text: offline, isError: true });
  assert.deepEqual(says(api, { status: "offline", standby: true }), { text: offline, isError: true });
  assert.deepEqual(says(api, { modelError: "no such model", model: "tiny", standby: true }), {
    text: "Shisu-ko: model tiny: no such model",
    isError: true,
  });
  // Standby over the language pause: this tab never asked the server about this video at all.
  assert.equal(says(api, { standby: true, languagePaused: true, heard: "en" }).text,
    "Shisu-ko: subtitles are running in another tab");
  // And the language pause over a model load and over "Transcribing…".
  assert.equal(says(api, { languagePaused: true, modelLoading: "large-v3" }).text,
    "Shisu-ko paused: the speech is not in the subtitle language");
});

// A session that is done with nothing to show used to look exactly like a broken one: the line
// fell silent at the covered end whatever the cue count.
test("statusText says when nothing was heard in a video covered to its end", () => {
  const { api } = loadContent();
  const silent = { text: "No speech found in this video", isError: false };
  const nothing = { text: null, isError: false };
  const done = { ahead: 1200, coveredFrom: 0, cueCount: 0 }; // the whole video, and not one cue
  assert.deepEqual(says(api, done), silent);
  assert.deepEqual(says(api, { ...done, ahead: 1199.5 }), silent); // the "done" reading: a second of slack
  assert.deepEqual(says(api, { ...done, coveredFrom: 0.5 }), silent); // the first window starts half a second before the playhead
  assert.deepEqual(says(api, { ...done, cueCount: 3 }), nothing); // cues: caught up, nothing to say
  assert.deepEqual(says(api, { ...done, cueCount: 40 }), nothing);
  // Not covered to the end yet: the work in progress, as before, however few cues there are.
  assert.deepEqual(says(api, { ahead: null, cueCount: 0 }), { text: "Transcribing…", isError: false });
  assert.deepEqual(says(api, { ahead: 105, coveredFrom: 0, cueCount: 0 }), { text: "Transcribing… (ready to 1:45)", isError: false });
  assert.deepEqual(says(api, { ahead: 400, coveredFrom: 0, cueCount: 0 }), nothing); // minutes ahead, the end still out
  // A live stream's cues keep coming, and a length still unknown decides nothing.
  assert.deepEqual(says(api, { ...done, live: true }), nothing);
  assert.deepEqual(says(api, { t: 0, ahead: 0, coveredFrom: 0, duration: 0, cueCount: 0 }), nothing);
  // A progress message, not an error: it obeys the setting like "Transcribing…" does.
  assert.deepEqual(says(api, { ...done, showStatus: false }), nothing);
  // And below every verdict that outranks the status switch.
  assert.equal(says(api, { ...done, standby: true }).text, "Shisu-ko: subtitles are running in another tab");
  assert.equal(says(api, { ...done, modelLoading: "large-v3" }).text,
    "Loading model large-v3… (a first use downloads it)");
});

// Covered "to the end" is the end of the range the playhead sits in, which need not start at the
// start: a video resumed near its end (YouTube's saved position, a t= link) gets one window from
// the playhead to the end and nothing before it, and an outro without a word there says nothing
// about the eighteen minutes of speech the server never looked at.
test("statusText claims no speech only when the covered range is the whole video", () => {
  const { api } = loadContent();
  const nothing = { text: null, isError: false };
  assert.deepEqual(says(api, { t: 1150, ahead: 1200, coveredFrom: 1099.5, cueCount: 0 }), nothing);
  assert.deepEqual(says(api, { t: 1150, ahead: 1200, coveredFrom: 1, cueCount: 0 }), nothing); // the first second missing
  assert.deepEqual(says(api, { t: 1150, ahead: 1200, coveredFrom: null, cueCount: 0 }), nothing); // a view without the start
});

test("updateStatus reads the cue count and the live flag off the state", () => {
  const { api } = loadContent();
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 10, paused: true };
  api.state.serverStatus = "ready";
  api.state.duration = 34;
  api.state.covered = [[0, 34]];
  api.state.cues = [];
  api.updateStatus();
  assert.equal(el.textContent, "No speech found in this video");
  assert.ok(!el.classes.has("shisuko-status-error"));
  assert.ok(!el.classes.has("shisuko-hidden"));

  api.state.live = true; // a 歌枠 between songs: its cues are still to come
  api.updateStatus();
  assert.ok(el.classes.has("shisuko-hidden"));

  api.state.live = false;
  api.state.cues = [cue(0, "一")];
  api.updateStatus();
  assert.ok(el.classes.has("shisuko-hidden")); // a cue arrived: nothing to say any more

  // Resumed at 1100 s of 1200: the server's one window runs from the playhead to the end, and an
  // outro without a word in it is no verdict about the rest.
  api.state.cues = [];
  api.state.duration = 1200;
  api.state.covered = [[1099.5, 1200]];
  api.state.video.currentTime = 1150;
  api.updateStatus();
  assert.ok(el.classes.has("shisuko-hidden"));
});

// The player's own API says live only in Firefox (wrappedJSObject) and not through an ad; the
// server says it in every /sync answer, everywhere.
test("sync takes the server's live flag for the status line, and a new video forgets it", async () => {
  const { api, sandbox } = loadContent();
  await settled(); // the load's own discovery, which finds no player in this document
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 1150, paused: true };
  const answer = (patch) => Object.assign({ status: "ready", session: "s1", duration: 1200, covered: [[0, 1200]], cues: [] }, patch);
  serverAnswering(sandbox, [answer({ live: true }), answer({ live: false }), answer({ live: true }), answer({ session: "s2" })]);
  // A stream's waiting screen: covered to the buffer's end with no cue yet, on a browser whose
  // player never says live (Chrome), or before the first sync outside a pre-roll ad (Firefox).
  await api.sync();
  assert.equal(api.state.live, false);
  assert.ok(el.classes.has("shisuko-hidden"));
  await api.sync(); // the same numbers for a video: the verdict
  assert.equal(el.textContent, "No speech found in this video");
  assert.ok(!el.classes.has("shisuko-hidden"));
  await api.sync();
  assert.ok(el.classes.has("shisuko-hidden"));
  // The next video is asked about anew: an answer without the key (an older server) leaves the
  // flag where the new video put it, off.
  sandbox.location.href = "https://www.youtube.com/watch?v=zyxwvu9876";
  api.onVideoChanged("zyxwvu9876");
  await settled();
  assert.equal(el.textContent, "No speech found in this video");
  assert.ok(!el.classes.has("shisuko-hidden"));
});

// ------------------------------------------------------------------ standby

test("a standby answer teaches the tab nothing about the server", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  const bodies = serverAnswering(sandbox, [
    { status: "ready", session: "a", cues: [cue(0, "一"), cue(1, "二")], next: 2, covered: [[0, 30]], duration: 1200 },
    { status: "standby" }, // the background refused this tab: it never left the browser
    { status: "ready", session: "a", cues: [cue(2, "三")], next: 3, covered: [[0, 45]], duration: 1200 },
  ]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 10, paused: false };
  api.state.settings.autoMine = true;

  await api.sync();
  assert.equal(api.state.standby, false);
  assert.equal(api.premineAllowed(), true);
  assert.equal(api.ankiPollAllowed(), true);
  const before = {
    cues: plain(api.state.cues),
    since: api.state.since,
    covered: plain(api.state.covered),
    duration: api.state.duration,
    session: api.state.serverSession,
    status: api.state.serverStatus,
  };
  assert.equal(before.since, 2);

  await api.sync();
  assert.equal(api.state.standby, true);
  assert.deepEqual(plain(api.state.cues), before.cues);
  assert.equal(api.state.since, before.since);
  assert.deepEqual(plain(api.state.covered), before.covered);
  assert.equal(api.state.duration, before.duration);
  assert.equal(api.state.serverSession, before.session); // not a new session, not a restart
  assert.equal(api.state.serverStatus, before.status);   // "standby" is not a server status
  assert.equal(api.state.offline, false, "nothing was learned about the server either way");
  assert.equal(api.state.serverError, null);
  // A tab that may not sync must not fetch clips or poll Anki against a server it never reached.
  assert.equal(api.premineAllowed(), false);
  assert.equal(api.ankiPollAllowed(), false);
  // Paused, it falls back to the idle heartbeat instead of asking every tick; playing, it keeps
  // asking, which is what makes taking the right back immediate.
  const st = { paused: true, t: 10, status: api.state.serverStatus, covered: api.state.covered, duration: 1200, lastSyncAt: NOW, standby: true };
  assert.equal(api.shouldSync(st, NOW + 1000), false);
  assert.equal(api.shouldSync(st, NOW + 5000), true);
  assert.equal(api.shouldSync(Object.assign({}, st, { paused: false }), NOW + 1000), true);

  await api.sync();
  assert.equal(bodies[2].since, 2, "the tab asks on from where it was, not from the start");
  assert.equal(api.state.standby, false);
  assert.equal(api.cueById(2).text, "三");
  assert.equal(api.state.since, 3);
  assert.equal(api.premineAllowed(), true);
  assert.equal(api.ankiPollAllowed(), true);
});

test("a request refused after standby clears the flag: the tab holds the right again, and the error shows", async () => {
  const refused = (error) => ({ ok: false, error, data: { ok: false, error } }); // a 4xx/5xx, as apiRequest passes it on
  const { api, sandbox } = loadContent();
  await settled();
  serverAnswering(sandbox, [
    { status: "ready", session: "a", cues: [cue(0, "一")], next: 1, covered: [[0, 30]], duration: 1200 },
    { status: "standby" },
    refused("cache is broken"), // the viewer clicked into the tab: the election let this one through
    refused("cache is broken"),
    { status: "ready", session: "a", cues: [], next: 1, covered: [[0, 30]], duration: 1200 },
    { status: "standby" },
    { ok: false, offline: true, error: "Server unreachable" },
  ]);
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 10, paused: false };
  api.state.settings.autoMine = true;
  await api.sync();
  await api.sync();
  assert.equal(api.state.standby, true);

  for (const round of [1, 2]) {
    await api.sync();
    // The background answers standby only with ok: true, so this answer means the request went to
    // the server. The flag used to survive it, for as long as the server kept refusing the video:
    // the line read "running in another tab", the guards stayed closed and a paused tab fell back
    // to the heartbeat, all against the branch's own verdict.
    assert.equal(api.state.standby, false, `round ${round}`);
    assert.equal(api.state.serverStatus, "error", `round ${round}`);
    assert.equal(api.state.offline, false, `round ${round}`);
    assert.equal(el.textContent, "Shisu-ko: cache is broken", `round ${round}`);
    assert.ok(el.classes.has("shisuko-status-error"), `round ${round}`);
    assert.equal(api.premineAllowed(), true, `round ${round}`);
    assert.equal(api.ankiPollAllowed(), true, `round ${round}`);
    const st = { paused: true, t: 10, status: api.state.serverStatus, covered: api.state.covered, duration: 1200, lastSyncAt: NOW, standby: api.state.standby };
    assert.equal(api.shouldSync(st, NOW + 1000), true, "an error is watched every tick, paused or not");
  }
  await api.sync();
  assert.equal(api.state.serverStatus, "ready");
  assert.equal(api.state.standby, false);

  // Unreachable is not standby either, whatever the last tick said: offline outranks the flag in
  // every guard, but the flag must not be what is left once the server is back.
  await api.sync();
  assert.equal(api.state.standby, true);
  await api.sync();
  assert.equal(api.state.standby, false);
  assert.equal(api.state.offline, true);
  assert.equal(api.state.serverStatus, "offline");
});

test("sync keeps the server's language verdict and drops it with the cues", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  serverAnswering(sandbox, [
    { status: "ready", session: "a", language_paused: true, heard: "en", cues: [], next: 0 },
    { status: "standby" },
    { status: "ready", session: "a", cues: [], next: 0 },
    { status: "pending", session: "b", cues: [], next: 0 }, // the server restarted: nothing heard yet
  ]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 10, paused: false };

  await api.sync();
  assert.equal(api.state.languagePaused, true);
  assert.equal(api.state.heard, "en");
  await api.sync();
  assert.equal(api.state.languagePaused, true, "a standby answer says nothing about the video");
  assert.equal(api.state.heard, "en");
  await api.sync();
  assert.equal(api.state.languagePaused, false, "the server is listening again");
  assert.equal(api.state.heard, null);

  api.state.languagePaused = true;
  api.state.heard = "en";
  await api.sync();
  assert.equal(api.state.languagePaused, false);
  assert.equal(api.state.heard, null);
});

test("a request refused by the server drops the language pause, so its error shows in the pause's place", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  serverAnswering(sandbox, [
    { status: "ready", session: "a", language_paused: true, heard: "en", cues: [], next: 0 },
    { ok: false, error: "division by zero", data: { ok: false, error: "division by zero" } }, // a 500
    { status: "ready", session: "a", language_paused: true, heard: "en", cues: [], next: 0 },
    { ok: false, error: "invalid t/since", data: { ok: false, error: "invalid t/since" } }, // a 400
    { status: "ready", session: "a", language_paused: true, heard: "en", cues: [], next: 0 },
    { ok: false, offline: true, error: "Server unreachable" },
  ]);
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 10, paused: false };
  const paused = "Shisu-ko paused: the speech is not in the subtitle language (hearing en)";

  await api.sync();
  assert.equal(el.textContent, paused);
  for (const error of ["division by zero", "invalid t/since"]) {
    await api.sync();
    // statusText() ranks the pause above the status switch, so the verdict of the last good answer
    // used to sit in front of the server's words, unstyled, for as long as the server refused.
    assert.equal(api.state.serverStatus, "error", error);
    assert.equal(api.state.languagePaused, false, error);
    assert.equal(api.state.heard, null, error);
    assert.equal(el.textContent, `Shisu-ko: ${error}`);
    assert.ok(el.classes.has("shisuko-status-error"), error);
    await api.sync();
    assert.equal(api.state.languagePaused, true, "the next real answer brings the verdict back");
    assert.equal(el.textContent, paused);
  }
  await api.sync();
  assert.equal(api.state.languagePaused, false, "unreachable says as little about the session");
  assert.equal(api.state.heard, null);
  assert.equal(el.textContent, "Shisu-ko server offline. Start it with server/run.cmd or docker/up.cmd");
});

// ------------------------------------------------------------------ keyboard commands

test("the master switch stops the mine, transcript and known-word commands, and only the switch itself works", () => {
  const { api, sandbox, sent, onCommand } = loadContent();
  subtitleBox(api, sandbox);
  api.mergeCues([{ id: 0, start: 4, end: 7, text: "これはテスト字幕です" }]);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 5, paused: false };
  const mines = () => sent.filter((m) => m.type === "mine");
  const saves = () => sent.filter((m) => m.type === "saveSettings").map((m) => plain(m.settings));

  api.state.settings.enabled = false; // the cues outlive the switch, as they do in the page
  onCommand({ type: "command", name: "mine-current" });
  assert.equal(mines().length, 0); // used to capture a frame and write the newest card
  onCommand({ type: "command", name: "toggle-transcript" });
  assert.deepEqual(saves(), []);
  sandbox.document.getSelection = () => ({ isCollapsed: false, rangeCount: 1, getRangeAt: () => ({ commonAncestorContainer: api.state.subBox }), toString: () => "字幕" });
  onCommand({ type: "command", name: "mark-known" }); // would put the selected word on the known list
  assert.deepEqual(saves(), []);
  onCommand({ type: "command", name: "toggle-subtitles" });
  assert.deepEqual(saves(), [{ enabled: true }]);

  api.state.settings.enabled = true;
  onCommand({ type: "command", name: "mine-current" });
  assert.equal(mines().length, 1);
  assert.equal(mines()[0].cue.text, "これはテスト字幕です");
  assert.equal(mines()[0].auto, false);
  onCommand({ type: "command", name: "toggle-transcript" });
  assert.deepEqual(saves()[1], { showTranscript: true });
  onCommand({ type: "command", name: "mark-known" });
  assert.deepEqual(saves()[2], { knownWords: "字幕" });
  onCommand({ type: "other" }); // not a command: nothing happens
  assert.equal(mines().length, 1);
});

test("Alt+Shift+H switches the status badge off and on, says so, and does nothing while off", () => {
  const { api, sandbox, sent, onCommand } = loadContent();
  api.state.toastEl = sandbox.document.createElement("div");
  const saves = () => sent.filter((m) => m.type === "saveSettings").map((m) => plain(m.settings));
  api.state.settings.enabled = false;
  onCommand({ type: "command", name: "toggle-status" });
  assert.deepEqual(saves(), []);
  assert.equal(api.state.toastEl.textContent, "");

  api.state.settings.enabled = true;
  assert.equal(api.state.settings.statusBadge, true, "on by default");
  onCommand({ type: "command", name: "toggle-status" });
  assert.deepEqual(saves(), [{ statusBadge: false }]);
  assert.equal(api.state.toastEl.textContent, "Status badge hidden: the same shortcut or the popup shows it again");
  api.state.settings.statusBadge = false; // what the storage listener brings back
  onCommand({ type: "command", name: "toggle-status" });
  assert.deepEqual(saves(), [{ statusBadge: false }, { statusBadge: true }]);
  assert.equal(api.state.toastEl.textContent, "Status badge shown");
});

// ------------------------------------------------------------------ sync answers

test("a /sync answer from before a detour to another video is not applied to this one", async () => {
  const { api, sandbox } = loadContent();
  await settled();
  const bodies = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  sandbox.browser.runtime.sendMessage = async (msg) => {
    if (msg.path !== "/sync") return { ok: true };
    bodies.push(msg.body);
    if (bodies.length === 1) {
      await gate; // A's request hangs (the server stalled) while the viewer clicks B and comes back
      return { ok: true, data: { status: "ready", session: "sA", cues: [cue(500, "遅い")], next: 501, covered: [[0, 1010]] } };
    }
    return { ok: true, data: { status: "ready", session: "sA", cues: [], next: 0 } };
  };
  api.state.videoId = "A";
  api.state.video = { currentTime: 1000, paused: false };
  api.state.since = 500;
  api.state.serverSession = "sA";
  const first = api.sync();
  assert.equal(bodies[0].since, 500);
  api.onVideoChanged("B");
  api.onVideoChanged("A"); // both syncs are dropped: A's request is still out
  assert.equal(bodies.length, 1);
  release();
  await first;
  // The stale answer used to set `since` to 501, so cues 0..499 were never fetched again.
  assert.equal(api.state.since, 0);
  assert.deepEqual(plain(api.state.cues), []);
  assert.deepEqual(plain(api.state.covered), []);
  assert.equal(api.state.serverSession, null);
  assert.equal(api.state.serverStatus, "connecting");
  await api.sync();
  assert.equal(bodies[1].since, 0);
});

test("an error answered by a running server is not 'offline', and keeps the server's words", async () => {
  for (const error of ["invalid video_id", "division by zero", "origin not allowed"]) {
    const { api, sandbox, sent } = loadContent();
    await settled();
    sandbox.browser.runtime.sendMessage = async (msg) => {
      sent.push(msg);
      return msg.path === "/sync" ? { ok: false, error, data: { ok: false, error } } : { ok: true }; // a 4xx/5xx, as apiRequest passes it on
    };
    const el = statusElement();
    api.state.statusEl = el;
    api.state.videoId = "abcdef1234";
    api.state.video = { currentTime: 12, paused: false };
    api.mergeCues([{ id: 0, start: 10, end: 13, text: "いち" }]);
    await api.sync();
    assert.equal(api.state.offline, false, error);
    assert.equal(api.state.serverStatus, "error", error);
    assert.equal(api.state.serverError, error);
    assert.equal(el.textContent, `Shisu-ko: ${error}`); // used to say the server was offline
    assert.ok(el.classes.has("shisuko-status-error"));
    assert.equal(api.premineAllowed(), true, error); // mining goes on: /clip is another handler
    assert.equal(api.ankiPollAllowed(), true, error);
  }
  // Unreachable is still offline, with the fixed hint.
  const { api, sandbox } = loadContent();
  await settled();
  sandbox.browser.runtime.sendMessage = async (msg) => (msg.path === "/sync" ? { ok: false, offline: true, error: "Server unreachable" } : { ok: true });
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 12, paused: false };
  await api.sync();
  assert.equal(api.state.offline, true);
  assert.equal(api.state.serverStatus, "offline");
  assert.equal(el.textContent, "Shisu-ko server offline. Start it with server/run.cmd or docker/up.cmd");
  assert.equal(api.premineAllowed(), false);
});

test("the background gone, or something not ours on the port, is 'offline', not a refusal by the server", async () => {
  const cases = [
    // sendMessage's own {ok, error} during a reload or a worker restart, in both of its shapes.
    ["rejects", () => Promise.reject(new Error("Could not establish connection. Receiving end does not exist."))],
    ["throws", () => { throw new Error("Extension context invalidated."); }],
    ["non-JSON", async () => ({ ok: false, error: "Server returned a non-JSON response" })],
  ];
  for (const [name, answer] of cases) {
    const { api, sandbox } = loadContent();
    await settled();
    sandbox.browser.runtime.sendMessage = (msg) => (msg.path === "/sync" ? answer(msg) : Promise.resolve({ ok: true }));
    const el = statusElement();
    api.state.statusEl = el;
    api.state.videoId = "abcdef1234";
    api.state.video = { currentTime: 12, paused: false };
    api.mergeCues([{ id: 0, start: 10, end: 13, text: "いち" }]);
    await api.sync();
    assert.equal(api.state.offline, true, name); // used to show the exception's text as the server's verdict
    assert.equal(api.state.serverStatus, "offline", name);
    assert.equal(el.textContent, "Shisu-ko server offline. Start it with server/run.cmd or docker/up.cmd", name);
    assert.equal(api.premineAllowed(), false, name); // every other message would fail the same way
    assert.equal(api.ankiPollAllowed(), false, name);
  }
  // An HTTP error with an empty body (a bare 502 from a proxy) is still an answer from the port:
  // `data` null, not missing.
  const { api, sandbox } = loadContent();
  await settled();
  sandbox.browser.runtime.sendMessage = async (msg) => (msg.path === "/sync" ? { ok: false, error: "HTTP 502", data: null } : { ok: true });
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 12, paused: false };
  await api.sync();
  assert.equal(api.state.offline, false);
  assert.equal(api.state.serverStatus, "error");
  assert.equal(el.textContent, "Shisu-ko: HTTP 502");
});

// ------------------------------------------------------------------ transcript panel

// An overlay the way buildOverlay leaves it, out of the harness's stub elements; createElement and
// replaceChildren are counted, since the point of the panel's bookkeeping is what it does not build.
function panel(loaded) {
  const { api, sandbox, stubElement } = loaded;
  const list = stubElement("div");
  const counts = { created: 0, replaced: 0 };
  const create = sandbox.document.createElement;
  sandbox.document.createElement = (tag) => {
    counts.created++;
    return create(tag);
  };
  const replace = list.replaceChildren;
  list.replaceChildren = (...nodes) => {
    counts.replaced++;
    return replace(...nodes);
  };
  Object.assign(api.state, { root: stubElement("div"), transcriptEl: stubElement("div"), transcriptList: list, subBox: stubElement("div"), subText: stubElement("span") });
  api.state.settings.showTranscript = true;
  return { list, counts, ids: () => list.children.map((line) => Number(line.dataset.id)), reset: () => { counts.created = 0; counts.replaced = 0; } };
}

const at = (id, start) => ({ id, start, end: start + 1, text: `c${id}`, seg: id });

test("a settings change leaves the transcript panel alone; a panel shown again only renders what is pending", () => {
  const loaded = loadContent();
  const { api, onSettingsChanged } = loaded;
  const { list, counts, ids, reset } = panel(loaded);
  api.mergeCues([at(0, 0), at(1, 10), at(2, 20)]);
  assert.deepEqual(ids(), [0, 1, 2]);
  assert.equal(counts.replaced, 1); // the first cues replace the placeholder
  reset();
  api.setSubtitle(api.cueById(1));
  assert.ok(list.children[1].classList.contains("shisuko-active"));
  list.scrollTop = 4321; // the reader scrolled away
  // What the popup saves on every slider tick: the whole settings object, here with one value changed.
  for (const patch of [{ fontScale: 1.4 }, { subPosition: 12 }, { subTextColor: "#ff0000" }, { autoMine: false }, {}]) {
    onSettingsChanged({ settings: { newValue: Object.assign({}, plain(api.state.settings), patch) } }, "local");
    assert.equal(counts.created, 0, JSON.stringify(patch)); // used to rebuild every line
    assert.equal(counts.replaced, 0, JSON.stringify(patch));
    assert.equal(list.scrollTop, 4321, JSON.stringify(patch));
  }
  assert.deepEqual(ids(), [0, 1, 2]);
  assert.ok(list.children[1].classList.contains("shisuko-active"));

  // Hidden, two lines arrive in order and the active line moves on; shown again, only those two
  // are built and the highlight catches up.
  onSettingsChanged({ settings: { newValue: Object.assign({}, plain(api.state.settings), { showTranscript: false }) } }, "local");
  api.mergeCues([at(3, 30), at(4, 40)]);
  api.setSubtitle(api.cueById(3));
  assert.equal(counts.created, 0);
  assert.equal(list.children.length, 3);
  onSettingsChanged({ settings: { newValue: Object.assign({}, plain(api.state.settings), { showTranscript: true }) } }, "local");
  assert.equal(counts.created, 8); // four elements a line, used to be 20
  assert.equal(counts.replaced, 0);
  assert.deepEqual(ids(), [0, 1, 2, 3, 4]);
  assert.ok(!list.children[1].classList.contains("shisuko-active"));
  assert.ok(list.children[3].classList.contains("shisuko-active"));
  assert.equal(api.state.activeLineEl, list.children[3]);
});

test("cues transcribed behind the rendered lines are inserted in place, never rebuilt", () => {
  const loaded = loadContent();
  const { api } = loaded;
  const { list, counts, ids, reset } = panel(loaded);
  const order = () => plain(api.state.cues.map((c) => c.id));
  api.mergeCues([at(0, 0), at(1, 10), at(2, 20), at(3, 5400), at(4, 5410)]); // watched the start, jumped to 1:30:00
  reset();
  api.setSubtitle(api.cueById(4));
  // Seeked back to 0:45:00: every window the server finishes there sorts in behind the last lines.
  api.mergeCues([at(5, 2700), at(6, 2704), at(7, 2708)]);
  assert.equal(counts.created, 12);
  assert.equal(counts.replaced, 0); // used to rebuild all eight lines
  assert.deepEqual(ids(), order());
  assert.deepEqual(ids(), [0, 1, 2, 5, 6, 7, 3, 4]);
  reset();
  // A batch spread over the whole array: before the first line, between existing ones, after the last.
  api.mergeCues([at(8, -1), at(9, 15), at(10, 2702), at(11, 6000), at(12, 5405)]);
  assert.equal(counts.created, 20);
  assert.equal(counts.replaced, 0);
  assert.deepEqual(ids(), order());
  assert.deepEqual(ids(), [8, 0, 1, 9, 2, 5, 10, 6, 7, 3, 12, 4, 11]);
  assert.ok(list.children[11].classList.contains("shisuko-active")); // still the line being read
  reset();
  // The same start as the last line, with an earlier end: sorted in front of it.
  api.mergeCues([{ id: 13, start: 6000, end: 6000.5, text: "c13", seg: 13 }]);
  assert.deepEqual(ids(), order());
  assert.deepEqual(ids().slice(-2), [13, 11]);
  assert.equal(counts.replaced, 0);
  reset();
  // Still in order after all that: the append path.
  api.mergeCues([at(14, 7000)]);
  assert.equal(counts.created, 4);
  assert.equal(counts.replaced, 0);
  assert.deepEqual(ids(), order());
  for (const cue of api.state.cues) assert.equal(api.state.lineById.get(cue.id), list.children[order().indexOf(cue.id)]);
  reset();
  // Dropped cues (another video, a new session) still rebuild: the placeholder, then the first lines.
  api.onVideoChanged("zyxwvu9876");
  assert.equal(counts.replaced, 1);
  assert.equal(list.children.length, 1);
  assert.equal(list.children[0].textContent, "No transcript yet.");
  api.mergeCues([at(0, 0)]);
  assert.equal(counts.replaced, 2);
  assert.deepEqual(ids(), [0]);
});

test("out-of-order cues that arrived while the panel was hidden are inserted once it shows", () => {
  const loaded = loadContent();
  const { api, onSettingsChanged } = loaded;
  const { counts, ids, reset } = panel(loaded);
  api.mergeCues([at(0, 0), at(1, 100), at(2, 200)]);
  onSettingsChanged({ settings: { newValue: Object.assign({}, plain(api.state.settings), { showTranscript: false }) } }, "local");
  reset();
  api.mergeCues([at(3, 50)]); // behind
  api.mergeCues([at(4, 300)]); // in order again
  assert.equal(counts.created, 0);
  onSettingsChanged({ settings: { newValue: Object.assign({}, plain(api.state.settings), { showTranscript: true }) } }, "local");
  assert.equal(counts.created, 8);
  assert.equal(counts.replaced, 0);
  assert.deepEqual(ids(), [0, 3, 1, 2, 4]);
});

// ------------------------------------------------------------------ pre-mining frames

// A tab with a line on screen, ready to be prepared; the frame read is counted through the canvas.
async function premining(settings) {
  const loaded = loadContent();
  await settled();
  const { api, sandbox, sent } = loaded;
  const timers = [];
  sandbox.setTimeout = (fn) => timers.push(fn);
  let canvases = 0;
  const create = sandbox.document.createElement;
  sandbox.document.createElement = (tag) => {
    if (tag === "canvas") canvases++;
    return create(tag);
  };
  Object.assign(api.state.settings, settings);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 0.5, paused: false, videoWidth: 1920, videoHeight: 1080 };
  api.mergeCues([at(0, 0), at(1, 3)]);
  api.state.activeCueId = 0;
  const premines = () => sent.filter((m) => m.type === "premine");
  return { api, timers, premines, canvases: () => canvases };
}

test("pre-mining reads no frame back unless automatic Anki mining can use it; the clip is still prepared", async () => {
  for (const settings of [{ autoMine: false }, { mineTarget: "download" }, { autoMine: false, mineTarget: "download" }]) {
    const { api, timers, premines, canvases } = await premining(settings);
    assert.equal(api.autoAnkiMining(), false);
    assert.equal(api.premineAllowed(), true);
    await api.premineNow(0);
    assert.equal(canvases(), 0, JSON.stringify(settings)); // used to read the GPU back and ship 200 KB per line
    assert.equal(premines().length, 2);
    assert.ok(!("imageDataUrl" in premines()[0]), JSON.stringify(settings));
    assert.equal(premines()[0].key, 0); // this sentence's clip
    assert.equal(premines()[1].key, 1); // and the next one's
    assert.equal(premines()[1].ahead, true);
    api.captureHoverFrame();
    assert.equal(timers.length, 0); // a hover reads nothing back either
  }
  const { api, timers, premines, canvases } = await premining({});
  assert.equal(api.autoAnkiMining(), true);
  await api.premineNow(0);
  assert.equal(canvases(), 1);
  assert.ok("imageDataUrl" in premines()[0]);
  api.captureHoverFrame();
  assert.equal(timers.length, 1);
});

// ------------------------------------------------------------------ synthetic events

test("a click or hover the viewer did not make (isTrusted false) mines, seeks and pauses nothing", () => {
  const { api, sent } = loadContent();
  api.mergeCues([{ id: 0, start: 4, end: 7, text: "これはテスト字幕です" }]);
  api.state.videoId = "abcdef1234";
  api.state.activeCueId = 0; // the line on screen: mined without a seek, so the message goes out at once
  let paused = 0;
  api.state.video = { currentTime: 5, paused: false, ended: false, pause: () => { paused++; }, play: async () => {} };
  const mines = () => sent.filter((m) => m.type === "mine");
  const click = (isTrusted, target) => ({ isTrusted, target, preventDefault: () => {}, stopPropagation: () => {} });

  api.onMineClick(click(false)); // document.querySelector(".shisuko-mine").click() from a page script
  assert.equal(mines().length, 0);
  api.onMineClick(click(true));
  assert.equal(mines().length, 1);

  const line = { dataset: { id: "0", start: "4" } };
  const mineButton = { closest: (sel) => (sel === ".shisuko-line" ? line : null) };
  const target = { closest: (sel) => (sel === ".shisuko-line-mine" ? mineButton : sel === ".shisuko-time" ? { closest: () => line } : null) };
  api.state.mining = false;
  api.onTranscriptClick(click(false, target));
  assert.equal(mines().length, 1);
  assert.equal(api.state.video.currentTime, 5);
  api.onTranscriptClick(click(true, target));
  assert.equal(mines().length, 2);

  api.onSubtitleEnter({ isTrusted: false });
  assert.equal(paused, 0);
  assert.equal(api.state.hoverPaused, false);
  api.onSubtitleEnter({ isTrusted: true });
  assert.equal(paused, 1);
  assert.equal(api.state.hoverPaused, true);
});

// ------------------------------------------------------------------ the player on screen

// A player element with its video, the way discover() finds them: by id, and the video inside.
function playerWithVideo(stubElement, id, currentTime, paused) {
  const player = stubElement("div");
  player.id = id;
  const video = stubElement("video");
  Object.assign(video, { currentTime, paused });
  player.querySelector = (sel) => (sel.startsWith("video") ? video : null);
  return { player, video };
}

test("isShortsUrl tells a Short's address from a watch page's", () => {
  const { api } = loadContent();
  assert.equal(api.isShortsUrl("https://www.youtube.com/shorts/SHORTID1234"), true);
  assert.equal(api.isShortsUrl("https://www.youtube.com/watch?v=abcdef1234"), false);
  assert.equal(api.isShortsUrl("https://www.youtube.com/"), false);
  assert.equal(api.isShortsUrl("not a url"), false);
});

test("a Short reached from a watch page is followed on the Shorts player, not the hidden watch player", async () => {
  const loaded = loadContent({ href: "https://www.youtube.com/watch?v=WATCHID1234" });
  const { api, sandbox, sent, stubElement } = loaded;
  // YouTube's SPA keeps the watch page in the document when it leaves it: #movie_player stays,
  // under a hidden ytd-watch-flexy, with its video paused; #shorts-player is the one on screen.
  // In document order the watch player comes first, so it is what the class alone finds.
  const movie = playerWithVideo(stubElement, "movie_player", 123.4, true);
  const shorts = playerWithVideo(stubElement, "shorts-player", 2.5, false);
  const byQuery = { "#movie_player": movie.player, ".html5-video-player": movie.player, "#shorts-player": shorts.player, "video.html5-main-video": movie.video };
  sandbox.document.querySelector = (sel) => byQuery[sel] || null;
  await settled(); // the boot's discover(): the watch page
  const syncs = () => sent.filter((m) => m.path === "/sync").map((m) => m.body);
  assert.equal(api.state.player, movie.player);
  assert.equal(syncs()[0].video_id, "WATCHID1234");
  assert.equal(syncs()[0].t, 123.4);
  sent.length = 0;

  sandbox.location.href = "https://www.youtube.com/shorts/SHORTID1234";
  api.state.rediscover = true; // what yt-navigate-finish does
  api.discover();
  await settled();
  assert.equal(api.state.player, shorts.player); // used to keep the hidden watch player
  assert.equal(api.state.root.parentNode, shorts.player);
  assert.equal(api.state.videoId, "SHORTID1234");
  // The Short's own clock, not the hidden video's: the server used to be asked for the Short at
  // 123.4 s of a paused video it then fetched and transcribed whole, for cues nobody could see.
  assert.deepEqual(plain(syncs().map((b) => [b.video_id, b.t, b.paused])), [["SHORTID1234", 2.5, false]]);

  // Back on a watch page, #movie_player is the one again.
  sandbox.location.href = "https://www.youtube.com/watch?v=WATCHID1234";
  api.state.rediscover = true;
  api.discover();
  assert.equal(api.state.player, movie.player);
  assert.equal(api.state.root.parentNode, movie.player);
});

test("findPlayer falls back to the class on a Short without #shorts-player, and to #movie_player elsewhere", async () => {
  const { api, sandbox } = loadContent();
  await settled(); // the boot's discover() finds nothing; only the queries below see the fake page
  const asked = [];
  sandbox.document.querySelector = (sel) => {
    asked.push(sel);
    return sel === ".html5-video-player" ? "by-class" : null;
  };
  assert.equal(api.findPlayer("https://www.youtube.com/shorts/SHORTID1234"), "by-class");
  assert.deepEqual(asked, ["#shorts-player", "#movie_player", ".html5-video-player"]);
  asked.length = 0;
  assert.equal(api.findPlayer("https://www.youtube.com/watch?v=abcdef1234"), "by-class");
  assert.deepEqual(asked, ["#movie_player", ".html5-video-player"]); // never the Shorts player on a watch page
});

// ------------------------------------------------------------------ ads

test("startTimeFromUrl reads a link's t= in every spelling, and nothing else", () => {
  const { api } = loadContent();
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=90"), 90);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=90s"), 90);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=1m30s"), 90);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=1h2m3s"), 3723);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=1h"), 3600);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234"), 0);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t="), 0);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=abc"), 0);
  assert.equal(api.startTimeFromUrl("https://www.youtube.com/watch?v=abcdef1234&t=-5"), 0);
  assert.equal(api.startTimeFromUrl("not a url"), 0);
});

test("an ad no longer holds up /sync: the request carries the video's own position, not the ad's clock", async () => {
  const { api, sandbox, sent } = loadContent({ href: "https://www.youtube.com/watch?v=abcdef1234&t=1m30s" });
  await settled();
  const classes = new Set(["ad-showing"]);
  api.state.player = { classList: { contains: (c) => classes.has(c) }, removeEventListener: () => {} };
  api.state.video = { currentTime: 3.2, paused: false }; // the pre-roll ad, on its own clock
  const bodies = () => sent.filter((m) => m.path === "/sync").map((m) => [m.body.t, m.body.paused]);
  api.onVideoChanged("abcdef1234");
  await settled();
  // Used to send nothing until the ad was over, so the server fetched and transcribed nothing
  // meanwhile; the link's t= is where the video will start.
  assert.deepEqual(plain(bodies()), [[90, false]]);
  await api.sync();
  assert.deepEqual(plain(bodies()), [[90, false], [90, false]]);

  classes.delete("ad-showing");
  api.state.video.currentTime = 95;
  await api.sync();
  assert.deepEqual(plain(bodies()).pop(), [95, false]);
  // A mid-roll: the last position outside an ad, not the video's opening and not the ad's 2 s.
  classes.add("ad-interrupting");
  api.state.video.currentTime = 2;
  api.state.video.paused = true;
  await api.sync();
  assert.deepEqual(plain(bodies()).pop(), [95, true]);
  // The status line judges that position too: covered far ahead of 95 s, there is nothing to say.
  api.state.statusEl = statusElement();
  Object.assign(api.state, { serverStatus: "ready", covered: [[90, 1000]], duration: 1500 });
  api.updateStatus();
  assert.ok(api.state.statusEl.classes.has("shisuko-hidden")); // not "Transcribing…" for the ad's 2 s
  classes.delete("ad-interrupting");
  api.state.video.currentTime = 96;
  api.state.video.paused = false;
  await api.sync();
  assert.deepEqual(plain(bodies()).pop(), [96, false]);
  // Another video starts over, at its own start.
  sandbox.location.href = "https://www.youtube.com/watch?v=zyxwvu9876";
  classes.add("ad-showing");
  api.onVideoChanged("zyxwvu9876");
  await settled();
  assert.deepEqual(plain(bodies()).pop(), [0, false]);
});

// ------------------------------------------------------------------ the blank between lines

test("findActiveCue holds a line over a gap whose blank, after the linger, would be a flicker", () => {
  const { api } = loadContent();
  // The server leaves gaps of 0.5 s and more open, so this is the commonest gap there is. With
  // the default linger of 0.3 s and the next line up 0.05 s early, only 0.15 s of it would be
  // blank: a render tick of empty box, judged on the whole gap until now.
  api.mergeCues([{ id: 0, start: 0, end: 10, text: "A" }, { id: 1, start: 10.5, end: 20, text: "B" }]);
  for (const t of [10.2, 10.3, 10.35, 10.4]) assert.equal(api.findActiveCue(t).text, "A", String(t));
  assert.equal(api.findActiveCue(10.45).text, "B");
  // A gap whose blank is a real pause stays one.
  const wide = loadContent().api;
  wide.mergeCues([{ id: 0, start: 0, end: 10, text: "A" }, { id: 1, start: 10.7, end: 20, text: "B" }]);
  assert.equal(wide.findActiveCue(10.3).text, "A");
  assert.equal(wide.findActiveCue(10.35), null);
  assert.equal(wide.findActiveCue(10.6), null);
  assert.equal(wide.findActiveCue(10.65).text, "B");
  // Without a linger the same 0.5 s gap is 0.45 s of blank: a pause.
  const bare = loadContent().api;
  bare.state.settings.lingerSeconds = 0;
  bare.mergeCues([{ id: 0, start: 0, end: 10, text: "A" }, { id: 1, start: 10.5, end: 20, text: "B" }]);
  assert.equal(bare.findActiveCue(10.05).text, "A");
  assert.equal(bare.findActiveCue(10.2), null);
});

// ------------------------------------------------------------------ YouTube's own keyboard controls

// Enough of a selector engine for KEY_SKIP_SELECTOR, so closest() answers the way a browser would
// instead of being stubbed to "no match": a list of compound selectors made of a tag, #id, .class,
// [attr], [attr="value"] and :not(...), matched up a chain of fake elements.
function compound(sel) {
  const parts = [];
  const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=("?)([^\]"]*)\5)?\]|:not\(([^)]*)\)/g;
  let consumed = 0;
  for (let m = re.exec(sel); m; m = re.exec(sel)) {
    if (m.index !== consumed) throw new Error(`unparsed selector: ${sel}`);
    consumed = re.lastIndex;
    const [, tag, id, cls, attr, , val, not] = m;
    if (tag) parts.push((el) => el.tag === tag.toLowerCase());
    else if (id) parts.push((el) => el.attrs.id === id);
    else if (cls) parts.push((el) => (el.attrs.class || "").split(/\s+/).includes(cls));
    else if (attr) parts.push(val === undefined ? (el) => attr in el.attrs : (el) => el.attrs[attr] === val);
    else parts.push(((inner) => (el) => !inner(el))(compound(not)));
  }
  if (consumed !== sel.length) throw new Error(`unparsed selector: ${sel}`);
  return (el) => parts.every((p) => p(el));
}

function fakeElement(tag, attrs = {}, parent = null) {
  const node = { tag, attrs, parent };
  node.closest = (selector) => {
    const alternatives = selector.split(",").map((s) => compound(s.trim()));
    for (let n = node; n; n = n.parent) if (alternatives.some((match) => match(n))) return n;
    return null;
  };
  return node;
}

test("the arrow keys leave YouTube's focused sliders, menus and dialog controls alone", () => {
  // The shapes YouTube renders, from its player and its Polymer dialogs.
  const body = fakeElement("body");
  const player = fakeElement("div", { id: "movie_player", class: "html5-video-player" }, body);
  const controls = fakeElement("div", { class: "ytp-chrome-bottom" }, player);
  const volume = fakeElement("div", { class: "ytp-volume-panel", role: "slider", tabindex: "0" }, controls);
  const progress = fakeElement("div", { class: "ytp-progress-bar", role: "slider", tabindex: "0" }, controls);
  const playButton = fakeElement("button", { class: "ytp-play-button ytp-button" }, controls);
  const menu = fakeElement("div", { class: "ytp-panel-menu", role: "menu" }, player);
  const quality = fakeElement("div", { class: "ytp-menuitem", role: "menuitem", tabindex: "0" }, menu);
  const stableVolume = fakeElement("div", { class: "ytp-menuitem", role: "menuitemcheckbox", tabindex: "0" }, menu);
  const speed = fakeElement("div", { class: "ytp-menuitem", role: "menuitemradio", tabindex: "0" }, menu);
  const dialog = fakeElement("tp-yt-paper-dialog", { role: "dialog" }, body);
  const radio = fakeElement("tp-yt-paper-radio-button", { role: "radio", tabindex: "0" }, fakeElement("tp-yt-paper-radio-group", { role: "radiogroup" }, dialog));
  const option = fakeElement("tp-yt-paper-item", { role: "option", tabindex: "0" }, fakeElement("tp-yt-paper-listbox", { role: "listbox" }, dialog));
  const search = fakeElement("input", { id: "search" }, body);
  const comment = fakeElement("div", { contenteditable: "true" }, fakeElement("ytd-comments", {}, body));

  const jumps = (target) => {
    const { api } = loadContent();
    api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }, { id: 1, start: 5, end: 7, text: "に" }]);
    api.state.videoId = "abcdef1234";
    api.state.covered = [[0, 30]];
    api.state.video = { currentTime: 3, paused: true };
    const ev = arrowKey("ArrowRight");
    ev.target = target;
    api.onKeyDown(ev);
    return ev.count() === 2 && api.state.video.currentTime.toFixed(2) === "4.85";
  };
  // Nothing of YouTube's has the key: the jump, as before.
  for (const [name, target] of Object.entries({ body, player, progress, playButton })) assert.equal(jumps(target), true, name);
  // A control of YouTube's that moves on the arrows has them: the volume slider used to jump a
  // line instead of lowering the volume, and the menus and dialogs lost their arrows the same way.
  for (const [name, target] of Object.entries({ volume, menu, quality, stableVolume, speed, radio, option, search, comment })) {
    assert.equal(jumps(target), false, name);
  }
});

// ------------------------------------------------------------------ the status line

test("updateStatus shows a session error only as text, capped like the model error", async () => {
  const { api } = loadContent();
  const el = statusElement();
  api.state.statusEl = el;
  api.state.videoId = "abcdef1234";
  api.state.serverStatus = "error";
  api.state.serverError = "e".repeat(160);
  api.updateStatus();
  assert.equal(el.textContent, `Shisu-ko: ${"e".repeat(160)}`); // exactly the cap: untouched
  api.state.serverError = "e".repeat(161);
  api.updateStatus();
  assert.equal(el.textContent, `Shisu-ko: ${"e".repeat(159)}…`); // used to run the whole way
  // Not a string is not the server's words: neither "[object Object]" nor "a,b".
  const object = await syncWith({ status: "error", error: { code: 5 } });
  assert.equal(object.api.state.serverError, null);
  const list = await syncWith({ status: "error", error: ["a", "b"] });
  assert.equal(list.api.state.serverError, null);
  const words = await syncWith({ status: "error", error: "This video is only available to members" });
  assert.equal(words.api.state.serverError, "This video is only available to members");
  // The 500 path, where the raw exception comes as the error (with the body as `data`, the way
  // apiRequest passes an HTTP error on), is capped the same way.
  const { api: five, sandbox } = loadContent();
  await settled();
  const http = (error) => ({ ok: false, error, data: { ok: false, error } });
  sandbox.browser.runtime.sendMessage = async (msg) => (msg.path === "/sync" ? http("x".repeat(400)) : { ok: true });
  five.state.statusEl = statusElement();
  five.state.videoId = "abcdef1234";
  five.state.video = { currentTime: 12, paused: false };
  await five.sync();
  assert.equal(five.state.statusEl.textContent, `Shisu-ko: ${"x".repeat(159)}…`);
  sandbox.browser.runtime.sendMessage = async (msg) => (msg.path === "/sync" ? http({ code: 5 }) : { ok: true });
  await five.sync();
  assert.equal(five.state.statusEl.textContent, "Shisu-ko: error");
});

// ------------------------------------------------------------------ a video change mid-flight

test("a video change while a frame is read back files nothing under the new video", async () => {
  const { api, sandbox, sent } = loadContent();
  await settled();
  const create = sandbox.document.createElement;
  sandbox.document.createElement = (tag) => {
    const el = create(tag);
    // The viewer clicks the next video while the frame is being encoded.
    if (tag === "canvas") el.toBlob = (cb) => { api.onVideoChanged("bbbbbbbbbbb"); cb(null); };
    return el;
  };
  api.state.videoId = "aaaaaaaaaaa";
  api.state.video = { currentTime: 0.5, paused: false, videoWidth: 1920, videoHeight: 1080 };
  api.mergeCues([at(0, 0), at(1, 3)]);
  api.state.activeCueId = 0;
  await api.premineNow(0);
  await settled();
  assert.equal(api.state.videoId, "bbbbbbbbbbb");
  // Used to send { videoId: "bbbbbbbbbbb", key: 0, sentence: c0 at 0-1 s }: the old line under the
  // new video, keyed like its first line, and its frame on the new video's first card.
  assert.deepEqual(sent.filter((m) => m.type === "premine"), []);
});

test("a mine whose seek was overtaken by a change of video sends nothing under the new one", async () => {
  const { api, sandbox, sent } = loadContent();
  await settled();
  const timers = [];
  sandbox.setTimeout = (fn) => timers.push(fn);
  const listeners = {};
  const video = {
    currentTime: 30, paused: true, videoWidth: 1920, videoHeight: 1080,
    pause() { this.paused = true; },
    play: async () => {},
    addEventListener: (type, fn) => { listeners[type] = fn; },
    removeEventListener: () => {},
  };
  api.state.videoId = "aaaaaaaaaaa";
  api.state.video = video;
  api.mergeCues([at(0, 4), at(1, 28)]);
  api.state.activeCueId = 1; // line 0 is not on screen: mining it seeks back for its frame
  const line = { dataset: { id: "0", start: "4" } };
  const mineButton = { closest: (sel) => (sel === ".shisuko-line" ? line : null) };
  const target = { closest: (sel) => (sel === ".shisuko-line-mine" ? mineButton : null) };
  api.onTranscriptClick({ isTrusted: true, target, preventDefault: () => {} });
  assert.equal(video.currentTime, 4.4); // the seek is out
  assert.equal(api.state.mining, true);
  api.onVideoChanged("bbbbbbbbbbb"); // the viewer clicked the next video meanwhile; the element stays
  // The seek lands, then the paint wait; a second round for the restoring seek there used to be.
  for (let round = 0; round < 2; round++) {
    listeners.seeked();
    while (timers.length) timers.shift()();
    await settled();
  }
  assert.equal(api.state.mining, false);
  // Used to be { videoId: "bbbbbbbbbbb", cue: c0 at 4-5 s }: a clip cut from the new video at the
  // old line's times. And the old video's position is not restored onto the new one.
  assert.deepEqual(sent.filter((m) => m.type === "mine"), []);
  assert.equal(video.currentTime, 4.4);
});

test("a premine answered after the video changed fills nothing, and the old sentence is dropped behind it", async () => {
  const { api, sandbox, sent } = loadContent();
  await settled();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  sandbox.browser.runtime.sendMessage = async (msg) => {
    sent.push(msg);
    if (msg.type !== "premine") return { ok: true };
    if (msg.videoId === "A") {
      await gate; // the background is slow to answer: its settings read went to storage
      return { ok: true, held: [{ key: 0, cueIds: [0], image: false, audio: true }, { key: 2, cueIds: [2], image: false, audio: true }] };
    }
    return { ok: true, held: [{ key: msg.key, cueIds: msg.cueIds, image: false, audio: false }] };
  };
  api.state.settings.autoMine = false; // the clip alone: no frame to read back
  api.state.videoId = "A";
  api.state.video = { currentTime: 0.5, paused: false };
  api.mergeCues([at(0, 0), at(1, 3), at(2, 6)]);
  api.state.activeCueId = 0;
  const premines = () => sent.filter((m) => m.type === "premine").map((m) => `${m.videoId}:${m.key}`);
  const first = api.premineNow(0);
  await settled();
  assert.deepEqual(premines(), ["A:0"]);
  api.onVideoChanged("B"); // with A:0 still out
  assert.deepEqual(plain(api.state.premined), []);
  release();
  await first;
  await settled();
  // Used to take A's inventory as the tab's: keys 0 and 2, which are B's first lines.
  assert.deepEqual(plain(api.state.premined), []);
  // The background files A:0 once its settings read returns, after a reset fired at the change
  // would have passed: the reset goes out behind the answer instead (and used to not go at all).
  const reset = sent.findIndex((m) => m.type === "premineReset");
  assert.ok(reset > sent.findIndex((m) => m.type === "premine"));
  assert.equal(sent.filter((m) => m.type === "premineReset").length, 1);
  // B's first line, id 0 again, is prepared like any other instead of counting as A's.
  api.mergeCues([at(0, 0), at(1, 3)]);
  api.state.activeCueId = 0;
  await api.premineNow(0);
  assert.deepEqual(premines(), ["A:0", "B:0", "B:1"]);
  assert.deepEqual(plain(api.state.premined.map((e) => e.key)), [1]);
});

// ------------------------------------------------------------------ the hover frame

test("re-entering the subtitle over the same paused frame reads nothing back and ships nothing again", async () => {
  const { api, sandbox, sent } = loadContent();
  await settled();
  const timers = [];
  sandbox.setTimeout = (fn) => timers.push(fn);
  let reads = 0;
  let canvases = 0;
  const create = sandbox.document.createElement;
  sandbox.document.createElement = (tag) => {
    const el = create(tag);
    if (tag === "canvas") {
      canvases++;
      el.getContext = () => ({ drawImage: () => { reads++; } });
      el.toBlob = (cb) => cb("ZnJhbWU=");
    }
    return el;
  };
  sandbox.browser.runtime.sendMessage = async (msg) => {
    sent.push(msg);
    if (msg.type !== "premine") return { ok: true };
    return { ok: true, held: [{ key: msg.key, cueIds: msg.cueIds, image: !!msg.imageDataUrl, audio: false }] };
  };
  api.state.videoId = "abcdef1234";
  const video = { currentTime: 0.5, paused: false, ended: false, videoWidth: 1920, videoHeight: 1080, pause() { this.paused = true; }, play: async () => {} };
  api.state.video = video;
  api.mergeCues([at(0, 0), at(1, 3)]);
  api.state.activeCueId = 0;
  const hovers = () => sent.filter((m) => m.type === "premine" && m.hover);
  const flush = async () => {
    while (timers.length) timers.shift()();
    await settled();
    await settled();
  };
  api.onSubtitleEnter({ isTrusted: true });
  assert.equal(video.paused, true);
  await flush();
  assert.equal(reads, 1);
  assert.equal(hovers().length, 1);
  assert.ok(hovers()[0].imageDataUrl.startsWith("data:image/jpeg;base64,"));
  // Out to the dictionary popup and back, once per word looked up: the video is paused on the
  // same frame, and the background holds it already.
  for (let i = 0; i < 3; i++) {
    api.onSubtitleLeave({ isTrusted: true, relatedTarget: null });
    api.onSubtitleEnter({ isTrusted: true });
    await flush();
  }
  assert.equal(reads, 1, "used to read the same frame back on every crossing");
  assert.equal(hovers().length, 1, "and ship it again");
  // Another position is another frame, and one canvas serves every read.
  video.currentTime = 1.2;
  api.onSubtitleLeave({ isTrusted: true, relatedTarget: null });
  api.onSubtitleEnter({ isTrusted: true });
  await flush();
  assert.equal(reads, 2);
  assert.equal(hovers().length, 2);
  assert.equal(canvases, 1);
});

// ------------------------------------------------------------------ cards from the ledger

// A tab polling for cards: a line on screen, the background's ankiPoll answer scripted, and the
// toast within reach.
async function polling(answer) {
  const loaded = loadContent();
  await settled(); // discover() has run: it finds no player and would reset what is set up here
  const { api, sandbox, sent, stubElement } = loaded;
  sandbox.browser.runtime.sendMessage = async (msg) => {
    sent.push(msg);
    return msg.type === "ankiPoll" ? answer() : { ok: true };
  };
  api.mergeCues([
    { id: 0, start: 4, end: 7, text: "これはテスト字幕です" },
    { id: 1, start: 8, end: 10, text: "別の行が続きます" },
  ]);
  api.state.videoId = "abcdef1234";
  api.state.activeCueId = 0;
  api.state.video = { currentTime: 5, paused: false, ended: false, pause() {}, play: async () => {} };
  const toast = stubElement("div");
  api.state.toastEl = toast;
  assert.equal(api.ankiPollAllowed(), true);
  return { api, toast, mines: () => sent.filter((m) => m.type === "mine") };
}

const NOBODYS = { sentence: "全然違う文章を読んでいます", word: "文章" };

test("a card from the ledger (replayed) is attached to the line it matches, like the tab's own find", async () => {
  const note = { sentence: "これはテスト字幕です", word: "テスト" };
  const { api, toast, mines } = await polling(() => ({ ok: true, newNoteId: 41, note, replayed: true }));
  await api.pollForNewCard();
  await settled();
  assert.equal(mines().length, 1);
  assert.equal(mines()[0].noteId, 41);
  assert.equal(mines()[0].auto, true);
  assert.equal(mines()[0].cue.text, "これはテスト字幕です");
  assert.equal(toast.textContent, "Mined");
});

test("a replayed card matching no line here is another tab's: no toast, no fallback to the playhead, nothing mined", async () => {
  const { api, toast, mines } = await polling(() => ({ ok: true, newNoteId: 42, note: NOBODYS, replayed: true }));
  await api.pollForNewCard();
  await settled();
  assert.equal(mines().length, 0);
  assert.equal(toast.textContent, "");
  assert.equal(api.state.mining, false);
  // A replayed card with nothing to match on (the ledger never carries one) is not attached at
  // the playhead either, as the tab's own would be.
  api.autoMine(43, { sentence: "", word: "" }, true);
  await settled();
  assert.equal(mines().length, 0);
  assert.equal(toast.textContent, "");
});

test("the tab's own find (no replayed flag) keeps saying when the card matches no subtitle", async () => {
  const { api, toast, mines } = await polling(() => ({ ok: true, newNoteId: 44, note: NOBODYS }));
  await api.pollForNewCard();
  await settled();
  assert.equal(mines().length, 0);
  assert.equal(toast.textContent, "New card's sentence matches no subtitle; nothing attached");
  assert.ok(toast.className.includes("shisuko-toast-warn"));
  // And its card with neither sentence nor word still goes to the line at the playhead.
  api.autoMine(45, { sentence: "", word: "" });
  await settled();
  assert.equal(mines().length, 1);
  assert.equal(mines()[0].noteId, 45);
  assert.equal(mines()[0].cue.text, "これはテスト字幕です");
});

// ------------------------------------------------------------------ transcript hover

// A tab with the transcript panel up, every line's listeners within reach, and the timers by
// hand; the background answers a premine with the hovered sentence in front, as heldFor does.
async function transcriptHover() {
  const loaded = loadContent();
  await settled();
  const { api, sandbox, sent } = loaded;
  const create = sandbox.document.createElement;
  sandbox.document.createElement = (tag) => {
    const el = create(tag);
    el.listeners = {};
    el.addEventListener = (name, fn) => {
      el.listeners[name] = fn;
    };
    return el;
  };
  const timers = new Map();
  let nextTimer = 1;
  sandbox.setTimeout = (fn) => {
    timers.set(nextTimer, fn);
    return nextTimer++;
  };
  sandbox.clearTimeout = (id) => {
    timers.delete(id);
  };
  sandbox.browser.runtime.sendMessage = async (msg) => {
    sent.push(msg);
    if (msg.type !== "premine") return { ok: true };
    return { ok: true, held: [{ key: msg.key, cueIds: msg.cueIds, image: false, audio: false }] };
  };
  const { list } = panel(loaded);
  api.state.videoId = "abcdef1234";
  api.state.video = { currentTime: 0.5, paused: false, videoWidth: 1920, videoHeight: 1080 };
  api.mergeCues([at(0, 0), { id: 1, start: 3, end: 4, text: "c1", seg: 1 }, { id: 2, start: 4.2, end: 5, text: "c2", seg: 1 }]);
  api.state.activeCueId = 0;
  const fire = () => {
    for (const [id, fn] of [...timers]) {
      timers.delete(id);
      fn();
    }
  };
  const enter = (line, isTrusted = true) => line.listeners.mouseenter({ isTrusted, currentTarget: line });
  const leave = (line, isTrusted = true) => line.listeners.mouseleave({ isTrusted, currentTarget: line });
  return { api, sent, list, timers, fire, enter, leave, premines: () => sent.filter((m) => m.type === "premine") };
}

test("a trusted hover on a transcript line ranks its sentence first: a premine with hover set and no frame", async () => {
  const { api, list, timers, fire, enter, premines } = await transcriptHover();
  const line = list.children[1]; // cue 1, the first of two cues Whisper put in one segment
  enter(line);
  assert.equal(premines().length, 0); // only once the pointer has rested on the line
  assert.equal(timers.size, 1);
  fire();
  await settled();
  assert.equal(premines().length, 1);
  const msg = premines()[0];
  assert.equal(msg.hover, true);
  assert.ok(!("imageDataUrl" in msg), "the panel is not the video: no frame is read");
  assert.ok(!msg.ahead);
  assert.equal(msg.videoId, "abcdef1234");
  assert.equal(msg.key, 1);
  // Cue 2 shares cue 1's segment 0.2 s later; the hovered line is still the whole card.
  assert.deepEqual(plain(msg.cueIds), [1]);
  assert.deepEqual(plain(msg.sentence), { start: 3, end: 4, text: "c1" });
  // What the background answered is the order a card is matched by: this line before the playing one.
  assert.equal(api.rankOfCue(api.state.premined, api.cueById(1)), 0);
  assert.equal(api.rankOfCue(api.state.premined, api.cueById(2)), Infinity);
  assert.equal(api.rankOfCue(api.state.premined, api.cueById(0)), Infinity);
});

test("leaving a transcript line, or a hover the viewer did not make, sends nothing; nor does a switched-off add-on", async () => {
  const { api, sent, list, timers, fire, enter, leave, premines } = await transcriptHover();
  const first = list.children[0];
  const second = list.children[1];
  // The pointer only crossed the line.
  enter(first);
  assert.equal(timers.size, 1);
  leave(first);
  assert.equal(timers.size, 0);
  fire();
  await settled();
  assert.equal(premines().length, 0);
  // On to another line before the wait is out: only the line the pointer rests on is sent.
  enter(first);
  enter(second);
  assert.equal(timers.size, 1);
  fire();
  await settled();
  assert.equal(premines().length, 1);
  assert.equal(premines()[0].key, 1);
  // A mouseenter a page script dispatched.
  enter(second, false);
  assert.equal(timers.size, 0);
  // Another video before the wait is out: the line is the old video's.
  enter(second);
  api.onVideoChanged("zzzzzz1234");
  assert.equal(timers.size, 0);
  await settled(); // the video change drops what the background held for the old one
  assert.equal(sent[sent.length - 1].type, "premineReset");
  // The master switch: off at the hover, and off between the hover and its wait.
  api.mergeCues([at(0, 0), at(1, 3)]);
  const sentSoFar = sent.length;
  api.state.settings.enabled = false;
  assert.equal(api.premineAllowed(), false);
  enter(list.children[0]);
  assert.equal(timers.size, 0);
  api.state.settings.enabled = true;
  enter(list.children[0]);
  assert.equal(timers.size, 1);
  api.state.settings.enabled = false;
  fire();
  await settled();
  assert.equal(sent.length, sentSoFar);
  assert.equal(premines().length, 1);
});

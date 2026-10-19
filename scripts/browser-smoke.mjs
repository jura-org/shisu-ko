#!/usr/bin/env node

/*
 * Deterministic Chrome smoke test. It exercises the packaged extension against a
 * YouTube-shaped document and local HTTP fixtures; no YouTube or Anki account is
 * involved. Run after `npm run build` with Playwright available.
 */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";
import { chromium } from "playwright";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const extension = resolve(process.env.SHISUKO_CHROME_DIST || join(root, "dist", "chrome"));
const videoId = "smoke123";
const secondVideoId = "smoke456";
const regressionVideoId = "smoke789";

function json(res, body, status = 200) {
  res.writeHead(status, { "content-type": "application/json", "access-control-allow-origin": "*" });
  res.end(JSON.stringify(body));
}

async function listen(handler) {
  const server = createServer(handler);
  await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  return { server, port: server.address().port };
}

const mediaBytes = (() => {
  const sampleRate = 8000;
  const data = Buffer.alloc(44 + sampleRate * 20 * 2);
  const dataSize = data.length - 44;
  data.write("RIFF", 0); data.writeUInt32LE(36 + dataSize, 4);
  data.write("WAVEfmt ", 8); data.writeUInt32LE(16, 16);
  data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
  data.writeUInt32LE(sampleRate, 24); data.writeUInt32LE(sampleRate * 2, 28);
  data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
  data.write("data", 36); data.writeUInt32LE(dataSize, 40);
  return data;
})();
const mediaDataUrl = `data:audio/wav;base64,${mediaBytes.toString("base64")}`;
// The fixture AnkiConnect, which the extension must never call: the server reads Anki for the word
// colours (DeckIndexes in server.py) and writes the mined media (Miner); here the fixture server
// stands in for both. The stored settings name it under ankiUrl, the address 0.22.0 kept, so an
// extension that still read that setting would knock here.
const anki = { actions: [] };
// The deck the fixture server reads for the word colours: rows as /sync's `deck` carries them, the
// first answer every row, the next ones the rows changed since the token the client names.
const deck = { bodies: [], version: 1, rows: new Map([[1, [1, "テスト", "", "new", "heiban"]], [2, [2, "字幕", "", "learned", "atamadaka"]]]), changed: new Map() };
function deckAnswer(asked) {
  const token = `smoke.${deck.version}`;
  if (asked.since === token) return undefined;
  const base = /^smoke\.(\d+)$/.exec(asked.since || "");
  if (!base) return { token, status: "ok", deck: "Smoke::Mining", automatic: !asked.name, full: true, notes: [...deck.rows.values()], removed: [] };
  const ids = new Set();
  for (let v = Number(base[1]) + 1; v <= deck.version; v++) for (const id of deck.changed.get(v) || []) ids.add(id);
  return { token, status: "ok", deck: "Smoke::Mining", automatic: !asked.name, full: false, notes: [...ids].map((id) => deck.rows.get(id)), removed: [] };
}
// The fixture server's mining log, as server.py's /sync answers it: one entry per mine asked for.
const mining = { bodies: [], log: [] };
async function requestBody(req) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return body ? JSON.parse(body) : {};
}
async function poll(read, expected, timeout = 5000) {
  const deadline = Date.now() + timeout;
  let value;
  while (Date.now() < deadline) {
    value = await read();
    if (expected(value)) return value;
    await new Promise((resolvePoll) => setTimeout(resolvePoll, 100));
  }
  throw new Error(`Timed out waiting for browser state (last value: ${JSON.stringify(value)})`);
}
let syncCount = 0;
let healthCount = 0;
const syncSeen = new Map(); // video id -> how many /sync requests actually reached the server
let languagePaused = false; // fixture switch for the wrong-language answer
const api = await listen(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (url.pathname === "/health") {
    healthCount++;
    // The popup's deck list comes from the server too (/health?decks=1).
    const decks = url.searchParams.get("decks") === "1" ? { anki_decks: { ok: true, decks: ["Default", "Smoke::Mining"], seen: "Smoke::Mining" } } : {};
    return json(res, { model: "smoke", device: "cpu", compute_type: "test", ...decks });
  }
  if (url.pathname === "/sync") {
    syncCount++;
    const body = await requestBody(req);
    const id = typeof body.video_id === "string" ? body.video_id : "";
    syncSeen.set(id, (syncSeen.get(id) || 0) + 1);
    mining.bodies.push({ id, anki: body.anki, mine: body.mine, mined_since: body.mined_since });
    const deckReply = body.deck ? deckAnswer(body.deck) : undefined;
    if (body.deck) deck.bodies.push(body.deck);
    // A mine is answered once per nonce, the way the server dedupes the resends.
    if (body.mine && !mining.log.some((entry) => entry.nonce === body.mine.nonce)) {
      mining.log.push({ seq: mining.log.length, nonce: body.mine.nonce, ok: true, status: "attached", note: 404,
        message: "Added Picture + SentenceAudio to the newest Anki card" });
    }
    const since = typeof body.mined_since === "number" ? body.mined_since : null;
    const minedLog = { mined: since === null ? [] : mining.log.filter((entry) => entry.seq >= since), mined_next: mining.log.length };
    const head = { session: "smoke-session", status: "ready", duration: 3600, covered: [[0, 3600]], next: 1, ...minedLog,
      ...(deckReply ? { deck: deckReply } : {}) };
    if (languagePaused) return json(res, { ...head, cues: [], language_paused: true, heard: "en" });
    // Cues come with the first request only, the way the real server answers a `since` cursor. A
    // tab returning from standby with a reset cursor would therefore go blank instead of keeping
    // the cue it already had, which is what the standby round trip must not do.
    const regressionCues = [
      { id: 10, start: 2, end: 4, text: "前の字幕", seg: 7 },
      { id: 11, start: 4.5, end: 6, text: "狙った字幕", seg: 7 },
      { id: 13, start: 6.1, end: 7, text: "つなぎの字幕", seg: 7 },
      { id: 12, start: 8, end: 10, text: "次の字幕", seg: 7 },
    ];
    const cues = body.since ? [] : id === regressionVideoId ? regressionCues : [{ id: 1, start: 0, end: 3600, text: "これはテスト字幕です", seg: 1 }];
    return json(res, { ...head, cues });
  }
  if (url.pathname === "/anki") {
    const request = req.method === "POST" ? await requestBody(req) : {};
    anki.actions.push(request.action || req.method);
    return json(res, { result: null, error: "the extension must not call AnkiConnect" }, 400);
  }
  return json(res, { error: "not found" }, 404);
});
const closedHealth = await listen(() => {});
await new Promise((resolveClose) => closedHealth.server.close(resolveClose));
const closedHealthUrl = `http://127.0.0.1:${closedHealth.port}`;

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
html,body { margin: 0; background: #111; color: white; } #movie_player { position: relative; width: 900px; height: 520px; }
video { width: 900px; height: 520px; background: #222; }
</style></head><body><div id="movie_player" class="html5-video-player"><video class="html5-main-video"></video></div></body></html>`;
let context;
try {
  const launchOptions = {
    headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, "--no-sandbox"],
  };
  if (process.env.CHROMIUM_PATH) launchOptions.executablePath = process.env.CHROMIUM_PATH;
  else launchOptions.channel = "chromium";
  context = await chromium.launchPersistentContext("", launchOptions);
  const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
  const extensionId = new URL(worker.url()).host;
  const commands = await worker.evaluate(() => new Promise((resolve) => chrome.commands.getAll(resolve)));
  for (const [name, shortcut] of [["toggle-subtitles", "Alt+Shift+S"], ["toggle-transcript", "Alt+Shift+L"], ["mine-current", "Alt+Shift+M"]]) {
    const command = commands.find((entry) => entry.name === name);
    // Chrome spells the same shortcut in the Mac's glyphs (⌥⇧S), so compare the key, not the text.
    const mac = `⌥⇧${shortcut.slice(-1)}`;
    assert.ok(command?.shortcut === shortcut || command?.shortcut === mac,
      `Chrome command ${name} should register ${shortcut}, got ${command?.shortcut}`);
  }
  // A fresh update check with no release keeps every popup this test opens off api.github.com:
  // the popup's first question asks for the day's check, and the background answers it from a
  // stored one that is under a day old (background.js, startupCheck).
  const updateCheck = { checkedAt: Date.now(), latest: null, error: null };
  // ankiAutoSync is what 0.20.0 and 0.21.0 stored for their Anki sync: left in a viewer's storage,
  // it must change nothing (no AnkiConnect `sync`, asserted at the end of the run).
  await worker.evaluate((stored) => new Promise((resolve) => chrome.storage.local.set(stored, resolve)),
    { settings: { serverUrl: closedHealthUrl, ankiAutoSync: true, ankiUrl: `http://127.0.0.1:${api.port}/anki` }, updateCheck });
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${extensionId}/popup.html`);
  await poll(() => popup.locator("#server-status").textContent(), (value) => value === "Server offline");
  assert.equal(await popup.locator("#serverUrl").inputValue(), closedHealthUrl);
  await popup.locator("details").last().locator("summary").click();
  // The seeded check answers the result line; a live one would name a release or a failure.
  await poll(() => popup.locator("#update-result").textContent(), (value) => value === "No release found, checked just now");
  assert.equal(await popup.locator("#update-banner").isHidden(), true, "no banner without a release");
  await popup.locator("#serverUrl").fill(`http://127.0.0.1:${api.port}`);
  await popup.locator("#pauseOnHover").check();
  // The word colours: the deck list and its hint come from the server's /health?decks=1.
  await popup.locator("#cardStatus").check();
  await popup.locator("#pitchAccent").check();
  await popup.locator("#showTranscript").check();
  await popup.waitForTimeout(600);
  await poll(() => popup.locator("#server-status").textContent(), (value) => value === "Server online");
  assert.ok(healthCount > 0, "popup health check must reach the fixture server");
  await poll(() => popup.locator("#deck-hint").textContent(), (value) => value === "Looking at Smoke::Mining", 5000);
  assert.deepEqual(await popup.locator("#cardStatusDeck option").evaluateAll((options) => options.map((o) => o.value)), ["", "Default", "Smoke::Mining"]);
  await popup.close();

  const openWatch = async (id, options = {}) => {
    const tab = await context.newPage();
    await tab.route("https://www.youtube.com/watch**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: html }));
    await tab.goto(`https://www.youtube.com/watch?v=${id}`);
    await tab.evaluate(async ({ mediaUrl }) => {
      const video = document.querySelector("video");
      if (mediaUrl) video.src = mediaUrl;
      const canvas = document.createElement("canvas");
      canvas.width = 900; canvas.height = 520;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#234"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      video.muted = true;
      if (!mediaUrl) video.srcObject = canvas.captureStream(10);
      await video.play();
    }, { mediaUrl: options.media ? mediaDataUrl : null });
    await tab.locator(".shisuko-subtext").waitFor({ timeout: 10000 });
    return tab;
  };

  const page = await openWatch(videoId);
  await poll(() => page.locator(".shisuko-subtext").textContent(), (value) => value.includes("これはテスト字幕です"), 10000);
  assert.ok(syncCount > 0, "content script must reach the fixture server");

  // The word colours from the server's rows: テスト new with its heiban overbar, 字幕 learned.
  const marks = () => page.locator(".shisuko-subtext .shisuko-word").evaluateAll((spans) => spans.map((s) => [s.textContent, s.dataset.status || null, s.dataset.pitch || null]));
  await poll(marks, (value) => JSON.stringify(value) === JSON.stringify([["テスト", "new", "heiban"], ["字幕", "learned", "atamadaka"]]), 10000);
  assert.deepEqual(deck.bodies[0], { name: "", word: "", pitch: "", since: null }, "the automatic deck, words.js's default fields, nothing held");
  assert.ok(deck.bodies.some((b) => b.since === "smoke.1"), "the next /sync names the token held");
  // A card reviewed: the next answer is that row alone, and the line follows it.
  deck.rows.set(1, [1, "テスト", "", "learned", "heiban"]);
  deck.changed.set(++deck.version, [1]);
  await poll(marks, (value) => value[0] && value[0][1] === "learned", 10000);
  assert.equal(await page.locator(".shisuko-transcript").evaluate((el) => !el.classList.contains("shisuko-hidden")), true);

  const video = page.locator("video");
  const subBox = await page.locator(".shisuko-sub").boundingBox();
  assert.ok(subBox, "subtitle must have a layout box");
  await page.mouse.move(subBox.x + subBox.width / 2, subBox.y + subBox.height / 2);
  await poll(() => video.evaluate((el) => el.paused), (value) => value, 3000);
  const box = await video.boundingBox();
  assert.ok(box, "fixture video must have a layout box");
  await page.mouse.move(box.x + 10, box.y + 10);
  await poll(() => video.evaluate((el) => !el.paused), (value) => value, 3000);

  // Drag the subtitle's grip to each dock: the box follows the pointer, snaps to the nearest dock
  // on release, and the dock is saved. The fixture player sits at the page's top left, so there
  // is room to its right for vertical columns outside it, and the window bottom lies below it.
  const viewport = page.viewportSize();
  const dragGrip = async (x, y) => {
    const sub = await page.locator(".shisuko-sub").boundingBox();
    await page.mouse.move(sub.x + sub.width / 2, sub.y + sub.height / 2);
    const grip = await poll(() => page.locator(".shisuko-drag").boundingBox(), (value) => !!value && value.width > 0, 3000);
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.move(grip.x + grip.width / 2 + 10, grip.y + grip.height / 2 + 10, { steps: 3 });
    assert.equal(await page.locator(".shisuko-dock-target").count() >= 4, true, "the docks show while dragging");
    await page.mouse.move(x, y, { steps: 8 });
    await page.mouse.up();
    assert.equal(await page.locator(".shisuko-dock-target").count(), 0, "the targets go with the release");
  };
  const storedDock = () => worker.evaluate(() => new Promise((resolveDock) => chrome.storage.local.get("settings", (got) => resolveDock(got.settings?.subDock))));
  const playerRect = await page.locator("#movie_player").boundingBox();
  await dragGrip(playerRect.x + playerRect.width + 60, playerRect.y + 200);
  await poll(storedDock, (value) => value === "video-right", 3000);
  const right = await page.locator(".shisuko-sub").evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { mode: getComputedStyle(el).writingMode, left: r.left, outside: !!el.closest(".shisuko-outside") };
  });
  assert.equal(right.mode, "vertical-rl");
  assert.equal(right.outside, true, "the right dock lives outside the player");
  assert.ok(right.left >= playerRect.x + playerRect.width, `the column must stand right of the video (left ${right.left})`);
  await dragGrip(playerRect.x + playerRect.width / 2, viewport.height - 10);
  await poll(storedDock, (value) => value === "window-bottom", 3000);
  const windowBottom = await page.locator(".shisuko-sub").evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { mode: getComputedStyle(el).writingMode, bottom: r.bottom };
  });
  assert.equal(windowBottom.mode, "horizontal-tb");
  assert.ok(windowBottom.bottom > playerRect.y + playerRect.height && windowBottom.bottom <= viewport.height,
    `the box must sit below the video at the window's bottom (bottom ${windowBottom.bottom})`);
  await dragGrip(playerRect.x + playerRect.width / 2, playerRect.y + playerRect.height * 0.85);
  await poll(storedDock, (value) => value === "video-bottom", 3000);
  assert.equal(await page.locator(".shisuko-sub").evaluate((el) => !!el.closest(".shisuko-root")), true);
  assert.equal(await page.locator(".shisuko-outside").evaluate((el) => el.classList.contains("shisuko-hidden")), true);
  // A press without a move is a click, and moves nothing.
  const grip = await page.locator(".shisuko-drag").boundingBox();
  if (grip) {
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.up();
    assert.equal(await storedDock(), "video-bottom");
  }
  await page.mouse.move(box.x + 10, box.y + 10);
  await poll(() => video.evaluate((el) => !el.paused), (value) => value, 3000);

  // The viewer's mine: a line's button puts a request on the next /sync, with the popup's Anki
  // settings, and the server's answer comes back in the log and as the toast.
  await page.locator(".shisuko-line-mine").first().click({ force: true });
  const asked = await poll(() => mining.bodies.find((entry) => entry.id === videoId && entry.mine), Boolean, 10000);
  assert.equal(asked.mine.cue_id, 1);
  assert.equal(asked.mine.session, "smoke-session");
  assert.deepEqual(asked.anki, { auto: true, picture: "Picture", audio: "SentenceAudio", sentence: "", word: "", pad_ms: 200, format: "mp3" });
  assert.equal(typeof asked.mined_since, "number", "the cursor from the first answer");
  await poll(() => page.locator(".shisuko-toast").textContent(), (value) => value === "Added Picture + SentenceAudio to the newest Anki card", 10000);
  const nonce = asked.mine.nonce;
  await poll(() => mining.bodies.filter((entry) => entry.mine && entry.mine.nonce === nonce).length, (n) => n >= 1, 3000);
  const resent = mining.bodies.filter((entry) => entry.mine && entry.mine.nonce === nonce).length;
  await page.waitForTimeout(2500);
  assert.equal(mining.bodies.filter((entry) => entry.mine && entry.mine.nonce === nonce).length, resent, "an answered mine is not sent again");

  // The popup's Anki settings reach the server inside /sync; the extension itself writes nothing.
  const cdp = await context.newCDPSession(page);
  const ankiPopup = await context.newPage();
  await ankiPopup.goto(`chrome-extension://${extensionId}/popup.html`);
  await ankiPopup.locator("details").last().locator("summary").click();
  await ankiPopup.locator("#autoMine").uncheck();
  await ankiPopup.locator("#ankiImageField").fill("picture");
  await ankiPopup.locator("#ankiAudioField").fill("sentenceAudio");
  await ankiPopup.locator("#ankiSentenceField").fill("Sentence");
  await ankiPopup.locator("#ankiSentenceField").press("Tab"); // a text field saves on its change event
  await ankiPopup.locator("#clipFormat").selectOption("wav");
  await ankiPopup.locator("#enabled").check();
  await ankiPopup.waitForTimeout(400);
  await ankiPopup.close();
  const changed = await poll(() => mining.bodies.filter((entry) => entry.id === videoId).at(-1), (entry) => entry?.anki?.picture === "picture" && entry.anki.format === "wav" && entry.anki.sentence === "Sentence", 10000);
  assert.deepEqual(changed.anki, { auto: false, picture: "picture", audio: "sentenceAudio", sentence: "Sentence", word: "", pad_ms: 200, format: "wav" });
  await page.goto("about:blank");

  // Force the MV3 worker down, then reopen the popup. This catches code that only
  // appears to persist settings while the original worker remains in memory.
  const targets = await cdp.send("Target.getTargets");
  const serviceTarget = targets.targetInfos.find((target) => target.type === "service_worker" && target.url.startsWith(`chrome-extension://${extensionId}/`));
  assert.ok(serviceTarget, "extension service worker target should exist");
  const oldTargetId = serviceTarget.targetId;
  await worker.evaluate(() => { globalThis.__shisukoSmokeGeneration = "before-stop"; });
  await cdp.send("ServiceWorker.enable");
  await cdp.send("ServiceWorker.stopAllWorkers");
  await poll(async () => {
    const state = await cdp.send("Target.getTargets");
    return state.targetInfos.some((target) => target.targetId === oldTargetId);
  }, (present) => !present, 5000);

  const popupAfterRestart = await context.newPage();
  await popupAfterRestart.goto(`chrome-extension://${extensionId}/popup.html`);
  assert.equal(await popupAfterRestart.locator("#serverUrl").inputValue(), `http://127.0.0.1:${api.port}`);
  const restartedTarget = await poll(async () => {
    const state = await cdp.send("Target.getTargets");
    return state.targetInfos.find((target) => target.type === "service_worker" && target.url.startsWith(`chrome-extension://${extensionId}/`));
  }, (target) => !!target, 5000);
  const restartedWorker = context.serviceWorkers().find((candidate) => candidate.url().startsWith(`chrome-extension://${extensionId}/`));
  assert.ok(restartedWorker, `restarted worker target missing: ${JSON.stringify(restartedTarget)}`);
  assert.equal(await restartedWorker.evaluate(() => globalThis.__shisukoSmokeGeneration), undefined, "worker must have a fresh global after suspension");
  assert.equal(await popupAfterRestart.locator("#ankiImageField").inputValue(), "picture", "the Anki settings outlive the worker");
  await popupAfterRestart.close();

  // Two YouTube tabs at once: only the one the viewer is looking at may drive the server.
  const statusPopup = await context.newPage();
  await statusPopup.goto(`chrome-extension://${extensionId}/popup.html`);
  await statusPopup.locator("#showStatus").uncheck();
  await statusPopup.waitForTimeout(400);
  await statusPopup.close();

  const first = await openWatch(videoId);
  const firstText = first.locator(".shisuko-subtext");
  const firstStatus = first.locator(".shisuko-status");
  await poll(() => firstText.textContent(), (value) => value.includes("これはテスト字幕です"), 10000);
  const second = await openWatch(secondVideoId);
  const secondText = second.locator(".shisuko-subtext");
  const secondStatus = second.locator(".shisuko-status");
  await poll(() => secondText.textContent(), (value) => value.includes("これはテスト字幕です"), 10000);

  const visible = (locator) => locator.evaluate((el) => !el.classList.contains("shisuko-hidden"));
  const seen = (id) => syncSeen.get(id) || 0;
  const grewOnlyFor = async (holderId, standbyId, by) => {
    const mark = new Map(syncSeen);
    await poll(() => seen(holderId), (count) => count >= (mark.get(holderId) || 0) + by, 10000);
    assert.equal(seen(standbyId), mark.get(standbyId) || 0,
      `${standbyId} must not reach the server while ${holderId} holds the sync right ` +
      `(saw ${seen(standbyId)}, expected ${mark.get(standbyId) || 0})`);
  };

  await first.bringToFront();
  await poll(() => secondStatus.textContent(), (value) => value.includes("another tab"), 8000);
  assert.equal(await visible(secondStatus), true, "the standby line must show even with showStatus off");
  await grewOnlyFor(videoId, secondVideoId, 4);

  // Focus moves, the right follows it.
  await second.bringToFront();
  await poll(() => firstStatus.textContent(), (value) => value.includes("another tab"), 8000);
  assert.equal(await visible(firstStatus), true, "the standby line must show even with showStatus off");
  await grewOnlyFor(secondVideoId, videoId, 3);
  assert.match(await firstText.textContent(), /これはテスト字幕です/,
    "a tab on standby must keep the subtitle it already had");

  // Coming back must not cost the cues: the fixture only sends them at `since: 0`, so a standby
  // round trip that reset the cursor or cleared the cue list would leave the line blank.
  await first.bringToFront();
  assert.match(await firstText.textContent(), /これはテスト字幕です/,
    "the returning tab must still show its cue");
  await grewOnlyFor(videoId, secondVideoId, 3);
  assert.match(await firstText.textContent(), /これはテスト字幕です/,
    "the returning tab must still show its cue after syncing again");

  // The server hears a language that is not the subtitle language and stops.
  languagePaused = true;
  const paused = await poll(() => firstStatus.textContent(), (value) => value.includes("not in the subtitle language"), 8000);
  assert.match(paused, /hearing en/, `language-paused line must name the language heard (got "${paused}")`);
  assert.equal(await visible(firstStatus), true, "the language-paused line must show even with showStatus off");
  languagePaused = false;
  await poll(() => visible(firstStatus), (shown) => !shown, 8000);

  // Four displayed cues share one Whisper segment. A finite local WAV makes trusted keyboard
  // events seek the packaged media element, and the second transcript row drives the full UI mine:
  // the request names that row's cue alone, never the segment around it.
  const regressionPopup = await context.newPage();
  await regressionPopup.goto(`chrome-extension://${extensionId}/popup.html`);
  await regressionPopup.locator("details").last().locator("summary").click();
  await regressionPopup.locator("#clipPaddingMs").focus();
  await regressionPopup.keyboard.press("Home");
  await regressionPopup.locator("#enabled").check();
  await regressionPopup.waitForTimeout(700);
  const regression = await openWatch(regressionVideoId, { media: true });
  await regression.evaluate(() => { const video = document.querySelector("video"); video.pause(); video.currentTime = 5; });
  await poll(() => regression.locator(".shisuko-subtext").textContent(), (value) => value.includes("狙った字幕"), 10000);
  const seek = async (time, key, expected) => {
    await regression.evaluate((value) => { document.querySelector("video").currentTime = value; }, time);
    await regression.keyboard.press(key);
    await poll(() => regression.locator("video").evaluate((video) => ({ time: video.currentTime, seeking: video.seeking })), (state) => !state.seeking && Math.abs(state.time - expected) < 0.01);
  };
  await seek(5.75, "ArrowLeft", 1.85);
  await seek(5.5, "ArrowRight", 6.1);
  await seek(7.5, "ArrowLeft", 6.1);
  await seek(6.5, "ArrowRight", 7.85);
  await regression.locator(".shisuko-line").nth(1).hover();
  await regression.locator(".shisuko-line-mine").nth(1).click();
  const selected = await poll(() => mining.bodies.find((entry) => entry.id === regressionVideoId && entry.mine), Boolean, 10000);
  assert.equal(selected.mine.cue_id, 11, "the row's own cue (狙った字幕), not its segment's first");
  assert.equal(selected.anki.pad_ms, 0);
  await poll(() => regression.locator(".shisuko-toast").textContent(), (value) => value.includes("Added Picture + SentenceAudio"), 10000);
  await regression.close();
  await regressionPopup.close();

  assert.deepEqual(anki.actions, [], `the extension called AnkiConnect: ${anki.actions.join(", ")}`);
  console.log(`browser smoke passed (syncs=${syncCount}, AnkiConnect calls=${anki.actions.length}, deck asks=${deck.bodies.length}, mines=${mining.log.length}, ` +
    `${[...syncSeen].map(([id, count]) => `${id}=${count}`).join(" ")})`);
} finally {
  await context?.close();
  await new Promise((resolveClose) => api.server.close(resolveClose));
}

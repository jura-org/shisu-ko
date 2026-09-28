#!/usr/bin/env node

/*
 * Deterministic Chrome smoke test. It exercises the packaged extension against a
 * YouTube-shaped document and local HTTP fixtures; no YouTube or Anki account is
 * involved. Run after `npm run build` with Playwright available.
 */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
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

const clipBytes = Buffer.from("RIFFSMOKE-WAVE-DATA", "ascii");
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
const tinyJpeg = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/AYf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AYf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/Aqf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IV//2gAMAwEAAgADAAAAEP/EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQMBAT8QH//EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQIBAT8QH//EABQQAQAAAAAAAAAAAAAAAAAAABD/2gAIAQEAAT8QH//Z";
const anki = { notes: new Map(), media: [], updates: [] };
const clipRequests = [];
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
    return json(res, { model: "smoke", device: "cpu", compute_type: "test" });
  }
  if (url.pathname === "/sync") {
    syncCount++;
    const body = await requestBody(req);
    const id = typeof body.video_id === "string" ? body.video_id : "";
    syncSeen.set(id, (syncSeen.get(id) || 0) + 1);
    const head = { session: "smoke-session", status: "ready", duration: 3600, covered: [[0, 3600]], next: 1 };
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
  if (url.pathname === "/clip") {
    clipRequests.push({ video_id: url.searchParams.get("video_id"), start: Number(url.searchParams.get("start")), end: Number(url.searchParams.get("end")) });
    res.writeHead(200, { "content-type": "audio/wav", "content-length": clipBytes.length });
    return res.end(clipBytes);
  }
  if (url.pathname === "/anki" && req.method === "POST") {
    const request = await requestBody(req);
    let result = null;
    if (request.action === "requestPermission") result = { permission: "granted" };
    else if (request.action === "findNotes") result = [...anki.notes.keys()];
    else if (request.action === "notesInfo") result = (request.params.notes || []).map((id) => ({ id, fields: anki.notes.get(Number(id)) || {} }));
    else if (request.action === "storeMediaFile") {
      anki.media.push(request.params);
      result = request.params.filename;
    } else if (request.action === "updateNoteFields") {
      anki.updates.push(request.params.note);
      result = null;
    } else return json(res, { result: null, error: `unknown action ${request.action}` }, 400);
    return json(res, { result, error: null });
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
let downloadsDir;
try {
  downloadsDir = await mkdtemp(join(tmpdir(), "shisuko-browser-"));
  const launchOptions = {
    headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, `--download-default-directory=${downloadsDir}`, "--no-sandbox"],
    acceptDownloads: true,
    downloadsPath: downloadsDir,
  };
  if (process.env.CHROMIUM_PATH) launchOptions.executablePath = process.env.CHROMIUM_PATH;
  else launchOptions.channel = "chromium";
  context = await chromium.launchPersistentContext("", launchOptions);
  const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
  const downloadControlPage = await context.newPage();
  const workerCdp = await context.newCDPSession(downloadControlPage);
  await workerCdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadsDir });
  await downloadControlPage.close();
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
  await worker.evaluate((stored) => new Promise((resolve) => chrome.storage.local.set(stored, resolve)), { settings: { serverUrl: closedHealthUrl }, updateCheck });
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${extensionId}/popup.html`);
  await poll(() => popup.locator("#server-status").textContent(), (value) => value === "Server offline");
  assert.equal(await popup.locator("#serverUrl").inputValue(), closedHealthUrl);
  await popup.locator("details").last().locator("summary").click();
  // The seeded check answers the result line; a live one would name a release or a failure.
  await poll(() => popup.locator("#update-result").textContent(), (value) => value === "No release found, checked just now");
  assert.equal(await popup.locator("#update-banner").isHidden(), true, "no banner without a release");
  await popup.locator("#serverUrl").fill(`http://127.0.0.1:${api.port}`);
  await popup.locator("#mineTarget").selectOption("download");
  await popup.locator("#pauseOnHover").check();
  await popup.locator("#showTranscript").check();
  await popup.waitForTimeout(600);
  await poll(() => popup.locator("#server-status").textContent(), (value) => value === "Server online");
  assert.ok(healthCount > 0, "popup health check must reach the fixture server");
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

  await page.locator(".shisuko-line-mine").first().click({ force: true });
  const records = await poll(() => worker.evaluate(() => new Promise((resolve) => chrome.downloads.search({ orderBy: ["-startTime"] }, resolve))), (items) => items.length >= 2 && items.filter((item) => item.state === "complete" && item.filename).length >= 2, 10000);
  const files = await Promise.all(records.map((item) => readFile(item.filename).catch(() => null)));
  assert.ok(files.some((bytes) => bytes?.equals(clipBytes)), `downloaded audio must contain fixture bytes (${records.map((item, i) => `${item.filename}:${files[i]?.length || 0}`).join(", ")})`);
  assert.ok(files.some((bytes) => bytes?.subarray(0, 2).equals(Buffer.from([0xff, 0xd8]))), "downloaded image must be JPEG");

  // Exercise the real popup -> service worker -> local AnkiConnect path.
  const cdp = await context.newCDPSession(page);
  await page.goto("about:blank");
  const ankiPopup = await context.newPage();
  await ankiPopup.goto(`chrome-extension://${extensionId}/popup.html`);
  const send = (msg) => ankiPopup.evaluate((value) => browser.runtime.sendMessage(value), msg);
  await ankiPopup.locator("details").last().locator("summary").click();
  await ankiPopup.locator("#ankiUrl").fill(`http://127.0.0.1:${api.port}/anki`);
  await ankiPopup.locator("#mineTarget").selectOption("anki");
  await ankiPopup.locator("#autoMine").check();
  await ankiPopup.locator("#ankiImageField").fill("Picture");
  await ankiPopup.locator("#ankiAudioField").fill("SentenceAudio");
  await ankiPopup.locator("#ankiSentenceField").fill("");
  await ankiPopup.locator("#serverUrl").fill(`http://127.0.0.1:${api.port}`);
  await ankiPopup.locator("#enabled").check();
  await ankiPopup.waitForTimeout(400);
  anki.notes.clear();
  assert.equal((await send({ type: "ankiPoll" })).newNoteId, null, "first Anki poll establishes baseline");

  anki.notes.set(101, {
    Picture: { value: "" }, SentenceAudio: { value: "" }, Sentence: { value: "これはテスト字幕です" },
  });
  const found = await poll(() => send({ type: "ankiPoll" }), (value) => value?.newNoteId === 101, 3000);
  assert.equal(found.newNoteId, 101);
  const mediaBefore = anki.media.length;
  const updatesBefore = anki.updates.length;
  const mined = await send({ type: "mine", auto: true, videoId, noteId: 101, cue: { start: 0, end: 1, text: "これはテスト字幕です" }, imageDataUrl: tinyJpeg });
  assert.equal(mined.ok, true, JSON.stringify(mined));
  assert.equal(anki.media.length, mediaBefore + 2);
  assert.ok(anki.media.some((item) => Buffer.from(item.data, "base64").subarray(0, 2).equals(Buffer.from([0xff, 0xd8]))));
  assert.ok(anki.media.some((item) => Buffer.from(item.data, "base64").equals(clipBytes)), "uploaded audio must contain fixture bytes");
  assert.equal(anki.updates.length, updatesBefore + 1);
  assert.equal(anki.updates.at(-1).id, 101);
  assert.match(anki.updates.at(-1).fields.Picture, /<img src=/);
  assert.match(anki.updates.at(-1).fields.SentenceAudio, /\[sound:/);
  anki.notes.set(202, { Sentence: { value: "別の字幕です" } });
  const mismatchMedia = anki.media.length;
  const mismatchUpdates = anki.updates.length;
  const mismatch = await send({ type: "mine", auto: true, videoId, noteId: 202, cue: { start: 0, end: 1, text: "これはテスト字幕です" }, imageDataUrl: tinyJpeg });
  assert.equal(mismatch.mismatch, true);
  assert.equal(anki.media.length, mismatchMedia);
  assert.equal(anki.updates.length, mismatchUpdates);
  await poll(() => send({ type: "ankiPoll" }), (value) => value?.newNoteId === 202, 3000);
  await ankiPopup.close();

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
  anki.notes.set(303, { Sentence: { value: "restart note" } });

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
  assert.equal((await popupAfterRestart.evaluate(() => browser.runtime.sendMessage({ type: "ankiPoll" }))).newNoteId, null, "restart poll establishes a fresh baseline");
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
  // events seek the packaged media element, and the second transcript row drives the full UI mine.
  const regressionPopup = await context.newPage();
  await regressionPopup.goto(`chrome-extension://${extensionId}/popup.html`);
  await regressionPopup.locator("details").last().locator("summary").click();
  await regressionPopup.locator("#ankiUrl").fill(`http://127.0.0.1:${api.port}/anki`);
  await regressionPopup.locator("#mineTarget").selectOption("anki");
  await regressionPopup.locator("#autoMine").uncheck();
  await regressionPopup.locator("#clipPaddingMs").focus();
  await regressionPopup.keyboard.press("Home");
  await regressionPopup.locator("#ankiImageField").fill("Picture");
  await regressionPopup.locator("#ankiAudioField").fill("SentenceAudio");
  await regressionPopup.locator("#ankiSentenceField").fill("");
  await regressionPopup.locator("#serverUrl").fill(`http://127.0.0.1:${api.port}`);
  await regressionPopup.locator("#enabled").check();
  await regressionPopup.waitForTimeout(700);
  const regressionSend = (msg) => regressionPopup.evaluate((value) => browser.runtime.sendMessage(value), msg);
  anki.notes.clear();
  assert.equal((await regressionSend({ type: "ankiPoll" })).newNoteId, null, "regression Anki poll establishes baseline");
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
  anki.notes.set(404, { Sentence: { value: "<b>狙った</b>" }, Picture: { value: "" }, SentenceAudio: { value: "" } });
  await regression.locator(".shisuko-line").nth(1).hover();
  await regression.locator(".shisuko-line-mine").nth(1).click();
  const selectedClip = await poll(() => clipRequests.find((request) => request.video_id === regressionVideoId && request.start === 4.5 && request.end === 6), Boolean, 10000);
  await poll(() => anki.updates.at(-1), (update) => update?.id === 404, 10000);
  assert.equal(anki.updates.at(-1).fields.Sentence, "<b>狙った</b>字幕");
  assert.match(anki.updates.at(-1).fields.SentenceAudio, /\[sound:shisuko_smoke789_4500\.wav\]/);
  assert.doesNotMatch(anki.updates.at(-1).fields.Sentence, /前の字幕|つなぎの字幕|次の字幕/);
  assert.deepEqual(selectedClip, { video_id: regressionVideoId, start: 4.5, end: 6 });
  assert.ok(anki.media.some((item) => item.filename === "shisuko_smoke789_4500.wav" && Buffer.from(item.data, "base64").equals(clipBytes)));
  await regression.close();
  await regressionPopup.close();

  console.log(`browser smoke passed (syncs=${syncCount}, downloads=${records.length}, ` +
    `${[...syncSeen].map(([id, count]) => `${id}=${count}`).join(" ")})`);
} finally {
  await context?.close();
  await new Promise((resolveClose) => api.server.close(resolveClose));
  if (downloadsDir) await rm(downloadsDir, { recursive: true, force: true });
}

"use strict";

/*
 * Background (event page). Jobs:
 *  1. Proxy API calls from content scripts to the local Whisper server.
 *  2. Own the settings object in browser.storage.local.
 *  3. Sentence mining is the server's (Miner in server.py): it attaches the audio and the picture
 *     to the note Yomitan made, and takes the viewer's own mine through /sync. With no Anki to
 *     write to, the viewer's mine saves the line's audio and a frame to Downloads (saveMine).
 *  4. Start the server for the popup: an extension cannot spawn a process, so the request goes
 *     to the "shisuko" native host (server/native_host.py), which runs the project's launcher.
 *  5. Updates: read the newest release from the server's /health (the server asks GitHub once a
 *     day; the extension makes no request beyond the viewer's machine), tell the viewer (toolbar
 *     badge, one system notification, the popup's banner) and, on request, ask the server to
 *     update itself: POST /update makes it exit so that run.cmd / run.sh run update.py and start
 *     it again. The extension never installs itself; its updates come from addons.mozilla.org
 *     (a Chrome Web Store install, from the store; an unpacked Chrome build, only from the viewer).
 *  6. Word colours: the server reads the Anki deck; the rows it sends with /sync are kept here
 *     and handed to the content script as [word, status, pitch] entries, which colours the words
 *     of every line by them. The extension makes no AnkiConnect call.
 */

const DEFAULT_SETTINGS = SHISUKO_DEFAULT_SETTINGS; // from settings.js

const REQUEST_TIMEOUT_MS = 10000;

// The native host answers at once (it only spawns run.cmd / run.sh and reports), but
// sendNativeMessage has no timeout and no AbortSignal: a host that hangs, or a Python that takes
// its time on a cold disk, would leave the popup on "Starting…" for good. 15 s is well past a
// Python start-up and well short of the popup's own 90 s patience.
const NATIVE_TIMEOUT_MS = 15000;
const NATIVE_HOST = "shisuko";
// A launch is remembered for as long as the popup waits for the server. The popup document dies
// with every click outside it, and a reopened one must not offer a second start while the first
// is still loading its model, which is the whole time /health stays silent (server.py binds the
// port after load_model()): a second run.cmd would load a second model onto the same GPU. 90 s is
// past any healthy start (10-40 s on a GPU after the launcher's venv check, longer on a CPU);
// after it the popup shows the log hint and the button again, since a launch that failed has to
// be retried somehow. The record goes to storage.session as well as memory: Firefox ends an idle
// event page after 30 s, and a model load takes longer than that.
const START_WINDOW_MS = 90000;
const START_KEY = "startServer";
const LAUNCHER_HINT = "Run server\\setup.cmd (Windows) or bash server/setup.sh once, or start the server by hand once: run.cmd / run.sh register the launcher";
const NATIVE_PERMISSION_HINT = "Allow Shisu-ko to talk to its launcher when the browser asks";
const UPDATE_RUNNING_HINT = "The launcher restarts the server itself once update.py is done";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Every message from a content script used to re-read storage; at one sync tick and one Anki poll
// per second per tab that is two cross-process reads a second for a value that changes when the
// viewer touches the popup. The listener below is registered at the top level so the event page
// wakes up for a change it made while suspended.
let settingsCache = null;

// Frozen because every caller now shares one object: a stray write would change what the next
// caller reads, and a throw here is cheaper to find than that.
function cacheSettings(settings) {
  settingsCache = Object.freeze(settings);
  return settingsCache;
}

async function getSettings() {
  if (settingsCache) return settingsCache;
  const stored = await browser.storage.local.get("settings");
  return cacheSettings(Object.assign({}, DEFAULT_SETTINGS, stored.settings || {}));
}

// A save is a read, a merge and a write, and the cache only learns of the write once it is done:
// a second save landing during the first's storage write (the popup's flush and the content
// script's Alt+Shift+S both come through here) would merge its patch onto the settings from
// before the first and write the first patch away. One save at a time, in the order they came.
let saveChain = Promise.resolve();

function saveSettings(patch) {
  const save = saveChain.then(async () => {
    const current = await getSettings();
    const next = Object.assign({}, current, patch || {});
    await browser.storage.local.set({ settings: next });
    return cacheSettings(next);
  });
  // A failed write must not fail every save after it.
  saveChain = save.catch(() => {});
  return save;
}

browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes && changes.settings) settingsCache = null;
});

function normalizeBase(url, fallback) {
  const value = String(url || fallback).trim().replace(/\/+$/, "");
  return /^https?:\/\//i.test(value) ? value : fallback;
}

// ------------------------------------------------------------------ one tab at a time

// The server transcribes one video per session and a second tab would fight the first for GPU,
// bandwidth and disk. So only one tab's /sync reaches it. The right belongs to the tab the viewer
// is watching -- the active tab of the focused window -- and otherwise stays where it is; the
// other tabs are told to stand by and take over the moment they are looked at.
const HOLD_TIMEOUT_MS = 12000; // two missed 5 s heartbeats: the tab left the video, was turned off or died
// How long a focused holder keeps the right without syncing. It has to exceed the content script's
// 5 s idle heartbeat, or a *paused* focused tab would look quiet between two heartbeats and the
// right would flap; it has to stay well under HOLD_TIMEOUT_MS so that a focused tab which really
// stopped syncing -- master switch off, the YouTube home page, a throttled background tab --
// hands the right on in one heartbeat instead of twelve seconds of blank overlays elsewhere. An
// ad is not one of them: the content script syncs through it on the video's own position (or the
// session would time out under a long mid-roll), so the watched tab keeps the right for its length.
const FOCUS_STALE_MS = 7000;

const activeTabs = new Map(); // windowId -> the tab active in it
const syncers = new Map();    // tabId -> { at, paused }: every tab that recently asked to sync
let focusedWindowId = null;
let holder = null;            // the tab allowed to talk to the server, or null

function focusedTabId() {
  if (focusedWindowId === null) return null;
  const id = activeTabs.get(focusedWindowId);
  return id === undefined ? null : id;
}

// Which tab may sync, given the one asking now. Pure: every input is an argument, so the rules can
// be tested without windows, tabs or a clock.
//
// No tab becomes the holder unless it is the one asking; only an existing holder keeps a right it
// already has. Naming a tab that did not ask looks tempting -- the focused tab is the one the
// viewer wants -- but a focused tab that has stopped syncing (the master switch, the YouTube
// home page) would then hold the right in silence and every other tab would stand by
// until its entry aged out. A focused tab that is not the holder needs no help: its own next tick
// takes the right through rule 1, a second later at most.
function electSyncTab(candidate, current, focused, tabs, now) {
  const fresh = (id, within) => {
    const seen = tabs.get(id);
    return !!seen && now - seen.at < within;
  };
  if (focused === candidate) return candidate;                          // 1. the viewer is watching the tab that is asking
  // 2. nobody holds it, the asker already holds it, or the holder has gone quiet. Past this line
  // `current` is a tab id with a fresh entry in `tabs`, which is what makes `held` below defined.
  if (current === null || current === undefined || current === candidate || !fresh(current, HOLD_TIMEOUT_MS)) return candidate;
  if (current === focused && fresh(current, FOCUS_STALE_MS)) return current; // 3. the watched tab keeps it, playing or paused
  const held = tabs.get(current);
  const asking = tabs.get(candidate); // the asker's entry, written just before this call; absent only in a test
  // 4. a playing video beats a holder that is paused, or that has gone quiet since the focus
  // window: a tab whose switch was turned off keeps reporting nothing, and the viewer should not
  // wait out the whole hold timeout for it.
  if (asking && !asking.paused && (held.paused || !fresh(current, FOCUS_STALE_MS))) return candidate;
  return current;                                                       // 5. otherwise the holder keeps it
}

browser.tabs.onActivated.addListener(({ tabId, windowId }) => {
  activeTabs.set(windowId, tabId);
});

// WINDOW_ID_NONE means focus left the browser altogether. The last focused window keeps the
// priority then: switching to another app must not hand the right to some other tab.
//
// Browsers fire onActivated only when the tab selection changes *inside* a window, never when
// focus moves between windows, so a window we have not seen a selection change in has no entry
// here -- after a browser start, after the event page restarted. Ask for its active tab, or
// focusedTabId() answers null forever and the tab the viewer is actually watching stands by.
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) return undefined;
  focusedWindowId = windowId;
  return browser.tabs
    .query({ active: true, windowId })
    .then((tabs) => {
      for (const tab of tabs) {
        if (typeof tab.id === "number") activeTabs.set(windowId, tab.id);
      }
    })
    .catch(() => {});
});

// The event page starts after the windows already exist, so ask once for the current one.
browser.tabs
  .query({ active: true, lastFocusedWindow: true })
  .then((tabs) => {
    for (const tab of tabs) {
      if (typeof tab.id !== "number" || typeof tab.windowId !== "number") continue;
      activeTabs.set(tab.windowId, tab.id);
      // A focus change that fired while this query was out knows better than its answer does.
      if (focusedWindowId === null) focusedWindowId = tab.windowId;
    }
  })
  .catch(() => {});

// ------------------------------------------------------------------ Whisper server proxy

async function apiRequest(path, body) {
  if (typeof path !== "string" || !path.startsWith("/")) {
    return { ok: false, error: "Invalid API path" };
  }
  const settings = await getSettings();
  const base = normalizeBase(settings.serverUrl, DEFAULT_SETTINGS.serverUrl);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const init = { method: body === undefined ? "GET" : "POST", signal: controller.signal, headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(base + path, init);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      return { ok: false, error: "Server returned a non-JSON response" };
    }
    if (!res.ok) {
      return { ok: false, error: (data && data.error) || `HTTP ${res.status}`, data };
    }
    if (path === "/health") {
      forgetStart(); // the popup's poll: the server the launch waited for is up
      await noteHealth(data);
    }
    return { ok: true, data };
  } catch (err) {
    const timedOut = err && err.name === "AbortError";
    if (path === "/health") await noteHealth(null);
    return { ok: false, offline: true, error: timedOut ? "Server timed out" : "Server unreachable" };
  } finally {
    clearTimeout(timer);
  }
}

// ------------------------------------------------------------------ starting the server

// What the browser says when the native host is not there, in a line the viewer can act on.
// Firefox says "No such native application shisuko" whether the host manifest is missing or does
// not list this extension; Chrome splits that into "Specified native messaging host not found."
// and "Access to the specified native messaging host is forbidden." Anything about permission
// means the optional nativeMessaging grant is missing.
function nativeError(err) {
  const text = String((err && err.message) || err || "");
  if (/no such native application|not found|nonexistent|forbidden/i.test(text)) {
    return { ok: false, error: "launcher not registered", hint: LAUNCHER_HINT };
  }
  if (/permission|denied|not available/i.test(text)) {
    return { ok: false, error: "permission missing", hint: NATIVE_PERMISSION_HINT };
  }
  return { ok: false, error: text || "the launcher failed" };
}

let lastStart = null;     // the launch under way: its answer, {ok, started, already, loading, log, deadline}
let startInFlight = null; // the host's pending answer, shared by every request until it lands

function sessionArea() {
  const area = browser.storage && browser.storage.session;
  return area && typeof area.get === "function" && typeof area.set === "function" ? area : null;
}

// The launch under way, or null once its window has passed. Storage outranks memory: the event
// page may have been restarted since the launch, and this page then knows nothing.
async function pendingStart() {
  const area = sessionArea();
  if (area) {
    try {
      const stored = (await area.get(START_KEY))[START_KEY];
      lastStart = stored && typeof stored === "object" ? stored : null;
    } catch (err) {
      /* memory keeps what this event page saw */
    }
  }
  return lastStart && typeof lastStart.deadline === "number" && Date.now() < lastStart.deadline ? lastStart : null;
}

async function rememberStart(answer) {
  lastStart = answer;
  const area = sessionArea();
  if (!area) return;
  try {
    await area.set({ [START_KEY]: answer });
  } catch (err) {
    /* memory keeps it for as long as this event page lives */
  }
}

// A server that answers /health is what the launch was waiting for: the next request must reach
// the host again (it will find the server running, or start it again after it was stopped).
function forgetStart() {
  if (lastStart) rememberStart(null);
}

// Ask the native host to start the server, once: a request while a launch is under way is answered
// from that launch, without asking again, and requests that overlap share one answer. Resolves
// {ok: true, started|already, loading, log, deadline} or {ok: false, error, hint}; it never rejects,
// so the popup always has a line to show. The host's own verdict passes through untouched: it
// knows why run.cmd could not be launched.
function startServer() {
  if (!startInFlight) {
    startInFlight = requestStart().finally(() => {
      startInFlight = null;
    });
  }
  return startInFlight;
}

async function requestStart() {
  const pending = await pendingStart();
  if (pending) return pending;
  // An update is a restart the launcher runs itself: between the old server's exit and the new
  // one's port the host sees neither /health nor the instance lock and would launch run.cmd a
  // second time, two update.py runs on one folder. The popup hides the button for this; a popup
  // that has not heard of the record yet is refused here.
  if (await pendingUpdate()) return { ok: false, error: "an update is under way", hint: UPDATE_RUNNING_HINT };
  // Looked up at call time, not at load: Firefox adds the method once the permission is granted.
  const send = browser.runtime.sendNativeMessage;
  if (typeof send !== "function") return { ok: false, error: "permission missing", hint: NATIVE_PERMISSION_HINT };
  const timedOut = Symbol("timeout");
  let timer = null;
  const noAnswer = new Promise((resolve) => {
    timer = setTimeout(() => resolve(timedOut), NATIVE_TIMEOUT_MS);
  });
  try {
    const answer = await Promise.race([send.call(browser.runtime, NATIVE_HOST, { cmd: "start" }), noAnswer]);
    if (answer === timedOut) return { ok: false, error: "the launcher did not answer" };
    if (!answer || typeof answer !== "object") return { ok: false, error: "the launcher gave no answer" };
    if (!answer.ok) return { ok: false, error: String(answer.error || "the launcher refused") };
    const result = {
      ok: true,
      started: !!answer.started,
      already: !!answer.already,
      // The host's `starting`: a server launched (by an earlier click, or by hand) that holds its
      // instance lock but does not listen yet, its model still loading or downloading. Not an
      // `already` the popup may blame on the server URL, and not `starting`, which is the popup's
      // word for a launch under way (startStatus below).
      loading: !!answer.starting,
      log: typeof answer.log === "string" && answer.log ? answer.log : null,
      deadline: Date.now() + START_WINDOW_MS,
    };
    await rememberStart(result);
    return result;
  } catch (err) {
    return nativeError(err);
  } finally {
    clearTimeout(timer);
  }
}

// The popup's question on opening: is a start under way? With the launch's details, a reopened
// popup resumes watching it, button disabled, where the closed one left off. An update under
// way travels with the answer (`updating`, its record): the popup asks this before its first
// paint, and updateStatus, which also carries the record, comes later, after the first /health,
// late enough for that paint to offer a start on top of the launcher's restart.
async function startStatus() {
  const start = startInFlight ? await startInFlight : await pendingStart();
  const status = !start || !start.ok ? { starting: false } : { starting: true, already: !!start.already, loading: !!start.loading, log: start.log, deadline: start.deadline };
  const updating = await pendingUpdate();
  if (updating) status.updating = updating;
  return status;
}

// ------------------------------------------------------------------ updates

// The newest release is the server's to find: it asks GitHub once a day and reports the answer
// in /health (`latest`, UpdateChecker in server.py). The extension makes no request beyond the
// viewer's machine, so it holds no host permission for GitHub and no token to leak.

// How long an update may take before the popup stops watching for the new server: the old one
// exits, the launcher runs update.py (a git fetch, or a release download and unpack) and the new
// one loads its model again, 10-40 s on a GPU and longer on a CPU. The popup owns the wait; the
// record lives here, in storage.session like the launch record, so a reopened popup resumes it
// and a restarted event page still knows about it.
const UPDATE_WINDOW_MS = 120000;
// The old server closes its port within a second of answering /update, so the old version seen
// this long after the request is the restarted server, whether or not a poll caught it down: the
// popup that asked closes with any click outside it, and nobody polls while it is closed. popup.js
// keeps a copy for its own verdict (addon/tests/popup-copies.test.js keeps the two equal).
const UPDATE_SHUTDOWN_MS = 10000;
// The notification path has no popup polling /health for it; the background polls at this pace
// itself, until the record ends or the window passes.
const UPDATE_POLL_MS = 3000;
// storage.local: the last `latest` a server reported, null for one that reported none. It decides
// while the server is offline, and its presence says a server has answered this profile before.
const LATEST_KEY = "serverLatest";
const UPDATE_KEY = "serverUpdate"; // storage.session: {requestedAt, from, to, deadline, down}
const SNOOZE_KEY = "updateSnoozed"; // storage.session: the version "Not now" was clicked for
const NOTIFIED_KEY = "notifiedVersion"; // storage.session: the version the notification was shown for
const UPDATE_NOTIFICATION = "shisuko-update";
const UPDATED_NOTIFICATION = "shisuko-updated";
const UPDATE_FAILED_NOTIFICATION = "shisuko-update-failed"; // its own id: a click on it must not post again
// The badge is a nudge, not an alarm: the popup's accent, dulled, and never the alert red.
const BADGE_COLOR = "#5b6fb8";

// storage.session with a memory fallback: a browser without the area keeps the value for the
// life of the event page, which is what the launch record does with `lastStart` above.
const sessionMemory = {};

async function sessionGet(key) {
  const area = sessionArea();
  if (area) {
    try {
      const stored = (await area.get(key))[key];
      return stored === undefined ? null : stored;
    } catch (err) {
      /* memory keeps what this event page saw */
    }
  }
  return Object.hasOwn(sessionMemory, key) ? sessionMemory[key] : null;
}

async function sessionSet(key, value) {
  sessionMemory[key] = value;
  const area = sessionArea();
  if (!area) return;
  try {
    await area.set({ [key]: value });
  } catch (err) {
    /* memory keeps it for as long as this event page lives */
  }
}

// "0.9.0" or "v0.9.0" as three numbers; a missing or unreadable part counts as 0, so a tag with
// a suffix still compares by what is in front of it and garbage sorts below every release.
function parseVersion(text) {
  const parts = String(text || "").trim().replace(/^v/i, "").split(".");
  return [0, 1, 2].map((i) => {
    const n = parseInt(parts[i], 10);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  });
}

function compareVersions(a, b) {
  const va = parseVersion(a);
  const vb = parseVersion(b);
  for (let i = 0; i < 3; i++) {
    if (va[i] !== vb[i]) return va[i] < vb[i] ? -1 : 1;
  }
  return 0;
}

// The server's update check out of a /health answer: {latest, checkedAt, error}, or null when it
// reports none (--update-check off, no check run yet, a server from before 0.24.0). `latest` is
// the release ({version, url, xpi, published}) or null when none is published. Only https pages
// are kept: the popup opens the release page in a tab, and a page from an answer is still a page.
function checkFromHealth(health) {
  const raw = health && typeof health === "object" ? health.latest : null;
  if (!raw || typeof raw !== "object") return null;
  const text = (value) => (typeof value === "string" && value ? value : null);
  const https = (value) => (typeof value === "string" && /^https:\/\//i.test(value) ? value : null);
  const version = text(raw.version);
  return {
    latest: version ? { version, url: https(raw.url), xpi: https(raw.xpi), published: text(raw.published) } : null,
    checkedAt: Number.isFinite(raw.checked) ? raw.checked : null,
    error: text(raw.error),
  };
}

// What the newest release means for the two halves. The server can only be updated through
// /update when its launcher runs update.py after the exit (`launcher` in /health); one started
// by Docker, Nix or by hand says false. A server from before the flag (every 0.8.0, the first
// this extension meets) says nothing: when its version is behind it is "behind", a badge and a
// banner that name the release but make no claim about run.cmd, since the flag would be the
// only ground for one. A server without even a version, the smoke fixture, is "unknown": no
// banner and no guess. `serverOnline` tells that apart from no server at all.
function decideUpdate(input) {
  const { latest, serverVersion, serverLauncher, extensionVersion: extension, serverOnline } = input || {};
  const newest = latest && typeof latest.version === "string" && latest.version ? latest.version : null;
  const decision = { server: "unknown", extension: "current" };
  if (!newest) return decision;
  if (typeof extension === "string" && extension && compareVersions(newest, extension) > 0) decision.extension = "newer";
  if (typeof serverVersion === "string" && serverVersion) {
    if (compareVersions(newest, serverVersion) <= 0) decision.server = "current";
    else if (serverLauncher === true) decision.server = "newer";
    else if (serverLauncher === false) decision.server = "cannot";
    else decision.server = "behind";
  } else {
    decision.server = serverOnline ? "unknown" : "offline";
  }
  return decision;
}

// What /health says about the server for the update question: null without a server, and a
// null field for whatever an older server does not report.
function serverInfo(health) {
  if (!health || typeof health !== "object") return null;
  return {
    version: typeof health.version === "string" && health.version ? health.version : null,
    launcher: typeof health.launcher === "boolean" ? health.launcher : null,
  };
}

function extensionVersion() {
  try {
    const manifest = browser.runtime.getManifest();
    return manifest && typeof manifest.version === "string" ? manifest.version : null;
  } catch (err) {
    return null;
  }
}

// The stored check: undefined when no server has answered this profile, else what the last one
// reported (null for none).
async function storedCheck() {
  try {
    const stored = await browser.storage.local.get(LATEST_KEY);
    if (!Object.hasOwn(stored, LATEST_KEY)) return undefined;
    return stored[LATEST_KEY] && typeof stored[LATEST_KEY] === "object" ? stored[LATEST_KEY] : null;
  } catch (err) {
    return undefined;
  }
}

let rememberedCheck; // the JSON last written, so that a popup polling /health writes nothing new

async function rememberCheck(check) {
  const text = JSON.stringify(check);
  if (text === rememberedCheck) return;
  rememberedCheck = text;
  try {
    await browser.storage.local.set({ [LATEST_KEY]: check });
  } catch (err) {
    rememberedCheck = undefined; /* the next answer tries again */
  }
}

// The check that decides: the server's when it answered, else the last one a server reported.
async function currentCheck(health) {
  return health ? checkFromHealth(health) : (await storedCheck()) || null;
}

function decisionFor(health, check) {
  const server = serverInfo(health);
  return decideUpdate({
    latest: check && check.latest,
    serverVersion: server ? server.version : null,
    serverLauncher: server ? server.launcher : null,
    extensionVersion: extensionVersion(),
    serverOnline: !!server,
  });
}

let badgeOn = null; // what the badge shows, null until this event page set it once

// The toolbar badge and the notifications are nudges the popup does not need, so a browser (or
// the test sandbox) without the API loses nothing; the guards keep every path from throwing.
async function setBadge(on) {
  const api = browser.action;
  if (!api || typeof api.setBadgeText !== "function" || on === badgeOn) return;
  badgeOn = on;
  try {
    if (on && typeof api.setBadgeBackgroundColor === "function") await api.setBadgeBackgroundColor({ color: BADGE_COLOR });
    await api.setBadgeText({ text: on ? "1" : "" });
  } catch (err) {
    /* no badge on this browser */
  }
}

// The badge follows the versions alone: a release newer than the server, whether or not the
// server can do anything about it, or newer than the extension.
function applyBadge(decision) {
  return setBadge(["newer", "cannot", "behind"].includes(decision.server) || decision.extension === "newer");
}

async function notify(id, title, message) {
  const api = browser.notifications;
  if (!api || typeof api.create !== "function") return false;
  try {
    await api.create(id, { type: "basic", iconUrl: browser.runtime.getURL("icons/icon-128.png"), title, message });
    return true;
  } catch (err) {
    console.debug("Shisu-ko: notification failed:", err);
    return false;
  }
}

async function clearNotification(id) {
  const api = browser.notifications;
  if (!api || typeof api.clear !== "function") return;
  try {
    await api.clear(id);
  } catch (err) {
    /* already gone */
  }
}

// One notification per release and browser session: the version it was shown for sits in
// storage.session, which the browser clears when it closes, so the next session asks once more.
async function notifyNewer(latest, serverVersion) {
  if ((await sessionGet(NOTIFIED_KEY)) === latest) return false;
  await sessionSet(NOTIFIED_KEY, latest);
  return notify(UPDATE_NOTIFICATION, `Shisu-ko ${latest} is available`, `The server runs ${serverVersion}. Click to update it now.`);
}

let lastUpdate; // the update under way; undefined until storage.session has been read once

// The update under way, or null once its window has passed. The background is the only writer,
// so storage is read once per event page and memory serves the popup's poll from there.
async function pendingUpdate() {
  if (lastUpdate === undefined) {
    const stored = await sessionGet(UPDATE_KEY);
    lastUpdate = stored && typeof stored === "object" ? stored : null;
  }
  return lastUpdate && typeof lastUpdate.deadline === "number" && Date.now() < lastUpdate.deadline ? lastUpdate : null;
}

async function rememberUpdate(record) {
  lastUpdate = record;
  await sessionSet(UPDATE_KEY, record);
}

// Every /health answer passes here. The server's check is kept for when it is offline, the badge
// follows it, and an update this background requested is followed: seeing the server down tells
// a restart apart from the old server still answering (it closes its port within a second of the
// answer), and so does the time since the request (UPDATE_SHUTDOWN_MS) when no poll ran while it
// was down; the new version ends the record, and the old version after a restart means update.py
// could not update, so the banner is back and the next request is posted rather than answered
// from a record of a restart that is over.
async function noteHealth(data) {
  try {
    if (data) await rememberCheck(checkFromHealth(data));
    await applyBadge(decisionFor(data, await currentCheck(data)));
    const record = await pendingUpdate();
    if (!record) return;
    if (!data) {
      if (!record.down) await rememberUpdate(Object.assign({}, record, { down: true }));
      return;
    }
    const server = serverInfo(data);
    if (!server || !server.version) return;
    const updated = record.to ? compareVersions(server.version, record.to) >= 0 : server.version !== record.from;
    if (updated) {
      await rememberUpdate(null);
      await notify(UPDATED_NOTIFICATION, `Shisu-ko updated to ${server.version}`, "The server restarted with the new version.");
    } else if (record.down || Date.now() - record.requestedAt >= UPDATE_SHUTDOWN_MS) {
      await rememberUpdate(null);
    }
  } catch (err) {
    console.debug("Shisu-ko: could not follow the update:", err);
  }
}

// The popup's question: what is newest, what the server runs, and what follows from the two.
// The popup hands over the /health answer it already has (`health`, null while offline) so the
// server is not asked twice; a caller without one gets a /health call here.
async function updateStatus(msg) {
  let health;
  if (msg && Object.hasOwn(msg, "health")) {
    health = msg.health && typeof msg.health === "object" ? msg.health : null;
  } else {
    const res = await apiRequest("/health");
    health = res.ok && res.data && typeof res.data === "object" ? res.data : null;
  }
  const check = (await currentCheck(health)) || { latest: null, checkedAt: null, error: null };
  const decision = decisionFor(health, check);
  await applyBadge(decision);
  // A server seen at the release no longer needs the update the notification offers; a viewer
  // who restarted run.cmd by hand would otherwise find the offer still up.
  if (decision.server === "current") await clearNotification(UPDATE_NOTIFICATION);
  const snoozed = await sessionGet(SNOOZE_KEY);
  return {
    latest: check.latest || null,
    checkedAt: typeof check.checkedAt === "number" ? check.checkedAt : null,
    error: typeof check.error === "string" && check.error ? check.error : null,
    server: serverInfo(health),
    decision,
    snoozed: typeof snoozed === "string" ? snoozed : null,
    updating: await pendingUpdate(),
    extensionVersion: extensionVersion(),
  };
}

async function snoozeUpdate(version) {
  if (typeof version !== "string" || !version) return { ok: false, error: "No version to snooze" };
  await sessionSet(SNOOZE_KEY, version);
  return { ok: true };
}

let updateInFlight = null; // the request under way, shared by the popup and the notification

// Ask the server to update itself, once: overlapping requests share one answer, and a request
// while an update is under way is answered from its record. Resolves {ok: true, restarting: true,
// from, to, requestedAt, deadline} or {ok: false, error, refused, offline}; it never rejects.
// `refused` marks the server's own verdict (409: not started by the launcher, or --no-update).
function updateServer(opts) {
  if (!updateInFlight) {
    updateInFlight = requestUpdate().finally(() => {
      updateInFlight = null;
    });
    if (opts && opts.watch) {
      updateInFlight.then((res) => {
        if (res.ok && !res.already) return watchUpdate();
        return undefined;
      }).catch(() => {});
    }
  }
  return updateInFlight;
}

async function requestUpdate() {
  // /health first: its answer passes through noteHealth(), which ends a record whose restart is
  // over (the old version back, or the new one up), so the pending check below sees the truth.
  const healthRes = await apiRequest("/health");
  const pending = await pendingUpdate();
  if (pending) return { ok: true, restarting: true, already: true, from: pending.from, to: pending.to, requestedAt: pending.requestedAt, deadline: pending.deadline };
  const health = healthRes.ok ? healthRes.data : null;
  const check = await currentCheck(health);
  const to = check && check.latest ? check.latest.version : null;
  const server = serverInfo(health);
  const from = server ? server.version : null;
  // The notification can be clicked long after the server was updated another way: nothing to
  // post, and nothing failed either, which `upToDate` tells the click apart from a refusal.
  if (from && to && compareVersions(from, to) >= 0) return { ok: false, upToDate: true, error: `the server already runs ${from}` };
  const res = await apiRequest("/update", {});
  if (!res.ok) {
    return { ok: false, error: res.error || "the server refused", refused: !res.offline && !!(res.data && res.data.error), offline: !!res.offline };
  }
  if (!res.data || res.data.restarting !== true) return { ok: false, error: "the server gave no answer" };
  const requestedAt = Date.now();
  const record = { requestedAt, from, to, deadline: requestedAt + UPDATE_WINDOW_MS, down: false };
  await rememberUpdate(record);
  await clearNotification(UPDATE_NOTIFICATION);
  return { ok: true, restarting: true, from, to, requestedAt, deadline: record.deadline };
}

// Poll /health for an update nobody watches from a popup; noteHealth() ends the record. Best
// effort: the browser may end an idle event page first, and the next popup catches up.
async function watchUpdate() {
  for (let i = 0; i < UPDATE_WINDOW_MS / UPDATE_POLL_MS; i++) {
    await sleep(UPDATE_POLL_MS);
    if (!(await pendingUpdate())) return;
    await apiRequest("/health");
  }
}

// The browser starting, or the extension installed or updated: one /health to the server, whose
// answer sets the badge; offline, the last check a server reported decides alone. When the
// server could update itself right now, one notification says so. A profile no server has
// answered yet asks nothing: its first /health is the popup's, which every viewer opens to set
// the server up. The extension's own update comes from its store, so a profile whose server is
// never started still learns of it there.
async function startupCheck() {
  try {
    if ((await storedCheck()) === undefined) return;
    const status = await updateStatus({});
    if (status.decision.server === "newer" && status.latest && status.server) {
      await notifyNewer(status.latest.version, status.server.version);
    }
  } catch (err) {
    console.debug("Shisu-ko: update check at start failed:", err);
  }
}

for (const event of [browser.runtime.onStartup, browser.runtime.onInstalled]) {
  if (event && typeof event.addListener === "function") event.addListener(() => startupCheck());
}

// The extension once asked GitHub itself and kept the answer under this key (0.23.0 and before).
if (browser.runtime.onInstalled && typeof browser.runtime.onInstalled.addListener === "function") {
  browser.runtime.onInstalled.addListener(() => browser.storage.local.remove("updateCheck").catch(() => {}));
}

if (browser.notifications && browser.notifications.onClicked && typeof browser.notifications.onClicked.addListener === "function") {
  browser.notifications.onClicked.addListener((id) => {
    if (id !== UPDATE_NOTIFICATION) return undefined;
    clearNotification(id);
    // The click asked for something and has no popup to answer in, so a request that did not
    // get through says so here: the notification would otherwise just vanish as if it had
    // worked. A server already at the release (updated another way while the notification sat
    // there) is no failure: the stale notification is gone with the clear above, and that is
    // all there was to do.
    return updateServer({ watch: true })
      .then((res) => (res.ok || res.upToDate ? undefined : notify(UPDATE_FAILED_NOTIFICATION, "Shisu-ko could not update the server", String(res.error || "the server gave no answer"))))
      .catch(() => {});
  });
}

// ------------------------------------------------------------------ the no-Anki fallback

// The viewer's mine with no Anki to write to (the server answered `unreachable`, or mines
// nothing: `off`) saves the line instead, as 0.21 did: its audio from the server's /clip and the
// frame the tab drew when the viewer mined, in Downloads/shisu-ko-mining.
const SAVE_FOLDER = "shisu-ko-mining";
const CLIP_TIMEOUT_MS = 20000;
// Long past any local download: an object URL revoked sooner could cut the file short.
const OBJECT_URL_TTL_MS = 120000;
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{6,20}$/;
const FRAME_PREFIX = "data:image/jpeg;base64,";

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

// Firefox refuses data: URLs in downloads.download (bug 1622986) and takes an object URL made
// here, in the background page that owns it. Chrome's service worker has no
// URL.createObjectURL, and takes the data: URL.
async function downloadBytes(bytes, mime, filename) {
  const options = { filename, conflictAction: "uniquify", saveAs: false };
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return browser.downloads.download({ ...options, url: `data:${mime};base64,${bytesToBase64(bytes)}` });
  }
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  setTimeout(() => URL.revokeObjectURL(url), OBJECT_URL_TTL_MS);
  return browser.downloads.download({ ...options, url });
}

// The line's clip as {bytes, mime, ext}, or {error}.
async function fetchClip(settings, videoId, start, end) {
  const base = normalizeBase(settings.serverUrl, DEFAULT_SETTINGS.serverUrl);
  const format = settings.clipFormat === "wav" ? "wav" : "mp3";
  const path = `/clip?video_id=${videoId}&start=${start.toFixed(3)}&end=${end.toFixed(3)}&format=${format}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CLIP_TIMEOUT_MS);
  try {
    const res = await fetch(base + path, { signal: controller.signal });
    if (!res.ok) return { error: `the server answered HTTP ${res.status}` };
    const mime = (res.headers.get("Content-Type") || "audio/mpeg").split(";")[0].trim();
    return { bytes: new Uint8Array(await res.arrayBuffer()), mime, ext: mime === "audio/wav" ? "wav" : "mp3" };
  } catch (err) {
    return { error: err && err.name === "AbortError" ? "the server timed out" : "the server is unreachable" };
  } finally {
    clearTimeout(timer);
  }
}

// {ok, message} or {ok: false, error}; never rejects. `frame` is the tab's JPEG data: URL, or
// null where the video would not be drawn (DRM taints the canvas).
async function saveMine(msg) {
  const cue = msg && msg.cue;
  if (!cue || !Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.end <= cue.start) return { ok: false, error: "No line to save" };
  if (typeof msg.videoId !== "string" || !VIDEO_ID_RE.test(msg.videoId)) return { ok: false, error: "No video to save from" };
  const settings = await getSettings();
  const pad = Math.max(0, Math.min(2000, Number(settings.clipPaddingMs) || 0)) / 1000;
  const name = `${SAVE_FOLDER}/shisuko_${msg.videoId}_${Math.round(cue.start * 1000)}`;
  const saved = [];
  const missing = [];
  try {
    if (typeof msg.frame === "string" && msg.frame.startsWith(FRAME_PREFIX)) {
      const binary = atob(msg.frame.slice(FRAME_PREFIX.length));
      const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
      await downloadBytes(bytes, "image/jpeg", `${name}.jpg`);
      saved.push("the picture");
    } else {
      missing.push("no picture (the video cannot be drawn)");
    }
    const clip = await fetchClip(settings, msg.videoId, Math.max(0, cue.start - pad), cue.end + pad);
    if (clip.bytes) {
      await downloadBytes(clip.bytes, clip.mime, `${name}.${clip.ext}`);
      saved.push("the audio");
    } else {
      missing.push(`no audio (${clip.error})`);
    }
  } catch (err) {
    return { ok: false, error: "Saving failed: " + String((err && err.message) || err) };
  }
  if (!saved.length) return { ok: false, error: "Nothing saved: " + missing.join("; ") };
  const note = missing.length ? ` (${missing.join("; ")})` : "";
  return { ok: true, message: `No Anki to mine into: saved ${saved.join(" and ")} to Downloads/${SAVE_FOLDER}${note}` };
}

// ------------------------------------------------------------------ word colours

// The server reads the viewer's Anki deck (DeckIndexes in server.py); the extension makes no
// AnkiConnect call. The tab that holds the server asks for the deck in its /sync (`deck`: the
// popup's deck and fields, and the token of the rows held here), and the answer carries `deck`
// only when the token moved: every row the first time, the rows that changed after that. The rows
// live here, one copy for every tab: each content script asks for them as buildIndex() entries
// (cardStatus), a tab standing by for another included.
const SERVER_TOO_OLD_TEXT = "The word colours need Shisu-ko server 0.23.0 or newer";

// The deck of the last mined card, which 0.22.0 and earlier kept here; the server keeps it now.
// It goes to the server as `seen` in each deck request until a server that reads Anki has
// answered one (the server takes it only while it knows no deck of its own), and is then removed.
const LEGACY_DECK_KEY = "ankiDeckSeen";
let legacyDeck = null; // the read of that record, once: a promise of its deck name or null

function legacyDeckName() {
  legacyDeck = legacyDeck || browser.storage.local
    .get(LEGACY_DECK_KEY)
    .then((stored) => {
      const deck = stored && stored[LEGACY_DECK_KEY] && stored[LEGACY_DECK_KEY].deck;
      return typeof deck === "string" && deck.trim() ? deck.trim() : null;
    })
    .catch(() => null);
  return legacyDeck;
}

function handedOver() {
  legacyDeck = Promise.resolve(null);
  browser.storage.local.remove(LEGACY_DECK_KEY).catch(() => {});
}

// key: the deck and fields the rows are about; rows: note id -> [id, word, reading, status, pitch];
// at: stamps `entries`, moved only when they change, so a tab holding them hears "unchanged".
let deckState = freshDeck("");

function freshDeck(key) {
  return { key, token: null, status: null, error: null, deck: null, automatic: false, rows: new Map(), entries: [], at: 0 };
}

function wordColoursOn(settings) {
  return !!settings.enabled && (!!settings.cardStatus || !!settings.pitchAccent);
}

const trimmed = (settings, key) => String((settings && settings[key]) || "").trim();

// The `deck` of the next /sync. Another deck or another field to read a word or its pitch from is
// another index: what is held goes, and the server sends every row of the new one.
function deckRequest(settings) {
  const name = trimmed(settings, "cardStatusDeck");
  const word = trimmed(settings, "ankiWordField");
  const pitch = trimmed(settings, "ankiPitchField");
  const key = JSON.stringify([name, word, pitch]);
  if (deckState.key !== key) deckState = freshDeck(key);
  return { name, word, pitch, since: deckState.token };
}

// The entries buildIndex() takes, by note id: the word, then the kana reading of a word usually
// written in kana (更に is subtitled さらに), each with the note's status and pitch. Pure.
function deckEntries(rows) {
  const entries = [];
  for (const [, word, reading, status, pitch] of [...rows].sort((a, b) => a[0] - b[0])) {
    entries.push([word, status, pitch]);
    if (reading) entries.push([reading, status, pitch]);
  }
  return entries;
}

const validRow = (row) => Array.isArray(row) && row.length === 5 && Number.isSafeInteger(row[0]) && typeof row[1] === "string" && row[1];

// The server's `deck` answer. A delta is applied to the rows held for the token it answered.
function takeDeck(answer) {
  if (!answer || typeof answer !== "object" || typeof answer.token !== "string") return;
  const state = deckState;
  state.token = answer.token;
  state.status = typeof answer.status === "string" ? answer.status : "error";
  state.error = typeof answer.error === "string" ? answer.error : null;
  state.automatic = !!answer.automatic;
  state.deck = typeof answer.deck === "string" ? answer.deck : null;
  if (state.status !== "ok") {
    // The server holds no rows for this deck (none read yet, or Anki away from the start).
    state.rows = new Map();
  } else {
    if (answer.full) state.rows = new Map();
    for (const row of Array.isArray(answer.notes) ? answer.notes : []) if (validRow(row)) state.rows.set(row[0], row);
    for (const id of Array.isArray(answer.removed) ? answer.removed : []) state.rows.delete(id);
  }
  const entries = deckEntries(state.rows.values());
  if (JSON.stringify(entries) !== JSON.stringify(state.entries)) {
    state.entries = entries;
    state.at = Date.now();
  }
}

// A /sync from the tab that holds the server: the deck asked for when a colour is on, and the
// answer's `deck` taken here rather than handed to the tab.
async function syncRequest(body) {
  const settings = await getSettings();
  const deck = wordColoursOn(settings) ? deckRequest(settings) : null;
  const seen = deck ? await legacyDeckName() : null;
  if (seen) deck.seen = seen;
  const asked = deckState;
  const res = await apiRequest("/sync", deck && body && typeof body === "object" ? { ...body, deck } : body);
  const data = res && res.ok && res.data && typeof res.data === "object" ? res.data : null;
  // "invalid" refused the request before reading `seen`, and "off" reads no Anki to keep it for.
  const answer = data && data.deck;
  if (seen && answer && typeof answer === "object" && !["invalid", "off"].includes(answer.token)) handedOver();
  if (deck && data && deckState === asked) {
    // A server that knows `deck` always answers one held nothing for: none at all is a server
    // from before the word colours moved there.
    if ("deck" in data) takeDeck(data.deck);
    else if (asked.token === null) Object.assign(asked, { status: "old", error: SERVER_TOO_OLD_TEXT });
  }
  if (data) delete data.deck;
  return res;
}

// The content script's ask: the deck's entries, or why there are none. `since` is the `at` of the
// entries it holds; the same entries again are answered without them.
async function cardStatus(msg) {
  const settings = await getSettings();
  if (!wordColoursOn(settings)) return { ok: false, reason: "off" };
  deckRequest(settings);
  const state = deckState;
  if (state.status === "ok") {
    const since = msg && typeof msg.since === "number" ? msg.since : null;
    if (since === state.at) return { ok: true, unchanged: true, at: state.at, deck: state.deck };
    return { ok: true, deck: state.deck, automatic: state.automatic, at: state.at, entries: state.entries };
  }
  if (!state.status || state.status === "loading") return { ok: false, reason: "pending", error: "Waiting for the server to read the deck" };
  return { ok: false, reason: state.status === "off" ? "unavailable" : state.status, error: state.error };
}

// The popup's deck list: Anki's deck names and the automatic deck, from the server.
async function ankiDecks() {
  const res = await apiRequest("/health?decks=1");
  const info = res.ok && res.data && typeof res.data.anki_decks === "object" ? res.data.anki_decks : null;
  if (!res.ok) return { ok: false, reason: "offline", error: "The Shisu-ko server is not running: it reads the decks" };
  if (!info) return { ok: false, reason: "error", error: SERVER_TOO_OLD_TEXT };
  // Until the first deck request hands it over, the server may not know the deck 0.22.0 remembered.
  const seen = typeof info.seen === "string" ? info.seen : await legacyDeckName();
  if (!info.ok) return { ok: false, reason: info.offline ? "offline" : "error", error: String(info.error || "unknown error"), seen };
  const decks = (Array.isArray(info.decks) ? info.decks : []).map(String).sort((a, b) => a.localeCompare(b));
  return { ok: true, decks, seen };
}

// ------------------------------------------------------------------ messaging

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || typeof msg !== "object") return undefined;
  // The asking tab, for the sync election; the popup (no tab) gets -1.
  const tabId = sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : -1;
  switch (msg.type) {
    case "api": {
      if (msg.path === "/sync" && tabId >= 0) {
        const now = Date.now();
        syncers.set(tabId, { at: now, paused: !!(msg.body && msg.body.paused) });
        holder = electSyncTab(tabId, holder, focusedTabId(), syncers, now);
        // Standing by is not an error: the tab keeps its overlay and its status line, and asks
        // again on the next tick, which is what makes taking over immediate.
        if (holder !== tabId) return Promise.resolve({ ok: true, data: { status: "standby" } });
      }
      return msg.path === "/sync" ? syncRequest(msg.body) : apiRequest(msg.path, msg.body);
    }
    case "getSettings":
      return getSettings();
    case "saveSettings":
      return saveSettings(msg.settings);
    case "startServer":
      return startServer();
    case "startServerStatus":
      return startStatus();
    case "updateStatus":
      return updateStatus(msg);
    case "updateServer":
      return updateServer();
    case "saveMine":
      return saveMine(msg);
    case "snoozeUpdate":
      return snoozeUpdate(msg.version);
    case "cardStatus":
      return cardStatus(msg);
    case "ankiDecks":
      return ankiDecks();
    default:
      return undefined;
  }
});

// A closed tab can never keep the right to sync.
browser.tabs.onRemoved.addListener((tabId) => {
  syncers.delete(tabId);
  for (const [windowId, id] of activeTabs) if (id === tabId) activeTabs.delete(windowId);
  if (holder === tabId) holder = null;
});

browser.commands.onCommand.addListener(async (name) => {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  for (const tab of tabs) {
    if (tab.id === undefined) continue;
    browser.tabs.sendMessage(tab.id, { type: "command", name }).catch(() => {});
  }
});

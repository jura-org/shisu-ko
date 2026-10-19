"use strict";

/*
 * Background (event page). Jobs:
 *  1. Proxy API calls from content scripts to the local Whisper server.
 *  2. Own the settings object in browser.storage.local.
 *  3. Sentence mining is the server's (Miner in server.py): it attaches the audio and the picture
 *     to the note Yomitan made, and takes the viewer's own mine through /sync. The background
 *     only remembers the deck a mined note went to, for the word colours (minedNote).
 *  4. Start the server for the popup: an extension cannot spawn a process, so the request goes
 *     to the "shisuko" native host (server/native_host.py), which runs the project's launcher.
 *  5. Updates: ask GitHub for the newest release once a day, tell the viewer (toolbar badge, one
 *     system notification, the popup's banner) and, on request, ask the server to update itself:
 *     POST /update makes it exit so that run.cmd / run.sh run update.py and start it again. The
 *     extension never installs itself; its updates come from addons.mozilla.org (a Chrome Web
 *     Store install, from the store; an unpacked Chrome build, only from the viewer).
 *  6. Word colours: turn one Anki deck's notes into [word, status, pitch] entries for the
 *     content script, which colours the words of every line by them.
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

// How long a permission verdict holds: a viewer who clicked No in Anki's dialog is asked again
// after a minute.
const ANKI_PERMISSION_RECHECK_MS = 60000;

const ankiWatch = { permission: null, permissionAskAt: 0, permissionPending: null };

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
// paint, and updateStatus, which also carries the record, may first wait ten seconds on GitHub,
// long enough for that paint to offer a start on top of the launcher's restart.
async function startStatus() {
  const start = startInFlight ? await startInFlight : await pendingStart();
  const status = !start || !start.ok ? { starting: false } : { starting: true, already: !!start.already, loading: !!start.loading, log: start.log, deadline: start.deadline };
  const updating = await pendingUpdate();
  if (updating) status.updating = updating;
  return status;
}

// ------------------------------------------------------------------ updates

// The newest release comes from GitHub's REST API, which answers cross-origin requests with
// Access-Control-Allow-Origin: *, so the manifest needs no host permission for it.
const GITHUB_LATEST_URL = "https://api.github.com/repos/Multysquid/shisu-ko/releases/latest";
// One check a day: releases are weeks apart, and GitHub allows sixty unauthenticated requests an
// hour per address. A check that failed does not count as one; it is tried again the next time
// the popup opens or the browser starts, which costs one quick failure while offline.
const UPDATE_CHECK_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const UPDATE_CHECK_TIMEOUT_MS = 10000;
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
const UPDATE_CHECK_KEY = "updateCheck"; // storage.local: {checkedAt, latest, error}
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

// The release GitHub's answer describes, or null when it names none. Only https URLs are kept:
// the popup opens the release page in a tab, and a page from an API answer is still a page.
function releaseFromApi(json) {
  if (!json || typeof json !== "object" || typeof json.tag_name !== "string" || !json.tag_name.trim()) return null;
  const tag = json.tag_name.trim();
  const https = (value) => (typeof value === "string" && /^https:\/\//i.test(value) ? value : null);
  const assets = Array.isArray(json.assets) ? json.assets : [];
  // The signed .xpi: a release AMO has not signed yet carries the unsigned build as
  // shisu-ko-<version>-firefox-unsigned.xpi, which regular Firefox refuses to install.
  const xpi = assets.find((asset) => {
    const name = asset && typeof asset.name === "string" ? asset.name.toLowerCase() : "";
    return name.endsWith(".xpi") && !name.endsWith("-unsigned.xpi");
  });
  return { version: tag.replace(/^v/i, ""), tag, url: https(json.html_url), xpi: xpi ? https(xpi.browser_download_url) : null };
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

async function readUpdateCheck() {
  try {
    const stored = (await browser.storage.local.get(UPDATE_CHECK_KEY))[UPDATE_CHECK_KEY];
    return stored && typeof stored === "object" ? stored : null;
  } catch (err) {
    return null;
  }
}

function checkIsFresh(check) {
  return !!check && !check.error && typeof check.checkedAt === "number" && Date.now() - check.checkedAt < UPDATE_CHECK_MAX_AGE_MS;
}

// {latest} or {error}, never a throw: a failed check is a line under the popup's button and a
// console.debug, never a notification. Nobody asked, and being offline is not news.
async function fetchLatestRelease() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPDATE_CHECK_TIMEOUT_MS);
  try {
    const res = await fetch(GITHUB_LATEST_URL, { headers: { Accept: "application/vnd.github+json" }, signal: controller.signal });
    if (res.status === 404) return { latest: null }; // no release published yet
    if (res.status === 403 || res.status === 429) return { error: "GitHub's rate limit is reached, try again in an hour" };
    if (!res.ok) return { error: `GitHub answered HTTP ${res.status}` };
    let json;
    try {
      json = await res.json();
    } catch (err) {
      return { error: "GitHub returned a non-JSON response" };
    }
    const latest = releaseFromApi(json);
    return latest ? { latest } : { error: "GitHub's answer names no release" };
  } catch (err) {
    return { error: err && err.name === "AbortError" ? "GitHub did not answer in time" : "could not reach GitHub" };
  } finally {
    clearTimeout(timer);
  }
}

let checkInFlight = null; // the check under way, shared by the popup and the start-up listener

// The stored check when it is fresh, else a new one: {checkedAt, latest, error}. A failure keeps
// the last release seen, so the banner still knows what is out there.
function checkForUpdate(opts) {
  if (!checkInFlight) {
    checkInFlight = runCheck(!!(opts && opts.force)).finally(() => {
      checkInFlight = null;
    });
  }
  return checkInFlight;
}

async function runCheck(force) {
  const cached = await readUpdateCheck();
  if (!force && checkIsFresh(cached)) return cached;
  const check = { checkedAt: Date.now(), latest: cached && cached.latest ? cached.latest : null, error: null };
  const verdict = await fetchLatestRelease();
  if (verdict.error) {
    check.error = verdict.error;
    console.debug("Shisu-ko: update check failed:", verdict.error);
  } else {
    check.latest = verdict.latest;
  }
  try {
    await browser.storage.local.set({ [UPDATE_CHECK_KEY]: check });
  } catch (err) {
    /* the answer still goes out; the next opening checks again */
  }
  return check;
}

// The toolbar badge and the notifications are nudges the popup does not need, so a browser (or
// the test sandbox) without the API loses nothing; the guards keep every path from throwing.
async function setBadge(on) {
  const api = browser.action;
  if (!api || typeof api.setBadgeText !== "function") return;
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

// Every /health answer passes here: the popup's poll is how the background follows an update it
// requested. Seeing the server down tells a restart apart from the old server still answering
// (it closes its port within a second of the answer), and so does the time since the request
// (UPDATE_SHUTDOWN_MS) when no poll ran while it was down; the new version ends the record, and
// the old version after a restart means update.py could not update, so the banner is back and
// the next request is posted rather than answered from a record of a restart that is over.
async function noteHealth(data) {
  try {
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
      const check = await readUpdateCheck();
      await applyBadge(decideUpdate({ latest: check && check.latest, serverVersion: server.version, serverLauncher: server.launcher, extensionVersion: extensionVersion(), serverOnline: true }));
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
// server is not asked twice; a caller without one gets a /health call here. `check` asks for a
// check when the stored one is a day old (the popup opening); otherwise the store is read.
async function updateStatus(msg) {
  const check = (msg && msg.check ? await checkForUpdate() : await readUpdateCheck()) || { checkedAt: null, latest: null, error: null };
  let health;
  if (msg && Object.hasOwn(msg, "health")) {
    health = msg.health && typeof msg.health === "object" ? msg.health : null;
  } else {
    const res = await apiRequest("/health");
    health = res.ok && res.data && typeof res.data === "object" ? res.data : null;
  }
  const server = serverInfo(health);
  const extension = extensionVersion();
  const decision = decideUpdate({
    latest: check.latest,
    serverVersion: server ? server.version : null,
    serverLauncher: server ? server.launcher : null,
    extensionVersion: extension,
    serverOnline: !!server,
  });
  await applyBadge(decision);
  // A server seen at the release no longer needs the update the notification offers; a viewer
  // who restarted run.cmd by hand would otherwise find the offer still up.
  if (decision.server === "current") await clearNotification(UPDATE_NOTIFICATION);
  const snoozed = await sessionGet(SNOOZE_KEY);
  return {
    latest: check.latest || null,
    checkedAt: typeof check.checkedAt === "number" ? check.checkedAt : null,
    error: typeof check.error === "string" && check.error ? check.error : null,
    server,
    decision,
    snoozed: typeof snoozed === "string" ? snoozed : null,
    updating: await pendingUpdate(),
    extensionVersion: extension,
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
  const check = await readUpdateCheck();
  const to = check && check.latest && typeof check.latest.version === "string" ? check.latest.version : null;
  const server = serverInfo(healthRes.ok ? healthRes.data : null);
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

// The browser starting, or the extension installed or updated: check (from the store when it is
// fresh), set the badge, and say so once when the server could update itself right now. A
// profile that has never checked is left alone: its first check is the popup's, which every
// viewer opens to set the server up, and a fresh profile then makes no request on its own. That
// keeps scripts/browser-smoke.mjs, which loads the built extension into a fresh Chromium profile
// with local fixtures only, off api.github.com at install and start; the popup it opens honours a
// stored check (`updateCheck` with a fresh `checkedAt`), which the test can seed with its settings.
async function startupCheck() {
  try {
    if (!(await readUpdateCheck())) return;
    const status = await updateStatus({ check: true });
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

if (browser.notifications && browser.notifications.onClicked && typeof browser.notifications.onClicked.addListener === "function") {
  browser.notifications.onClicked.addListener((id) => {
    if (id !== UPDATE_NOTIFICATION) return undefined;
    clearNotification(id);
    // The click asked for something and has no popup to answer in, so a request that did not
    // get through says so here: the notification would otherwise just vanish as if it had
    // worked. Checks stay silent; this is the one path the viewer set off. A server already at
    // the release (updated another way while the notification sat there) is no failure: the
    // stale notification is gone with the clear above, and that is all there was to do.
    return updateServer({ watch: true })
      .then((res) => (res.ok || res.upToDate ? undefined : notify(UPDATE_FAILED_NOTIFICATION, "Shisu-ko could not update the server", String(res.error || "the server gave no answer"))))
      .catch(() => {});
  });
}

// ------------------------------------------------------------------ AnkiConnect, for the word colours

// Mining is the server's: it attaches the audio and the picture to the note Yomitan made
// (Miner in server.py). The extension reads Anki for the word colours alone and never writes to it.

async function anki(url, action, params, timeoutMs) {
  // No Content-Type header on purpose: a "simple" request needs no CORS preflight, which
  // matters for the very first requestPermission call from a not-yet-allowed origin.
  // requestPermission blocks until the viewer answers Anki's dialog, so it gets no timeout.
  const controller = new AbortController();
  const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetch(url, {
      method: "POST",
      body: JSON.stringify({ action, version: 6, params: params || {} }),
      signal: controller.signal,
    });
    const data = await res.json();
    if (data && data.error) throw new Error(data.error);
    return data ? data.result : null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// A field's text by a name from the settings, in whatever case the note type spells it
// (SHISUKO_WORDS.fieldKey); "" when the note has no such field.
function noteField(fields, name) {
  const key = SHISUKO_WORDS.fieldKey(fields, name);
  return key ? String((fields[key] && fields[key].value) || "").trim() : "";
}

// The two fields that say what a card is about. The word is whatever the viewer configured, else
// the note's first field, which is where every Yomitan template puts the expression.
function noteSummary(info, settings) {
  const fields = (info && info.fields) || {};
  const read = (name) => noteField(fields, name);
  const sentenceName = String(settings.ankiSentenceField || "").trim() || "Sentence";
  const wordName = String(settings.ankiWordField || "").trim();
  let word = "";
  if (wordName) {
    word = read(wordName);
  } else {
    for (const value of Object.values(fields)) {
      if (value && Number(value.order) === 0) {
        word = String(value.value || "").trim();
        break;
      }
    }
  }
  return { sentence: read(sentenceName), word };
}

// One requestPermission at a time: Anki shows a dialog for it, and every caller that arrives
// while it is up (the popup's deck list, a tab's word colours) waits for the same
// answer (`permissionPending`) rather than asking for a second dialog or being told "denied" by
// the minute's recheck. `permissionAskAt` is when the next request may go: a minute after a
// verdict (ANKI_PERMISSION_RECHECK_MS, for a viewer who clicked No), stamped again when the
// verdict arrives, since the dialog can be up for longer than that. A request Anki never
// answered (closed, not installed) is no refusal and counts for nothing, or the next ask would
// report a dialog nobody saw: its error reaches every waiting caller, and the next ask (a tab's
// words, the popup's deck list) may try again at once.
async function ankiPermission(url) {
  if (ankiWatch.permission === "granted") return true;
  if (ankiWatch.permissionPending) return ankiWatch.permissionPending;
  const now = Date.now();
  if (now < ankiWatch.permissionAskAt) return false;
  // Set before the request: it blocks on Anki's dialog, and a caller landing meanwhile joins
  // the request under way rather than queue a dialog of its own.
  ankiWatch.permissionAskAt = now + ANKI_PERMISSION_RECHECK_MS;
  ankiWatch.permissionPending = (async () => {
    let perm;
    try {
      perm = await anki(url, "requestPermission", {});
    } catch (err) {
      // No verdict came (Anki not running, or still starting). The minute's wait is for a viewer
      // who clicked No; here it would turn Anki's absence into a minute of "denied" during which
      // a card it makes is never looked at.
      ankiWatch.permissionAskAt = 0;
      throw err;
    } finally {
      ankiWatch.permissionPending = null;
    }
    ankiWatch.permission = (perm && perm.permission) || "denied";
    // From the verdict, not the request: a No clicked after the dialog was up for over a
    // minute would otherwise be followed by a second dialog on the next poll.
    ankiWatch.permissionAskAt = Date.now() + ANKI_PERMISSION_RECHECK_MS;
    return ankiWatch.permission === "granted";
  })();
  return ankiWatch.permissionPending;
}

// ------------------------------------------------------------------ word colours

// The content script colours the words of a line by their Anki cards. It asks here for one
// deck's notes as [word, status, pitch] entries (words.js reads the fields and turns the five
// searches below into a status) and builds its own index from them. The deck is the one the
// popup names, else the one the last mined card went to, remembered under DECK_SEEN_KEY: a
// viewer who mines into a deck wants that deck's colours without setting anything up.
const CARD_STATUS_TTL_MS = 30000; // an index this fresh is answered from memory
const CARD_STATUS_TIMEOUT_MS = 20000; // per AnkiConnect request: a large deck takes its time
const NOTES_INFO_CHUNK = 200; // notes per notesInfo call
const DECK_SEEN_KEY = "ankiDeckSeen"; // storage.local: {deck, at, noteId}
const DECK_NOTES_KEY = "deckNotes"; // storage.session: {format, deck, wordField, pitchField, at, checkedAt, entries, notes: [[id, word, pitch, mod, reading]]}
// A record of another format is dropped whole: its notes lack what this one reads (the kana
// reading), and a restored note is never read again unless edited.
const DECK_NOTES_FORMAT = 2;
const DAY_MS = 86400000;
const ANKI_OFFLINE_TEXT = "Anki is not running or AnkiConnect is not installed";
const ANKI_DENIED_TEXT = "AnkiConnect denied access. Click Yes in Anki's permission dialog.";

// The searches that tell a card's state apart; suspended and unsuspended together are the deck.
// Since Anki 2.1.44 is:new, is:learn and is:review go by the card's type, which a suspended card
// keeps (and a buried learning card is in is:learn too), so each of the three leaves suspended
// cards out, and a note whose every card is suspended is caught by the suspended set alone.
const STATUS_QUERIES = Object.freeze({
  suspended: "is:suspended",
  unsuspended: "-is:suspended",
  new: "is:new -is:suspended",
  learning: "is:learn -is:suspended",
  review: "is:review -is:learn -is:suspended",
});

// The deck's index. `wordField` and `pitchField` are the fields it was read with: an index or a
// fetch for the same deck under other fields is about something else, and is never answered,
// joined or built on. `at` stamps the entries and stays while a refetch finds them the same, so a
// tab holding them is answered "unchanged"; `fetchedAt` is the clock the time to live runs on
// (rememberDeck sets it back to expire the index); `checkedAt` is when the notes were last
// looked through for edits; `notes` is what notesInfo said about each note, with the note's
// modification time, kept across refreshes and mines.
let cardIndex = null; // {deck, wordField, pitchField, at, fetchedAt, checkedAt, entries, notes: Map<noteId, {word, pitch, mod}>}
let cardIndexInFlight = null; // {deck, wordField, pitchField, promise, controller, expired}: the fetch under way, shared by every ask for that deck and those fields
// Counts the drops: an ask that read its settings before one, and only then got past Anki's
// permission dialog, reads them again rather than fetch the deck as it was set a moment ago.
let cardIndexGeneration = 0;

// The search clause for one deck and its subdecks. Anki reads a quoted name as a whole, and
// `\`, `"`, `*` and `_` mean something to it inside one.
function deckSearch(name) {
  return `"deck:${String(name).replace(/[\\"*_]/g, (ch) => "\\" + ch)}"`;
}

// Two names Anki reads as keywords rather than decks, with no way to escape them: "current" is
// the deck selected in its main window and "filtered" every filtered deck. Those two are searched
// by id instead, the deck's own and its subdecks'; null when no deck of that name exists, which
// is the empty deck any other missing name gives.
async function deckScope(url, deck) {
  if (deck !== "current" && deck !== "filtered") return deckSearch(deck);
  const named = await anki(url, "deckNamesAndIds", {}, CARD_STATUS_TIMEOUT_MS);
  const ids = [];
  for (const [name, id] of Object.entries(named && typeof named === "object" ? named : {})) {
    if ((name === deck || name.startsWith(deck + "::")) && Number.isFinite(Number(id))) ids.push(Number(id));
  }
  return ids.length ? `did:${ids.join(",")}` : null;
}

function wordColoursOn(settings) {
  return !!settings.cardStatus || !!settings.pitchAccent;
}

const noteIds = (ids) => (Array.isArray(ids) ? ids : []).map(Number).filter(Number.isFinite);
const fieldSetting = (settings, key) => String((settings && settings[key]) || "").trim();
// Whether an index, or a fetch under way, is about this deck read with the fields set now.
const indexFor = (index, deck, settings) =>
  !!index && index.deck === deck && index.wordField === fieldSetting(settings, "ankiWordField") && index.pitchField === fieldSetting(settings, "ankiPitchField");

// Forgotten along with the fetch under way, which stops at its next request: it lands with the
// deck or the fields of a moment ago, and the next ask must start over, notes included.
function dropCardIndex() {
  cardIndex = null;
  cardIndexGeneration++;
  if (cardIndexInFlight) {
    cardIndexInFlight.controller.abort();
    cardIndexInFlight = null;
  }
  sessionSet(DECK_NOTES_KEY, null);
}

// Expired, not forgotten: the next ask runs the searches again and reads only the notes it has
// not seen, while the notes it has stay. A fetch under way may have searched before the card
// existed, so its index lands expired too.
function expireCardIndex() {
  if (cardIndex) cardIndex.fetchedAt = 0;
  if (cardIndexInFlight) cardIndexInFlight.expired = true;
}

// The deck a note's cards sit in (the one with most of them, should a note type spread its
// cards). Best effort, after every note the server attached to (minedNote): a failure here is
// nothing the viewer asked about, and the mine is already done.
async function rememberDeck(url, noteId) {
  try {
    const cards = await anki(url, "findCards", { query: `nid:${noteId}` }, CARD_STATUS_TIMEOUT_MS);
    const decks = await anki(url, "getDecks", { cards: Array.isArray(cards) ? cards : [] }, CARD_STATUS_TIMEOUT_MS);
    let deck = null;
    let most = 0;
    for (const [name, ids] of Object.entries(decks && typeof decks === "object" ? decks : {})) {
      const count = Array.isArray(ids) ? ids.length : 0;
      if (count > most) {
        most = count;
        deck = name;
      }
    }
    if (!deck) return;
    await browser.storage.local.set({ [DECK_SEEN_KEY]: { deck, at: Date.now(), noteId } });
    // The card just made must show up red at once, not after the index's time to live.
    expireCardIndex();
  } catch (err) {
    console.debug("Shisu-ko: could not tell the new card's deck:", String((err && err.message) || err));
  }
}

// A tab heard from the server (/sync's `mined`) that a note got its media: its deck is the one
// the word colours look at when the popup names none. Not awaited by the tab.
async function minedNote(msg) {
  const noteId = Number(msg && msg.noteId);
  if (!Number.isSafeInteger(noteId) || noteId <= 0) return { ok: false, error: "No note" };
  const settings = await getSettings();
  await rememberDeck(normalizeBase(settings.ankiUrl, DEFAULT_SETTINGS.ankiUrl), noteId);
  return { ok: true };
}

async function seenDeck() {
  try {
    const stored = (await browser.storage.local.get(DECK_SEEN_KEY))[DECK_SEEN_KEY];
    return stored && typeof stored === "object" && typeof stored.deck === "string" && stored.deck ? stored : null;
  } catch (err) {
    return null;
  }
}

// The deck to look at: the popup's choice, else the last mined card's; automatic and nothing
// mined yet means no deck at all, so a viewer's whole collection is never searched by guesswork.
async function resolveDeck(settings) {
  const chosen = String(settings.cardStatusDeck || "").trim();
  if (chosen) return { deck: chosen, automatic: false };
  const seen = await seenDeck();
  return { deck: seen ? seen.deck : null, automatic: true };
}

// The notes read so far outlive the event page in storage.session, so a return to YouTube after
// the page ended does not read the whole deck again. The record names the deck and the two
// fields the words and pitches were read with: one for anything else is worth nothing. It
// carries the entries and their stamp too, so a page that finds the deck as the last one left
// it keeps the stamp, and the tabs holding those entries hear "unchanged" rather than get them
// all again.
function notesRecord(index) {
  const notes = [];
  for (const [id, note] of index.notes) notes.push([id, note.word, note.pitch, note.mod, note.reading]);
  return { format: DECK_NOTES_FORMAT, deck: index.deck, wordField: index.wordField, pitchField: index.pitchField, at: index.at, checkedAt: index.checkedAt, entries: index.entries, notes };
}

async function restoreNotes(deck, settings) {
  const record = await sessionGet(DECK_NOTES_KEY);
  if (!record || typeof record !== "object" || record.format !== DECK_NOTES_FORMAT || !Array.isArray(record.notes)) return null;
  if (!indexFor(record, deck, settings)) return null;
  const notes = new Map();
  for (const row of record.notes) {
    const id = Array.isArray(row) ? Number(row[0]) : NaN;
    if (Number.isFinite(id)) notes.set(id, { word: String(row[1] || ""), reading: String(row[4] || ""), pitch: row[2] || null, mod: Number(row[3]) || 0 });
  }
  // A stamp of 0 is what a tab holding nothing sends: entries under it would never reach one.
  const at = typeof record.at === "number" && record.at > 0 ? record.at : 0;
  const entries = at && Array.isArray(record.entries) && record.entries.every(Array.isArray) ? record.entries : undefined;
  return { notes, checkedAt: typeof record.checkedAt === "number" ? record.checkedAt : 0, at, entries };
}

// The notes edited since the previous look (`edited:` counts days back from the coming day
// rollover, so one more than the days elapsed always reaches it). A search Anki does not know
// (before 2.1.28) finds nothing: the words then lag an edit, as they did.
async function editedNotes(url, scope, previous, now) {
  const days = Math.max(2, Math.ceil((now - previous.checkedAt) / DAY_MS) + 1);
  try {
    return new Set(noteIds(await anki(url, "findNotes", { query: `${scope} edited:${days}` }, CARD_STATUS_TIMEOUT_MS)));
  } catch (err) {
    if (err && (err.name === "TypeError" || err.name === "AbortError")) throw err;
    console.debug("Shisu-ko: could not search for edited notes:", String((err && err.message) || err));
    return new Set();
  }
}

// Which of the notes the edit search listed really changed, by modification time: AnkiConnect
// answers {} for a note that vanished, and an older one without the action fails as a whole,
// which reads every note listed again.
async function changedNotes(url, ids, known) {
  const changed = new Set();
  let times;
  try {
    times = await anki(url, "notesModTime", { notes: ids }, CARD_STATUS_TIMEOUT_MS);
  } catch (err) {
    if (err && (err.name === "TypeError" || err.name === "AbortError")) throw err;
    console.debug("Shisu-ko: notesModTime failed for", ids.length, "notes:", String((err && err.message) || err));
    return new Set(ids);
  }
  const reported = new Map();
  (Array.isArray(times) ? times : []).forEach((row, k) => {
    const id = Number(row && row.noteId);
    reported.set(Number.isFinite(id) ? id : ids[k], Number(row && row.mod) || 0);
  });
  for (const id of ids) {
    if (reported.get(id) !== known.get(id).mod) changed.add(id);
  }
  return changed;
}

// One deck as entries. notesInfo is the expensive call, so what it said about a note is kept
// across refreshes (`notes`) for as long as the note is in the deck and unedited; the five
// searches are cheap and run every time, since they are what changes when the viewer reviews,
// and a sixth lists the notes edited since the last look, of which those whose modification
// time moved are read again. `signal` is the drop of the index: the walk ends at its next request.
async function fetchDeckIndex(url, deck, settings, signal) {
  const started = Date.now();
  const wordField = fieldSetting(settings, "ankiWordField");
  const pitchField = fieldSetting(settings, "ankiPitchField");
  const dropped = () => {
    if (signal && signal.aborted) throw Object.assign(new Error("index dropped"), { dropped: true });
  };
  dropped();
  const scope = await deckScope(url, deck);
  const sets = {};
  for (const [name, clause] of Object.entries(STATUS_QUERIES)) {
    dropped();
    const ids = scope === null ? [] : await anki(url, "findNotes", { query: `${scope} ${clause}` }, CARD_STATUS_TIMEOUT_MS);
    sets[name] = new Set(noteIds(ids));
  }
  // By note id, whatever order the searches answered in: Anki's has none (no ORDER BY, so a
  // review that moves a card's due may move its note), and the entries must come out the same
  // for the same deck, or every tab would take them in again for nothing.
  const ids = new Set([...sets.suspended, ...sets.unsuspended].sort((a, b) => a - b));
  // What is known already: this page's index, else the notes an earlier page left behind.
  const remembered = indexFor(cardIndex, deck, settings) ? cardIndex : null;
  const previous = remembered || (await restoreNotes(deck, settings));
  const restored = !remembered && !!previous;
  const known = previous ? previous.notes : new Map();
  dropped();
  const edited = previous && scope !== null && known.size ? await editedNotes(url, scope, previous, started) : new Set();
  const suspect = [...ids].filter((id) => edited.has(id) && known.has(id));
  dropped();
  const reread = suspect.length ? await changedNotes(url, suspect, known) : new Set();
  const notes = new Map();
  const missing = [];
  for (const id of ids) {
    const note = known.get(id);
    if (note && !reread.has(id)) notes.set(id, note);
    else missing.push(id);
  }
  let read = 0;
  for (let i = 0; i < missing.length; i += NOTES_INFO_CHUNK) {
    const chunk = missing.slice(i, i + NOTES_INFO_CHUNK);
    dropped();
    let infos;
    try {
      infos = await anki(url, "notesInfo", { notes: chunk }, CARD_STATUS_TIMEOUT_MS);
    } catch (err) {
      // Anki gone, or not answering: the ask fails as a whole. Anki's own complaint about a
      // chunk (a note deleted since the search) costs those notes alone, and a note read again
      // for an edit keeps what it said before rather than lose its colour until the next try.
      if (err && (err.name === "TypeError" || err.name === "AbortError")) throw err;
      console.debug("Shisu-ko: notesInfo failed for", chunk.length, "notes:", String((err && err.message) || err));
      for (const id of chunk) {
        const old = known.get(id);
        if (old) notes.set(id, old);
      }
      continue;
    }
    (Array.isArray(infos) ? infos : []).forEach((info, k) => {
      // Answers come in the order asked; a note that vanished answers as an empty object.
      const reported = Number(info && info.noteId);
      const id = Number.isFinite(reported) ? reported : chunk[k];
      if (!ids.has(id) || !info || !info.fields || typeof info.fields !== "object") return;
      // A word longer than the content script's index keeps is a sentence in the word field:
      // stored as no word, so it is neither kept nor sent to every tab only to be dropped there.
      const word = SHISUKO_WORDS.plainWord(noteSummary(info, settings).word);
      notes.set(id, {
        word: word.length > SHISUKO_WORDS.MAX_WORD_LEN ? "" : word,
        reading: SHISUKO_WORDS.kanaReadingOf(info.fields, settings),
        pitch: SHISUKO_WORDS.pitchOf(info.fields, settings),
        mod: Number(info.mod) || 0,
      });
      read++;
    });
  }
  // A fetch dropped during its last request answers nobody either: what it read is about a deck
  // or fields no longer asked for.
  dropped();
  // In the order of the ids, whenever a note was read: a note read again for an edit that left
  // its word, status and pitch as they were must not move, or the entries would compare as
  // changed twice over, and every tab would take them in twice.
  const entries = [];
  for (const id of ids) {
    const note = notes.get(id);
    if (!note || !note.word) continue;
    const status = SHISUKO_WORDS.statusOf(sets, id);
    if (!status) continue;
    entries.push([note.word, status, note.pitch]);
    // A word usually written in kana is subtitled in kana: 更に is heard as さらに.
    if (note.reading) entries.push([note.reading, status, note.pitch]);
  }
  // The same entries keep their stamp, so a tab holding them hears "unchanged" rather than
  // getting them all again. The notes changed when one was read or dropped (a chunk that
  // failed changed nothing, so its notes are asked for again without the record being written
  // again), and a record another page left is written once more with what this one found.
  const same = !!(previous && previous.entries && sameEntries(previous.entries, entries));
  const changed = read > 0 || notes.size !== known.size || restored;
  return { deck, wordField, pitchField, at: same ? previous.at : started, fetchedAt: started, checkedAt: started, entries, notes, changed };
}

function sameEntries(a, b) {
  return a.length === b.length && a.every((e, i) => e[0] === b[i][0] && e[1] === b[i][1] && e[2] === b[i][2]);
}

// One fetch at a time per deck and fields; asks that overlap wait for it. A fetch dropped while
// under way (a settings change, or another deck or field asked for: the last mined card went
// elsewhere) is abandoned and whoever waited asks again; one expired meanwhile (rememberDeck)
// is kept, but lands expired.
function refreshCardIndex(url, deck, settings) {
  if (!indexFor(cardIndexInFlight, deck, settings)) {
    if (cardIndexInFlight) cardIndexInFlight.controller.abort();
    const controller = new AbortController();
    const flight = { deck, wordField: fieldSetting(settings, "ankiWordField"), pitchField: fieldSetting(settings, "ankiPitchField"), promise: null, controller, expired: false };
    flight.promise = fetchDeckIndex(url, deck, settings, controller.signal)
      .then((index) => {
        if (cardIndexInFlight === flight) {
          if (flight.expired) index.fetchedAt = 0;
          cardIndex = index;
          if (index.changed) sessionSet(DECK_NOTES_KEY, notesRecord(index));
        }
        return index;
      })
      .finally(() => {
        if (cardIndexInFlight === flight) cardIndexInFlight = null;
      });
    cardIndexInFlight = flight;
  }
  return cardIndexInFlight.promise;
}

// The content script's ask: the deck's entries, or why there are none. `since` is the `at` of
// the index it holds; the same index again is answered without the entries. A failed refresh
// keeps and answers the old index (`stale`): colours a minute old beat none.
async function cardStatus(msg, retried) {
  const generation = cardIndexGeneration;
  const settings = await getSettings();
  if (!wordColoursOn(settings)) return { ok: false, reason: "off" };
  const { deck, automatic } = await resolveDeck(settings);
  if (!deck) return { ok: false, reason: "noDeck", error: "No card mined yet; pick a deck in the popup" };
  const url = normalizeBase(settings.ankiUrl, DEFAULT_SETTINGS.ankiUrl);
  const since = msg && typeof msg.since === "number" ? msg.since : null;
  const answer = (index, stale) => {
    const res = since === index.at ? { ok: true, unchanged: true, at: index.at, deck } : { ok: true, deck, automatic, at: index.at, entries: index.entries };
    if (stale) res.stale = true;
    return res;
  };
  try {
    if (!(await ankiPermission(url))) return { ok: false, reason: "denied", error: ANKI_DENIED_TEXT };
    // The index was dropped while this ask waited (Anki's permission dialog can be up for as
    // long as it likes): once more, for the deck and fields set now, not the ones read above.
    if (generation !== cardIndexGeneration && !retried) return cardStatus(msg, true);
    const fresh = indexFor(cardIndex, deck, settings) && Date.now() - cardIndex.fetchedAt < CARD_STATUS_TTL_MS ? cardIndex : null;
    return answer(fresh || (await refreshCardIndex(url, deck, settings)), false);
  } catch (err) {
    // Dropped under the ask, another deck or field having been chosen: once more, for what is set now.
    if (err && err.dropped && !retried) return cardStatus(msg, true);
    const stale = indexFor(cardIndex, deck, settings) && cardIndex.entries.length ? cardIndex : null;
    if (stale) return answer(stale, true);
    const network = err && err.name === "TypeError";
    return network ? { ok: false, reason: "offline", error: ANKI_OFFLINE_TEXT } : { ok: false, reason: "error", error: String((err && err.message) || err) };
  }
}

// The popup's deck list, with the deck the last mined card went to.
async function ankiDecks() {
  const settings = await getSettings();
  const url = normalizeBase(settings.ankiUrl, DEFAULT_SETTINGS.ankiUrl);
  const seen = await seenDeck();
  const seenName = seen ? seen.deck : null;
  try {
    if (!(await ankiPermission(url))) return { ok: false, reason: "denied", error: ANKI_DENIED_TEXT, seen: seenName };
    const names = await anki(url, "deckNames", {}, CARD_STATUS_TIMEOUT_MS);
    const decks = (Array.isArray(names) ? names : []).map(String).sort((a, b) => a.localeCompare(b));
    return { ok: true, decks, seen: seenName };
  } catch (err) {
    const network = err && err.name === "TypeError";
    return { ok: false, reason: network ? "offline" : "error", error: network ? ANKI_OFFLINE_TEXT : String((err && err.message) || err), seen: seenName };
  }
}

// The entries depend on these three settings: another deck, or another field to read a word or
// its pitch from, makes the index one about something else.
const CARD_INDEX_SETTINGS = ["cardStatusDeck", "ankiPitchField", "ankiWordField"];

browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes || !changes.settings) return;
  // All three are strings, and one never stored is the default: unset and "" are the same setting.
  const value = (settings, key) => String((settings && settings[key]) || "").trim();
  const { oldValue, newValue } = changes.settings;
  if (CARD_INDEX_SETTINGS.some((key) => value(oldValue, key) !== value(newValue, key))) dropCardIndex();
});

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
      return apiRequest(msg.path, msg.body);
    }
    case "getSettings":
      return getSettings();
    case "saveSettings":
      return saveSettings(msg.settings);
    case "minedNote":
      return minedNote(msg);
    case "startServer":
      return startServer();
    case "startServerStatus":
      return startStatus();
    case "updateStatus":
      return updateStatus(msg);
    case "checkForUpdate":
      return checkForUpdate({ force: !!msg.force });
    case "updateServer":
      return updateServer();
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

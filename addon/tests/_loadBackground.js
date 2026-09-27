"use strict";

// addon/background.js is a plain extension script (no exports): it expects a global `browser`
// WebExtension API and DOM globals like fetch/btoa. To unit-test its pure/mockable logic without
// Firefox, we run the file's source in a `vm` sandbox with those globals stubbed out. Top-level
// `function` declarations in script (non-module) code become properties of the global object even
// in strict mode, so the sandbox object ends up exposing normalizeBase, bytesToBase64, mineCue, etc.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SETTINGS_PATH = path.join(__dirname, "..", "settings.js");
const MATCH_PATH = path.join(__dirname, "..", "match.js");
const WORDS_PATH = path.join(__dirname, "..", "words.js");
const SOURCE_PATH = path.join(__dirname, "..", "background.js");

function makeMemoryStorage(initial) {
  let store = { ...(initial || {}) };
  return {
    async get(key) {
      if (key === undefined) return { ...store };
      return Object.prototype.hasOwnProperty.call(store, key) ? { [key]: store[key] } : {};
    },
    async set(patch) {
      store = { ...store, ...patch };
    },
    _dump() {
      return store;
    },
  };
}

// Minimal btoa: background.js only feeds it byte-string data built from Uint8Array.
function btoa(binary) {
  return Buffer.from(binary, "binary").toString("base64");
}

// background.js caches the settings and drops the cache when storage.onChanged reports a write, so
// the sandbox has to deliver that event the way Firefox does: after every storage.local.set.
function notifyingStorage(storage, listeners) {
  return {
    get: (key) => storage.get(key),
    async set(patch) {
      const before = await storage.get(undefined);
      await storage.set(patch);
      const changes = {};
      for (const key of Object.keys(patch)) changes[key] = { oldValue: before[key], newValue: patch[key] };
      for (const fn of listeners.onChanged.slice()) fn(changes, "local");
    },
  };
}

const MANIFEST_VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8")).version;

function loadBackground(overrides = {}) {
  const source = fs.readFileSync(SOURCE_PATH, "utf8");
  const storage = overrides.storage || makeMemoryStorage();
  // storage.session outlives an event page but not the browser; a test hands the same store to a
  // second loadBackground to play a restarted page, or null for a browser without the area.
  const session = overrides.session === null ? undefined : overrides.session || makeMemoryStorage();
  const listeners = { onMessage: [], onCommand: [], onChanged: [], onTabRemoved: [], onStartup: [], onInstalled: [], onNotificationClicked: [],
    onTabActivated: [], onWindowFocusChanged: [] };
  // What the update nudges did: the badge calls and the notifications, in order. A test passes
  // null for `action` or `notifications` to play a browser (or a service worker) without the API.
  const badge = [];
  const notifications = [];
  const action = overrides.action === null ? undefined : {
    setBadgeText: async (details) => badge.push(["text", details.text]),
    setBadgeBackgroundColor: async (details) => badge.push(["color", details.color]),
  };
  const notificationsApi = overrides.notifications === null ? undefined : {
    create: async (id, options) => {
      notifications.push({ id, ...options });
      return id;
    },
    clear: async (id) => {
      notifications.push({ id, cleared: true });
      return true;
    },
    onClicked: { addListener: (fn) => listeners.onNotificationClicked.push(fn) },
  };

  const sandbox = {
    console,
    setTimeout: overrides.setTimeout || setTimeout,
    clearTimeout,
    AbortController,
    Uint8Array,
    btoa,
    atob: (b64) => Buffer.from(b64, "base64").toString("binary"),
    Blob,
    URL: {
      createObjectURL: overrides.createObjectURL || (() => "blob:moz-extension://test/" + Math.random().toString(16).slice(2)),
      revokeObjectURL: overrides.revokeObjectURL || (() => {}),
    },
    fetch: overrides.fetch || (async () => {
      throw new Error("fetch() was not mocked for this test");
    }),
    browser: {
      storage: {
        local: notifyingStorage(storage, listeners),
        session,
        onChanged: { addListener: (fn) => listeners.onChanged.push(fn) },
      },
      downloads: {
        download: overrides.download || (async () => 1),
        onChanged: { addListener: () => {}, removeListener: () => {} },
      },
      runtime: {
        getURL: (path = "") => (overrides.runtimeURL || "moz-extension://test/") + path,
        getManifest: () => ({ version: overrides.extensionVersion || MANIFEST_VERSION }),
        onMessage: { addListener: (fn) => listeners.onMessage.push(fn) },
        onStartup: { addListener: (fn) => listeners.onStartup.push(fn) },
        onInstalled: { addListener: (fn) => listeners.onInstalled.push(fn) },
        // Absent unless a test supplies one: that is what Firefox shows a background page whose
        // nativeMessaging permission was never granted.
        sendNativeMessage: overrides.sendNativeMessage,
      },
      commands: {
        onCommand: { addListener: (fn) => listeners.onCommand.push(fn) },
      },
      tabs: {
        // The election asks which tab a window is showing; a test supplies the answer.
        query: overrides.tabsQuery || (async () => []),
        sendMessage: async () => {},
        onRemoved: { addListener: (fn) => listeners.onTabRemoved.push(fn) },
        onActivated: { addListener: (fn) => listeners.onTabActivated.push(fn) },
      },
      windows: {
        WINDOW_ID_NONE: -1,
        onFocusChanged: { addListener: (fn) => listeners.onWindowFocusChanged.push(fn) },
      },
      action,
      notifications: notificationsApi,
    },
  };
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  // settings.js defines SHISUKO_DEFAULT_SETTINGS in the shared global lexical scope, exactly as the
  // manifest loads it before background.js in Firefox.
  new vm.Script(fs.readFileSync(SETTINGS_PATH, "utf8"), { filename: SETTINGS_PATH }).runInContext(sandbox);
  // match.js sits between them in the manifest too: background.js reads SHISUKO_MATCH at load time.
  new vm.Script(fs.readFileSync(MATCH_PATH, "utf8"), { filename: MATCH_PATH }).runInContext(sandbox);
  // words.js follows match.js in the manifest: the deck index reads notes through SHISUKO_WORDS.
  new vm.Script(fs.readFileSync(WORDS_PATH, "utf8"), { filename: WORDS_PATH }).runInContext(sandbox);
  new vm.Script(source, { filename: SOURCE_PATH }).runInContext(sandbox);
  // Top-level `const`/`let` (DEFAULT_SETTINGS, REQUEST_TIMEOUT_MS, sleep) live in the global
  // *lexical* environment, not as globalThis properties, but that environment is shared across
  // scripts run against the same context — so a second script can still see them by name and
  // copy them onto globalThis for the test harness to read.
  new vm.Script(
    "globalThis.DEFAULT_SETTINGS = DEFAULT_SETTINGS; globalThis.REQUEST_TIMEOUT_MS = REQUEST_TIMEOUT_MS;" +
      " globalThis.NATIVE_TIMEOUT_MS = NATIVE_TIMEOUT_MS; globalThis.LAUNCHER_HINT = LAUNCHER_HINT;" +
      " globalThis.START_WINDOW_MS = START_WINDOW_MS;" +
      " globalThis.GITHUB_LATEST_URL = GITHUB_LATEST_URL; globalThis.UPDATE_CHECK_MAX_AGE_MS = UPDATE_CHECK_MAX_AGE_MS;" +
      " globalThis.UPDATE_WINDOW_MS = UPDATE_WINDOW_MS; globalThis.UPDATE_POLL_MS = UPDATE_POLL_MS;" +
      " globalThis.ANKI_PERMISSION_RECHECK_MS = ANKI_PERMISSION_RECHECK_MS; globalThis.ANKI_REPORT_WINDOW_MS = ANKI_REPORT_WINDOW_MS;" +
      " globalThis.CLIP_TIMEOUT_MS = CLIP_TIMEOUT_MS; globalThis.ANKI_REQUEST_TIMEOUT_MS = ANKI_REQUEST_TIMEOUT_MS;" +
      " globalThis.ANKI_PERMISSION_TIMEOUT_MS = ANKI_PERMISSION_TIMEOUT_MS; globalThis.ANKI_PERMISSION_RETRY_MS = ANKI_PERMISSION_RETRY_MS;" +
      " globalThis.ANKI_REPORT_MIN_CHARS = ANKI_REPORT_MIN_CHARS;" +
      " globalThis.CARD_STATUS_TTL_MS = CARD_STATUS_TTL_MS; globalThis.DECK_SEEN_KEY = DECK_SEEN_KEY;" +
      " globalThis.DECK_NOTES_KEY = DECK_NOTES_KEY; globalThis.DECK_NOTES_FORMAT = DECK_NOTES_FORMAT;" +
      " globalThis.ankiWatch = ankiWatch; globalThis.premined = premined;" +
      " globalThis.HOLD_TIMEOUT_MS = HOLD_TIMEOUT_MS; globalThis.FOCUS_STALE_MS = FOCUS_STALE_MS;" +
      " globalThis.syncers = syncers; globalThis.activeTabs = activeTabs;",
    { filename: SOURCE_PATH }
  ).runInContext(sandbox);

  // Firefox hands every listener the sender as the second argument; the tab id in it is what
  // tells the pre-mine store whose material this is.
  function dispatch(msg, tabId) {
    const sender = tabId === undefined ? {} : { tab: { id: tabId } };
    for (const fn of listeners.onMessage) {
      const result = fn(msg, sender);
      if (result !== undefined) return result;
    }
    return undefined;
  }

  function closeTab(tabId) {
    for (const fn of listeners.onTabRemoved.slice()) fn(tabId, {});
  }

  // The browser starting (or the extension installed): every listener runs, and the promise
  // settles when each has done its work, which the listeners return.
  function startup(installed) {
    const fns = installed ? listeners.onInstalled : listeners.onStartup;
    return Promise.all(fns.slice().map((fn) => fn(installed ? { reason: "update" } : undefined)));
  }

  function clickNotification(id) {
    return Promise.all(listeners.onNotificationClicked.slice().map((fn) => fn(id)));
  }

  // The clock as background.js reads it (Date.now in the context, which is the sandbox's own
  // realm): a test moves it to play a restart that took longer than a poll interval.
  const setNow = new vm.Script("(at) => { Date.now = () => at; }", { filename: SOURCE_PATH }).runInContext(sandbox);

  // Switching tabs inside a window, and moving focus between windows: the two events the
  // election listens to. Firefox fires only the first when the tab selection changes.
  function activateTab(tabId, windowId) {
    for (const fn of listeners.onTabActivated.slice()) fn({ tabId, windowId });
  }

  function focusWindow(windowId) {
    return Promise.all(listeners.onWindowFocusChanged.slice().map((fn) => fn(windowId)));
  }

  return { sandbox, storage, session, listeners, dispatch, closeTab, badge, notifications, startup, clickNotification, setNow,
    activateTab, focusWindow };
}

module.exports = { loadBackground, makeMemoryStorage };

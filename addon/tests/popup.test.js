"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const ADDON = path.join(__dirname, "..");
const HTML = fs.readFileSync(path.join(ADDON, "popup.html"), "utf8");
const CSS = fs.readFileSync(path.join(ADDON, "popup.css"), "utf8").replace(/\/\*[^]*?\*\//g, "");
const POPUP_JS = fs.readFileSync(path.join(ADDON, "popup.js"), "utf8");

// The declarations of the popup.css rule with exactly this selector list, as "property: value".
function cssDeclarations(selector) {
  for (const block of CSS.split("}")) {
    const brace = block.lastIndexOf("{");
    if (brace < 0 || block.slice(0, brace).trim() !== selector) continue;
    return block
      .slice(brace + 1)
      .split(";")
      .map((d) => d.replace(/\s+/g, " ").trim())
      .filter(Boolean);
  }
  return assert.fail(`popup.css has no rule for ${selector}`);
}

// popup.js, driven without Firefox. It paints and reads elements by id; the fake document holds
// one plain object per element popup.html declares, with the tag, the type and the range bounds
// off the markup, the members the form, the status line and the hints touch, and the listeners
// init() registers, which a test fires with dispatch(). An id popup.js asks for that the markup
// lacks gets a bare element, so no test trips over a missing member. A select's options are its
// children: renderDeckOptions builds them with createElement and replaceChildren.
function fakeElement(tag = "div", attrs = {}) {
  const listeners = new Map();
  const styles = new Map();
  let text = "";
  const el = {
    tagName: tag.toUpperCase(),
    id: attrs.id || "",
    type: tag === "select" ? "select-one" : tag === "textarea" ? "textarea" : attrs.type || (tag === "input" ? "text" : ""),
    min: attrs.min || "",
    max: attrs.max || "",
    className: "",
    disabled: false,
    hidden: false,
    value: "",
    checked: false,
    options: [],
    writes: 0, // textContent assignments, the mutations a repaint follows
    attrWrites: 0, // placeholder assignments: an attribute change, a mutation like textContent's
    toggles: 0, // classList.toggle calls, one per paint of the element's block
    style: { setProperty: (name, value) => styles.set(name, value), getPropertyValue: (name) => styles.get(name) || "" },
    listeners,
  };
  Object.defineProperty(el, "textContent", {
    get: () => text,
    set: (value) => {
      el.writes++;
      text = value;
    },
  });
  let placeholder = "";
  Object.defineProperty(el, "placeholder", {
    get: () => placeholder,
    set: (value) => {
      el.attrWrites++;
      placeholder = value;
    },
  });
  el.classList = {
    toggle: (name, force) => {
      el.toggles++;
      if (name === "hidden") el.hidden = !!force;
    },
    add: () => {},
    remove: () => {},
  };
  el.addEventListener = (name, fn) => listeners.set(name, [...(listeners.get(name) || []), fn]);
  el.dispatch = (name) => {
    for (const fn of listeners.get(name) || []) fn({ type: name, target: el });
  };
  el.appendChild = (child) => el.options.push(child);
  el.replaceChildren = (...children) => {
    el.options = children;
  };
  return el;
}

function elementsFromHtml() {
  const elements = new Map();
  for (const [, tag, attrText] of HTML.matchAll(/<(input|select|textarea|button|output|p|span|div|datalist)\b([^>]*)>/g)) {
    const attrs = Object.fromEntries([...attrText.matchAll(/(\w+)="([^"]*)"/g)].map((m) => [m[1], m[2]]));
    if (attrs.id) elements.set(attrs.id, fakeElement(tag, attrs));
  }
  return elements;
}

// `answer` plays the background: it gets every runtime.sendMessage and returns the reply. The
// popup runs as Firefox's unless `runtimeURL` says otherwise: on Chrome the button is for the
// store install alone (CHROME_STORE_URL), and "chrome-extension://test/" plays an unpacked build.
// `opts.installedFonts` names the families the fake canvas measures differently from a generic.
// `permissionRequests` records each permissions.request as it is made, so a test can see whether
// the click made it before its first await; `opts.grant: false` refuses them.
// The clocks init() sets up never run here; `intervals` holds them ({fn, ms}) for a test to tick.
function loadPopup(answer, runtimeURL = "moz-extension://test/", opts = {}) {
  const elements = elementsFromHtml();
  const permissionRequests = [];
  const installed = new Set(opts.installedFonts || []);
  const canvas = { canvases: 0, contexts: 0 };
  const listened = () => {
    const listeners = new Map();
    return {
      addEventListener: (name, fn) => listeners.set(name, [...(listeners.get(name) || []), fn]),
      dispatch: (name) => {
        for (const fn of listeners.get(name) || []) fn({ type: name });
      },
    };
  };
  const document = {
    ...listened(),
    hidden: false,
    activeElement: null,
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, fakeElement(id));
      return elements.get(id);
    },
    createElement: (tag) => {
      if (tag !== "canvas") return fakeElement(tag);
      canvas.canvases++;
      return {
        getContext: () => {
          canvas.contexts++;
          const ctx = { font: "", measureText: () => ({ width: [...installed].some((f) => ctx.font.includes(`"${f}"`)) ? 200 : 100 }) };
          return ctx;
        },
      };
    },
    body: { classList: { toggle: () => {} } },
  };
  const window = listened();
  const intervals = [];
  const storageListeners = [];
  const opened = [];
  const sandbox = {
    document,
    window,
    console,
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => intervals.push({ fn, ms }),
    browser: {
      // The id is the URL's host, as Chrome's is; Firefox's is the gecko id, which never matters.
      runtime: { sendMessage: async (msg) => answer(msg), getURL: () => runtimeURL, id: new URL(runtimeURL).host },
      permissions: {
        contains: async () => true,
        request: (what) => {
          permissionRequests.push(JSON.parse(JSON.stringify(what)));
          return Promise.resolve(opts.grant !== false);
        },
      },
      tabs: { query: async () => [], create: async (opts) => opened.push(opts.url) },
      storage: { onChanged: { addListener: (fn) => storageListeners.push(fn) } },
    },
  };
  vm.createContext(sandbox);
  new vm.Script(fs.readFileSync(path.join(ADDON, "settings.js"), "utf8")).runInContext(sandbox);
  new vm.Script(fs.readFileSync(path.join(ADDON, "popup.js"), "utf8"), { filename: "popup.js" }).runInContext(sandbox);
  const api = new vm.Script(
    "({ startFlow, resumeStart, checkServer, startServerFromPopup, startNotUpHint, START_NOT_UP_HINT, START_ELSEWHERE_HINT, OFFLINE_HINT," +
      " START_AVAILABLE, ON_FIREFOX, CHROME_STORE_ID, START_PERMISSION_HINT," +
      " updateFlow, refreshUpdate, updateServerFromPopup, snoozeUpdateFromPopup, checkForUpdatesFromPopup, openReleasePage, relativeTime, renderStatus," +
      " UPDATE_LOST_HINT, stillOldHint, init, resetStyle, onChange, renderDeckOptions, refreshDecks, DECK_NONE_HINT, HEALTH_REFRESH_MS, DECKS_RETRY_MS })"
  ).runInContext(sandbox);
  // What the background's storage.local write of the settings looks like from the popup.
  const fireStorage = (settings) => {
    for (const fn of storageListeners) fn({ settings: { newValue: settings } }, "local");
  };
  return { ...api, el: (id) => document.getElementById(id), opened, document, window, intervals, storageListeners, canvas, fireStorage, permissionRequests };
}

const DEFAULTS = (() => {
  const sandbox = {};
  vm.createContext(sandbox);
  new vm.Script(fs.readFileSync(path.join(ADDON, "settings.js"), "utf8")).runInContext(sandbox);
  return JSON.parse(JSON.stringify(new vm.Script("SHISUKO_DEFAULT_SETTINGS").runInContext(sandbox)));
})();

// The Chrome Web Store install: the one Chrome id native_host.py registers the launcher for.
const CHROME_STORE_URL = "chrome-extension://ecenifonpkaiccmmknpbllbebbfigjnm/";
const offline = { ok: false, offline: true, error: "Server unreachable" };
const online = { ok: true, data: { model: "large-v3", device: "cuda", compute_type: "float16" } };
// checkServer() paints the status line at once and asks the background about updates behind it;
// a turn of the event loop lets that answer land before a test reads the banner.
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a reopened popup resumes the launch the background reports, with the button disabled", async () => {
  const deadline = Date.now() + 50000;
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, already: false, deadline } : offline));
  await popup.resumeStart();
  assert.equal(popup.startFlow.state, "waiting");
  assert.equal(popup.startFlow.deadline, deadline);
  await popup.checkServer(false);
  assert.equal(popup.el("server-status").textContent, "Starting server");
  assert.equal(popup.el("server-detail").textContent, "launched, waiting for it to answer");
  const button = popup.el("start-server");
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Starting…");
});

test("with no launch under way the popup offers the button", async () => {
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: false } : offline));
  await popup.resumeStart();
  assert.equal(popup.startFlow.state, "idle");
  await popup.checkServer(true);
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("server-detail").textContent, popup.OFFLINE_HINT);
  assert.equal(popup.el("start-server").hidden, false);
  assert.equal(popup.el("start-server").disabled, false);
  assert.equal(popup.el("start-server").textContent, "Start server");
});

test("past the deadline the detail points at the log and the button is back", async () => {
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, deadline: Date.now() - 1 } : offline));
  await popup.resumeStart();
  await popup.checkServer(false);
  assert.equal(popup.startFlow.state, "failed");
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("server-detail").textContent, popup.START_NOT_UP_HINT);
  assert.match(popup.START_NOT_UP_HINT, /server\.log/);
  assert.equal(popup.el("start-server").disabled, false);
});

// The launcher names the log it opened (server.log under SHISUKO_HOME, elsewhere than the hint's
// ~/.shisu-ko when the variable is set); a launch that failed at once has its reason only there.
test("past the deadline the detail names the log the launcher opened", async () => {
  const log = "/srv/shisuko/server.log";
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, log, deadline: Date.now() - 1 } : offline));
  await popup.resumeStart();
  assert.equal(popup.startFlow.log, log);
  await popup.checkServer(false);
  assert.equal(popup.el("server-detail").textContent, popup.startNotUpHint(log));
  assert.match(popup.el("server-detail").textContent, /look at \/srv\/shisuko\/server\.log before/);
  assert.doesNotMatch(popup.el("server-detail").textContent, /\.shisu-ko\/server\.log/);
});

// The launcher's `starting` (the background's `loading`): a server launched, by an earlier click
// or by hand, that holds its instance lock while the model loads or downloads. It is not up at
// 127.0.0.1:8790 any more than one this popup launched, so the deadline must not blame the URL.
test("a server the launcher finds still loading is waited for like a launched one", async () => {
  let deadline = Date.now() + 50000;
  const popup = loadPopup((msg) => (msg.type === "startServer" ? { ok: true, started: false, already: true, loading: true, log: null, deadline } : offline));
  await popup.startServerFromPopup();
  assert.equal(popup.startFlow.state, "waiting");
  assert.equal(popup.startFlow.loading, true);
  assert.equal(popup.el("server-status").textContent, "Starting server");
  assert.equal(popup.el("server-detail").textContent, "still loading by the launcher's account, waiting for it to answer");
  assert.equal(popup.el("start-server").disabled, true);
  deadline = Date.now() - 1;
  popup.startFlow.deadline = deadline;
  await popup.checkServer(false);
  assert.equal(popup.startFlow.state, "failed");
  assert.equal(popup.el("server-detail").textContent, popup.START_NOT_UP_HINT);
  assert.equal(popup.el("start-server").disabled, false);
});

test("a server the launcher finds answering, and this popup does not, points at the server URL", async () => {
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, already: true, loading: false, log: null, deadline: Date.now() - 1 } : offline));
  await popup.resumeStart();
  assert.equal(popup.el("server-detail").textContent, "already running by the launcher's account, waiting for it to answer");
  await popup.checkServer(false);
  assert.equal(popup.startFlow.state, "failed");
  assert.equal(popup.el("server-detail").textContent, popup.START_ELSEWHERE_HINT);
  assert.notEqual(popup.START_ELSEWHERE_HINT, popup.START_NOT_UP_HINT);
});

// Chrome takes four suggested shortcuts and the build drops the fifth, Alt+Shift+H, whichever
// way it was installed: the store install gets the Start button, not the key.
test("the Alt+Shift+H hint shows in Firefox and not on Chrome, whose build has no key for it", async () => {
  for (const [url, hidden] of [["moz-extension://test/", false], ["chrome-extension://test/", true], [CHROME_STORE_URL, true]]) {
    const bg = updateBackground({});
    const popup = loadPopup(bg.answer, url);
    await popup.init();
    await settle();
    assert.equal(popup.el("statusBadgeKey").hidden, hidden, url);
    assert.equal(popup.el("statusBadge").checked, true, "the switch itself is there in both");
  }
});

// native_host.py registers the launcher for the Chrome Web Store install's origin alone. An
// unpacked build (dist/chrome, the release zip) has an id derived from its folder's path, and
// the button there would answer "launcher not registered" with a hint (run setup.cmd) that
// registers nothing Chrome would let that id reach.
test("on an unpacked Chrome build the button stays hidden while the server is offline", async () => {
  for (const url of ["chrome-extension://test/", "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"]) {
    const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: false } : offline), url);
    assert.equal(popup.START_AVAILABLE, false, url);
    await popup.resumeStart();
    await popup.checkServer(true);
    assert.equal(popup.el("server-status").textContent, "Server offline");
    assert.equal(popup.el("server-detail").textContent, popup.OFFLINE_HINT);
    assert.equal(popup.el("start-server").hidden, true, url);
  }
});

test("the Chrome Web Store install offers the button, and the click asks for nativeMessaging first", async () => {
  const sent = [];
  const popup = loadPopup((msg) => {
    sent.push(msg.type);
    if (msg.type === "startServerStatus") return { starting: false };
    if (msg.type === "startServer") return { ok: true, started: true, already: false, log: null, deadline: Date.now() + 90000 };
    return offline;
  }, CHROME_STORE_URL);
  assert.equal(popup.START_AVAILABLE, true);
  assert.equal(popup.ON_FIREFOX, false);
  assert.equal(`chrome-extension://${popup.CHROME_STORE_ID}/`, CHROME_STORE_URL);
  await popup.resumeStart();
  await popup.checkServer(true);
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("start-server").hidden, false);
  assert.equal(popup.el("start-server").disabled, false);
  const click = popup.startServerFromPopup();
  // Asked before the click's first await, while its user gesture lasts: Chrome refuses the prompt
  // after that, and every click would end on START_PERMISSION_HINT.
  assert.deepEqual(popup.permissionRequests, [{ permissions: ["nativeMessaging"] }]);
  await click;
  assert.deepEqual(sent.filter((type) => type === "startServer"), ["startServer"]);
  assert.equal(popup.startFlow.state, "waiting");
  // The store id's origin exactly: an id that merely contains it is another install.
  for (const url of ["chrome-extension://aecenifonpkaiccmmknpbllbebbfigjnm/", "chrome-extension://ecenifonpkaiccmmknpbllbebbfigjnma/"]) {
    assert.equal(loadPopup(() => offline, url).START_AVAILABLE, false, url);
  }
});

// Both browsers demand the user gesture for the prompt, and the answer decides: a refusal ends on
// the hint without a word to the background, whose native host Chrome would not let it reach.
test("the click asks for nativeMessaging before its first await, and a refusal starts nothing", async () => {
  for (const url of ["moz-extension://test/", CHROME_STORE_URL]) {
    const sent = [];
    const popup = loadPopup((msg) => (sent.push(msg.type), offline), url, { grant: false });
    const click = popup.startServerFromPopup();
    assert.deepEqual(popup.permissionRequests, [{ permissions: ["nativeMessaging"] }], url);
    await click;
    assert.equal(popup.startFlow.state, "failed", url);
    assert.equal(popup.el("server-detail").textContent, popup.START_PERMISSION_HINT, url);
    assert.equal(popup.el("start-server").disabled, false, url);
    assert.deepEqual(sent.filter((type) => type === "startServer"), [], url);
  }
});

test("the server answering ends the wait and hides the button", async () => {
  let health = offline;
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, deadline: Date.now() + 50000 } : health));
  await popup.resumeStart();
  await popup.checkServer(false);
  assert.equal(popup.el("server-status").textContent, "Starting server");
  health = online;
  await popup.checkServer(false);
  assert.equal(popup.startFlow.state, "idle");
  assert.equal(popup.el("server-status").textContent, "Server online");
  assert.equal(popup.el("start-server").hidden, true);
});

test("the click sends one startServer and follows the deadline the background answers", async () => {
  const deadline = Date.now() + 90000;
  const sent = [];
  const popup = loadPopup((msg) => {
    sent.push(msg.type);
    if (msg.type === "startServer") return { ok: true, started: true, already: false, log: null, deadline };
    return offline;
  });
  await Promise.all([popup.startServerFromPopup(), popup.startServerFromPopup()]); // a double click
  assert.equal(sent.filter((type) => type === "startServer").length, 1);
  assert.equal(popup.startFlow.state, "waiting");
  assert.equal(popup.startFlow.deadline, deadline);
  assert.equal(popup.el("start-server").disabled, true);
});

test("a refusal from the background puts its reason and hint on the detail line", async () => {
  const popup = loadPopup((msg) => (msg.type === "startServer" ? { ok: false, error: "launcher not registered", hint: "Run setup" } : offline));
  await popup.startServerFromPopup();
  assert.equal(popup.startFlow.state, "failed");
  assert.equal(popup.el("server-detail").textContent, "launcher not registered. Run setup");
  assert.equal(popup.el("start-server").disabled, false);
});

// ------------------------------------------------------------------ updates

const LATEST = { version: "0.9.0", tag: "v0.9.0", url: "https://github.com/Multysquid/shisu-ko/releases/tag/v0.9.0", xpi: null };
const healthOf = (version, launcher) => ({ ok: true, data: { version, launcher, model: "large-v3", device: "cuda", compute_type: "float16" } });

// A background for the update flow and the form: /health from `state.health` (a function is
// called, so an answer can be held back), updateStatus judged from the /health answer the popup
// sends with the question (the way background.js does, in short), the settings from
// `state.settings` over the defaults with saveSettings merging its patch in, as background.js
// does, and writing it back to the popup's storage listener the way the browser would, and the
// other messages answered from `state` and recorded.
function updateBackground(state) {
  const sent = [];
  const answer = (msg) => {
    sent.push(msg);
    switch (msg.type) {
      case "api":
        return typeof state.health === "function" ? state.health() : state.health;
      case "getSettings":
        return Object.assign({}, DEFAULTS, state.settings || {});
      case "saveSettings": {
        if (state.failSave) throw new Error("storage write failed");
        state.settings = Object.assign({}, DEFAULTS, state.settings || {}, msg.settings || {});
        const settings = state.settings;
        // `onSave` plays the store's echo of the write. A promise it returns holds the reply: the
        // store notifies the popup before the background's set() resolves and the reply goes out.
        const echoed = state.onSave ? state.onSave(settings) : undefined;
        return echoed && typeof echoed.then === "function" ? echoed.then(() => settings) : settings;
      }
      case "startServerStatus":
        return { starting: false };
      case "updateStatus": {
        const health = msg.health;
        const server = health ? { version: typeof health.version === "string" ? health.version : null, launcher: typeof health.launcher === "boolean" ? health.launcher : null } : null;
        const latest = state.latest === undefined ? LATEST : state.latest;
        let verdict = "unknown";
        if (latest) {
          if (!server) verdict = "offline";
          else if (server.version === null) verdict = "unknown";
          else if (server.version >= latest.version) verdict = "current";
          else if (server.launcher === null) verdict = "behind";
          else verdict = server.launcher ? "newer" : "cannot";
        }
        return {
          latest,
          checkedAt: state.checkedAt === undefined ? Date.now() - 3 * 60000 : state.checkedAt,
          error: state.error || null,
          server,
          decision: { server: verdict, extension: state.extension || "current" },
          snoozed: state.snoozed || null,
          updating: state.updating || null,
          extensionVersion: "0.9.0",
        };
      }
      case "checkForUpdate":
        if (state.onCheck) state.onCheck();
        return { latest: state.latest === undefined ? LATEST : state.latest, checkedAt: Date.now(), error: state.error || null };
      case "updateServer":
        return typeof state.update === "function" ? state.update() : state.update;
      case "snoozeUpdate":
        state.snoozed = msg.version;
        return { ok: true };
      case "ankiDecks":
        return typeof state.anki === "function" ? state.anki() : state.anki || decksOffline(null);
      default:
        return offline;
    }
  };
  return { answer, sent, types: () => sent.map((m) => m.type) };
}

async function open(state, runtimeURL) {
  const bg = updateBackground(state);
  const popup = loadPopup(bg.answer, runtimeURL);
  await popup.resumeStart();
  await popup.checkServer(true);
  await settle();
  return { popup, bg, state };
}

test("a newer release than the server runs is offered in the banner", async () => {
  const { popup, bg } = await open({ health: healthOf("0.8.0", true) });
  assert.equal(popup.el("server-status").textContent, "Server online");
  assert.equal(popup.el("update-banner").hidden, false);
  assert.equal(popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0.");
  assert.equal(popup.el("update-now").hidden, false);
  assert.equal(popup.el("update-now").disabled, false);
  assert.equal(popup.el("update-now").textContent, "Update");
  assert.equal(popup.el("update-later").hidden, false);
  assert.equal(popup.el("update-release").hidden, true);
  // The first question of the popup asks for the day's check; the poll asks again only when the
  // server's answer changes.
  const asked = bg.sent.filter((m) => m.type === "updateStatus");
  assert.equal(asked.length, 1);
  assert.equal(asked[0].check, true);
  assert.deepEqual(asked[0].health, healthOf("0.8.0", true).data);
  await popup.checkServer(false);
  await settle();
  assert.equal(bg.sent.filter((m) => m.type === "updateStatus").length, 1);
  assert.equal(popup.el("update-result").textContent, "Newest release: 0.9.0, checked 3 min ago");
});

test("a server not started by the launcher gets the explanation and no Update button", async () => {
  const { popup } = await open({ health: healthOf("0.8.0", false) });
  assert.equal(popup.el("update-banner").hidden, false);
  assert.equal(popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0 and was not started by run.cmd / run.sh, so it cannot update itself; restart it by hand to update");
  assert.equal(popup.el("update-now").hidden, true);
  assert.equal(popup.el("update-later").hidden, false);
});

test("a server from before the launcher flag gets a banner that names the release and no Update button", async () => {
  // Every 0.8.0 server: /health carries the version and nothing about the launcher.
  const { popup } = await open({ health: { ok: true, data: { version: "0.8.0", model: "large-v3", device: "cuda", compute_type: "float16" } } });
  assert.equal(popup.el("server-status").textContent, "Server online");
  assert.equal(popup.el("update-banner").hidden, false);
  assert.equal(popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0, which cannot be updated from here; restart it by hand to update (run.cmd / run.sh update it at start)");
  assert.equal(popup.el("update-now").hidden, true);
  assert.equal(popup.el("update-release").hidden, true);
  assert.equal(popup.el("update-later").hidden, false);
});

test("an extension behind the release is sent to the release page", async () => {
  const { popup } = await open({ health: healthOf("0.9.0", true), extension: "newer", latest: { ...LATEST, xpi: "https://github.com/Multysquid/shisu-ko/releases/download/v0.9.0/shisu_ko-0.9.0.xpi" } });
  assert.equal(popup.el("update-text").textContent, "A newer extension (0.9.0) is on the release page (the addons.mozilla.org listing may get it later)");
  assert.equal(popup.el("update-now").hidden, true);
  assert.equal(popup.el("update-release").hidden, false);
  popup.openReleasePage();
  await settle();
  assert.deepEqual(popup.opened, [LATEST.url]);
});

// The release is there before its signed .xpi: Firefox is not sent to a page it cannot install
// from, an unpacked Chrome build (whose zip is there from the start) is, and is not told about a
// listing on addons.mozilla.org, which never updates a Chrome install.
test("an extension behind a release without its signed .xpi yet is told so, and not sent to the page in Firefox", async () => {
  const { popup } = await open({ health: healthOf("0.9.0", true), extension: "newer" });
  assert.equal(popup.el("update-text").textContent, "A newer extension (0.9.0) is out; its signed .xpi reaches the release page once addons.mozilla.org has signed it");
  assert.equal(popup.el("update-release").hidden, true);
  assert.equal(popup.el("update-later").hidden, false);
  const onChrome = await open({ health: healthOf("0.9.0", true), extension: "newer" }, "chrome-extension://test/");
  assert.equal(onChrome.popup.el("update-text").textContent, "A newer extension (0.9.0) is on the release page; an unpacked build does not update itself");
  assert.equal(onChrome.popup.el("update-release").hidden, false);
});

// Chrome updates a store install from the Chrome Web Store, after the store's review: the popup
// says so and offers no release page, whose unpacked build would be a second extension, under an
// id the launcher does not answer (no Start button there).
test("a Chrome Web Store install behind the release is told the store updates it, and not sent to the page", async () => {
  const TEXT = "A newer extension (0.9.0) is out; the Chrome Web Store updates this one once it has reviewed that version, which can take days after the GitHub release";
  for (const latest of [LATEST, { ...LATEST, xpi: "https://github.com/Multysquid/shisu-ko/releases/download/v0.9.0/shisu_ko-0.9.0.xpi" }]) {
    const { popup } = await open({ health: healthOf("0.9.0", true), extension: "newer", latest }, CHROME_STORE_URL);
    assert.equal(popup.el("update-banner").hidden, false);
    assert.equal(popup.el("update-text").textContent, TEXT);
    assert.equal(popup.el("update-now").hidden, true);
    assert.equal(popup.el("update-release").hidden, true);
    assert.equal(popup.el("update-later").hidden, false);
  }
  // Every server banner (newer, cannot, behind) comes first there as anywhere: the store says
  // nothing about the server, and a server that needs a restart by hand must still say so.
  const server = await open({ health: healthOf("0.8.0", true), extension: "newer" }, CHROME_STORE_URL);
  assert.equal(server.popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0.");
  assert.equal(server.popup.el("update-now").hidden, false);
  const cannot = await open({ health: healthOf("0.8.0", false), extension: "newer" }, CHROME_STORE_URL);
  assert.equal(cannot.popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0 and was not started by run.cmd / run.sh, so it cannot update itself; restart it by hand to update");
  assert.equal(cannot.popup.el("update-release").hidden, true);
  const behind = await open({ health: { ok: true, data: { version: "0.8.0", model: "large-v3", device: "cuda", compute_type: "float16" } }, extension: "newer" }, CHROME_STORE_URL);
  assert.equal(behind.popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0, which cannot be updated from here; restart it by hand to update (run.cmd / run.sh update it at start)");
  assert.equal(behind.popup.el("update-release").hidden, true);
  // An offline server has no line of its own (its next start updates it); the store's stays,
  // beside the Start button the store install has.
  const serverOff = await open({ health: offline, extension: "newer" }, CHROME_STORE_URL);
  assert.equal(serverOff.popup.el("server-status").textContent, "Server offline");
  assert.equal(serverOff.popup.el("start-server").hidden, false);
  assert.equal(serverOff.popup.el("update-text").textContent, TEXT);
  assert.equal(serverOff.popup.el("update-release").hidden, true);
  // "Not now" hides it for the session, as it does the release page's banner.
  const snoozed = await open({ health: healthOf("0.9.0", true), extension: "newer", snoozed: "0.9.0" }, CHROME_STORE_URL);
  assert.equal(snoozed.popup.el("update-banner").hidden, true);
  // Nothing newer: no banner at all.
  const current = await open({ health: healthOf("0.9.0", true) }, CHROME_STORE_URL);
  assert.equal(current.popup.el("update-banner").hidden, true);
});

test("no banner while the server is offline, and none for a server that says no version", async () => {
  const { popup } = await open({ health: offline });
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("update-banner").hidden, true);
  assert.equal(popup.el("update-result").textContent, "Newest release: 0.9.0, checked 3 min ago");
  // The smoke fixture: {model, device, compute_type} and nothing else.
  const fixture = await open({ health: online });
  assert.equal(fixture.popup.el("server-status").textContent, "Server online");
  assert.equal(fixture.popup.el("server-detail").textContent, "large-v3 · cuda · float16");
  assert.equal(fixture.popup.el("update-banner").hidden, true);
  // Nothing newer, and no release known at all.
  const current = await open({ health: healthOf("0.9.0", true) });
  assert.equal(current.popup.el("update-banner").hidden, true);
  const none = await open({ health: healthOf("0.9.0", true), latest: null });
  assert.equal(none.popup.el("update-banner").hidden, true);
  assert.equal(none.popup.el("update-result").textContent, "No release found, checked 3 min ago");
});

test("an answer overtaken by a later question is dropped, and the day's check it carried is asked for again", async () => {
  // The popup opens while the server is offline and the day's check is due, with GitHub slow;
  // the server comes online before GitHub answers, and the second question, answered from the
  // store at once, puts the banner up. The first answer, "offline", then lands and must not undo
  // it. But it is the only one that waited for the check, and the check found a newer release
  // than the store held: without a third question, answered from the store now that the check
  // is in it, the popup would show the old release and its stale date for as long as it lives,
  // since no further question is asked while the server's answer stays the same.
  const NEWER = { version: "0.9.1", tag: "v0.9.1", url: "https://github.com/Multysquid/shisu-ko/releases/tag/v0.9.1", xpi: null };
  const state = { health: offline, latest: LATEST, checkedAt: Date.now() - 2 * 86400000 };
  const bg = updateBackground(state);
  let releaseFirst;
  const held = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const questions = []; // the updateStatus questions in the order they were asked
  const popup = loadPopup(async (msg) => {
    if (msg.type === "updateStatus") questions.push(msg);
    if (msg.type === "updateStatus" && msg.check) {
      await held;
      // runCheck() in background.js writes the store before the answer goes out.
      state.latest = NEWER;
      state.checkedAt = Date.now();
    }
    return bg.answer(msg);
  });
  await popup.resumeStart();
  await popup.checkServer(true);
  await settle();
  assert.equal(popup.el("update-banner").hidden, true);
  state.health = healthOf("0.8.0", true);
  await popup.checkServer(false);
  await settle();
  assert.equal(popup.el("update-banner").hidden, false);
  assert.equal(popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0.");
  assert.equal(popup.el("update-result").textContent, "Newest release: 0.9.0, checked 2 days ago");
  releaseFirst();
  await settle();
  await settle();
  await settle();
  assert.equal(popup.el("update-banner").hidden, false, "the late answer is dropped");
  assert.equal(popup.el("update-now").hidden, false);
  assert.deepEqual(questions.map((m) => m.check), [true, false, false], "the check's answer is asked for again, from the store");
  assert.deepEqual(JSON.parse(JSON.stringify(questions[2].health)), healthOf("0.8.0", true).data);
  assert.equal(popup.el("update-text").textContent, "Shisu-ko 0.9.1 is available — the server runs 0.8.0.");
  assert.equal(popup.el("update-result").textContent, "Newest release: 0.9.1, checked just now");
  // The same server again asks nothing more, and the banner is still up.
  await popup.checkServer(false);
  await settle();
  assert.equal(questions.length, 3);
  assert.equal(popup.el("update-banner").hidden, false);
});

test("Not now hides the banner for the session and tells the background", async () => {
  const { popup, bg, state } = await open({ health: healthOf("0.8.0", true) });
  await popup.snoozeUpdateFromPopup();
  assert.equal(popup.el("update-banner").hidden, true);
  assert.deepEqual(JSON.parse(JSON.stringify(bg.sent.filter((m) => m.type === "snoozeUpdate"))), [{ type: "snoozeUpdate", version: "0.9.0" }]);
  assert.equal(state.snoozed, "0.9.0");
  // A reopened popup is told the same and shows nothing.
  const again = await open(state);
  assert.equal(again.popup.el("update-banner").hidden, true);
});

test("Update asks the background, then watches /health until the new version answers", async () => {
  const state = { health: healthOf("0.8.0", true) };
  const { popup, bg } = await open(state);
  const requestedAt = Date.now();
  state.update = { ok: true, restarting: true, from: "0.8.0", to: "0.9.0", requestedAt, deadline: requestedAt + 120000 };
  await popup.updateServerFromPopup();
  assert.equal(bg.types().filter((t) => t === "updateServer").length, 1);
  assert.equal(popup.updateFlow.state, "updating");
  assert.equal(popup.el("server-status").textContent, "Updating server");
  assert.equal(popup.el("server-status").className, "badge");
  assert.equal(popup.el("server-detail").textContent, "Restarting with 0.9.0…");
  assert.equal(popup.el("update-banner").hidden, false, "the banner stays while the update runs");
  assert.equal(popup.el("update-now").disabled, true);
  assert.equal(popup.el("update-now").textContent, "Updating…");
  assert.equal(popup.el("update-later").hidden, true);
  assert.equal(popup.el("start-server").hidden, true);
  // The old server still answers for a moment, then nobody does, then the new one.
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "updating");
  state.health = offline;
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "updating");
  assert.equal(popup.updateFlow.down, true);
  assert.equal(popup.el("server-status").textContent, "Updating server");
  state.health = healthOf("0.9.0", true);
  await popup.checkServer(false);
  await settle();
  assert.equal(popup.updateFlow.state, "done");
  assert.equal(popup.el("server-status").textContent, "Server online");
  assert.equal(popup.el("server-status").className, "badge ok");
  assert.equal(popup.el("server-detail").textContent, "Updated to 0.9.0");
  assert.equal(popup.el("update-banner").hidden, true, "nothing is newer any more");
});

test("the old version back after the restart says update.py could not update, and the banner stays", async () => {
  const state = { health: healthOf("0.8.0", true) };
  const { popup } = await open(state);
  const requestedAt = Date.now();
  state.update = { ok: true, restarting: true, from: "0.8.0", to: "0.9.0", requestedAt, deadline: requestedAt + 120000 };
  await popup.updateServerFromPopup();
  state.health = offline;
  await popup.checkServer(false);
  state.health = healthOf("0.8.0", true);
  await popup.checkServer(false);
  await settle();
  assert.equal(popup.updateFlow.state, "stale");
  assert.equal(popup.el("server-status").textContent, "Server online");
  assert.equal(popup.el("server-detail").textContent, "The server restarted but still runs 0.8.0; look at its window: update.py said why");
  assert.equal(popup.el("update-banner").hidden, false);
  assert.equal(popup.el("update-now").disabled, false);
});

test("an old version long after the request counts as the restarted server even when no poll saw it down", async () => {
  const state = { health: healthOf("0.8.0", true) };
  const { popup } = await open(state);
  const requestedAt = Date.now() - 11000;
  state.update = { ok: true, restarting: true, from: "0.8.0", to: "0.9.0", requestedAt, deadline: requestedAt + 120000 };
  await popup.updateServerFromPopup();
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "stale");
  assert.equal(popup.el("server-detail").textContent, popup.stillOldHint("0.8.0"));
});

test("past the deadline without an answer the detail points at the log and the button is back", async () => {
  const state = { health: healthOf("0.8.0", true) };
  const { popup } = await open(state);
  state.update = { ok: true, restarting: true, from: "0.8.0", to: "0.9.0", requestedAt: Date.now(), deadline: Date.now() - 1 };
  await popup.updateServerFromPopup();
  state.health = offline;
  await popup.checkServer(false);
  await settle();
  assert.equal(popup.updateFlow.state, "lost");
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("server-detail").textContent, popup.UPDATE_LOST_HINT);
  assert.match(popup.UPDATE_LOST_HINT, /120 s/);
  assert.equal(popup.el("start-server").hidden, false);
  assert.equal(popup.el("start-server").disabled, false);
  assert.equal(popup.el("update-banner").hidden, true, "offline: the next start updates");
});

test("a refusal from the server shows its reason in the banner without the Update button", async () => {
  const state = { health: healthOf("0.8.0", true), update: { ok: false, error: "started with --no-update", refused: true, offline: false } };
  const { popup } = await open(state);
  await popup.updateServerFromPopup();
  assert.equal(popup.updateFlow.state, "failed");
  assert.equal(popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0. The server cannot update itself: started with --no-update");
  assert.equal(popup.el("update-now").hidden, true);
  assert.equal(popup.el("server-status").textContent, "Server online");
  // A request that did not get through keeps the button for another try.
  const gone = await open({ health: healthOf("0.8.0", true), update: { ok: false, error: "Server unreachable", refused: false, offline: true } });
  await gone.popup.updateServerFromPopup();
  assert.equal(gone.popup.el("update-text").textContent, "Shisu-ko 0.9.0 is available — the server runs 0.8.0. The update failed: Server unreachable");
  assert.equal(gone.popup.el("update-now").hidden, false);
  assert.equal(gone.popup.el("update-now").disabled, false);
});

test("a reopened popup resumes an update the background reports", async () => {
  const requestedAt = Date.now() - 5000;
  const state = { health: offline, updating: { from: "0.8.0", to: "0.9.0", requestedAt, deadline: requestedAt + 120000, down: true } };
  const { popup } = await open(state);
  assert.equal(popup.updateFlow.state, "updating");
  assert.equal(popup.updateFlow.down, true);
  assert.equal(popup.el("server-status").textContent, "Updating server");
  assert.equal(popup.el("server-detail").textContent, "Restarting with 0.9.0…");
  assert.equal(popup.el("start-server").hidden, true);
  state.health = healthOf("0.9.0", true);
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "done");
});

// The day's check failed (GitHub blackholed) but kept the release, so the banner offered Update;
// the viewer clicked it and reopened the popup while update.py runs. The first updateStatus
// answer now waits ten seconds on GitHub; the record must reach the popup before that, with the
// startServerStatus answer, or its first paint offers a start on top of the launcher's restart.
test("a reopened popup learns of an update from startServerStatus, before a slow first updateStatus", async () => {
  const requestedAt = Date.now() - 5000;
  const updating = { from: "0.8.0", to: "0.9.0", requestedAt, deadline: requestedAt + 120000, down: false };
  const state = { health: offline, updating };
  const bg = updateBackground(state);
  let releaseFirst;
  const held = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const popup = loadPopup(async (msg) => {
    if (msg.type === "startServerStatus") return { starting: false, updating };
    const answer = bg.answer(msg);
    if (msg.type === "updateStatus" && msg.check) await held;
    return answer;
  });
  await popup.resumeStart();
  assert.equal(popup.updateFlow.state, "updating");
  assert.equal(popup.el("server-status").textContent, "Updating server");
  await popup.checkServer(popup.startFlow.state === "idle" && popup.updateFlow.state === "idle");
  await settle();
  assert.equal(popup.el("server-status").textContent, "Updating server");
  assert.equal(popup.el("server-detail").textContent, "Restarting with 0.9.0…");
  assert.equal(popup.el("start-server").hidden, true);
  assert.equal(popup.updateFlow.down, true);
  await popup.startServerFromPopup();
  assert.equal(bg.types().includes("startServer"), false, "no start while the update runs");
  // The slow answer lands and changes nothing; the new version then ends the update.
  releaseFirst();
  await settle();
  await settle();
  assert.equal(popup.updateFlow.state, "updating");
  assert.equal(popup.el("start-server").hidden, true);
  state.health = healthOf("0.9.0", true);
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "done");
  assert.equal(popup.el("server-detail").textContent, "Updated to 0.9.0");
});

test("a start under way disables Update, and an update hides Start", async () => {
  const state = { health: offline, latest: LATEST };
  const bg = updateBackground(state);
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, deadline: Date.now() + 50000 } : bg.answer(msg)));
  await popup.resumeStart();
  await popup.checkServer(false);
  await settle();
  assert.equal(popup.startFlow.state, "waiting");
  // Offline shows no banner; the button's state is still kept right for the moment it shows.
  assert.equal(popup.el("update-now").disabled, true);
  await popup.updateServerFromPopup();
  assert.equal(bg.types().includes("updateServer"), false, "no update while a start is under way");
});

test("Check for updates asks for a check now and shows the result, or the failure", async () => {
  let checks = 0;
  const state = { health: healthOf("0.9.0", true), onCheck: () => checks++ };
  const { popup, bg } = await open(state);
  const before = bg.sent.length;
  await popup.checkForUpdatesFromPopup();
  assert.equal(checks, 1);
  assert.deepEqual(bg.sent.slice(before).map((m) => m.type), ["checkForUpdate", "updateStatus"]);
  assert.equal(bg.sent[before].force, true);
  assert.equal(bg.sent[before + 1].check, false);
  assert.equal(popup.el("check-updates").disabled, false);
  assert.equal(popup.el("update-result").textContent, "Newest release: 0.9.0, checked 3 min ago");
  assert.equal(popup.el("update-result").className, "hint");
  state.error = "could not reach GitHub";
  await popup.checkForUpdatesFromPopup();
  assert.equal(popup.el("update-result").textContent, "Update check failed: could not reach GitHub (last seen: 0.9.0)");
  assert.equal(popup.el("update-result").className, "hint warn");
});

// ------------------------------------------------------------------ the form

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The popup as init() leaves it: the form filled from `state.settings`, every listener in place,
// the first /health answered. `saves()` lists the patches the background got, in order.
async function openForm(state, opts) {
  const bg = updateBackground(state);
  const popup = loadPopup(bg.answer, undefined, opts);
  state.onSave = popup.fireStorage;
  await popup.init();
  await settle();
  // The patches come out of the sandbox; a JSON round trip makes them deepEqual's own objects.
  const saves = () => JSON.parse(JSON.stringify(bg.sent.filter((m) => m.type === "saveSettings").map((m) => m.settings)));
  // A write by another writer (the content script, the other copy of this form): the store
  // changes and the popup's storage listener hears of it.
  const elsewhere = (patch) => {
    state.settings = Object.assign({}, state.settings, patch);
    popup.fireStorage(Object.assign({}, DEFAULTS, state.settings));
  };
  return { popup, bg, state, saves, elsewhere };
}

test("an edit saves the field it changed and nothing else, and a change made elsewhere lands in the form", async () => {
  const state = { health: offline, settings: {} };
  const { popup, saves, elsewhere } = await openForm(state);
  assert.equal(popup.storageListeners.length, 1);
  assert.equal(popup.el("enabled").checked, true);
  assert.equal(popup.el("enabled-label").textContent, "On");
  assert.equal(popup.el("fontScaleOut").textContent, "100%");
  // Alt+Shift+S and Alt+Shift+L on the video, and a model chosen in the other copy of this form.
  elsewhere({ enabled: false, showTranscript: true, model: "large-v3-turbo" });
  assert.equal(popup.el("enabled").checked, false);
  assert.equal(popup.el("enabled-label").textContent, "Off");
  assert.equal(popup.el("showTranscript").checked, true);
  assert.equal(popup.el("model").value, "large-v3-turbo");
  // Ticking a box saves that box, and puts nothing back.
  popup.el("subOutline").checked = true;
  popup.el("subOutline").dispatch("input");
  popup.el("fontScale").value = 1.2;
  popup.el("fontScale").dispatch("input");
  assert.equal(popup.el("fontScaleOut").textContent, "120%");
  assert.deepEqual(saves(), [], "the save is debounced");
  await wait(200);
  assert.deepEqual(saves(), [{ subOutline: true, fontScale: 1.2 }]);
  assert.equal(state.settings.enabled, false);
  assert.equal(state.settings.showTranscript, true);
  assert.equal(state.settings.model, "large-v3-turbo");
  assert.equal(state.settings.subOutline, true);
  // The save's own echo changed nothing, and a second edit starts a new patch.
  assert.equal(popup.el("fontScale").value, 1.2);
  popup.el("subOutline").checked = false;
  popup.el("subOutline").dispatch("input");
  await wait(200);
  assert.deepEqual(saves()[1], { subOutline: false });
});

test("a change from elsewhere leaves a field with an edit of its own alone", async () => {
  const state = { health: offline, settings: {} };
  const { popup, saves, elsewhere } = await openForm(state);
  const family = popup.el("subFontFamily");
  const slider = popup.el("fontScale");
  const box = popup.el("subOutline");
  popup.document.activeElement = family;
  family.value = "Yu Go"; // being typed
  family.dispatch("input");
  slider.value = 1.4; // waiting for its save
  slider.dispatch("input");
  popup.document.activeElement = box; // a checkbox keeps the focus, and is not being typed in
  elsewhere({ subFontFamily: "Meiryo", fontScale: 0.8, lingerSeconds: 1, subOutline: true });
  assert.equal(family.value, "Yu Go");
  assert.equal(slider.value, 1.4);
  assert.equal(popup.el("lingerSeconds").value, 1);
  assert.equal(popup.el("lingerSecondsOut").textContent, "1.0 s");
  assert.equal(box.checked, true);
  await wait(200);
  assert.deepEqual(saves(), [{ subFontFamily: "Yu Go", fontScale: 1.4 }]);
  assert.equal(state.settings.lingerSeconds, 1);
  assert.equal(state.settings.fontScale, 1.4);
  // The model field is being typed in when a change lands: the typing stays, the rest lands.
  const model = popup.el("model");
  popup.document.activeElement = model;
  model.value = "large-v3-tur";
  elsewhere({ model: "small", clipFormat: "wav" });
  assert.equal(model.value, "large-v3-tur");
  assert.equal(popup.el("clipFormat").value, "wav");
});

// The flush clears `dirty` as it sends, and the store's echo of that write is one storage round
// trip away. An echo of an earlier write landing in between (another writer's save, queued in the
// background just before the popup's; this form's own previous save, still being written when the
// next edit was flushed) carries the value from before the edit: the field waits for its own echo
// instead of being put back for a round trip. A write that failed has no echo coming; its reply
// ends the wait.
test("a field keeps the value it sent until the store echoes it, and an earlier write's echo does not put it back", async () => {
  const state = { health: offline, settings: {} };
  const { popup, saves } = await openForm(state);
  // Every write from here on is held: its echo and, behind it, its reply.
  const held = [];
  state.onSave = (settings) => new Promise((release) => held.push({ settings: Object.assign({}, settings), release }));
  const echo = (i) => {
    popup.fireStorage(held[i].settings);
    held[i].release();
  };
  const slider = popup.el("fontScale");
  const out = popup.el("fontScaleOut");
  // Alt+Shift+S on the video, its save written just before the popup's: its echo still carries
  // the slider from before the drag.
  state.settings = Object.assign({}, DEFAULTS, state.settings, { enabled: false });
  held.push({ settings: Object.assign({}, state.settings), release: () => {} });
  slider.value = 1.4;
  slider.dispatch("input");
  await wait(200);
  assert.deepEqual(saves(), [{ fontScale: 1.4 }]);
  assert.equal(held.length, 2, "the flush wrote, and its echo is on its way");
  assert.equal(state.settings.fontScale, 1.4);
  echo(0);
  assert.equal(slider.value, 1.4, "the earlier write's echo does not put the slider back");
  assert.equal(out.textContent, "140%");
  assert.equal(popup.el("enabled").checked, false, "what that write changed lands");
  echo(1);
  assert.equal(slider.value, 1.4);
  assert.equal(out.textContent, "140%");
  await settle();
  // Its own echo ended the wait: the next change from elsewhere lands.
  state.settings = Object.assign({}, state.settings, { fontScale: 0.8 });
  popup.fireStorage(Object.assign({}, state.settings));
  assert.equal(slider.value, 0.8);
  assert.equal(out.textContent, "80%");
  // This form's own previous save, still being written when the next edit is flushed.
  slider.value = 1.2;
  slider.dispatch("input");
  await wait(200);
  slider.value = 1.4;
  slider.dispatch("input");
  await wait(200);
  assert.deepEqual(saves().slice(1), [{ fontScale: 1.2 }, { fontScale: 1.4 }]);
  assert.equal(held.length, 4);
  echo(2);
  await settle();
  assert.equal(slider.value, 1.4, "the echo of the previous save does not put the slider back");
  assert.equal(out.textContent, "140%");
  echo(3);
  assert.equal(slider.value, 1.4);
  await settle();
  state.settings = Object.assign({}, state.settings, { fontScale: 1 });
  popup.fireStorage(Object.assign({}, state.settings));
  assert.equal(slider.value, 1, "the wait ended with the echo of the last save");
  // A write that fails has no echo: the reply ends the wait, and the next change lands.
  const box = popup.el("subOutline");
  state.failSave = true;
  box.checked = true;
  box.dispatch("input");
  await wait(200);
  assert.deepEqual(saves().slice(3), [{ subOutline: true }]);
  assert.equal(held.length, 4, "nothing was written");
  await settle();
  state.failSave = false;
  state.settings = Object.assign({}, state.settings, { subOutline: false });
  popup.fireStorage(Object.assign({}, state.settings));
  assert.equal(box.checked, false, "the failed write's reply ended the wait");
});

// The popup closes with a click outside it; the text field's change event fires on that blur and
// the document is gone within the millisecond, before the debounce could run.
test("a text edit still on its way when the popup closes is saved from pagehide, before anything is awaited", async () => {
  const state = { health: offline, settings: {} };
  const { popup, saves } = await openForm(state);
  const field = popup.el("ankiSentenceField");
  assert.deepEqual([...field.listeners.keys()], ["change"], "a text field saves once the edit is done");
  field.value = "Sentence";
  field.dispatch("change");
  assert.deepEqual(saves(), [], "the debounce is running");
  popup.window.dispatch("pagehide");
  assert.deepEqual(saves(), [{ ankiSentenceField: "Sentence" }], "sent synchronously");
  await wait(200);
  assert.deepEqual(saves(), [{ ankiSentenceField: "Sentence" }], "and not again by the timer");
  assert.equal(state.settings.ankiSentenceField, "Sentence");
  // A page put out of view (the options page's tab) saves the same way.
  popup.el("serverUrl").value = "http://127.0.0.1:8791";
  popup.el("serverUrl").dispatch("change");
  popup.document.hidden = true;
  popup.document.dispatch("visibilitychange");
  assert.deepEqual(saves()[1], { serverUrl: "http://127.0.0.1:8791" });
  await wait(200);
  assert.equal(saves().length, 2);
});

// The viewer's own known words and the katakana switch: two settings like any other, the list a
// textarea that saves once the edit is done (a save recolours every tab's lines), stored one word
// per line with the blanks and the spaces out, the way the content script reads it.
test("the known words and the katakana switch load into the form, and save trimmed, one word per line", async () => {
  const state = { health: offline, settings: { knownWords: "食べる\n日本語", katakanaKnown: true } };
  const { popup, saves, elsewhere } = await openForm(state);
  const list = popup.el("knownWords");
  assert.equal(list.type, "textarea");
  assert.equal(list.value, "食べる\n日本語");
  assert.equal(popup.el("katakanaKnown").checked, true);
  assert.deepEqual([...list.listeners.keys()], ["change"], "the list saves once the edit is done, not per keystroke");
  list.value = "  食べる  \n\n 東京駅\n日本語\n   \n";
  list.dispatch("change");
  popup.el("katakanaKnown").checked = false;
  popup.el("katakanaKnown").dispatch("input");
  await wait(200);
  assert.deepEqual(saves(), [{ knownWords: "食べる\n東京駅\n日本語", katakanaKnown: false }]);
  assert.equal(state.settings.knownWords, "食べる\n東京駅\n日本語");
  // Alt+Shift+K on the video added a word: it lands in the list, unless the list is being typed in.
  elsewhere({ knownWords: "食べる\n東京駅\n日本語\n来る" });
  assert.equal(list.value, "食べる\n東京駅\n日本語\n来る");
  popup.document.activeElement = list;
  list.value = "食べる\n東京";
  elsewhere({ knownWords: "食べる" });
  assert.equal(list.value, "食べる\n東京", "a list being typed in keeps its typing");
  popup.document.activeElement = null;
  // The legend names the blue of a place name or Latin text beside the four card states, and says
  // it is a switch.
  assert.match(HTML, /<span class="sw proper"><\/span>names and Latin text, when switched on/);
  assert.ok(cssDeclarations(".sw.proper,\n.sw.heiban").includes("background: #4da3ff"));
});

// The options page lives for hours, and the focus stays in the list it was last clicked into
// while the viewer is on YouTube marking words with Alt+Shift+K. The focus alone is no typing:
// those marks land in the list, and the viewer's next edit saves them with it instead of
// putting the list from before them back.
test("a focused known-words list nobody is typing in takes the marks made on the video, and its next edit keeps them", async () => {
  const state = { health: offline, settings: { knownWords: "食べる" } };
  const { popup, saves, elsewhere } = await openForm(state);
  const list = popup.el("knownWords");
  popup.document.activeElement = list; // clicked into, then off to the video
  elsewhere({ knownWords: "食べる\n猫" });
  elsewhere({ knownWords: "食べる\n猫\n犬" });
  assert.equal(list.value, "食べる\n猫\n犬");
  // Back in the tab, one word typed at the end, and the field left: the whole list is saved.
  list.value = "食べる\n猫\n犬\n鳥";
  // Typing now: a mark landing meanwhile waits, the typing stays.
  elsewhere({ knownWords: "食べる\n猫\n犬\n馬" });
  assert.equal(list.value, "食べる\n猫\n犬\n鳥");
  list.dispatch("change");
  await wait(200);
  assert.deepEqual(saves(), [{ knownWords: "食べる\n猫\n犬\n鳥" }]);
  assert.equal(state.settings.knownWords, "食べる\n猫\n犬\n鳥");
  // What was sent is the list as the field holds it: still focused, the next mark lands again.
  elsewhere({ knownWords: "食べる\n猫\n犬\n鳥\n魚" });
  assert.equal(list.value, "食べる\n猫\n犬\n鳥\n魚");
  // A model name the field was given is no typing either (the same rule for every text field).
  const model = popup.el("model");
  popup.document.activeElement = model;
  elsewhere({ model: "small" });
  assert.equal(model.value, "small");
  popup.document.activeElement = null;
});

// The particle switch: off for a viewer who never touched it, a checkbox like any other, above the
// katakana one. The legend no longer promises green particles; the switch says it.
test("the particle switch loads unchecked by default, sits above the katakana one, and saves when ticked", async () => {
  const state = { health: offline, settings: {} };
  const { popup, saves, elsewhere } = await openForm(state);
  const box = popup.el("particlesKnown");
  assert.equal(box.type, "checkbox");
  assert.equal(box.checked, false);
  assert.equal(popup.el("katakanaKnown").checked, false);
  box.checked = true;
  box.dispatch("input");
  await wait(200);
  assert.deepEqual(saves(), [{ particlesKnown: true }]);
  assert.equal(state.settings.particlesKnown, true);
  // Unticked again in the other copy of the form: it lands here.
  elsewhere({ particlesKnown: false });
  assert.equal(box.checked, false);
  assert.match(HTML, /<input type="checkbox" id="particlesKnown">\s*<span>Particles count as known<\/span>/);
  assert.ok(HTML.indexOf('id="particlesKnown"') < HTML.indexOf('id="katakanaKnown"'));
  assert.doesNotMatch(HTML, /particles green/);
});

// The name switch: off for a viewer who never touched it, a checkbox like any other, after the
// katakana one.
test("the name switch loads unchecked by default, sits after the katakana one, and saves when ticked", async () => {
  const state = { health: offline, settings: {} };
  const { popup, saves, elsewhere } = await openForm(state);
  const box = popup.el("properNames");
  assert.equal(box.type, "checkbox");
  assert.equal(box.checked, false);
  box.checked = true;
  box.dispatch("input");
  await wait(200);
  assert.deepEqual(saves(), [{ properNames: true }]);
  assert.equal(state.settings.properNames, true);
  // Unticked again in the other copy of the form: it lands here.
  elsewhere({ properNames: false });
  assert.equal(box.checked, false);
  assert.match(HTML, /<input type="checkbox" id="properNames">\s*<span>Names and Latin text in blue<\/span>/);
  assert.ok(HTML.indexOf('id="katakanaKnown"') < HTML.indexOf('id="properNames"'));
});

// What colours without a card sits in a drawer of the Word colours section, closed like every
// other drawer: the particle, katakana and name switches and the known-words list with its hint.
// The section itself keeps the card colours, the deck and the pitch accent.
test("the switches that colour without a card and the known words sit in a closed drawer of the Word colours section", () => {
  const section = HTML.slice(HTML.indexOf("<h2>Word colours</h2>"), HTML.indexOf("<h2>Display</h2>"));
  const open = section.indexOf("<details>");
  const close = section.indexOf("</details>");
  assert.ok(open > 0 && close > open, "the section holds a drawer");
  assert.match(section.slice(open, close), /^<details>\s*<summary>More word colour options<\/summary>\s*<div class="drawer">/);
  const drawer = section.slice(open, close);
  for (const id of ["particlesKnown", "katakanaKnown", "properNames", "knownWords"]) {
    assert.ok(drawer.includes(`id="${id}"`), `${id} is in the drawer`);
    assert.equal(HTML.split(`id="${id}"`).length, 2, `${id} appears once`);
  }
  assert.match(drawer, /<p class="hint">Drawn as learned whatever their card says/);
  for (const id of ["cardStatus", "cardStatusDeck", "deck-hint", "pitchAccent"]) {
    const at = section.indexOf(`id="${id}"`);
    assert.ok(at > 0 && at < open, `${id} stays in the section, above the drawer`);
  }
  // Closed by default, and no script opens it: the drawers keep no state.
  assert.doesNotMatch(section.slice(open, open + 20), /open/);
  assert.doesNotMatch(POPUP_JS, /\bdetails\b|\.open\s*=/);
  // Nested in a section, the drawer takes the section's padding and line rather than its own.
  assert.ok(cssDeclarations("section details").includes("border-bottom: none"));
});

// The mousedown on the button blurs the text field, whose change event starts the 150 ms save;
// the click lands well inside them.
test("Reset style takes an edit still on its way with it, and runs the check it asked for", async () => {
  const state = { health: offline, settings: { subPosition: 30, subFont: "mincho" } };
  const { popup, bg, saves } = await openForm(state);
  assert.equal(popup.el("subPosition").value, 30);
  const health = () => bg.sent.filter((m) => m.type === "api" && m.path === "/health").length;
  const before = health();
  const pending = [];
  state.health = () => new Promise((resolve) => pending.push(resolve));
  const field = popup.el("serverUrl");
  field.value = "http://127.0.0.1:8791";
  field.dispatch("change");
  await popup.resetStyle();
  assert.equal(popup.el("subPosition").value, 11);
  assert.equal(popup.el("subPositionOut").textContent, "11%");
  assert.deepEqual(saves(), [
    { subPosition: 11, subFont: "default", subFontFamily: "", subTextColor: "#ffffff", subBackgroundOpacity: 72, subOutline: false, transcriptSide: "right", serverUrl: "http://127.0.0.1:8791" },
  ]);
  assert.equal(state.settings.serverUrl, "http://127.0.0.1:8791");
  assert.equal(state.settings.subFont, "default");
  assert.equal(health(), before + 1, "the new address is checked, and the check starts over");
  assert.equal(popup.el("server-status").textContent, "Checking server");
  pending[0](offline);
  await settle();
  assert.equal(popup.el("server-status").textContent, "Server offline");
  await wait(200);
  assert.equal(saves().length, 1, "the debounced save was folded in, not run as well");
});

// A mistyped address holds a /health request open for the request timeout; the corrected one
// must not wait behind it, and the old address's answer is not this server's.
test("a first check overtakes a refresh the old address holds up, and drops its answer", async () => {
  const pending = [];
  const state = { health: () => new Promise((resolve) => pending.push(resolve)) };
  const bg = updateBackground(state);
  const popup = loadPopup(bg.answer);
  const refresh = popup.checkServer(false); // the interval's tick, to the old address
  assert.equal(pending.length, 1);
  const first = popup.checkServer(true); // the corrected address, after its save
  assert.equal(pending.length, 2, "asked at once");
  assert.equal(popup.el("server-status").textContent, "Checking server");
  pending[0]({ ok: true, data: { model: "tiny", device: "cpu", compute_type: "int8" } }); // the old address, at last
  await refresh;
  await settle();
  assert.equal(popup.el("server-status").textContent, "Checking server", "the old address's answer is dropped");
  pending[1](offline);
  await first;
  await settle();
  assert.equal(popup.el("server-status").textContent, "Server offline");
  // A refresh still waits for the request under way, and the next one goes out after it.
  const again = popup.checkServer(false);
  const more = popup.checkServer(false);
  assert.equal(pending.length, 3);
  pending[2](healthOf("0.9.0", true));
  await again;
  await more;
  await settle();
  assert.equal(popup.el("server-status").textContent, "Server online");
  const later = popup.checkServer(false);
  assert.equal(pending.length, 4);
  pending[3](offline);
  await later;
  assert.equal(popup.el("server-status").textContent, "Server offline");
});

// updateOutputs() runs for every event of every control; the font probe (a canvas, a context and
// six measurements) and the outputs' text must not run with it.
test("the font probe runs once per name, and a slider drag redraws neither the sample nor an unchanged output", async () => {
  const state = { health: offline, settings: { subFontFamily: "Meiryo" } };
  const { popup, saves } = await openForm(state, { installedFonts: ["Meiryo"] });
  const sample = popup.el("font-sample");
  const hint = popup.el("font-hint");
  assert.equal(popup.canvas.canvases, 1);
  assert.equal(hint.textContent, "Meiryo is installed on this computer");
  assert.match(sample.style.fontFamily, /^"Meiryo", "Noto Sans JP"/);
  assert.equal(sample.style.fontWeight, "400");
  const slider = popup.el("fontScale");
  const linger = popup.el("lingerSecondsOut");
  assert.equal(linger.textContent, "0.3 s");
  const writes = linger.writes;
  for (let i = 1; i <= 100; i++) {
    slider.value = 1 + i * 0.01;
    slider.dispatch("input");
  }
  assert.equal(popup.el("fontScaleOut").textContent, "200%");
  assert.equal(slider.style.getPropertyValue("--fill"), `${((2 - 0.6) / (2.2 - 0.6)) * 100}%`);
  assert.equal(popup.canvas.canvases, 1, "no probe for a slider position");
  assert.equal(popup.canvas.contexts, 1);
  assert.equal(linger.writes, writes, "an unchanged output is left alone");
  assert.equal(hint.textContent, "Meiryo is installed on this computer");
  // A name is probed once, however often it is typed; the sample and the hint follow every change.
  const family = popup.el("subFontFamily");
  family.value = "Klee";
  family.dispatch("input");
  assert.equal(popup.canvas.canvases, 2);
  assert.equal(hint.textContent, "Klee was not found on this computer; the preset is used");
  assert.equal(hint.className, "hint warn");
  assert.match(sample.style.fontFamily, /^"Klee", "Noto Sans JP"/);
  family.value = "Meiryo";
  family.dispatch("input");
  family.value = "Klee";
  family.dispatch("input");
  assert.equal(popup.canvas.canvases, 2);
  assert.equal(hint.textContent, "Klee was not found on this computer; the preset is used");
  const preset = popup.el("subFont");
  preset.value = "mincho";
  preset.dispatch("change");
  assert.match(sample.style.fontFamily, /^"Klee", "Noto Serif JP".*serif$/);
  family.value = "";
  family.dispatch("input");
  assert.equal(hint.textContent, "Leave empty to use the preset.");
  assert.doesNotMatch(sample.style.fontFamily, /Klee/);
  assert.equal(popup.canvas.canvases, 2);
  await wait(200);
  assert.deepEqual(saves(), [{ fontScale: 2, subFontFamily: "", subFont: "mincho" }]);
});

// popup.html is the options page too, alive in a tab for as long as the tab is: the poll is for
// a status line somebody looks at.
test("the poll skips a page out of view and catches up when it is back", async () => {
  const state = { health: offline, settings: {} };
  const { popup, bg } = await openForm(state);
  assert.equal(popup.intervals.length, 2, "the health refresh and the deck retry");
  assert.equal(popup.intervals[0].ms, 2000);
  const polls = () => bg.sent.filter((m) => m.type === "api" && m.path === "/health").length;
  const before = polls();
  popup.intervals[0].fn();
  await settle();
  assert.equal(polls(), before + 1);
  popup.document.hidden = true;
  popup.document.dispatch("visibilitychange");
  for (let i = 0; i < 10; i++) popup.intervals[0].fn();
  await settle();
  assert.equal(polls(), before + 1, "nothing while out of view");
  popup.document.hidden = false;
  popup.document.dispatch("visibilitychange");
  await settle();
  assert.equal(polls(), before + 2, "one check on return");
  popup.intervals[0].fn();
  await settle();
  assert.equal(polls(), before + 3);
});

// Every tick of the poll paints the status line again, and nothing on it changes while the
// server's answer does not: a text set to its own value is left alone (setText), and so must be
// the model field's placeholder, since an attribute set to its own value is a mutation all the
// same; and the update banner is only painted again when the background was asked something.
test("an unchanged /health answer is painted once per tick and rewrites no placeholder", async () => {
  const data = { version: "0.9.0", launcher: true, default_model: "large-v3", models: ["large-v3", "small"], model: "large-v3", device: "cuda", compute_type: "float16" };
  const state = { health: { ok: true, data }, settings: {} };
  const { popup, bg } = await openForm(state);
  const model = popup.el("model");
  const button = popup.el("start-server");
  assert.equal(model.placeholder, "large-v3 (server default)");
  assert.equal(popup.el("model-suggestions").options.map((o) => o.value).join(), "large-v3,small");
  const placeholderWrites = model.attrWrites;
  const paints = button.toggles;
  const asked = bg.types().filter((t) => t === "updateStatus").length;
  for (let i = 0; i < 5; i++) {
    popup.intervals[0].fn();
    await settle();
  }
  assert.equal(model.attrWrites, placeholderWrites, "the placeholder it already has is left alone");
  assert.equal(button.toggles, paints + 5, "one paint per tick, none for an update answer nobody asked for");
  assert.equal(bg.types().filter((t) => t === "updateStatus").length, asked);
  assert.equal(popup.el("model-suggestions").options.length, 2);
  // A new default is still written, and a changed server still gets the banner painted.
  state.health = { ok: true, data: Object.assign({}, data, { version: "0.8.0", default_model: "small" }) };
  popup.intervals[0].fn();
  await settle();
  assert.equal(model.placeholder, "small (server default)");
  assert.equal(model.attrWrites, placeholderWrites + 1);
  assert.equal(button.toggles, paints + 7, "painted with the answer, and again with the banner");
  assert.equal(popup.el("update-banner").hidden, false);
});

// The outcome of a wait belongs to the address it waited at: "no answer after 90 s", or the hint
// that names the very server URL the viewer has just changed, must not stand in for the offline
// hint at the new address, nor an update's outcome for the model line of the server there.
test("a new server address drops the verdicts reached at the old one", async () => {
  const state = { health: offline, settings: {} };
  const bg = updateBackground(state);
  const popup = loadPopup((msg) => (msg.type === "startServerStatus" ? { starting: true, already: true, loading: false, deadline: Date.now() - 1 } : bg.answer(msg)));
  state.onSave = popup.fireStorage;
  await popup.init();
  await settle();
  assert.equal(popup.startFlow.state, "failed");
  assert.equal(popup.el("server-detail").textContent, popup.START_ELSEWHERE_HINT);
  const field = popup.el("serverUrl");
  const move = async (url) => {
    field.value = url;
    field.dispatch("change");
    await wait(200);
    await settle();
  };
  await move("http://127.0.0.1:8791");
  assert.equal(popup.startFlow.state, "idle");
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("server-detail").textContent, popup.OFFLINE_HINT);
  assert.equal(popup.el("start-server").hidden, false);
  assert.equal(popup.el("start-server").disabled, false);
  // An update the old address never answered again.
  state.health = healthOf("0.8.0", true);
  await popup.checkServer(false);
  await settle();
  state.update = { ok: true, restarting: true, from: "0.8.0", to: "0.9.0", requestedAt: Date.now(), deadline: Date.now() - 1 };
  await popup.updateServerFromPopup();
  state.health = offline;
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "lost");
  assert.equal(popup.el("server-detail").textContent, popup.UPDATE_LOST_HINT);
  await move("http://127.0.0.1:8792");
  assert.equal(popup.updateFlow.state, "idle");
  assert.equal(popup.el("server-status").textContent, "Server offline");
  assert.equal(popup.el("server-detail").textContent, popup.OFFLINE_HINT);
  // And one the old address came back from with the old version: the new address has its own.
  state.health = healthOf("0.8.0", true);
  await popup.checkServer(false);
  await settle();
  state.update = { ok: true, restarting: true, from: "0.8.0", to: "0.9.0", requestedAt: Date.now() - 11000, deadline: Date.now() + 120000 };
  await popup.updateServerFromPopup();
  await popup.checkServer(false);
  assert.equal(popup.updateFlow.state, "stale");
  assert.equal(popup.el("server-detail").textContent, popup.stillOldHint("0.8.0"));
  await move("http://127.0.0.1:8793");
  assert.equal(popup.updateFlow.state, "idle");
  assert.equal(popup.el("server-status").textContent, "Server online");
  assert.equal(popup.el("server-detail").textContent, "large-v3 · cuda · float16");
  assert.equal(popup.el("update-banner").hidden, false, "the new address's server is offered the update");
  assert.equal(popup.el("update-now").disabled, false);
});

// The detail line and the hints carry text the server wrote: a repo id under "Loading model" and
// in the model hint (up to 193 characters, with no character a browser breaks after), the last line
// of a load error (a URL, a path). One such word must wrap, not widen the 360 px popup and push the
// switch off it; the header's 1fr column is as wide as its widest unbreakable word otherwise.
test("a server-supplied word without spaces wraps in the status detail and the hints", () => {
  for (const selector of [".detail", ".hint", ".sample"]) {
    assert.ok(cssDeclarations(selector).includes("overflow-wrap: anywhere"), `${selector} does not wrap anywhere`);
  }
});

test("relativeTime rounds to the unit that says something", () => {
  const { relativeTime } = loadPopup(() => offline);
  const now = 1000000000000;
  assert.equal(relativeTime(now - 10000, now), "just now");
  assert.equal(relativeTime(now - 59000, now), "just now");
  assert.equal(relativeTime(now - 3 * 60000, now), "3 min ago");
  assert.equal(relativeTime(now - 59 * 60000, now), "59 min ago");
  assert.equal(relativeTime(now - 90 * 60000, now), "2 h ago");
  assert.equal(relativeTime(now - 23 * 3600000, now), "23 h ago");
  assert.equal(relativeTime(now - 24 * 3600000, now), "1 day ago");
  assert.equal(relativeTime(now - 3 * 24 * 3600000, now), "3 days ago");
  assert.equal(relativeTime(now + 5000, now), "just now", "a clock that went backwards is not the future");
});

// ------------------------------------------------------------------ word colours

// Anki's decks as the background lists them (sorted there; the popup sorts again, so the order
// here does not matter) and the answers the ankiDecks message can carry.
const DECKS = ["Vocab", "Default", "Mining::JP"];
const decksOk = (seen) => ({ ok: true, decks: DECKS, seen });
const decksOffline = (seen) => ({ ok: false, reason: "offline", error: "Anki is not running or AnkiConnect is not installed", seen });
const optionsOf = (select) => select.options.map((o) => [o.value, o.textContent]);

// A popup with a background that answers getSettings from `settings` over the defaults and
// ankiDecks from `anki` (a value or a function; a test that changes the answer as it goes hands
// in a function reading its own variable, which may hold a function in turn), recording every
// message type and every settings patch saved.
function deckPopup(settings, anki) {
  const sent = [];
  const saves = [];
  const answer = (value) => (typeof value === "function" ? answer(value()) : value);
  const popup = loadPopup((msg) => {
    sent.push(msg.type);
    if (msg.type === "getSettings") return Object.assign({}, DEFAULTS, settings);
    if (msg.type === "ankiDecks") return answer(anki);
    // The patches come out of the sandbox; a JSON round trip makes them deepEqual's own objects.
    if (msg.type === "saveSettings") saves.push(JSON.parse(JSON.stringify(msg.settings)));
    if (msg.type === "startServerStatus") return { starting: false };
    return offline;
  });
  return { popup, sent, saves };
}
// The debounced save is 150 ms behind the edit.
const saved = () => new Promise((resolve) => setTimeout(resolve, 250));

test("renderDeckOptions keeps the automatic entry first, names the seen deck in it, and keeps the stored deck even when unlisted", () => {
  const { popup } = deckPopup({}, null);
  const select = popup.el("cardStatusDeck");
  popup.renderDeckOptions(DECKS, null, "");
  const auto = select.options[0];
  assert.deepEqual(optionsOf(select), [["", "Automatic: no card mined yet"], ["Default", "Default"], ["Mining::JP", "Mining::JP"], ["Vocab", "Vocab"]]);
  assert.equal(select.value, "");
  popup.renderDeckOptions(DECKS, "Mining::JP", "Vocab");
  assert.equal(select.options[0], auto, "the automatic option is the same element");
  assert.equal(auto.textContent, "Automatic: Mining::JP");
  assert.equal(auto.value, "");
  assert.equal(select.value, "Vocab");
  // Anki offline: no list, and the stored deck still has its option, so the value survives.
  popup.renderDeckOptions([], null, "Old::Deck");
  assert.deepEqual(optionsOf(select), [["", "Automatic: no card mined yet"], ["Old::Deck", "Old::Deck"]]);
  assert.equal(select.value, "Old::Deck");
  // A stored deck Anki does not list sorts in among the others, once.
  popup.renderDeckOptions(DECKS, "Vocab", "Gone");
  assert.deepEqual(optionsOf(select), [["", "Automatic: Vocab"], ["Default", "Default"], ["Gone", "Gone"], ["Mining::JP", "Mining::JP"], ["Vocab", "Vocab"]]);
  assert.equal(select.value, "Gone");
  popup.renderDeckOptions(DECKS, "Vocab", "Vocab");
  assert.equal(optionsOf(select).filter(([value]) => value === "Vocab").length, 1);
  // Before Anki was asked the entry keeps the page's description; junk in the list is dropped.
  popup.renderDeckOptions(["Vocab", 5, null, ""], undefined, "");
  assert.deepEqual(optionsOf(select), [["", "Automatic: the deck of the last mined card"], ["Vocab", "Vocab"]]);
});

test("refreshDecks paints the hint: automatic with and without a seen deck, a manual deck listed or not, Anki offline", async () => {
  let anki = decksOk(null);
  const { popup, sent } = deckPopup({}, () => anki);
  const select = popup.el("cardStatusDeck");
  const hint = popup.el("deck-hint");
  popup.el("cardStatus").checked = true; // the hint is about the colours: painted while a feature is on
  await popup.refreshDecks();
  assert.deepEqual(sent, ["ankiDecks"]);
  assert.equal(hint.textContent, popup.DECK_NONE_HINT);
  assert.equal(hint.className, "hint warn");
  assert.equal(select.options[0].textContent, "Automatic: no card mined yet");
  anki = decksOk("Mining::JP");
  await popup.refreshDecks();
  assert.equal(hint.textContent, "Looking at Mining::JP");
  assert.equal(hint.className, "hint");
  assert.equal(select.options[0].textContent, "Automatic: Mining::JP");
  // The deck of the last mined card was renamed or deleted since: Anki no longer lists it, and
  // the background's searches in it find nothing, so "Looking at" would be a false promise.
  anki = decksOk("Mining");
  await popup.refreshDecks();
  assert.equal(hint.textContent, "The last mined card's deck Mining is no longer in Anki; mine a card, or choose a deck");
  assert.equal(hint.className, "hint warn");
  assert.equal(select.options[0].textContent, "Automatic: Mining");
  assert.equal(select.value, "");
  // A manual deck that Anki lists needs no hint; one it does not list is an error.
  select.value = "Vocab";
  await popup.refreshDecks();
  assert.equal(hint.textContent, "");
  assert.equal(hint.className, "hint");
  assert.equal(select.value, "Vocab");
  select.value = "Gone";
  await popup.refreshDecks();
  assert.equal(hint.textContent, "No deck named Gone in Anki");
  assert.equal(hint.className, "hint error");
  assert.equal(select.value, "Gone", "the stored deck is kept for Anki to come back with it");
  // Anki offline: its reason on the hint, the seen deck still named, the stored deck kept.
  anki = decksOffline("Mining::JP");
  await popup.refreshDecks();
  assert.equal(hint.textContent, "Anki is not running or AnkiConnect is not installed");
  assert.equal(hint.className, "hint warn");
  assert.deepEqual(optionsOf(select), [["", "Automatic: Mining::JP"], ["Gone", "Gone"]]);
  assert.equal(select.value, "Gone");
  // A message that fails outright reads the same way on the hint; the automatic entry keeps the
  // page's description, since nobody got as far as the deck of the last mined card.
  anki = () => {
    throw new Error("Could not establish connection");
  };
  await popup.refreshDecks();
  assert.equal(hint.textContent, "Could not establish connection");
  assert.equal(hint.className, "hint warn");
  assert.equal(select.options[0].textContent, "Automatic: the deck of the last mined card");
  assert.equal(select.value, "Gone");
});

test("an answer overtaken by a later question does not overwrite the hint", async () => {
  let releaseFirst;
  const held = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  let asks = 0;
  const { popup } = deckPopup({}, async () => {
    if (++asks === 1) {
      await held;
      return decksOk("Default");
    }
    return decksOk("Vocab");
  });
  popup.el("cardStatus").checked = true;
  const first = popup.refreshDecks();
  await popup.refreshDecks();
  assert.equal(popup.el("deck-hint").textContent, "Looking at Vocab");
  releaseFirst();
  await first;
  assert.equal(popup.el("deck-hint").textContent, "Looking at Vocab");
  assert.equal(popup.el("cardStatusDeck").options[0].textContent, "Automatic: Vocab");
});

test("init asks Anki for its decks only when a word-colour feature is on, and keeps the stored deck either way", async () => {
  const off = deckPopup({ cardStatus: false, pitchAccent: false, cardStatusDeck: "Mining::JP" }, decksOk("Vocab"));
  await off.popup.init();
  await settle();
  assert.equal(off.sent.includes("ankiDecks"), false, "no ask for a viewer who never uses the feature");
  assert.equal(off.popup.el("cardStatusDeck").value, "Mining::JP");
  assert.deepEqual(optionsOf(off.popup.el("cardStatusDeck")), [["", "Automatic: the deck of the last mined card"], ["Mining::JP", "Mining::JP"]]);
  assert.equal(off.popup.el("deck-hint").textContent, "");

  const status = deckPopup({ cardStatus: true, pitchAccent: false, cardStatusDeck: "" }, decksOk("Vocab"));
  await status.popup.init();
  await settle();
  assert.equal(status.sent.filter((t) => t === "ankiDecks").length, 1);
  assert.equal(status.popup.el("deck-hint").textContent, "Looking at Vocab");
  assert.equal(status.popup.el("cardStatusDeck").options[0].textContent, "Automatic: Vocab");

  const pitch = deckPopup({ cardStatus: false, pitchAccent: true, cardStatusDeck: "Mining::JP" }, decksOffline(null));
  await pitch.popup.init();
  await settle();
  assert.equal(pitch.sent.filter((t) => t === "ankiDecks").length, 1);
  assert.equal(pitch.popup.el("cardStatusDeck").value, "Mining::JP", "an offline Anki does not lose the stored deck");
  assert.equal(pitch.popup.el("deck-hint").textContent, "Anki is not running or AnkiConnect is not installed");
});

// The options page is the same popup.html and lives for hours: a verdict from the one ask at
// init would stay long after Anki was started (or its dialog clicked), while the tab's colours,
// asking on their own clock, already work. init() sets a slow clock beside the health refresh
// that asks again while the newest answer failed and a feature is on.
const retryClock = (popup) => popup.intervals.find((clock) => clock.ms === popup.DECKS_RETRY_MS);
const asks = (sent) => sent.filter((t) => t === "ankiDecks").length;

test("a failed deck verdict is asked again on the slow clock, so Anki started after the page opened reaches the hint and the list", async () => {
  let anki = decksOffline(null);
  const { popup, sent } = deckPopup({ cardStatus: true, pitchAccent: false, cardStatusDeck: "" }, () => anki);
  const select = popup.el("cardStatusDeck");
  const hint = popup.el("deck-hint");
  await popup.init();
  await settle();
  assert.equal(asks(sent), 1);
  assert.equal(hint.textContent, "Anki is not running or AnkiConnect is not installed");
  assert.equal(popup.DECKS_RETRY_MS, 30000);
  assert.ok(popup.intervals.some((clock) => clock.ms === popup.HEALTH_REFRESH_MS), "the health refresh is still set up");
  const clock = retryClock(popup);
  assert.ok(clock, "init sets the deck retry clock");
  // Anki still away: asked again, the same verdict painted again.
  clock.fn();
  await settle();
  assert.equal(asks(sent), 2);
  assert.equal(hint.textContent, "Anki is not running or AnkiConnect is not installed");
  assert.equal(hint.className, "hint warn");
  assert.deepEqual(optionsOf(select), [["", "Automatic: no card mined yet"]]);
  // Its permission dialog up and not clicked yet: the same, for the "denied" verdict.
  anki = { ok: false, reason: "denied", error: "AnkiConnect denied access. Click Yes in Anki's permission dialog.", seen: null };
  clock.fn();
  await settle();
  assert.equal(asks(sent), 3);
  assert.equal(hint.textContent, "AnkiConnect denied access. Click Yes in Anki's permission dialog.");
  // Anki started and Yes clicked since: the next tick brings the list and the deck it looks at.
  anki = decksOk("Vocab");
  clock.fn();
  await settle();
  assert.equal(asks(sent), 4);
  assert.equal(hint.textContent, "Looking at Vocab");
  assert.equal(hint.className, "hint");
  assert.equal(select.options[0].textContent, "Automatic: Vocab");
  assert.deepEqual(optionsOf(select).map(([value]) => value), ["", "Default", "Mining::JP", "Vocab"]);
  // A good answer is left alone: the list changes with the viewer's own edits, which ask on their own.
  clock.fn();
  clock.fn();
  await settle();
  assert.equal(asks(sent), 4);
});

// The deck retry follows the health refresh's rule (see "the poll skips a page out of view"): a
// hint nobody sees is not worth a knock at Anki every thirty seconds, and a page back in view
// catches up now.
test("the deck retry skips a page out of view and catches up when it is back", async () => {
  let anki = decksOffline(null);
  const { popup, sent } = deckPopup({ cardStatus: true, pitchAccent: false, cardStatusDeck: "" }, () => anki);
  const hint = popup.el("deck-hint");
  await popup.init();
  await settle();
  assert.equal(asks(sent), 1);
  const clock = retryClock(popup);
  popup.document.hidden = true;
  popup.document.dispatch("visibilitychange");
  for (let i = 0; i < 10; i++) clock.fn();
  await settle();
  assert.equal(asks(sent), 1, "nothing while out of view");
  assert.equal(hint.textContent, "Anki is not running or AnkiConnect is not installed");
  // Anki started meanwhile: the return brings the list, without waiting for the clock.
  anki = decksOk("Vocab");
  popup.document.hidden = false;
  popup.document.dispatch("visibilitychange");
  await settle();
  assert.equal(asks(sent), 2, "one ask on return");
  assert.equal(hint.textContent, "Looking at Vocab");
  // A good verdict is left alone on return too, like on the clock.
  popup.document.hidden = true;
  popup.document.dispatch("visibilitychange");
  popup.document.hidden = false;
  popup.document.dispatch("visibilitychange");
  await settle();
  assert.equal(asks(sent), 2);
});

test("the deck retry asks nothing with both features off, nothing while an ask is still out, and nothing after a good answer", async () => {
  let anki = decksOffline(null);
  const { popup, sent } = deckPopup({ cardStatus: true, pitchAccent: false, cardStatusDeck: "" }, () => anki);
  const checkbox = popup.el("cardStatus");
  const hint = popup.el("deck-hint");
  await popup.init();
  await settle();
  const clock = retryClock(popup);
  assert.equal(asks(sent), 1);
  // Both features off since the failed verdict: nobody uses the colours, so Anki is not knocked at.
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.equal(hint.textContent, "");
  clock.fn();
  await settle();
  assert.equal(asks(sent), 1, "no ask for a viewer who uses neither feature");
  // The feature back on: the save asks, and while that ask is out (Anki's dialog up, a slow
  // Anki) the clock sends no second one.
  let release;
  anki = () =>
    new Promise((resolve) => {
      release = () => resolve(decksOk("Vocab"));
    });
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  await saved();
  assert.equal(asks(sent), 2);
  clock.fn();
  await settle();
  assert.equal(asks(sent), 2, "an ask still out is waited for, not doubled");
  release();
  await settle();
  assert.equal(hint.textContent, "Looking at Vocab");
  clock.fn();
  await settle();
  assert.equal(asks(sent), 2, "a good answer is left alone");
  // A popup opened with both features off never asked: the clock does not start asking either.
  const off = deckPopup({ cardStatus: false, pitchAccent: false, cardStatusDeck: "" }, decksOffline(null));
  await off.popup.init();
  await settle();
  retryClock(off.popup).fn();
  await settle();
  assert.equal(asks(off.sent), 0);
});

test("a checkbox turned on or another deck asks Anki after the save; a checkbox turned off does not", async () => {
  const { popup, sent } = deckPopup({}, decksOk(null));
  const checkbox = popup.el("cardStatus");
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent, ["saveSettings", "ankiDecks"]);
  assert.equal(popup.el("deck-hint").textContent, popup.DECK_NONE_HINT);
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent, ["saveSettings", "ankiDecks", "saveSettings"]);
  assert.equal(popup.el("deck-hint").textContent, "", "nothing to mine or choose a deck for once both features are off");
  const pitch = popup.el("pitchAccent");
  pitch.checked = true;
  popup.onChange({ target: pitch });
  await saved();
  assert.deepEqual(sent.slice(3), ["saveSettings", "ankiDecks"]);
  assert.equal(popup.el("deck-hint").textContent, popup.DECK_NONE_HINT);
  const select = popup.el("cardStatusDeck");
  select.value = "Vocab";
  popup.onChange({ target: select });
  await saved();
  assert.deepEqual(sent.slice(5), ["saveSettings", "ankiDecks"]);
  assert.equal(popup.el("deck-hint").textContent, "");
  assert.equal(select.value, "Vocab");
  // Any other control saves and asks nothing.
  popup.onChange({ target: popup.el("pauseOnHover") });
  await saved();
  assert.deepEqual(sent.slice(7), ["saveSettings"]);
});

// A double click on a checkbox lands both edits inside the 150 ms debounce: the save writes the
// feature off, and an ask then would bring up AnkiConnect's permission dialog for a viewer who
// uses neither feature, with a "no card mined yet" hint under a feature that is off.
test("a feature turned on and off again inside the debounce is saved off and asks Anki nothing", async () => {
  const { popup, sent, saves } = deckPopup({}, decksOk(null));
  const checkbox = popup.el("cardStatus");
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent, ["saveSettings"]);
  assert.deepEqual(saves[0], { cardStatus: false }, "the save carries the edited field alone");
  assert.equal(popup.el("deck-hint").textContent, "");
  // The same double click with the other feature on: the colours are in use, so the ask stands.
  // That feature is on since an earlier save and not in this patch: the checkboxes are judged
  // from the form.
  const pitch = popup.el("pitchAccent");
  pitch.checked = true;
  popup.onChange({ target: pitch });
  await saved();
  assert.deepEqual(sent.slice(1), ["saveSettings", "ankiDecks"]);
  assert.deepEqual(saves[1], { pitchAccent: true });
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent.slice(3), ["saveSettings", "ankiDecks"]);
  assert.deepEqual(saves[2], { cardStatus: false });
  // A deck change inside the same debounce keeps its ask whatever the checkboxes say.
  const select = popup.el("cardStatusDeck");
  select.value = "Vocab";
  popup.onChange({ target: select });
  pitch.checked = false;
  popup.onChange({ target: pitch });
  await saved();
  assert.deepEqual(sent.slice(5), ["saveSettings", "ankiDecks"]);
});

// The hint is about the colours: with both features off it says nothing. The options page is the
// same popup.html and lives on, so a verdict left behind by an untick would stay for good, and
// nudge the viewer to mine or choose a deck for features they just switched off.
test("both features off leave no deck hint: an untick clears the last verdict, and an answer landing after it paints none", async () => {
  let anki = decksOk(null);
  const { popup, sent } = deckPopup({}, () => anki);
  const checkbox = popup.el("cardStatus");
  const pitch = popup.el("pitchAccent");
  const select = popup.el("cardStatusDeck");
  const hint = popup.el("deck-hint");
  // A stored deck Anki does not list: the error, then nothing once the feature is off.
  select.value = "Gone";
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  await saved();
  assert.equal(hint.textContent, "No deck named Gone in Anki");
  assert.equal(hint.className, "hint error");
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent, ["saveSettings", "ankiDecks", "saveSettings"]);
  assert.equal(hint.textContent, "");
  assert.equal(hint.className, "hint");
  assert.equal(select.value, "Gone", "the stored deck is kept");
  // The other feature keeps the hint: one of the two is enough for the colours to matter.
  select.value = "";
  pitch.checked = true;
  popup.onChange({ target: pitch });
  await saved();
  assert.equal(hint.textContent, popup.DECK_NONE_HINT);
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  await saved();
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.equal(hint.textContent, popup.DECK_NONE_HINT, "pitch accent is still on");
  pitch.checked = false;
  popup.onChange({ target: pitch });
  await saved();
  assert.equal(hint.textContent, "");
  // An ask out while the viewer unticks: Anki's answer (the dialog clicked later, a slow list)
  // lands under two off features and paints nothing, though the list is worth keeping.
  let release;
  anki = () =>
    new Promise((resolve) => {
      release = () => resolve(decksOk("Vocab"));
    });
  pitch.checked = true;
  popup.onChange({ target: pitch });
  await saved();
  assert.equal(sent.filter((t) => t === "ankiDecks").length, 4);
  assert.equal(hint.textContent, "", "no answer yet");
  pitch.checked = false;
  popup.onChange({ target: pitch });
  await saved();
  release();
  await settle();
  assert.equal(hint.textContent, "");
  assert.equal(hint.className, "hint");
  assert.equal(select.options[0].textContent, "Automatic: Vocab");
  assert.deepEqual(optionsOf(select).map(([value]) => value), ["", "Default", "Mining::JP", "Vocab"]);
});

// The deck list and the hint describe one Anki: another AnkiConnect URL is another Anki (a second
// profile, another port), whose decks the background's asks now go to. Both features off, nothing
// is asked: a viewer who never uses the colours meets no permission dialog for a URL edit.
test("a new AnkiConnect URL asks that Anki for its decks while a feature is on, and nothing while both are off", async () => {
  let anki = decksOk("Mining::JP");
  const { popup, sent, saves } = deckPopup({}, () => anki);
  const url = popup.el("ankiUrl");
  const hint = popup.el("deck-hint");
  url.value = "http://127.0.0.1:8766";
  popup.onChange({ target: url });
  await saved();
  assert.deepEqual(sent, ["saveSettings"]);
  assert.equal(saves[0].ankiUrl, "http://127.0.0.1:8766");
  assert.equal(hint.textContent, "");
  const checkbox = popup.el("cardStatus");
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent.slice(1), ["saveSettings", "ankiDecks"]);
  assert.equal(hint.textContent, "Looking at Mining::JP");
  // The second Anki is not there: the hint says so instead of naming the first one's deck.
  anki = decksOffline("Mining::JP");
  url.value = "http://127.0.0.1:8767";
  popup.onChange({ target: url });
  await saved();
  assert.deepEqual(sent.slice(3), ["saveSettings", "ankiDecks"]);
  assert.equal(hint.textContent, "Anki is not running or AnkiConnect is not installed");
  // A deck change in the same debounce keeps its own ask; a URL edit with the feature unticked
  // in the same debounce asks nothing.
  anki = decksOk("Mining::JP");
  const select = popup.el("cardStatusDeck");
  select.value = "Vocab";
  popup.onChange({ target: select });
  url.value = "http://127.0.0.1:8765";
  popup.onChange({ target: url });
  await saved();
  assert.deepEqual(sent.slice(5), ["saveSettings", "ankiDecks"]);
  assert.equal(hint.textContent, "");
  url.value = "http://127.0.0.1:8766";
  popup.onChange({ target: url });
  checkbox.checked = false;
  popup.onChange({ target: checkbox });
  await saved();
  assert.deepEqual(sent.slice(7), ["saveSettings"]);
});

// Reset style takes an edit still on its way with it (see "Reset style takes an edit still on its
// way with it"): the deck ask that edit carried follows that one save, the way the server check
// does, and does not survive to the next, unrelated edit.
test("Reset style takes a pending deck edit with it, and the ask it carried follows that one save", async () => {
  const { popup, sent, saves } = deckPopup({}, decksOk(null));
  const select = popup.el("cardStatusDeck");
  const hint = popup.el("deck-hint");
  select.value = "Vocab";
  popup.onChange({ target: select });
  await popup.resetStyle();
  await settle();
  assert.deepEqual(sent, ["saveSettings", "ankiDecks"], "one save, the deck edit folded into the reset's, and the ask it carried");
  assert.equal(saves[0].cardStatusDeck, "Vocab");
  assert.equal(saves[0].subPosition, DEFAULTS.subPosition);
  assert.equal(select.value, "Vocab");
  assert.equal(hint.textContent, "", "both features off: no hint, though the list was asked for");
  assert.equal(select.options[0].textContent, "Automatic: no card mined yet");
  await saved();
  assert.deepEqual(sent, ["saveSettings", "ankiDecks"], "the debounced save was folded in, not run as well");
  popup.onChange({ target: popup.el("pauseOnHover") });
  await saved();
  assert.deepEqual(sent, ["saveSettings", "ankiDecks", "saveSettings"], "an unrelated edit asks nothing");
  // A feature ticked and the style reset in the same breath: the tick is saved, and the ask stands.
  const checkbox = popup.el("cardStatus");
  checkbox.checked = true;
  popup.onChange({ target: checkbox });
  await popup.resetStyle();
  await settle();
  assert.deepEqual(sent.slice(3), ["saveSettings", "ankiDecks"]);
  assert.equal(saves[2].cardStatus, true);
  assert.equal(hint.textContent, "", "Vocab is listed: nothing to say");
  popup.onChange({ target: popup.el("pauseOnHover") });
  await saved();
  assert.deepEqual(sent.slice(5), ["saveSettings"]);
});

// The options page and the toolbar popup are two copies of this form. A deck chosen in one has no
// option in the other until Anki lists it there, and a select given a value it has no option for
// shows none: the option is built from Anki's last answer here. The hint then follows the deck,
// the features and the AnkiConnect URL by the rules of an edit made here (onChange), less the
// save, which is the other copy's; the popup's own save, echoed, asks nothing more.
test("the word colours edited in the other copy of the form land in the select and the hint", async () => {
  const state = { health: offline, settings: {}, anki: decksOk("Mining::JP") };
  const { popup, bg, elsewhere } = await openForm(state);
  const select = popup.el("cardStatusDeck");
  const hint = popup.el("deck-hint");
  const asked = () => bg.types().filter((t) => t === "ankiDecks").length;
  assert.equal(asked(), 0, "both features off: nothing asked at init");
  assert.deepEqual(optionsOf(select), [["", "Automatic: the deck of the last mined card"]]);
  // A deck chosen elsewhere with both features off: it shows at once, and Anki is asked for its
  // standing, as for a deck chosen here; the hint says nothing under two off features.
  elsewhere({ cardStatusDeck: "Vocab" });
  assert.equal(select.value, "Vocab");
  assert.deepEqual(optionsOf(select), [["", "Automatic: the deck of the last mined card"], ["Vocab", "Vocab"]]);
  assert.equal(asked(), 1);
  await settle();
  assert.equal(hint.textContent, "");
  assert.deepEqual(optionsOf(select).map(([value]) => value), ["", "Default", "Mining::JP", "Vocab"]);
  // A feature turned on elsewhere: the hint is asked for.
  elsewhere({ cardStatus: true });
  assert.equal(popup.el("cardStatus").checked, true);
  assert.equal(asked(), 2);
  await settle();
  assert.equal(hint.textContent, "", "a listed manual deck needs no hint");
  // A deck Anki does not list: its option is built from Anki's last list, and the hint says so.
  elsewhere({ cardStatusDeck: "Gone" });
  assert.equal(select.value, "Gone");
  assert.deepEqual(optionsOf(select).map(([value]) => value), ["", "Default", "Gone", "Mining::JP", "Vocab"]);
  assert.equal(asked(), 3);
  await settle();
  assert.equal(hint.textContent, "No deck named Gone in Anki");
  assert.equal(hint.className, "hint error");
  // Back to automatic elsewhere.
  elsewhere({ cardStatusDeck: "" });
  assert.equal(select.value, "");
  assert.equal(asked(), 4);
  await settle();
  assert.equal(hint.textContent, "Looking at Mining::JP");
  assert.deepEqual(optionsOf(select).map(([value]) => value), ["", "Default", "Mining::JP", "Vocab"], "the unlisted deck went with its choice");
  // Another AnkiConnect URL elsewhere while a feature is on: that Anki is asked.
  state.anki = decksOffline("Mining::JP");
  elsewhere({ ankiUrl: "http://127.0.0.1:8766" });
  assert.equal(asked(), 5);
  await settle();
  assert.equal(hint.textContent, "Anki is not running or AnkiConnect is not installed");
  // The feature turned off elsewhere: the hint goes, and nothing is asked.
  elsewhere({ cardStatus: false });
  assert.equal(asked(), 5);
  assert.equal(hint.textContent, "");
  // A change elsewhere to anything else asks nothing, and neither does the echo of this form's
  // own edit: its save asked already.
  elsewhere({ fontScale: 1.2, ankiSentenceField: "Sentence" });
  assert.equal(asked(), 5);
  state.anki = decksOk("Mining::JP");
  popup.el("pitchAccent").checked = true;
  popup.el("pitchAccent").dispatch("input");
  await wait(200);
  await settle();
  assert.equal(state.settings.pitchAccent, true);
  assert.equal(asked(), 6, "once, from the save");
  assert.equal(hint.textContent, "Looking at Mining::JP");
  // The other feature turned on elsewhere asks, as here; turned off again with pitch accent still
  // on, it asks nothing, as an untick made here does not: the hint has nothing new to say.
  elsewhere({ cardStatus: true });
  assert.equal(asked(), 7);
  await settle();
  assert.equal(hint.textContent, "Looking at Mining::JP");
  elsewhere({ cardStatus: false });
  assert.equal(asked(), 7, "an untick with the other feature on asks nothing");
  assert.equal(hint.textContent, "Looking at Mining::JP", "pitch accent is still on");
  elsewhere({ pitchAccent: false });
  assert.equal(asked(), 7);
  assert.equal(hint.textContent, "", "both off: the hint goes without an ask");
});

"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadBackground, makeMemoryStorage } = require("./_loadBackground");

// Results returned from the vm sandbox are plain objects from a *different* realm, so
// assert.deepEqual's reference-equality check on prototypes rejects them even when every field
// matches. Round-tripping through JSON compares the same objects in this realm instead.
const plain = (value) => JSON.parse(JSON.stringify(value));

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
// notesInfo's answer for one Lapis-like note: its fields, the sentence filled in.
const noteFields = (sentence) => ({ fields: { Picture: { value: "" }, SentenceAudio: { value: "" }, Sentence: { value: sentence } } });
const instantTimers = { setTimeout: (fn) => { fn(); return 0; } };
// A turn of the event loop, for work the background does without being awaited.
const settle = () => new Promise((resolve) => setImmediate(resolve));

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

// ------------------------------------------------------------------ settings

test("getSettings merges stored values over the defaults", async () => {
  const storage = makeMemoryStorage({ settings: { fontScale: 1.5, clipFormat: "wav" } });
  const { sandbox } = loadBackground({ storage });
  const settings = await sandbox.getSettings();
  assert.equal(settings.fontScale, 1.5);
  assert.equal(settings.clipFormat, "wav");
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

// The server mines (Miner in server.py); a tab that hears of a note it attached to passes the
// note on (minedNote), and the deck that note went to is the word colours' deck from then on.
test("a mined note remembers the deck the card went to and expires the index, so the card shows at once", async () => {
  const sets = { ...DECK_SETS };
  const mock = ankiFetch({
    ...deckHandlers(DECK, sets, DECK_NOTES, { notesInfo: (p) => p.notes.map((id) => (id === 555 ? noteFields("文0") : DECK_NOTES[id] || {})) }),
    findCards: (p) => (p.query === "nid:555" ? [9001, 9002, 9003] : []),
    getDecks: { Default: [9003], "Japanese::Mining": [9001, 9002] },
  });
  const storage = makeMemoryStorage({ settings: { cardStatus: true } });
  const { sandbox, storage: store, dispatch } = loadBackground({ storage, fetch: mock.fetch });
  assert.equal((await sandbox.cardStatus({})).reason, "noDeck");

  const res = await dispatch({ type: "minedNote", noteId: 555 }, 1);
  assert.equal(res.ok, true, JSON.stringify(res));
  const seen = store._dump()[sandbox.DECK_SEEN_KEY];
  assert.equal(seen.deck, "Japanese::Mining");
  assert.equal(seen.noteId, 555);
  assert.equal(typeof seen.at, "number");
  assert.deepEqual(plain(mock.calls.find((c) => c.action === "findCards").params), { query: "nid:555" });
  assert.deepEqual(plain(mock.calls.find((c) => c.action === "getDecks").params), { cards: [9001, 9002, 9003] });
  assert.ok(!mock.actions().some((action) => /updateNoteFields|storeMediaFile/.test(action)), "the extension never writes to Anki");

  // Automatic now means that deck; the card just filled is looked up straight away.
  const coloured = await sandbox.cardStatus({});
  assert.equal(coloured.ok, true, JSON.stringify(coloured));
  assert.equal(coloured.deck, "Japanese::Mining");
  assert.equal(coloured.automatic, true);
  const before = mock.calls.filter((c) => c.action === "findNotes").length;
  // Yomitan's next card is in the deck by the time the index is asked again.
  sets.unsuspended = [...DECK_SETS.unsuspended, 555];
  sets.new = [...DECK_SETS.new, 555];
  assert.equal((await dispatch({ type: "minedNote", noteId: 555 }, 1)).ok, true);
  const read = mock.calls.filter((c) => c.action === "notesInfo").length;
  await sandbox.cardStatus({});
  assert.equal(mock.calls.filter((c) => c.action === "findNotes").length, before + 6, "the searches ran again after the mine");
  // Expired, not forgotten: only the card just made is read, not the whole deck again.
  const after = mock.calls.filter((c) => c.action === "notesInfo").slice(read);
  assert.deepEqual(after.map((c) => plain(c.params.notes)), [[555]]);
});

test("a mined note whose deck cannot be told remembers nothing, and a bad id asks Anki nothing", async () => {
  const mock = ankiFetch({
    findCards: () => {
      throw new Error("collection is not available");
    },
  });
  const { storage, dispatch } = loadBackground({ fetch: mock.fetch });
  assert.equal((await dispatch({ type: "minedNote", noteId: 555 }, 1)).ok, true);
  assert.equal(storage._dump().ankiDeckSeen, undefined);
  const calls = mock.calls.length;
  for (const noteId of [undefined, "x", -1, 1.5]) assert.equal((await dispatch({ type: "minedNote", noteId }, 1)).ok, false);
  assert.equal(mock.calls.length, calls);
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

test("a mined note while the index is being fetched expires that fetch when it lands", async () => {
  const sets = { ...DECK_SETS };
  const mock = ankiFetch({
    ...deckHandlers(DECK, sets, DECK_NOTES, { notesInfo: (p) => p.notes.map((id) => (id === 555 ? noteFields("文0") : DECK_NOTES[id] || {})) }),
    findCards: (p) => (p.query === "nid:555" ? [9001, 9002] : []),
    getDecks: { "Japanese::Mining": [9001, 9002] },
  });
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
    return mock.fetch(url, init);
  };
  const { sandbox, dispatch } = loadBackground({ storage: colourSettings(), fetch });
  sandbox.ankiWatch.permission = "granted";
  const asked = sandbox.cardStatus({});
  await settle();
  assert.equal(gated, true);
  assert.equal((await dispatch({ type: "minedNote", noteId: 555 }, 1)).ok, true);
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

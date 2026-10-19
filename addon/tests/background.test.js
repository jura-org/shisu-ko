"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadBackground, makeMemoryStorage } = require("./_loadBackground");

// Results returned from the vm sandbox are plain objects from a *different* realm, so
// assert.deepEqual's reference-equality check on prototypes rejects them even when every field
// matches. Round-tripping through JSON compares the same objects in this realm instead.
const plain = (value) => JSON.parse(JSON.stringify(value));

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

// A fake Shisu-ko server for the word colours: every /sync body is kept, and its answer carries
// whatever `deck` the test queued next (none once the queue is empty, as for a client up to date).
function deckServer(decks, extra) {
  const bodies = [];
  const fetch = async (url, init) => {
    if (url.includes("/health")) return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, ...(extra || {}) }) };
    const body = JSON.parse(init.body);
    bodies.push(body);
    const data = { ok: true, status: "ready", cues: [] };
    if (body.deck && decks.length) data.deck = decks.shift();
    return { ok: true, status: 200, text: async () => JSON.stringify(data) };
  };
  return { fetch, bodies };
}

const SYNC = { type: "api", path: "/sync", body: { video_id: "abcdef1234", url: "", t: 0, since: 0 } };
const colours = (patch) => makeMemoryStorage({ settings: { enabled: true, cardStatus: true, ...patch } });
const ROWS = [[30, "猫", "", "new", "atamadaka"], [10, "更に", "さらに", "learning", null], [20, "食べる", "", "learned", "nakadaka"]];

test("deckEntries puts the rows in id order, a kana reading after its word", () => {
  const { sandbox } = loadBackground();
  assert.deepEqual(plain(sandbox.deckEntries(ROWS)), [
    ["更に", "learning", null], ["さらに", "learning", null], ["食べる", "learned", "nakadaka"], ["猫", "new", "atamadaka"],
  ]);
});

test("the holder's /sync asks for the deck the popup names and keeps the rows from the tab", async () => {
  const server = deckServer([{ token: "e1.1", status: "ok", deck: "Japanese", automatic: false, full: true, notes: ROWS, removed: [] }]);
  const bg = loadBackground({ storage: colours({ cardStatusDeck: " Japanese ", ankiWordField: "Word", ankiPitchField: "" }), fetch: server.fetch });
  const answer = plain(await bg.dispatch(SYNC, 1));
  assert.deepEqual(server.bodies[0].deck, { name: "Japanese", word: "Word", pitch: "", since: null });
  assert.equal("deck" in answer.data, false, "the tab reads the deck through cardStatus");
  const status = plain(await bg.dispatch({ type: "cardStatus", since: 0 }, 1));
  assert.equal(status.ok, true);
  assert.equal(status.deck, "Japanese");
  assert.deepEqual(status.entries, plain(bg.sandbox.deckEntries(ROWS)));
  assert.deepEqual(plain(await bg.dispatch({ type: "cardStatus", since: status.at }, 2)), { ok: true, unchanged: true, at: status.at, deck: "Japanese" });
  // The next /sync names the token held, and an answer without `deck` changes nothing.
  await bg.dispatch(SYNC, 1);
  assert.equal(server.bodies[1].deck.since, "e1.1");
  assert.equal(plain(await bg.dispatch({ type: "cardStatus", since: status.at }, 1)).unchanged, true);
});

test("a delta updates the rows held, and entries that come out the same keep their stamp", async () => {
  const server = deckServer([
    { token: "e1.1", status: "ok", deck: "Japanese", full: true, notes: ROWS, removed: [] },
    { token: "e1.2", status: "ok", deck: "Japanese", full: false, notes: [[30, "猫", "", "learned", "atamadaka"]], removed: [20] },
    { token: "e1.3", status: "ok", deck: "Japanese", full: false, notes: [[30, "猫", "", "learned", "atamadaka"]], removed: [] },
  ]);
  const bg = loadBackground({ storage: colours({ cardStatusDeck: "Japanese" }), fetch: server.fetch });
  bg.setNow(1000);
  await bg.dispatch(SYNC, 1);
  bg.setNow(2000);
  await bg.dispatch(SYNC, 1);
  const status = plain(await bg.dispatch({ type: "cardStatus", since: 0 }, 1));
  assert.deepEqual(status.entries, [["更に", "learning", null], ["さらに", "learning", null], ["猫", "learned", "atamadaka"]]);
  assert.equal(status.at, 2000);
  bg.setNow(3000);
  await bg.dispatch(SYNC, 1);
  assert.equal(server.bodies[2].deck.since, "e1.2");
  assert.equal(plain(await bg.dispatch({ type: "cardStatus", since: 2000 }, 1)).unchanged, true);
});

test("another deck or field starts over: the rows go and the server is asked from scratch", async () => {
  const server = deckServer([{ token: "e1.1", status: "ok", deck: "Japanese", full: true, notes: ROWS, removed: [] }]);
  const storage = colours({ cardStatusDeck: "Japanese" });
  const bg = loadBackground({ storage, fetch: server.fetch });
  await bg.dispatch(SYNC, 1);
  await bg.dispatch({ type: "saveSettings", settings: { ankiPitchField: "Accent" } });
  const status = plain(await bg.dispatch({ type: "cardStatus", since: 0 }, 1));
  assert.equal(status.reason, "pending");
  await bg.dispatch(SYNC, 1);
  assert.deepEqual(server.bodies[1].deck, { name: "Japanese", word: "", pitch: "Accent", since: null });
});

test("the server's own verdicts reach the tab; a server without Anki is 'unavailable', not 'off'", async () => {
  for (const [answer, reason] of [
    [{ token: "nodeck", status: "no_deck", error: "No card mined yet; pick a deck in the popup", automatic: true }, "no_deck"],
    [{ token: "e1.1", status: "offline", error: "Anki is not running or AnkiConnect is not installed" }, "offline"],
    [{ token: "off", status: "off", error: "This server reads no Anki" }, "unavailable"],
  ]) {
    const bg = loadBackground({ storage: colours(), fetch: deckServer([answer]).fetch });
    await bg.dispatch(SYNC, 1);
    const status = plain(await bg.dispatch({ type: "cardStatus" }, 1));
    assert.deepEqual(status, { ok: false, reason, error: answer.error });
  }
});

test("a server that ignores `deck` is too old, and says so", async () => {
  const bg = loadBackground({ storage: colours(), fetch: deckServer([]).fetch });
  assert.equal(plain(await bg.dispatch({ type: "cardStatus" }, 1)).reason, "pending");
  await bg.dispatch(SYNC, 1);
  assert.deepEqual(plain(await bg.dispatch({ type: "cardStatus" }, 1)), { ok: false, reason: "old", error: bg.sandbox.SERVER_TOO_OLD_TEXT });
});

test("with the colours off, or the add-on off, /sync asks for no deck and cardStatus says 'off'", async () => {
  for (const patch of [{ cardStatus: false }, { enabled: false }]) {
    const server = deckServer([]);
    const bg = loadBackground({ storage: colours(patch), fetch: server.fetch });
    await bg.dispatch(SYNC, 1);
    assert.equal("deck" in server.bodies[0], false);
    assert.deepEqual(plain(await bg.dispatch({ type: "cardStatus" }, 1)), { ok: false, reason: "off" });
  }
});

test("a tab standing by gets the deck the holder's /sync brought", async () => {
  const server = deckServer([{ token: "e1.1", status: "ok", deck: "Japanese", full: true, notes: ROWS, removed: [] }]);
  const bg = loadBackground({ storage: colours({ cardStatusDeck: "Japanese" }), fetch: server.fetch });
  await bg.dispatch(SYNC, 1);
  assert.equal(plain(await bg.dispatch(SYNC, 2)).data.status, "standby");
  assert.equal(server.bodies.length, 1);
  assert.equal(plain(await bg.dispatch({ type: "cardStatus", since: 0 }, 2)).entries.length, 4);
});

test("ankiDecks reads the deck list from the server's /health?decks=1", async () => {
  let asked = null;
  const reply = (anki) => async (url) => {
    asked = url;
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, ...(anki === undefined ? {} : { anki_decks: anki }) }) };
  };
  let bg = loadBackground({ fetch: reply({ ok: true, decks: ["b", "A", "c"], seen: "A" }) });
  assert.deepEqual(plain(await bg.dispatch({ type: "ankiDecks" })), { ok: true, decks: ["A", "b", "c"], seen: "A" });
  assert.ok(asked.endsWith("/health?decks=1"));
  bg = loadBackground({ fetch: reply({ ok: false, offline: true, error: "Anki is not running", seen: null }) });
  assert.deepEqual(plain(await bg.dispatch({ type: "ankiDecks" })), { ok: false, reason: "offline", error: "Anki is not running", seen: null });
  bg = loadBackground({ fetch: reply(undefined) });
  assert.deepEqual(plain(await bg.dispatch({ type: "ankiDecks" })), { ok: false, reason: "error", error: bg.sandbox.SERVER_TOO_OLD_TEXT });
  bg = loadBackground({ fetch: async () => { throw new TypeError("NetworkError"); } });
  assert.equal(plain(await bg.dispatch({ type: "ankiDecks" })).reason, "offline");
});

test("nothing in the extension talks to AnkiConnect", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const dir = path.join(__dirname, "..");
  for (const name of fs.readdirSync(dir).filter((f) => /\.(js|html)$/.test(f))) {
    const text = fs.readFileSync(path.join(dir, name), "utf8");
    assert.doesNotMatch(text, /8765/, `${name} names AnkiConnect's port`);
    assert.doesNotMatch(text, /["'`](requestPermission|findNotes|notesInfo|notesModTime|deckNames|deckNamesAndIds|findCards|getDecks)["'`]/, `${name} speaks AnkiConnect`);
  }
});

test("the deck 0.22.0 remembered for the automatic choice is removed: the server keeps it now", async () => {
  const storage = makeMemoryStorage({ ankiDeckSeen: { deck: "Japanese", at: 1, noteId: 2 }, settings: { cardStatus: true } });
  loadBackground({ storage });
  await settle();
  assert.equal("ankiDeckSeen" in storage._dump(), false);
  assert.equal(storage._dump().settings.cardStatus, true);
});

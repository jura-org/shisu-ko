"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "..", "browser-api.js"), "utf8");

function loadChrome() {
  const listeners = [];
  const calls = [];
  const chrome = {
    runtime: {
      lastError: null,
      getURL: () => "chrome-extension://test/",
      sendMessage(msg, callback) { calls.push(["sendMessage", msg]); callback({ echoed: msg }); },
      sendNativeMessage(application, msg, callback) { calls.push(["sendNativeMessage", application, msg]); callback({ ok: true, started: true }); },
      onMessage: { addListener(fn) { listeners.push(fn); } },
    },
    storage: { local: {
      get(key, callback) { callback({ [key]: 1 }); },
      set(value, callback) { callback(); },
    }, session: {
      get(key, callback) { calls.push(["session.get", key]); callback({ [key]: 2 }); },
      set(value, callback) { calls.push(["session.set", value]); callback(); },
    }, onChanged: {} },
    tabs: { query(q, callback) { callback([{ id: 7, q }]); }, reload(id, callback) { callback(); }, sendMessage(id, msg, callback) { callback({ id, msg }); } },
    permissions: { contains(_, callback) { callback(true); }, request(_, callback) { callback(false); } },
    downloads: { download(_, callback) { callback(12); }, onChanged: {} },
    commands: { onCommand: {} },
  };
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  return { browser: sandbox.browser, chrome, listeners, calls };
}

test("Firefox browser namespace is preserved", () => {
  const native = { runtime: { getURL: () => "moz-extension://test/" } };
  const sandbox = { browser: native, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  assert.equal(sandbox.browser, native);
});

test("content-script Chrome namespace does not assume privileged APIs exist", () => {
  const { chrome } = loadChrome();
  delete chrome.tabs;
  delete chrome.permissions;
  delete chrome.downloads;
  delete chrome.commands;
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  assert.doesNotThrow(() => new vm.Script(source).runInContext(sandbox));
  assert.equal(sandbox.browser.tabs, undefined);
  assert.equal(sandbox.browser.downloads, undefined);
});

test("Chrome browser alias is bridged even when it is already present", async () => {
  const { browser, chrome } = loadChrome();
  const nativeAlias = { runtime: { getURL: () => "chrome-extension://test/" } };
  const sandbox = { browser: nativeAlias, chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  assert.notEqual(sandbox.browser, nativeAlias);
  assert.deepEqual(await sandbox.browser.runtime.sendMessage({ type: "ping" }), { echoed: { type: "ping" } });
});

test("Chrome callback APIs become promises", async () => {
  const { browser } = loadChrome();
  assert.deepEqual(await browser.runtime.sendMessage({ type: "ping" }), { echoed: { type: "ping" } });
  assert.deepEqual(await browser.tabs.query({ active: true }), [{ id: 7, q: { active: true } }]);
  assert.equal(await browser.permissions.contains({ origins: ["*"] }), true);
  assert.equal(await browser.downloads.download({ url: "data:x" }), 12);
});

test("Chrome storage.session is bridged like storage.local, and only where it exists", async () => {
  const { browser, chrome, calls } = loadChrome();
  assert.deepEqual(await browser.storage.session.get("startServer"), { startServer: 2 });
  assert.equal(await browser.storage.session.set({ startServer: null }), undefined);
  assert.deepEqual(calls.slice(-2), [["session.get", "startServer"], ["session.set", { startServer: null }]]);
  delete chrome.storage.session;
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  assert.equal(sandbox.browser.storage.session, undefined);
  assert.equal(typeof sandbox.browser.storage.local.get, "function");
});

test("Chrome runtime.lastError rejects the promise", async () => {
  const { browser, chrome } = loadChrome();
  chrome.runtime.sendMessage = (_msg, callback) => {
    chrome.runtime.lastError = { message: "No receiver" };
    callback();
    chrome.runtime.lastError = null;
  };
  await assert.rejects(browser.runtime.sendMessage({}), /No receiver/);
});

test("runtime message bridge replies, envelopes errors, and leaves unknown messages alone", async () => {
  const { browser, listeners } = loadChrome();
  browser.runtime.onMessage.addListener((msg) => {
    if (msg.type === "ok") return Promise.resolve({ ok: true });
    if (msg.type === "bad") return Promise.reject(new Error("broken"));
  });
  const send = (msg) => new Promise((resolve) => {
    const claimed = listeners[0](msg, {}, resolve);
    if (claimed === undefined) resolve(undefined);
  });
  assert.deepEqual(await send({ type: "ok" }), { ok: true });
  assert.equal(JSON.stringify(await send({ type: "bad" })), JSON.stringify({ ok: false, error: "broken" }));
  assert.equal(await send({ type: "other" }), undefined);
});

test("runtime message bridge handles synchronous replies and throws", async () => {
  const { browser, listeners } = loadChrome();
  browser.runtime.onMessage.addListener((msg) => {
    if (msg.type === "sync") return { ok: true };
    if (msg.type === "throw") throw new Error("bad sync");
  });
  const send = (msg) => new Promise((resolve) => {
    const claimed = listeners[0](msg, {}, resolve);
    if (claimed === undefined) resolve(undefined);
  });
  assert.deepEqual(await send({ type: "sync" }), { ok: true });
  assert.equal(JSON.stringify(await send({ type: "throw" })), JSON.stringify({ ok: false, error: "bad sync" }));
});

test("Chrome sendNativeMessage is bridged to a promise, with lastError as a rejection", async () => {
  const { browser, chrome, calls } = loadChrome();
  assert.deepEqual(await browser.runtime.sendNativeMessage("shisuko", { cmd: "start" }), { ok: true, started: true });
  assert.deepEqual(calls.at(-1), ["sendNativeMessage", "shisuko", { cmd: "start" }]);
  chrome.runtime.sendNativeMessage = (_app, _msg, callback) => {
    chrome.runtime.lastError = { message: "Specified native messaging host not found." };
    callback();
    chrome.runtime.lastError = null;
  };
  // The bridge reads the chrome method at call time, so the swapped stub is what the wrapper runs.
  await assert.rejects(browser.runtime.sendNativeMessage("shisuko", { cmd: "start" }), /not found/);
});

test("a Chrome runtime without sendNativeMessage gets no wrapper for it", () => {
  const { chrome } = loadChrome();
  delete chrome.runtime.sendNativeMessage;
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  assert.equal(typeof sandbox.browser.runtime.sendNativeMessage, "undefined");
  assert.equal(typeof sandbox.browser.permissions.request, "function");
});

// Chrome grants a permission only during the user gesture, and the popup's Start click spends it
// at its first await. The popup calls browser.permissions.request before that await; the bridge
// must hand the call to chrome.permissions.request within that same call, not a tick later.
test("Chrome permissions.request reaches chrome.permissions.request before the call returns", async () => {
  const { browser, chrome } = loadChrome();
  const requests = [];
  chrome.permissions.request = (perms, callback) => { requests.push(perms); callback(true); };
  const pending = browser.permissions.request({ permissions: ["nativeMessaging"] });
  assert.deepEqual(requests, [{ permissions: ["nativeMessaging"] }], "requested synchronously, inside the gesture");
  assert.equal(await pending, true);
});

// The popup grants nativeMessaging while the service worker runs; Chrome adds the method only
// then, possibly on a chrome.runtime it rebuilds. A bridge that decided at load would answer
// "permission missing" to the Start button until the worker restarted.
test("sendNativeMessage granted after load reaches the bridge, on the old runtime or a rebuilt one", async () => {
  const { chrome } = loadChrome();
  const stub = chrome.runtime.sendNativeMessage;
  delete chrome.runtime.sendNativeMessage;
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  const { browser } = sandbox;
  assert.equal(typeof browser.runtime.sendNativeMessage, "undefined");
  chrome.runtime.sendNativeMessage = stub; // granted: the method lands on the runtime there was
  assert.deepEqual(await browser.runtime.sendNativeMessage("shisuko", { cmd: "start" }), { ok: true, started: true });
  const calls = [];
  chrome.runtime = { // granted: Chrome hands out a new runtime object with the method on it
    lastError: null,
    sendNativeMessage(application, msg, callback) { calls.push([application, msg]); callback({ ok: true, already: true }); },
  };
  assert.deepEqual(await browser.runtime.sendNativeMessage.call(browser.runtime, "shisuko", { cmd: "start" }), { ok: true, already: true });
  assert.deepEqual(calls, [["shisuko", { cmd: "start" }]]);
  delete chrome.runtime.sendNativeMessage; // revoked: missing again, which background.js reads as no permission
  assert.equal(typeof browser.runtime.sendNativeMessage, "undefined");
  assert.equal(browser.runtime.getURL(""), "chrome-extension://test/", "the rest of runtime still comes through");
});

// The update nudges: badge, notifications and the release page tab. Chrome's action and
// notifications namespaces answer callbacks like the rest; the events pass through untouched.
test("Chrome action and notifications are bridged to promises, with the events passed through", async () => {
  const { chrome } = loadChrome();
  const calls = [];
  const onClicked = { addListener() {} };
  chrome.action = {
    setBadgeText(details, callback) { calls.push(["setBadgeText", details]); callback(); },
    setBadgeBackgroundColor(details, callback) { calls.push(["setBadgeBackgroundColor", details]); callback(); },
  };
  chrome.notifications = {
    create(id, options, callback) { calls.push(["create", id, options]); callback(id); },
    clear(id, callback) { calls.push(["clear", id]); callback(true); },
    onClicked,
  };
  chrome.tabs.create = (options, callback) => { calls.push(["tabs.create", options]); callback({ id: 9 }); };
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(source).runInContext(sandbox);
  const { browser } = sandbox;
  assert.equal(await browser.action.setBadgeText({ text: "1" }), undefined);
  assert.equal(await browser.action.setBadgeBackgroundColor({ color: "#5b6fb8" }), undefined);
  assert.equal(await browser.notifications.create("shisuko-update", { type: "basic", title: "t", message: "m" }), "shisuko-update");
  assert.equal(await browser.notifications.clear("shisuko-update"), true);
  assert.equal(browser.notifications.onClicked, onClicked);
  assert.deepEqual(await browser.tabs.create({ url: "https://example.com/" }), { id: 9 });
  assert.deepEqual(calls, [
    ["setBadgeText", { text: "1" }],
    ["setBadgeBackgroundColor", { color: "#5b6fb8" }],
    ["create", "shisuko-update", { type: "basic", title: "t", message: "m" }],
    ["clear", "shisuko-update"],
    ["tabs.create", { url: "https://example.com/" }],
  ]);
  // runtime.onStartup and onInstalled are Chrome's own, reachable through the prototype.
  chrome.runtime.onStartup = { addListener() {} };
  assert.equal(browser.runtime.onStartup, chrome.runtime.onStartup);
});

test("a Chrome without action or notifications (a content script) gets neither wrapper", () => {
  const { chrome } = loadChrome();
  delete chrome.action;
  delete chrome.notifications;
  const sandbox = { chrome, console, Promise, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  assert.doesNotThrow(() => new vm.Script(source).runInContext(sandbox));
  assert.equal(sandbox.browser.action, undefined);
  assert.equal(sandbox.browser.notifications, undefined);
});

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const ADDON = path.join(__dirname, "..");

function loadSchema() {
  const sandbox = {};
  vm.createContext(sandbox);
  new vm.Script(fs.readFileSync(path.join(ADDON, "settings.js"), "utf8")).runInContext(sandbox);
  new vm.Script("globalThis.schema = SHISUKO_DEFAULT_SETTINGS;").runInContext(sandbox);
  return JSON.parse(JSON.stringify(sandbox.schema));
}

test("the schema is frozen and has the expected core keys", () => {
  const schema = loadSchema();
  for (const key of ["enabled", "serverUrl", "fontScale", "pauseOnHover", "lingerSeconds", "autoMine"]) {
    assert.ok(key in schema, `missing ${key}`);
  }
  // The server reads Anki (--anki-url); the extension names no AnkiConnect address.
  assert.equal("ankiUrl" in schema, false);
  assert.equal(schema.serverUrl, "http://127.0.0.1:8790");
  // The status badge shows until the viewer switches it off (Alt+Shift+H); the progress messages
  // are a separate, narrower switch.
  assert.equal(schema.statusBadge, true);
  assert.equal(schema.showStatus, true);
});

// The word colours need Anki and a deck, so both start off; of the switches that refine them,
// particles and katakana words count as known, and names and Latin text are blue, only when the
// viewer says so, and the viewer's own list starts empty.
test("the word colours start off, the particle, katakana and name switches start off, the known list empty", () => {
  const schema = loadSchema();
  assert.equal(schema.cardStatus, false);
  assert.equal(schema.pitchAccent, false);
  assert.equal(schema.particlesKnown, false);
  assert.equal(schema.katakanaKnown, false);
  assert.equal(schema.properNames, false);
  assert.equal(schema.knownWords, "");
});

test("popup.html has an input for every setting", () => {
  const schema = loadSchema();
  const html = fs.readFileSync(path.join(ADDON, "popup.html"), "utf8");
  const ids = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
  for (const key of Object.keys(schema)) assert.ok(ids.has(key), `popup.html has no input with id="${key}"`);
});

// Firefox MV3 grants host permissions only when the user asks for them, and it will not even offer
// the YouTube origins unless the manifest lists them, so a content script match with no matching
// host permission can never run on a normal page load.
test("every content script match is a host permission, and YouTube is listed", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ADDON, "manifest.json"), "utf8"));
  const hosts = new Set(manifest.host_permissions);
  for (const origin of ["*://www.youtube.com/*", "*://m.youtube.com/*", "*://youtube.com/*"]) {
    assert.ok(hosts.has(origin), `host_permissions is missing ${origin}`);
  }
  for (const entry of manifest.content_scripts) {
    for (const match of entry.matches) assert.ok(hosts.has(match), `content script match ${match} is not a host permission`);
  }
});

test("the content script runs as soon as the DOM is there, not after load", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ADDON, "manifest.json"), "utf8"));
  for (const entry of manifest.content_scripts) assert.equal(entry.run_at, "document_end");
});

// The keyboard commands: the background forwards each by name to the watched tab, and the
// content script's listener answers to these names and no other.
test("the manifest names the five commands, and the content script handles each", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ADDON, "manifest.json"), "utf8"));
  const keys = Object.fromEntries(Object.entries(manifest.commands).map(([name, cmd]) => [name, cmd.suggested_key.default]));
  assert.deepEqual(keys, {
    "toggle-subtitles": "Alt+Shift+S", "toggle-transcript": "Alt+Shift+L", "mine-current": "Alt+Shift+M",
    "mark-known": "Alt+Shift+K", "toggle-status": "Alt+Shift+H",
  });
  assert.equal(new Set(Object.values(keys)).size, Object.keys(keys).length, "no shortcut twice");
  const content = fs.readFileSync(path.join(ADDON, "content.js"), "utf8");
  for (const name of Object.keys(keys)) assert.ok(content.includes(`msg.name === "${name}"`), `content.js does not handle ${name}`);
});

test("settings.js is loaded before the scripts that use it", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ADDON, "manifest.json"), "utf8"));
  // The background reads no note (the server does): words.js is the content script's alone.
  assert.deepEqual(manifest.background.scripts, ["browser-api.js", "settings.js", "background.js"]);
  for (const entry of manifest.content_scripts) assert.deepEqual(entry.js, ["browser-api.js", "settings.js", "words.js", "content.js"]);
  const html = fs.readFileSync(path.join(ADDON, "popup.html"), "utf8");
  assert.ok(html.indexOf('src="browser-api.js"') < html.indexOf('src="settings.js"'));
  assert.ok(html.indexOf('src="settings.js"') < html.indexOf('src="popup.js"'));
  for (const file of ["background.js", "content.js"]) {
    assert.ok(!/const DEFAULT_SETTINGS = (Object\.freeze\()?\{/.test(fs.readFileSync(path.join(ADDON, file), "utf8")), `${file} still defines its own defaults`);
  }
});

// The "Start server" button talks to a native host, which needs nativeMessaging. Optional, not
// required: the install prompt stays as it is, and the permission is asked for on the first click.
test("nativeMessaging is an optional permission, never a required one", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ADDON, "manifest.json"), "utf8"));
  assert.ok(manifest.optional_permissions.includes("nativeMessaging"));
  assert.ok(!manifest.permissions.includes("nativeMessaging"));
  const html = fs.readFileSync(path.join(ADDON, "popup.html"), "utf8");
  assert.match(html, /<button id="start-server" type="button" class="linkbtn hidden">Start server<\/button>/);
});

// The update check tells the viewer about a release with a system notification, which needs the
// permission at install: the popup cannot ask for it at browser start, when the notification
// goes out. The extension never installs itself, so the manifest names no update_url, neither
// Firefox's nor Chrome's: its updates come from addons.mozilla.org and, for a Chrome Web Store
// install, from the store. It carries no key: the store keeps the item's own and needs none, and
// it refuses any key on a new item and one that is not the item's own on an update.
test("notifications is a required permission, and the manifest names no update_url", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ADDON, "manifest.json"), "utf8"));
  assert.ok(manifest.permissions.includes("notifications"));
  // By its full name, so a reviewer who searches the package for the API finds the call.
  assert.match(fs.readFileSync(path.join(ADDON, "background.js"), "utf8"), /browser\.notifications\.create\(/, "the permission has its use");
  assert.equal(manifest.browser_specific_settings.gecko.update_url, undefined);
  assert.equal(manifest.update_url, undefined);
  assert.equal(manifest.key, undefined);
  const html = fs.readFileSync(path.join(ADDON, "popup.html"), "utf8");
  assert.match(html, /<div id="update-banner" class="banner notice hidden">/);
  for (const id of ["update-text", "update-now", "update-later", "update-release", "update-result"]) {
    assert.ok(html.includes(`id="${id}"`), `popup.html has no element with id="${id}"`);
  }
  // The server checks for releases; the popup has no button that would make the extension check.
  assert.ok(!html.includes('id="check-updates"'));
});

// The extension makes no request beyond the viewer's machine: the server asks GitHub for the
// newest release (UpdateChecker in server.py) and the extension reads it from /health. The two
// fetch() calls, apiRequest()'s and fetchClip()'s, go to the server's address (127.0.0.1 by
// default), and the one https address in the code is the release page the popup opens in a tab
// when the viewer clicks.
test("no request leaves the machine: fetches go to the server, and no remote address but a link", () => {
  const files = fs.readdirSync(ADDON).filter((name) => /\.(js|html)$/.test(name));
  const fetches = [];
  const remote = [];
  for (const name of files) {
    const text = fs.readFileSync(path.join(ADDON, name), "utf8");
    for (const match of text.matchAll(/\bfetch\s*\(([^)]*)\)/g)) fetches.push(`${name}: ${match[1]}`);
    for (const word of ["XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource"]) {
      assert.ok(!text.includes(word), `${name} uses ${word}`);
    }
    for (const match of text.matchAll(/https?:\/\/[^\s"'`)]+/g)) {
      if (!/^http:\/\/(127\.0\.0\.1|localhost)[:/]/.test(match[0])) remote.push(`${name}: ${match[0]}`);
    }
  }
  // apiRequest()'s and fetchClip()'s, both to the server's address.
  assert.deepEqual(fetches, ["background.js: base + path, init", "background.js: base + path, { signal: controller.signal }"]);
  assert.deepEqual(remote, ["popup.js: https://github.com/Multysquid/shisu-ko/releases/latest"]);
  const popup = fs.readFileSync(path.join(ADDON, "popup.js"), "utf8");
  assert.match(popup, /browser\.tabs\.create\(\{ url \}\)/, "the release page is opened in a tab, never fetched");
  assert.equal(JSON.parse(fs.readFileSync(path.join(ADDON, "settings.js"), "utf8").match(/serverUrl: ("[^"]+")/)[1]), "http://127.0.0.1:8790");
});

// The Anki sync after mining is the server's job (AnkiWatcher in server/server.py): the extension
// never asks AnkiConnect for `sync`, needs no alarm, and has no setting for it. 0.20.0 and 0.21.0
// stored `ankiAutoSync`; nothing reads it now, so a stored value is inert. The permissions
// themselves are pinned in scripts/tests/build.test.mjs.
test("the extension never calls AnkiConnect's sync, holds no alarms and no ankiAutoSync", () => {
  const files = fs.readdirSync(ADDON).filter((name) => /\.(js|html|json)$/.test(name));
  for (const name of files) {
    const text = fs.readFileSync(path.join(ADDON, name), "utf8");
    assert.doesNotMatch(text, /["'`]sync["'`]/, `${name} names the AnkiConnect action "sync"`);
    assert.doesNotMatch(text, /\balarms\b/, `${name} uses alarms`);
    assert.doesNotMatch(text, /ankiAutoSync|ankiSync/, `${name} still knows the old Anki sync`);
  }
});

// Mining is the server's (Miner in server/server.py): it attaches the audio and the picture to the
// note Yomitan made, and takes the viewer's own mine through /sync. The extension never writes to
// Anki and has no matcher of its own. Its one file save is the no-Anki fallback, saveMine() in
// background.js, the only user of `downloads`. 0.21.1 stored `mineTarget` and
// `mineFallbackDownload`; nothing reads them now.
test("the extension never writes to Anki, and only saveMine() saves files", () => {
  const files = fs.readdirSync(ADDON).filter((name) => /\.(js|html|json)$/.test(name));
  for (const name of files) {
    const text = fs.readFileSync(path.join(ADDON, name), "utf8");
    for (const action of ["updateNoteFields", "storeMediaFile", "addNote", "updateNote", "addNotes"]) {
      assert.doesNotMatch(text, new RegExp(`["'\`]${action}["'\`]`), `${name} calls AnkiConnect's ${action}`);
    }
    if (name === "background.js") {
      assert.equal(text.match(/browser\.downloads\.download\(/g).length, 2, "downloadBytes() alone saves, on either path");
    } else if (name !== "browser-api.js" && name !== "manifest.json") {
      assert.doesNotMatch(text, /\.downloads\b|"downloads"/, `${name} uses browser.downloads`);
    }
    assert.doesNotMatch(text, /mineTarget|mineFallbackDownload|SHISUKO_MATCH|premine/, `${name} still knows the extension's own mining`);
  }
  assert.ok(!fs.existsSync(path.join(ADDON, "match.js")), "match.js moved to the server (best_cue in server.py)");
});

"use strict";

// popup.js keeps copies of a few things it cannot import: the font rule and the preset stacks from
// content.js (for the sample under the font field), the model name rule from server.py (for the
// hint under the model field) and the version helpers from background.js (for judging the server
// that comes back from an update). A copy that drifts would preview one thing and apply another,
// so the files are read as text and the copies compared, tolerant of whitespace only. On top of
// that, the two fontStack() copies are run side by side, and the model hint's reading of /health is
// run against the answers the server gives.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const { loadContent } = require("./_loadContent");

const ADDON = path.join(__dirname, "..");
const POPUP = fs.readFileSync(path.join(ADDON, "popup.js"), "utf8");
const CONTENT = fs.readFileSync(path.join(ADDON, "content.js"), "utf8");
const SERVER = fs.readFileSync(path.join(ADDON, "..", "server", "server.py"), "utf8");

const squash = (s) => s.replace(/\s+/g, " ").trim();

// The text of `const NAME = ...;` in a JS file, up to the first `;` that ends a line.
function jsConst(source, name, file) {
  const m = source.match(new RegExp(`^\\s*const ${name} = ([^]*?);[ \\t]*$`, "m"));
  assert.ok(m, `${file} defines no const ${name}`);
  return m[1];
}

// A JS regex literal's pattern as Python would write it: delimiters and flags off, `\/` back to `/`.
function jsRegexPattern(literal) {
  const m = literal.match(/^\/(.*)\/([a-z]*)$/s);
  assert.ok(m, `${literal} is not a regex literal`);
  return { pattern: m[1].replace(/\\\//g, "/"), flags: m[2] };
}

test("popup.js and content.js share the font family rule and the preset stacks", () => {
  assert.equal(jsConst(POPUP, "FONT_FAMILY_RE", "popup.js"), jsConst(CONTENT, "FONT_FAMILY_RE", "content.js"));
  assert.equal(jsConst(POPUP, "GOTHIC_STACK", "popup.js"), jsConst(CONTENT, "GOTHIC_STACK", "content.js"));
  assert.equal(squash(jsConst(POPUP, "SUB_FONTS", "popup.js")), squash(jsConst(CONTENT, "SUB_FONTS", "content.js")));
});

test("the popup's dock select offers exactly the docks content.js knows, the default first", () => {
  const html = fs.readFileSync(path.join(ADDON, "popup.html"), "utf8");
  const select = html.match(/<select id="subDock">([^]*?)<\/select>/);
  assert.ok(select, "popup.html has no subDock select");
  const options = [...select[1].matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);
  const docks = JSON.parse(jsConst(CONTENT, "SUB_DOCKS", "content.js"));
  assert.deepEqual(options, docks);
  assert.equal(loadContent().api.state.settings.subDock, docks[0]);
});

test("popup.js mirrors the server's model name rule", () => {
  const js = jsRegexPattern(jsConst(POPUP, "MODEL_NAME_RE", "popup.js"));
  assert.equal(js.flags, "", "the server's rule has no flags");
  const py = SERVER.match(/^MODEL_NAME_RE = re\.compile\(r"([^"]*)"\)/m);
  assert.ok(py, "server.py defines no MODEL_NAME_RE raw string");
  assert.equal(js.pattern, py[1]);
  // The second half of the rule, the ".." ban, lives outside the pattern in both.
  assert.match(POPUP, /!name\.includes\("\.\."\)/);
  assert.match(SERVER, /"\.\." not in name/);
});

// popup.js runs as a top-level script; its consts and functions sit in the global lexical scope of
// the context, so a second script in that context reads them out (as _loadBackground.js does) and
// can set the last /health answer the popup keeps in `health`.
function loadPopupHelpers() {
  const sandbox = { document: { addEventListener: () => {} }, console };
  vm.createContext(sandbox);
  new vm.Script(fs.readFileSync(path.join(ADDON, "settings.js"), "utf8")).runInContext(sandbox);
  new vm.Script(POPUP, { filename: "popup.js" }).runInContext(sandbox);
  return new vm.Script("({ fontStack, modelNameOk, modelErrorFor, UPDATE_LOST_HINT, setHealth: (h) => { health = h; } })").runInContext(sandbox);
}

test("the two fontStack copies build the same CSS value", () => {
  const popup = loadPopupHelpers();
  const { api } = loadContent();
  const cases = [
    ["default", ""], ["default", "Yu Gothic UI"], ["gothic-bold", " 游ゴシック "], ["rounded", "M PLUS 1p"], ["mincho", "Klee"],
    ["default", 'Yu "Gothic"'], ["default", "a\\b"], ["nonsense", "Meiryo"], ["mincho", undefined], ["rounded", "x".repeat(101)],
  ];
  for (const [preset, family] of cases) {
    assert.equal(popup.fontStack(preset, family), api.fontStack(preset, family), `${preset} / ${family}`);
  }
});

test("the popup's early hint agrees with the server's rule on model names", () => {
  const { modelNameOk } = loadPopupHelpers();
  for (const ok of ["large-v3", "large-v3-turbo", "kotoba-tech/kotoba-whisper-v2.0-faster", "a", "a/b", "x".repeat(96), `${"x".repeat(96)}/${"y".repeat(96)}`]) {
    assert.equal(modelNameOk(ok), true, ok);
  }
  for (const bad of ["", "../x", "a/../b", "a..b", "/abs", "a/", "a/b/c", "-a", ".a", "a b", "C:\\models", "x".repeat(97), "a/-b"]) {
    assert.equal(modelNameOk(bad), false, bad);
  }
});

// /health reports a failed model under the spelling the server keeps (large-v3-turbo for turbo and
// mobiuslabsgmbh/faster-whisper-large-v3-turbo alike) and lists every spelling of the same weights in
// `names` (model_spellings() in server.py: the aliases in faster-whisper's table, then the repo id),
// so the verdict lands under the field whichever of them the viewer typed, and under no other name.
// The answers below are the ones server/tests/test_model_switch.py asserts health() gives; the
// shape is read off health() first, so a popup expecting a key the server dropped fails here.
test("the model hint takes the server's verdict under every spelling of the failed name", () => {
  assert.match(SERVER, /"model_error": \(\{"model": error\[0\], "error": error\[1\], "names": model_spellings\(error\[0\]\)\}/);
  const { modelErrorFor, setHealth } = loadPopupHelpers();
  const error = "could not reach Hugging Face to download 'large-v3-turbo'";
  const spellings = ["large-v3-turbo", "turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"];
  setHealth({ default_model: "large-v3", model_error: { model: "large-v3-turbo", error, names: spellings } });
  for (const name of spellings) assert.equal(modelErrorFor(name), error, name);
  for (const name of ["Turbo", "large-v3", "large", "small", "mobiuslabsgmbh/faster-whisper-large-v3-turbo/x"]) {
    assert.equal(modelErrorFor(name), null, name); // case and all: repo ids are case-sensitive
  }
  assert.equal(modelErrorFor(""), null); // the empty field asks for the default, which did not fail

  // The server's default failed: the empty field asks for it, an alias of it as well.
  setHealth({ default_model: "large-v3", model_error: { model: "large-v3", error: "boom", names: ["large-v3", "large", "Systran/faster-whisper-large-v3"] } });
  assert.equal(modelErrorFor(""), "boom");
  assert.equal(modelErrorFor("large"), "boom");
  assert.equal(modelErrorFor("large-v3-turbo"), null);

  // A repo id outside faster-whisper's table is its own only spelling.
  setHealth({ model_error: { model: "kotoba-tech/kotoba-whisper-v2.0-faster", error: "boom", names: ["kotoba-tech/kotoba-whisper-v2.0-faster"] } });
  assert.equal(modelErrorFor("kotoba-tech/kotoba-whisper-v2.0-faster"), "boom");
  assert.equal(modelErrorFor("kotoba-whisper-v2.0-faster"), null);

  // An older server without `names` (or one with junk in it): the reported spelling still matches, nothing else.
  setHealth({ model_error: { model: " large-v3-turbo ", error } });
  assert.equal(modelErrorFor("large-v3-turbo"), error);
  assert.equal(modelErrorFor("turbo"), null);
  setHealth({ model_error: { model: "large-v3-turbo", error, names: [5, null, "turbo"] } });
  assert.equal(modelErrorFor("turbo"), error);

  // No verdict, a malformed one, or no server at all.
  setHealth({ model_error: null });
  assert.equal(modelErrorFor("turbo"), null);
  setHealth({ model_error: { model: 7, error, names: ["turbo"] } });
  assert.equal(modelErrorFor("turbo"), null);
  setHealth({ model_error: { model: "turbo", error: "", names: ["turbo"] } });
  assert.equal(modelErrorFor("turbo"), null);
  setHealth(null);
  assert.equal(modelErrorFor("turbo"), null);
});

// The text of `function NAME(...) {` up to its closing brace at column 0.
function jsFunction(source, name, file) {
  const m = source.match(new RegExp(`^function ${name}\\([^)]*\\) \\{\\n[^]*?\\n\\}`, "m"));
  assert.ok(m, `${file} defines no function ${name}`);
  return m[0];
}

// popup.js judges the server that comes back from an update (version >= the release, or the old
// version once the restart must be over) with copies of the background's version helpers and its
// shutdown allowance; the two must read a version, and the clock, the same way. The hint for an
// update that got no answer names the background's window, from a copy of that too.
test("popup.js and background.js share the version helpers, the shutdown allowance and the update window", () => {
  const BACKGROUND = fs.readFileSync(path.join(ADDON, "background.js"), "utf8");
  for (const name of ["parseVersion", "compareVersions"]) {
    assert.equal(jsFunction(POPUP, name, "popup.js"), jsFunction(BACKGROUND, name, "background.js"), name);
  }
  for (const name of ["UPDATE_SHUTDOWN_MS", "UPDATE_WINDOW_MS"]) {
    assert.equal(jsConst(POPUP, name, "popup.js"), jsConst(BACKGROUND, name, "background.js"), name);
  }
  const seconds = Number(jsConst(BACKGROUND, "UPDATE_WINDOW_MS", "background.js")) / 1000;
  const { UPDATE_LOST_HINT } = loadPopupHelpers();
  assert.match(UPDATE_LOST_HINT, new RegExp(`\\b${seconds} s after the update\\b`));
});

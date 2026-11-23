"use strict";

// start-options.js: the server options the popup's Start server button may pass, and their check.
// server/tests/test_native_host.py keeps the table equal to native_host.py's START_OPTIONS, which
// checks every option again; here the parser the popup and the background share.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "start-options.js");

function load() {
  const sandbox = {};
  vm.createContext(sandbox);
  new vm.Script(fs.readFileSync(SOURCE, "utf8"), { filename: SOURCE }).runInContext(sandbox);
  return new vm.Script("({ SHISUKO_START_OPTIONS, SHISUKO_START_MAX_OPTION_TOKENS, parseStartOptions })").runInContext(sandbox);
}

const { SHISUKO_START_OPTIONS, SHISUKO_START_MAX_OPTION_TOKENS, parseStartOptions } = load();
const plain = (value) => JSON.parse(JSON.stringify(value));
const parse = (text) => plain(parseStartOptions(text));

test("no options: an empty list, whatever the field holds of blanks", () => {
  for (const text of ["", "   ", "\n\t ", null, undefined]) assert.deepEqual(parse(text), { ok: true, options: [] }, String(text));
});

test("options and their values become the words the launcher is sent", () => {
  assert.deepEqual(parse("--lyrics off --beam-size 1 --no-update"), { ok: true, options: ["--lyrics", "off", "--beam-size", "1", "--no-update"] });
  assert.deepEqual(parse("--window=45.5  --device cpu"), { ok: true, options: ["--window", "45.5", "--device", "cpu"] });
  assert.deepEqual(parse("--model-idle-minutes 0\n--lookahead 0"), { ok: true, options: ["--model-idle-minutes", "0", "--lookahead", "0"] });
});

test("a word of a set typed in another case goes in the set's spelling", () => {
  assert.deepEqual(parse("--lyrics OFF --log-level debug --device CPU"), { ok: true, options: ["--lyrics", "off", "--log-level", "DEBUG", "--device", "cpu"] });
});

test("anything off the list is refused, naming the word", () => {
  const cases = {
    "--host 0.0.0.0": "--host is not an option the Start button passes",
    "--port 8791": "--port is not an option the Start button passes",
    "--model large-v3": "--model is not an option the Start button passes",
    "--anki-url http://x:1": "--anki-url is not an option the Start button passes",
    "--cookies-from-browser chrome": "--cookies-from-browser is not an option the Start button passes",
    "--js-runtime node": "--js-runtime is not an option the Start button passes",
    "--initial-prompt hi": "--initial-prompt is not an option the Start button passes",
    "--check": "--check is not an option the Start button passes",
    "--Lyrics off": "--Lyrics is not an option the Start button passes",
    "--mod x": "--mod is not an option the Start button passes",
    "off": "off is not an option the Start button passes",
    "--lyrics off extra": "extra is not an option the Start button passes",
    "--lyrics": "--lyrics needs a value",
    "--lyrics=": "--lyrics needs a value",
    "--lyrics maybe": "--lyrics takes auto or off",
    "--lyrics off&calc": "--lyrics takes auto or off",
    "--no-update=1": "--no-update takes no value",
    "--beam-size 0": "--beam-size takes a whole number from 1 to 16",
    "--beam-size 17": "--beam-size takes a whole number from 1 to 16",
    "--beam-size 1.5": "--beam-size takes a whole number from 1 to 16",
    "--beam-size -1": "--beam-size takes a whole number from 1 to 16",
    "--window 1e3": "--window takes a number from 1 to 600",
    "--window 0x10": "--window takes a number from 1 to 600",
    "--window 10%": "--window takes a number from 1 to 600",
    "--window １０": "--window takes a number from 1 to 600",
    "--window Infinity": "--window takes a number from 1 to 600",
    "--window 601": "--window takes a number from 1 to 600",
  };
  for (const [text, error] of Object.entries(cases)) assert.deepEqual(parse(text), { ok: false, error }, text);
});

test("a long stray word is cut in the message", () => {
  const res = parse(`--${"x".repeat(100)}`);
  assert.equal(res.ok, false);
  assert.ok(res.error.length < 100 && res.error.includes("…"), res.error);
});

test("more words than the launcher takes are refused", () => {
  const ok = Array(SHISUKO_START_MAX_OPTION_TOKENS).fill("--no-update").join(" ");
  assert.equal(parse(ok).ok, true);
  assert.deepEqual(parse(`${ok} --no-update`), { ok: false, error: `more than ${SHISUKO_START_MAX_OPTION_TOKENS} words of options` });
});

test("every word the parser can pass is one no command line reads into", () => {
  // The launcher's command line goes through cmd.exe on Windows: none of & | < > ^ % ! " ( ) or a
  // space may come from a message. The host checks again; the table itself allows none of them.
  const safe = /^[A-Za-z0-9._-]+$/;
  for (const [name, rule] of Object.entries(SHISUKO_START_OPTIONS)) {
    assert.match(name, /^--[a-z][a-z-]+$/);
    if (Array.isArray(rule)) for (const word of rule) assert.match(word, safe, name);
    else if (rule !== null) {
      assert.deepEqual(Object.keys(rule).filter((key) => !["min", "max", "int"].includes(key)), [], name);
      assert.ok(rule.min <= rule.max, name);
    }
  }
});

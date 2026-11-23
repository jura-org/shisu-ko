"use strict";

/*
 * The server options the popup's "Start server" button may pass, and their check. Loaded before
 * background.js (manifest.json, service-worker.js) and popup.js (popup.html); the setting that
 * holds them is `startOptions` (settings.js), what the viewer typed.
 *
 * server/native_host.py holds the same table (START_OPTIONS) and checks every option again before
 * the launcher sees one: the host decides. This copy gives the popup its hint and the background a
 * refusal it can word before asking the host. server/tests/test_native_host.py keeps the two equal
 * and reads the table below as JSON, so it stays a plain object literal: quoted keys, no comment
 * inside, no trailing comma.
 *
 * option -> what follows it: null nothing; a list one of those words (a word typed in another case
 * is put in the list's spelling, which is what goes to the host); {min, max} a number in that
 * range, written as digits with at most one decimal point ({..., int: true} a whole number). Left
 * out on purpose, and refused: --host and --port (the button's server is the one on
 * 127.0.0.1:8790), --model (the model field above picks the model), anything taking an address, a
 * path, a program, a browser's cookies, code from the network or free text, and the options that
 * exit instead of serving. The list in native_host.py says which.
 */
const SHISUKO_START_OPTIONS = Object.freeze({
  "--device": ["auto", "cuda", "mlx", "cpu"],
  "--compute-type": ["auto", "float16", "float32", "bfloat16", "int8", "int8_float16", "int8_float32", "int8_bfloat16", "int16"],
  "--beam-size": {"min": 1, "max": 16, "int": true},
  "--language-patience": {"min": 0, "max": 3600},
  "--lyrics": ["auto", "off"],
  "--human-subtitles": ["auto", "off"],
  "--sentence-ends": ["auto", "off"],
  "--anki-sync": ["auto", "off"],
  "--mine": ["auto", "off"],
  "--mine-picture": ["on", "off"],
  "--window": {"min": 1, "max": 600},
  "--first-window": {"min": 1, "max": 600},
  "--lookahead": {"min": 0, "max": 86400},
  "--max-cue-chars": {"min": 1, "max": 200, "int": true},
  "--max-cue-seconds": {"min": 0, "max": 60},
  "--min-cue-seconds": {"min": 0, "max": 60},
  "--idle-minutes": {"min": 0, "max": 10080, "int": true},
  "--model-idle-minutes": {"min": 0, "max": 10080},
  "--retry-after": {"min": 0, "max": 3600},
  "--client-timeout": {"min": 0, "max": 86400},
  "--cpu-threads": {"min": 0, "max": 256, "int": true},
  "--update-check": ["on", "off"],
  "--log-level": ["DEBUG", "INFO", "WARNING", "ERROR"],
  "--no-update": null
});

// More words than this are not options a viewer typed (native_host.py has the same limit).
const SHISUKO_START_MAX_OPTION_TOKENS = 64;

// The words the host is sent for `text`, the setting as typed: {ok: true, options: [...]}, the
// list empty for no options, or {ok: false, error} naming the first word that is wrong. Options
// are separated by spaces; "--name=value" is read as "--name value". Pure, so the popup can say
// under the field what the button will pass before anything is saved.
function parseStartOptions(text) {
  const words = String(text == null ? "" : text).trim().split(/\s+/).filter(Boolean);
  const options = [];
  const shown = (word) => (word.length > 40 ? `${word.slice(0, 40)}…` : word);
  for (let i = 0; i < words.length; i++) {
    let name = words[i];
    let inline = null;
    const eq = name.indexOf("=");
    if (name.startsWith("--") && eq > 2) {
      inline = name.slice(eq + 1);
      name = name.slice(0, eq);
    }
    if (!Object.hasOwn(SHISUKO_START_OPTIONS, name)) return { ok: false, error: `${shown(name)} is not an option the Start button passes` };
    const rule = SHISUKO_START_OPTIONS[name];
    options.push(name);
    if (rule === null) {
      if (inline !== null) return { ok: false, error: `${name} takes no value` };
      continue;
    }
    const value = inline !== null ? inline : words[++i];
    if (value === undefined || value === "") return { ok: false, error: `${name} needs a value` };
    if (Array.isArray(rule)) {
      const word = rule.find((candidate) => candidate.toLowerCase() === value.toLowerCase());
      if (!word) return { ok: false, error: `${name} takes ${rule.join(" or ")}` };
      options.push(word);
    } else {
      const shape = rule.int ? /^[0-9]{1,6}$/ : /^[0-9]{1,6}(\.[0-9]{1,3})?$/;
      const number = Number(value);
      if (!shape.test(value) || number < rule.min || number > rule.max) {
        return { ok: false, error: `${name} takes ${rule.int ? "a whole number" : "a number"} from ${rule.min} to ${rule.max}` };
      }
      options.push(value);
    }
  }
  if (options.length > SHISUKO_START_MAX_OPTION_TOKENS) return { ok: false, error: `more than ${SHISUKO_START_MAX_OPTION_TOKENS} words of options` };
  return { ok: true, options };
}

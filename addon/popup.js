"use strict";

// Every setting has an input with the same id in popup.html (checked by addon/tests/settings.test.js).
const FIELDS = Object.keys(SHISUKO_DEFAULT_SETTINGS);

// Firefox MV3 treats host permissions as optional: nothing is granted at install, so the content
// script never runs until the user allows youtube.com (clicking the toolbar icon only grants the
// current tab, for that visit). The banner makes the missing grant visible and fixable.
const YOUTUBE_ORIGINS = ["*://www.youtube.com/*", "*://m.youtube.com/*", "*://youtube.com/*"];

// The server's own rule for a model name (MODEL_NAME_RE in server.py), mirrored for an early hint
// only: the server decides, and a name the rule refuses would otherwise be taken for a directory.
const MODEL_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(\/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$/;
const MODEL_HINT = "Applies while a video plays. A model not downloaded yet is fetched from Hugging Face on first use (faster-whisper/CTranslate2 format only).";
const MODEL_NAME_HINT = "Use a model size such as large-v3 or a Hugging Face repo id such as owner/name";
// A model load takes seconds to minutes; while the popup is open its status line follows along.
const HEALTH_REFRESH_MS = 2000;
// The deck hint is a snapshot of Anki at the last ask, and the options page (the same popup.html)
// lives for hours: a verdict that Anki is away, or has not allowed the extension yet, is asked
// again this often while a word-colour feature is on (see retryDecks).
const DECKS_RETRY_MS = 30000;
const OFFLINE_HINT = "start server/run.cmd or docker/up.cmd";

// The "Start server" button. The server answers /health only once its model is loaded, and a
// cold start reads up to 3 GB of weights from disk: 10-40 s on a GPU, longer on a CPU, after the
// launcher's own venv check, and a first use downloads them before that. The background owns the
// launch and its 90 s window (START_WINDOW_MS there) and hands the popup the deadline; after it
// the viewer is sent to the log instead of watching a badge that never changes, and told that the
// server may still be on its way, so a second start waits for a look at the log. The launcher
// names the log it opened (SHISUKO_HOME moves it); without one (Windows, where the server has a
// window, or a launch the launcher did not make) the hint names the usual places.
function startNotUpHint(log) {
  const where = typeof log === "string" && log ? log : "its window (Windows) or ~/.shisu-ko/server.log";
  return `No answer from the server after 90 s: look at ${where} before starting it again; a first use downloads the model, which takes minutes`;
}
const START_NOT_UP_HINT = startNotUpHint(null);
// The launcher checks 127.0.0.1:8790 itself; when it saw a server there and this popup sees none,
// the two are looking at different addresses.
const START_ELSEWHERE_HINT = "The launcher finds a server on 127.0.0.1:8790, but the server URL below does not answer; check it under Anki, clips and server";
const START_PERMISSION_HINT = "Allow Shisu-ko to talk to its launcher to start the server from here";
const RUNTIME_URL = (() => {
  try {
    return String(browser.runtime.getURL(""));
  } catch (err) {
    return "";
  }
})();
const ON_FIREFOX = /^moz-extension:/.test(RUNTIME_URL);
// The Chrome Web Store install's id (CHROME_EXTENSION_ID in native_host.py, which names it in
// Chrome's host manifest; server/tests/test_native_host.py keeps the two equal). Chrome lets only
// the extensions a host manifest names reach the host, by an id that is fixed only for the store
// install: an unpacked build's comes from its folder's path, and there the button could only ever
// answer "launcher not registered" with a hint that cannot help. Firefox's id is the manifest's.
// Chrome also updates a store install by itself, once the store has reviewed the version; an
// unpacked build (dist/chrome, the release's Chrome zip) is updated by nothing but the viewer.
const CHROME_STORE_ID = "ecenifonpkaiccmmknpbllbebbfigjnm";
const CHROME_STORE_INSTALL = RUNTIME_URL === `chrome-extension://${CHROME_STORE_ID}/`;
const START_AVAILABLE = ON_FIREFOX || CHROME_STORE_INSTALL;

// The subtitle font, as content.js builds it (FONT_FAMILY_RE, SUB_FONTS, fontStack there): the
// popup cannot import the content script, so the sample keeps a copy. Keep the two in step.
const FONT_FAMILY_RE = /^[\p{L}\p{N}][\p{L}\p{N} _.\-]{0,99}$/u;
const GOTHIC_STACK = '"Noto Sans JP", "Noto Sans CJK JP", "Yu Gothic UI", "Yu Gothic", "Meiryo", "Hiragino Sans", sans-serif';
const SUB_FONTS = {
  default: GOTHIC_STACK,
  "gothic-bold": GOTHIC_STACK,
  rounded: '"M PLUS Rounded 1c", "Hiragino Maru Gothic ProN", "Hiragino Maru Gothic Pro", "Yu Gothic UI", "Yu Gothic", sans-serif',
  mincho: '"Noto Serif JP", "Noto Serif CJK JP", "Hiragino Mincho ProN", "Hiragino Mincho Pro", "Yu Mincho", "YuMincho", serif',
};
const FONT_PROBE_TEXT = "日本語の字幕 Subtitle 123";

let saveTimer = null;
// The fields edited since the last save, by id. The save sends those and nothing else: this form
// is not the only writer (the content script saves `enabled` and `showTranscript` for the keyboard
// commands and the transcript's close button, and this page is also the options page, alive in a
// tab), and a save of the whole form would put back what another writer changed meanwhile.
const dirty = new Set();
// The patch each field was last sent with, from the flush until the store echoes that value
// (onStorageChanged): the echo is a storage round trip away, and an echo of an earlier write
// landing in between (another writer's save, queued in the background just before this one;
// this form's own previous save, still being written) carries the value from before the edit.
// The reply to the save ends the wait too, for a write with no echo coming: one that failed, or
// one that left the store as it was (a slider dragged and put back within the debounce; Chrome
// reports no change then). The store notifies before the background's write resolves and the
// reply goes out, so the reply never overtakes the echo the field is waiting for.
const inFlight = new Map();
// What each text field held when it was last put in from the store (init, onStorageChanged) or
// sent (flushSave), read as readField() reads it. A text field with the focus keeps a change made
// elsewhere out only while its value differs from this, that is while the viewer is typing in it:
// the focus alone says nothing (it stays on the known-words list of the options page while the
// viewer is on YouTube marking words), and a list that skipped those marks would save itself
// over them at its next edit.
const typedBaseline = new Map();
// The debounced save only remembers the last event, so a server-address or model edit leaves a
// note here that the save flushes: "server" starts the status over, "model" refreshes the hint.
let serverCheckPending = null;
// The same for the word colours: a feature just turned on or another AnkiConnect URL ("feature")
// or another deck chosen ("deck") asks Anki for its decks once the save is through. The save
// judges "feature" again against the form it writes: a checkbox ticked and unticked inside the
// debounce is off, and the ask would bring up AnkiConnect's permission dialog for a viewer who
// uses neither feature.
let decksCheckPending = null;
let decksAsked = 0; // questions to Anki so far: an answer overtaken by a later question is dropped
// The newest question's answer: true when Anki listed its decks, false when it could not (away,
// permission not granted, an error), null while none has answered (nothing asked, or an ask out).
let decksOk = null;
// That answer as the select shows it: the decks Anki listed ([] while it could not) and the deck
// of the last mined card (undefined before any answer). A deck chosen in the other copy of this
// form gets its option built here from them (see onStorageChanged).
let decksListed = [];
let decksSeen;
let health = null; // the last /health answer, null while the server is unreachable
// /health requests so far, and the number of the one under way (0 between two). The interval
// waits for the answer under way; a first check, after a server-address edit, starts over and
// drops the answer of the request it overtakes, which the old address may still be holding up.
let healthAsked = 0;
let healthInFlight = 0;

// The start flow, one step at a time: idle -> requesting (the permission prompt is up) -> starting
// (the launcher was asked) -> waiting (it answered; /health is polled until the server does or the
// deadline passes) -> idle once the server is online, or failed with the reason on the detail line
// and the button back. Only the click handler moves it forward; the health refresh ends it. The
// launch itself is the background's: this document dies with a click outside the popup, and the
// next one picks the flow up at "waiting" from there (resumeStart) rather than at "idle" with a
// button that would start a second server.
// `already` and `loading` are the launcher's account of the server it found: one that answers
// /health, or one that holds its instance lock while its model loads; `log` is the file it opened.
const startFlow = { state: "idle", deadline: 0, already: false, loading: false, log: null, detail: "" };
const START_BUSY = new Set(["requesting", "starting", "waiting"]);

// The update flow, the start flow's twin: idle -> requesting (POST /update on its way) ->
// updating (the server answered and is restarting; /health is polled until the new version
// answers or the deadline passes) -> done, or stale (the server came back with the old version:
// update.py could not update, and its window says why), failed (the request was refused or did
// not get through) or lost (no answer within the window). The request and its record are the
// background's (UPDATE_WINDOW_MS there), so a reopened popup resumes at "updating"
// (refreshUpdate); the outcomes live in this document alone, and the banner and the status line
// read both. `from` and `to` are the versions the request was made from and for.
const updateFlow = { state: "idle", from: null, to: null, requestedAt: 0, deadline: 0, down: false, refused: false, detail: "", version: "" };
const UPDATE_BUSY = new Set(["requesting", "updating"]);
// The old server closes its port within a second of answering /update, so an old version seen
// this long after the request is the restarted server, whether or not a poll caught it down. The
// background judges its record by the same rule; this is its copy (popup-copies.test.js).
const UPDATE_SHUTDOWN_MS = 10000;
// How long the background waits for the updated server before the record ends: the deadline the
// popup follows is the background's, this copy only names the wait in the hint (popup-copies.test.js).
const UPDATE_WINDOW_MS = 120000;
const UPDATE_LOST_HINT = `No answer from the server ${UPDATE_WINDOW_MS / 1000} s after the update: look at its window (Windows) or ~/.shisu-ko/server.log before starting it again; a restart loads the model again, which takes a while`;
const RELEASES_URL = "https://github.com/Multysquid/shisu-ko/releases/latest";

function stillOldHint(version) {
  return `The server restarted but still runs ${version}; look at its window: update.py said why`;
}

let updateInfo = null; // the background's last updateStatus answer
let updateKey = null; // the server the answer was about (version and launcher flag), or "offline"
let updateAsked = 0; // questions asked so far: an answer overtaken by a later question is dropped
let checkingUpdates = false; // the "Check for updates" click, until its result is in

function readField(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "range" || el.type === "number") return Number(el.value);
  // A colour well always reports a normalised "#rrggbb"; trimming it would be harmless but a lie.
  if (el.type === "color") return el.value;
  // The known words: one per line, each trimmed, blank lines out (the content script reads the
  // setting the same way, so what is stored is what it uses).
  if (el.type === "textarea") return knownWordsText(el.value);
  return el.value.trim();
}

function knownWordsText(value) {
  return String(value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n");
}

// A field the viewer types in: its edit is done at its change event, not at every keystroke.
function typedField(el) {
  return el.type === "text" || el.type === "textarea";
}

// Each range shows its value and paints the travelled part of its own track (the --fill custom
// property; see popup.css), so the slider carries the value twice: by position and by length.
const RANGES = {
  fontScale: (v) => `${Math.round(v * 100)}%`,
  lingerSeconds: (v) => `${v.toFixed(1)} s`,
  subPosition: (v) => `${v}%`,
  subBackgroundOpacity: (v) => `${v}%`,
  clipPaddingMs: (v) => `${v} ms`,
};

// "Reset style" restores these and nothing else, so a botched experiment costs one click.
const STYLE_KEYS = ["subPosition", "subFont", "subFontFamily", "subTextColor", "subBackgroundOpacity", "subOutline", "transcriptSide"];

// Called for every event of every control (a slider drag is hundreds of them), so it only writes
// what changed: the popup repaints on every mutation, and an unchanged textContent is still one.
function updateOutputs() {
  for (const [id, format] of Object.entries(RANGES)) {
    const el = document.getElementById(id);
    const value = Number(el.value);
    const min = Number(el.min);
    const fill = `${((value - min) / (Number(el.max) - min)) * 100}%`;
    if (el.style.getPropertyValue("--fill") !== fill) el.style.setProperty("--fill", fill);
    setText(document.getElementById(`${id}Out`), format(value));
  }
  const on = document.getElementById("enabled").checked;
  setText(document.getElementById("enabled-label"), on ? "On" : "Off");
  document.body.classList.toggle("off", !on);
  renderFontSample();
}

// Only touch the DOM when the text changed: the popup repaints on every mutation, and the status
// line is refreshed every two seconds.
function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function setHint(el, text, kind) {
  setText(el, text);
  const cls = kind ? `hint ${kind}` : "hint";
  if (el.className !== cls) el.className = cls;
}

// ------------------------------------------------------------------ font family

function fontFamilyName(value) {
  const name = typeof value === "string" ? value.trim() : "";
  return FONT_FAMILY_RE.test(name) ? name : "";
}

function fontStack(subFont, subFontFamily) {
  const preset = SUB_FONTS[Object.hasOwn(SUB_FONTS, subFont) ? subFont : SHISUKO_DEFAULT_SETTINGS.subFont];
  const family = fontFamilyName(subFontFamily);
  return family ? `"${family}", ${preset}` : preset;
}

// Firefox offers no list of installed fonts, but a canvas tells whether one name resolves: text
// set in '"<family>", <generic>' measures the same as the generic alone only when the family fell
// through to it. Three generics, so a family that happens to match one still differs from another.
// A verdict holds for the life of the popup (fonts are not installed while it is open), so each
// name is measured once: the probe makes a canvas and a context, and updateOutputs() would
// otherwise run it for every slider position.
const fontProbes = new Map();

function fontInstalled(family) {
  if (fontProbes.has(family)) return fontProbes.get(family);
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return true; // nothing to measure with: better no warning than a wrong one
  let installed = false;
  for (const generic of ["monospace", "serif", "sans-serif"]) {
    ctx.font = `48px ${generic}`;
    const base = ctx.measureText(FONT_PROBE_TEXT).width;
    ctx.font = `48px "${family}", ${generic}`;
    if (ctx.measureText(FONT_PROBE_TEXT).width !== base) {
      installed = true;
      break;
    }
  }
  fontProbes.set(family, installed);
  return installed;
}

// The sample shows the stack the content script will use, weight from the preset included. It
// depends on the preset and the typed name alone, so it is rendered again only when one changed.
let fontSampleFor = null;

function renderFontSample() {
  const preset = document.getElementById("subFont").value;
  const typed = document.getElementById("subFontFamily").value.trim();
  const key = `${preset}\n${typed}`;
  if (key === fontSampleFor) return;
  fontSampleFor = key;
  const family = fontFamilyName(typed);
  const sample = document.getElementById("font-sample");
  sample.style.fontFamily = fontStack(preset, family);
  sample.style.fontWeight = preset === "gothic-bold" ? "700" : "400";
  const hint = document.getElementById("font-hint");
  if (!typed) setHint(hint, "Leave empty to use the preset.", "");
  else if (!family) setHint(hint, "A font name is letters, digits, spaces, dots, hyphens and underscores; the preset is used", "warn");
  else if (fontInstalled(family)) setHint(hint, `${family} is installed on this computer`, "");
  else setHint(hint, `${family} was not found on this computer; the preset is used`, "warn");
}

// ------------------------------------------------------------------ model

function modelNameOk(name) {
  return MODEL_NAME_RE.test(name) && !name.includes("..");
}

// The models already downloaded join the suggestions once the server has listed them.
function addModelSuggestions(names) {
  if (!Array.isArray(names)) return;
  const list = document.getElementById("model-suggestions");
  const known = new Set([...list.options].map((o) => o.value));
  for (const name of names) {
    if (typeof name !== "string" || known.has(name) || !modelNameOk(name)) continue;
    known.add(name);
    const option = document.createElement("option");
    option.value = name;
    list.appendChild(option);
  }
}

// The server's verdict on the model the field asks for (its own value, or the server's default
// while it is empty), or null. /health names the model its last failed load was for in the one
// spelling the server keeps (large-v3, whether the field said large or Systran/faster-whisper-large-v3)
// and lists the other spellings of the same weights in `names` (model_spellings() in server.py);
// the field's value must be one of them, compared exactly, since repo ids are case-sensitive. Only
// that name's verdict belongs under the field: a name the server has not judged shows the plain hint.
function modelErrorFor(value) {
  const failed = health && health.model_error;
  if (!failed || typeof failed.model !== "string" || typeof failed.error !== "string" || !failed.error) return null;
  const asked = value || (typeof health.default_model === "string" ? health.default_model.trim() : "");
  if (!asked) return null;
  const names = [failed.model, ...(Array.isArray(failed.names) ? failed.names : [])];
  return names.some((name) => typeof name === "string" && name.trim() === asked) ? failed.error : null;
}

function renderModelHint() {
  const value = document.getElementById("model").value.trim();
  const hint = document.getElementById("model-hint");
  const error = modelErrorFor(value);
  // The verdict on this very name outranks the load in progress: after a failed switch the server
  // reloads the previous model, and "Loading large-v3…" would hide why the new name was refused
  // (the badge still says a model is loading).
  if (value && !modelNameOk(value)) setHint(hint, MODEL_NAME_HINT, "warn");
  else if (error) setHint(hint, error, "error");
  else if (health && health.model_loading) setHint(hint, `Loading ${health.model_loading}…`, "");
  else setHint(hint, MODEL_HINT, "");
}

function renderModelField() {
  if (health) {
    const input = document.getElementById("model");
    if (typeof health.default_model === "string" && health.default_model) {
      // Guarded like the texts (setText): an attribute set to its own value is a mutation too,
      // and this runs with every /health answer.
      const placeholder = `${health.default_model} (server default)`;
      if (input.placeholder !== placeholder) input.placeholder = placeholder;
    }
    addModelSuggestions(health.models);
  }
  renderModelHint();
}

// ------------------------------------------------------------------ word colours

const DECK_NONE_HINT = "Automatic: no card mined yet — mine one, or choose a deck";

// The first entry of the deck select. Before Anki has been asked (`seen` undefined) it keeps the
// page's description: with both features off nothing is asked, and "no card mined yet" would be a
// claim nobody checked.
function automaticDeckText(seen) {
  if (seen === undefined) return "Automatic: the deck of the last mined card";
  return seen ? `Automatic: ${seen}` : "Automatic: no card mined yet";
}

// The deck select: the automatic entry first, then Anki's decks. The stored choice keeps an
// option even when Anki did not list it (Anki closed, or a deck renamed since): a select drops a
// value it has no option for, and the setting would go with it at the next save.
function renderDeckOptions(decks, seen, current) {
  const select = document.getElementById("cardStatusDeck");
  const auto = select.options[0] || document.createElement("option");
  auto.value = "";
  auto.textContent = automaticDeckText(seen);
  const value = typeof current === "string" ? current : "";
  const names = new Set(Array.isArray(decks) ? decks.filter((name) => typeof name === "string" && name) : []);
  if (value) names.add(value);
  const options = [...names].sort((a, b) => a.localeCompare(b)).map((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    return option;
  });
  select.replaceChildren(auto, ...options);
  select.value = value;
}

// Whether the form has a word-colour feature on: the checkboxes are the form, and what the
// debounced save writes.
function wordColoursOn() {
  return document.getElementById("cardStatus").checked || document.getElementById("pitchAccent").checked;
}

// Ask Anki for its decks and say under the select which one the word colours look at, or what
// stands in the way. Only asked while one of the two features is on, or when one was just turned
// on, another deck chosen or another Anki named: the first ask brings up AnkiConnect's permission
// dialog, which a viewer who never uses the feature must not meet. The answer names the deck the
// last mined card went to as well, which is what "automatic" means; without an answer the select
// keeps the stored deck and the hint says why the colours will not come. The hint is about the
// colours, so it is painted only while a feature is on: an answer that lands after the viewer
// unticked both (Anki's dialog clicked later, a slow list) fills the list and says nothing.
async function refreshDecks() {
  const asked = ++decksAsked;
  decksOk = null;
  const res = await browser.runtime.sendMessage({ type: "ankiDecks" }).catch((err) => ({ ok: false, error: String((err && err.message) || err) }));
  if (asked !== decksAsked) return;
  const select = document.getElementById("cardStatusDeck");
  const hint = document.getElementById("deck-hint");
  const ok = !!(res && typeof res === "object" && res.ok);
  decksOk = ok;
  // The background's every answer carries `seen`; only a message that failed outright has none,
  // and then the deck of the last mined card was never looked at (see automaticDeckText).
  const seen = res && typeof res === "object" && "seen" in res ? (typeof res.seen === "string" && res.seen ? res.seen : null) : undefined;
  const decks = ok && Array.isArray(res.decks) ? res.decks : [];
  const value = select.value;
  decksListed = decks;
  decksSeen = seen;
  renderDeckOptions(decks, seen, value);
  if (!wordColoursOn()) setHint(hint, "", "");
  else if (!ok) setHint(hint, res && typeof res.error === "string" && res.error ? res.error : "Anki gave no answer", "warn");
  else if (value && !decks.includes(value)) setHint(hint, `No deck named ${value} in Anki`, "error");
  else if (value) setHint(hint, "", "");
  // The deck of the last mined card may have been renamed or deleted since: the background would
  // search it all the same and find nothing, so the colours would not come without a word here.
  else if (seen && !decks.includes(seen)) setHint(hint, `The last mined card's deck ${seen} is no longer in Anki; mine a card, or choose a deck`, "warn");
  else if (seen) setHint(hint, `Looking at ${seen}`, "");
  else setHint(hint, DECK_NONE_HINT, "warn");
}

// The slow clock behind the deck hint. A failed verdict (Anki closed when the page opened, its
// permission dialog not clicked yet) would otherwise stay on an options page or a popup left
// open until a word-colour control is touched, while the tab's colours, asking on a clock of
// their own, already work: so it is asked again while a feature is on. A good answer is left
// alone (the viewer's own edits ask for what changes it), and so is an ask still out. The
// background answers "denied" from memory for a minute before it puts Anki's dialog up again, so
// this clock cannot make the dialog reappear more often than the tab's own asks already do.
function retryDecks() {
  if (decksOk === false && wordColoursOn()) refreshDecks();
}

function setField(el, value) {
  if (el.type === "checkbox") el.checked = !!value;
  else el.value = value === undefined || value === null ? "" : value;
}

async function resetStyle() {
  for (const key of STYLE_KEYS) {
    const el = document.getElementById(key);
    if (el) setField(el, SHISUKO_DEFAULT_SETTINGS[key]);
    dirty.add(key);
  }
  updateOutputs();
  // An edit still on its way to storage goes with the reset instead of being cancelled: a text
  // field's change fires on the blur this click causes, 150 ms before its save would have. The
  // deck ask that edit carried goes with it the same way (see flushSave).
  await flushSave();
}

function onChange(ev) {
  updateOutputs();
  dirty.add(ev.target.id);
  // A new server address starts over; a new model name only needs the hint brought up to date.
  if (ev.target.id === "serverUrl") serverCheckPending = "server";
  else if (ev.target.id === "model" && serverCheckPending !== "server") serverCheckPending = "model";
  // A word-colour feature turned on, or another deck: the ask waits for the save, like the checks.
  // Another AnkiConnect URL is another Anki, whose decks the list and the hint should describe;
  // the ask goes out for it only while a feature is on, so it needs no branch of its own.
  const id = ev.target.id;
  if (id === "cardStatusDeck") decksCheckPending = "deck";
  else if (((id === "cardStatus" || id === "pitchAccent") && ev.target.checked) || id === "ankiUrl") {
    if (decksCheckPending !== "deck") decksCheckPending = "feature";
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSave, 150);
}

// The save: the fields edited since the last one, read now (a slider's last position, not its
// first). The message is sent before anything is awaited, so the flush from pagehide gets out
// too: this document dies with a click outside the popup, and a text field's change event fires
// on that very close, with none of the 150 ms left for the timer. The check the edit asked for
// follows the save, so the background already reads the new address; the deck ask follows it the
// same way, for the AnkiConnect URL and the deck the background reads.
function flushSave() {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (dirty.size === 0) return Promise.resolve();
  const check = serverCheckPending;
  const pending = decksCheckPending;
  serverCheckPending = null;
  decksCheckPending = null;
  const patch = {};
  for (const key of dirty) {
    const el = document.getElementById(key);
    if (el) patch[key] = readField(el);
  }
  dirty.clear();
  // A feature turned on and off again inside the debounce is off: no ask for it. The checkboxes
  // are read from the form, not the patch, which holds the edited fields alone: the other
  // feature, on since an earlier save, is not in it.
  const on = wordColoursOn();
  const decks = pending === "deck" || (pending === "feature" && on);
  for (const key of Object.keys(patch)) {
    inFlight.set(key, patch);
    if (typedBaseline.has(key)) typedBaseline.set(key, patch[key]);
  }
  const saved = browser.runtime.sendMessage({ type: "saveSettings", settings: patch }).catch(() => {});
  saved.then(() => {
    // A later flush may have sent the field again: that one is still waited for.
    for (const key of Object.keys(patch)) if (inFlight.get(key) === patch) inFlight.delete(key);
  });
  if (check) saved.then(() => checkServer(check === "server"));
  // Both features off: the hint has nothing to be about, whatever the last ask painted (an ask
  // still out for this save, for another deck, lands under two off features and paints none).
  if (!on) setHint(document.getElementById("deck-hint"), "", "");
  if (decks) saved.then(() => refreshDecks());
  return saved;
}

// A change made elsewhere lands in the form (see `dirty`), except in a field with an edit of its
// own under way: one waiting for its save, one sent and waiting for the store's echo of it (see
// `inFlight`: an echo that carries another value is an earlier write's, and the field is already
// past it), or a text field with the focus, whose typing is not an edit until its change event
// (every other control's edit is in `dirty` at once, and a checkbox keeps the focus long after
// its click). The popup's own save comes back through here too, and changes nothing.
function onStorageChanged(changes, area) {
  if (area !== "local" || !changes.settings || !changes.settings.newValue) return;
  const next = changes.settings.newValue;
  const landed = new Set(); // the fields another writer changed
  for (const key of FIELDS) {
    const el = document.getElementById(key);
    if (!el || dirty.has(key) || typing(el, key) || !Object.hasOwn(next, key)) continue;
    const sent = inFlight.get(key);
    if (sent) {
      if (next[key] !== sent[key]) continue;
      inFlight.delete(key);
    }
    if (readField(el) === next[key]) continue;
    // A deck chosen in the other copy of this form has no option here until Anki lists it, and a
    // select given a value it has no option for shows none: the option first, the value after.
    if (key === "cardStatusDeck") renderDeckOptions(decksListed, decksSeen, next[key]);
    else setField(el, next[key]);
    if (typedBaseline.has(key)) typedBaseline.set(key, readField(el));
    landed.add(key);
  }
  updateOutputs();
  renderModelHint();
  // The word colours edited in the other copy of this form: the deck list and the hint follow the
  // rules of an edit made here (see onChange), less the save, which is that copy's. A feature
  // that landed checked was turned on; one that landed unchecked asks nothing, whether or not the
  // other feature keeps the hint up (the answer would only repaint what it says).
  const on = wordColoursOn();
  const turnedOn = (key) => landed.has(key) && document.getElementById(key).checked;
  if (landed.has("cardStatusDeck") || turnedOn("cardStatus") || turnedOn("pitchAccent") || (on && landed.has("ankiUrl"))) refreshDecks();
  else if (!on && (landed.has("cardStatus") || landed.has("pitchAccent"))) setHint(document.getElementById("deck-hint"), "", "");
}

// Whether the viewer is typing in `el`: a text field with the focus whose value is no longer the
// one last put in or sent (see typedBaseline).
function typing(el, key) {
  return typedField(el) && el === document.activeElement && readField(el) !== typedBaseline.get(key);
}

// The status line answers the popup's first question: can it transcribe right now? The badge word
// and its dot carry the state, the detail line the evidence (which model, which device) or the fix.
// The server's answer outranks the start flow: online is online, whoever started it. An update
// under way outranks the answer: the old server still answers for a moment, and then nobody does.
function renderStatus() {
  const badge = document.getElementById("server-status");
  const detail = document.getElementById("server-detail");
  const button = document.getElementById("start-server");
  const launched = startFlow.state === "starting" || startFlow.state === "waiting";
  const updating = UPDATE_BUSY.has(updateFlow.state);
  let word;
  let cls;
  let evidence;
  if (updating) {
    word = "Updating server";
    cls = "badge";
    evidence = `Restarting with ${updateFlow.to || "the newest release"}…`;
  } else if (health && health.model_loading) {
    word = "Loading model";
    cls = "badge";
    evidence = String(health.model_loading);
  } else if (health) {
    word = "Server online";
    cls = "badge ok";
    if (updateFlow.state === "done") evidence = `Updated to ${updateFlow.version}`;
    else if (updateFlow.state === "stale") evidence = updateFlow.detail;
    else evidence = `${health.model} · ${health.device} · ${health.compute_type}`;
  } else if (launched) {
    word = "Starting server";
    cls = "badge";
    if (startFlow.state === "starting") evidence = "";
    else if (startFlow.loading) evidence = "still loading by the launcher's account, waiting for it to answer";
    else if (startFlow.already) evidence = "already running by the launcher's account, waiting for it to answer";
    else evidence = "launched, waiting for it to answer";
  } else {
    word = "Server offline";
    cls = "badge bad";
    if (startFlow.state === "failed") evidence = startFlow.detail;
    else if (updateFlow.state === "lost") evidence = updateFlow.detail;
    else evidence = OFFLINE_HINT;
  }
  setText(badge, word);
  if (badge.className !== cls) badge.className = cls;
  setText(detail, evidence);
  // The button is the fix for one state only. It stays in place, disabled, while a start is under
  // way, so the line does not jump and a second click cannot launch a second server. An update
  // hides it: the launcher restarts the server itself, and a start on top would race it.
  const busy = START_BUSY.has(startFlow.state);
  button.classList.toggle("hidden", !!health || !START_AVAILABLE || updating);
  if (button.disabled !== busy) button.disabled = busy;
  setText(button, launched ? "Starting…" : "Start server");
  renderModelField();
  renderUpdate();
}

// Only the first check announces itself; the refreshes behind it change the text in place. A
// first check goes out even while a refresh is under way (see healthAsked): the refresh may be
// held up by the old address for the full request timeout, and its answer is not this server's.
async function checkServer(first) {
  if (healthInFlight && !first) return;
  if (first) {
    // The flows' outcomes (failed, done, stale, lost) were judged at the address this check
    // leaves behind: "no answer after 90 s", or the hint naming the very URL just changed, would
    // otherwise stand in for the offline hint at the new one for as long as it is offline too. A
    // launch or an update still under way is the background's, and goes on.
    if (!START_BUSY.has(startFlow.state)) startFlow.state = "idle";
    if (!UPDATE_BUSY.has(updateFlow.state)) updateFlow.state = "idle";
    const badge = document.getElementById("server-status");
    setText(badge, "Checking server");
    badge.className = "badge";
    setText(document.getElementById("server-detail"), "");
  }
  const asked = ++healthAsked;
  healthInFlight = asked;
  let res;
  try {
    res = await browser.runtime.sendMessage({ type: "api", path: "/health" }).catch(() => null);
  } finally {
    if (healthInFlight === asked) healthInFlight = 0;
  }
  if (asked !== healthAsked) return; // overtaken by a first check: the old address's answer
  health = res && res.ok && res.data ? res.data : null;
  // The start flow ends here, one way or the other: the server answered, or it had its 90 s.
  // A start still in the click handler's hands (requesting, starting) is left to it.
  if (health) {
    if (startFlow.state === "waiting" || startFlow.state === "failed") startFlow.state = "idle";
  } else if (startFlow.state === "waiting" && Date.now() >= startFlow.deadline) {
    // A server the launcher saw answering is a server at another address; one it saw loading is
    // as slow as one this popup launched, and gets the same advice.
    failStart(startFlow.already && !startFlow.loading ? START_ELSEWHERE_HINT : startNotUpHint(startFlow.log));
  }
  if (updateFlow.state === "updating") judgeUpdate();
  renderStatus();
  // The banner follows behind: the background's answer may wait on a check of GitHub, and the
  // status line must not. Most ticks ask nothing (the server's answer is the same), and paint
  // nothing more.
  refreshUpdate(false).then((answered) => {
    if (answered) renderStatus();
  });
}

// Where an update under way stands after this /health answer. The new version ends it; the old
// version ends it too, once the server has been seen down or has had the time to close its port,
// since a restart that brought the old code back means update.py could not update.
function judgeUpdate() {
  const now = Date.now();
  if (!health) {
    updateFlow.down = true;
    if (now >= updateFlow.deadline) {
      updateFlow.state = "lost";
      updateFlow.detail = UPDATE_LOST_HINT;
    }
    return;
  }
  const version = typeof health.version === "string" ? health.version : "";
  const updated = !!version && (updateFlow.to ? compareVersions(version, updateFlow.to) >= 0 : version !== updateFlow.from);
  if (updated) {
    updateFlow.state = "done";
    updateFlow.version = version;
  } else if (updateFlow.down || now - updateFlow.requestedAt >= UPDATE_SHUTDOWN_MS || now >= updateFlow.deadline) {
    updateFlow.state = "stale";
    updateFlow.detail = stillOldHint(version || updateFlow.from || "the old version");
  }
}

function failStart(detail) {
  startFlow.state = "failed";
  startFlow.detail = detail;
  renderStatus();
}

// Never throws and never prompts twice: a granted permission answers true without a prompt.
function requestNativeMessaging() {
  try {
    return Promise.resolve(browser.permissions.request({ permissions: ["nativeMessaging"] })).catch(() => false);
  } catch (err) {
    return Promise.resolve(false);
  }
}

// The click on "Start server". The permission request is issued before anything is awaited: it
// needs the user gesture, which the first await spends. The background then talks to the native
// host; the popup only watches /health from there on.
async function startServerFromPopup() {
  if (START_BUSY.has(startFlow.state) || UPDATE_BUSY.has(updateFlow.state)) return;
  const request = requestNativeMessaging();
  startFlow.state = "requesting";
  renderStatus();
  const granted = await request;
  if (!granted) {
    failStart(START_PERMISSION_HINT);
    return;
  }
  startFlow.state = "starting";
  renderStatus();
  const res = await browser.runtime.sendMessage({ type: "startServer" }).catch((err) => ({ ok: false, error: String((err && err.message) || err) }));
  if (!res || typeof res !== "object" || !res.ok) {
    const error = res && typeof res.error === "string" && res.error ? res.error : "the launcher gave no answer";
    failStart(res && typeof res.hint === "string" && res.hint ? `${error}. ${res.hint}` : error);
    return;
  }
  watchStart(res);
  checkServer(false); // a server that was already up answers now, not two seconds from now
}

// Follow a launch the background reported, its answer to the click or the one it had under way
// when the popup opened: /health is polled until the server answers or the deadline passes.
function watchStart(res) {
  startFlow.state = "waiting";
  startFlow.already = !!res.already;
  startFlow.loading = !!res.loading;
  startFlow.log = typeof res.log === "string" && res.log ? res.log : null;
  // The deadline is the background's; without one the wait ends at the next refresh, not never.
  startFlow.deadline = Number.isFinite(res.deadline) ? res.deadline : Date.now();
  renderStatus();
}

// A start requested from an earlier popup document may still be under way; the background says.
// So may an update, and it is picked up here rather than from the first updateStatus answer,
// which may wait ten seconds on GitHub: the first paint must not offer a start on top of it.
async function resumeStart() {
  const status = await browser.runtime.sendMessage({ type: "startServerStatus" }).catch(() => null);
  if (!status || typeof status !== "object") return;
  if (status.starting) watchStart(status);
  if (status.updating && typeof status.updating === "object") watchUpdate(status.updating);
}

// ------------------------------------------------------------------ updates

// The version helpers of background.js, copied: the popup cannot import the background, and it
// judges the server that comes back from an update by the same rule (addon/tests/popup-copies.test.js
// keeps the two in step).
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

// "checked 3 min ago": the result line says how old the check is, in the coarsest unit that
// still says something.
function relativeTime(at, now = Date.now()) {
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

function latestVersion() {
  const latest = updateInfo && updateInfo.latest;
  return latest && typeof latest.version === "string" && latest.version ? latest.version : "";
}

// Ask the background what the newest release means for the server this popup sees. The /health
// answer travels with the question, so the server is not asked twice; and the question is only
// asked when that answer changed (version, launcher flag, online or not) or after something this
// popup did, since the verdict changes with nothing else. The first question of a popup document
// also asks for the day's check of GitHub (the background decides whether one is due). The
// background's record of an update under way is picked up here: a reopened popup resumes it.
// Answers can cross: the first question may wait ten seconds on GitHub while the server comes
// online and a second one, answered from the store at once, has already put the banner up; the
// first answer then lands with "offline" and would take it down, so only the latest question's
// answer counts, and the questions are numbered for that. The dropped first answer carried the
// day's check, though, which the second one was answered without (the background reads the store
// for a question without `check`, and a check under way is not in the store yet), so the popup
// asks once more when it lands: nothing else would, while the server's answer stays the same.
// Resolves to whether a new answer was taken, which is when the banner needs painting again.
async function refreshUpdate(force) {
  const key = health ? `${health.version}|${health.launcher}` : "offline";
  const first = updateKey === null;
  if (!force && key === updateKey) return false;
  updateKey = key;
  const asked = ++updateAsked;
  const status = await browser.runtime.sendMessage({ type: "updateStatus", health, check: first }).catch(() => null);
  if (asked !== updateAsked) return first ? refreshUpdate(true) : false;
  if (!status || typeof status !== "object" || !status.decision || typeof status.decision !== "object") return false;
  updateInfo = status;
  if (status.updating && typeof status.updating === "object" && updateFlow.state === "idle") watchUpdate(status.updating);
  return true;
}

// The banner, from the background's verdict and this popup's own flow. Nothing is shown without a
// release to name or after "Not now" for that release, and nothing about the server when it can
// update itself at its next start anyway (offline: the launcher runs update.py before every
// start); an extension behind still gets its line then. While an update is under way the banner
// stays, its button muted, and goes once the new version answers.
function renderUpdate() {
  const banner = document.getElementById("update-banner");
  const text = document.getElementById("update-text");
  const update = document.getElementById("update-now");
  const release = document.getElementById("update-release");
  const later = document.getElementById("update-later");
  const latest = latestVersion();
  const decision = updateInfo && updateInfo.decision;
  const busy = UPDATE_BUSY.has(updateFlow.state);
  const server = (updateInfo && updateInfo.server && updateInfo.server.version) || updateFlow.from || "an older version";
  let message = "";
  let showUpdate = false;
  let showRelease = false;
  let showLater = false;
  if (latest && decision && (busy || updateInfo.snoozed !== latest)) {
    if (busy || decision.server === "newer") {
      message = `Shisu-ko ${latest} is available — the server runs ${server}.`;
      if (updateFlow.state === "failed") {
        message += updateFlow.refused ? ` The server cannot update itself: ${updateFlow.detail}` : ` The update failed: ${updateFlow.detail}`;
      }
      showUpdate = !(updateFlow.state === "failed" && updateFlow.refused);
      showLater = !busy;
    } else if (decision.server === "cannot") {
      message = `Shisu-ko ${latest} is available — the server runs ${server} and was not started by run.cmd / run.sh, so it cannot update itself; restart it by hand to update`;
      showLater = true;
    } else if (decision.server === "behind") {
      // A server from before /update (0.8.0): its launcher, if it has one, updates it at the
      // next start, and nothing here can tell whether it has one.
      message = `Shisu-ko ${latest} is available — the server runs ${server}, which cannot be updated from here; restart it by hand to update (run.cmd / run.sh update it at start)`;
      showLater = true;
    } else if (decision.extension === "newer" && CHROME_STORE_INSTALL) {
      // The Chrome Web Store updates this install once it has reviewed the version, which can
      // come days after the GitHub release. The release page is not offered: an unpacked build
      // from it would be a second extension beside this one, under its own id, and one without
      // the Start button, which the launcher's host manifest grants the store's id alone.
      message = `A newer extension (${latest}) is out; the Chrome Web Store updates this one once it has reviewed that version, which can take days after the GitHub release`;
      showLater = true;
    } else if (decision.extension === "newer") {
      // Firefox needs the signed .xpi, which the release gets once AMO has signed it: minutes
      // after the tag, later for a version AMO holds back. Until the check sees it, the release
      // page has nothing to install, so it is not offered. Chrome here is an unpacked build (a
      // store install took the branch above): its zip is there from the start, and no listing
      // ever updates it, so none is named.
      const signed = !ON_FIREFOX || !!(updateInfo.latest && updateInfo.latest.xpi);
      if (!ON_FIREFOX) message = `A newer extension (${latest}) is on the release page; an unpacked build does not update itself`;
      else if (signed) message = `A newer extension (${latest}) is on the release page (the addons.mozilla.org listing may get it later)`;
      else message = `A newer extension (${latest}) is out; its signed .xpi reaches the release page once addons.mozilla.org has signed it`;
      showRelease = signed;
      showLater = true;
    }
  }
  banner.classList.toggle("hidden", !message);
  setText(text, message);
  update.classList.toggle("hidden", !showUpdate);
  release.classList.toggle("hidden", !showRelease);
  later.classList.toggle("hidden", !showLater);
  // One thing at a time: a start and an update both end in a server loading its model.
  const disabled = busy || START_BUSY.has(startFlow.state);
  if (update.disabled !== disabled) update.disabled = disabled;
  setText(update, busy ? "Updating…" : "Update");
  renderUpdateResult();
}

function renderUpdateResult() {
  const line = document.getElementById("update-result");
  const button = document.getElementById("check-updates");
  if (button.disabled !== checkingUpdates) button.disabled = checkingUpdates;
  const info = updateInfo;
  if (checkingUpdates) setHint(line, "Checking…", "");
  else if (!info || typeof info.checkedAt !== "number") setHint(line, "", "");
  else if (info.error) setHint(line, `Update check failed: ${info.error}` + (latestVersion() ? ` (last seen: ${latestVersion()})` : ""), "warn");
  else if (latestVersion()) setHint(line, `Newest release: ${latestVersion()}, checked ${relativeTime(info.checkedAt)}`, "");
  else setHint(line, `No release found, checked ${relativeTime(info.checkedAt)}`, "");
}

// The click on "Update": the background posts /update; the server answers and exits, its launcher
// runs update.py and starts it again, and this popup watches /health for the new version.
async function updateServerFromPopup() {
  if (UPDATE_BUSY.has(updateFlow.state) || START_BUSY.has(startFlow.state)) return;
  const now = Date.now();
  Object.assign(updateFlow, {
    state: "requesting",
    from: (updateInfo && updateInfo.server && updateInfo.server.version) || null,
    to: latestVersion() || null,
    requestedAt: now,
    deadline: now,
    down: false,
    refused: false,
    detail: "",
    version: "",
  });
  renderStatus();
  const res = await browser.runtime.sendMessage({ type: "updateServer" }).catch((err) => ({ ok: false, error: String((err && err.message) || err) }));
  if (!res || typeof res !== "object" || !res.ok) {
    updateFlow.state = "failed";
    updateFlow.refused = !!(res && res.refused);
    updateFlow.detail = res && typeof res.error === "string" && res.error ? res.error : "the server gave no answer";
    renderStatus();
    return;
  }
  watchUpdate(res);
}

// Follow an update the background reported, its answer to the click or the record it held when
// the popup opened: /health is polled until the new version answers or the deadline passes.
function watchUpdate(res) {
  updateFlow.state = "updating";
  if (typeof res.from === "string") updateFlow.from = res.from;
  if (typeof res.to === "string") updateFlow.to = res.to;
  updateFlow.requestedAt = Number.isFinite(res.requestedAt) ? res.requestedAt : Date.now();
  // The deadline is the background's; without one the wait ends at the next refresh, not never.
  updateFlow.deadline = Number.isFinite(res.deadline) ? res.deadline : Date.now();
  updateFlow.down = !!res.down;
  renderStatus();
}

// "Not now": the banner goes for this browser session (storage.session in the background), the
// badge stays, and the next release asks again.
async function snoozeUpdateFromPopup() {
  const latest = latestVersion();
  if (!latest) return;
  updateInfo.snoozed = latest; // at once; the background's answer would say the same
  renderStatus();
  await browser.runtime.sendMessage({ type: "snoozeUpdate", version: latest }).catch(() => {});
}

// "Check for updates": a check now, whatever the age of the stored one, then the verdict again.
async function checkForUpdatesFromPopup() {
  if (checkingUpdates) return;
  checkingUpdates = true;
  renderUpdateResult();
  try {
    await browser.runtime.sendMessage({ type: "checkForUpdate", force: true }).catch(() => null);
    await refreshUpdate(true);
  } finally {
    checkingUpdates = false;
  }
  renderStatus();
}

// The release page: every release's signed .xpi (and the Chrome zip, for an unpacked build; a
// Chrome Web Store install is never sent here); the addons.mozilla.org listing gets only the
// releases published there, later.
function openReleasePage() {
  const latest = updateInfo && updateInfo.latest;
  const url = latest && typeof latest.url === "string" && /^https:\/\//.test(latest.url) ? latest.url : RELEASES_URL;
  try {
    Promise.resolve(browser.tabs.create({ url })).catch(() => {});
  } catch (err) {
    /* no tabs API: the address is on the release page anyway */
  }
}

// Reloading the open YouTube tabs is what actually injects the content script; a freshly granted
// permission does not reach pages that are already loaded.
async function reloadYouTubeTabs() {
  try {
    // tabs.query with a url filter needs the "tabs" permission to match against URLs.
    const tabs = await browser.tabs.query({ url: YOUTUBE_ORIGINS });
    for (const tab of tabs) await browser.tabs.reload(tab.id);
  } catch (err) {
    /* nothing to reload if the query is refused */
  }
}

async function setupPermissionBanner() {
  const banner = document.getElementById("permission-banner");
  const button = document.getElementById("grant-permission");
  try {
    if (await browser.permissions.contains({ origins: YOUTUBE_ORIGINS })) return;
    banner.classList.remove("hidden");
  } catch (err) {
    return; // no permissions API (older Firefox): leave the banner hidden
  }
  button.addEventListener("click", async () => {
    // request() must be called straight from the click handler; it needs the user gesture.
    const granted = await browser.permissions.request({ origins: YOUTUBE_ORIGINS }).catch(() => false);
    if (!granted) return;
    banner.classList.add("hidden");
    await reloadYouTubeTabs();
  });
}

async function init() {
  setupPermissionBanner();
  // Chrome allows four suggested shortcuts and the build drops the fifth, Alt+Shift+H, so the
  // popup does not advertise a key that is not there (chrome://extensions/shortcuts sets one).
  document.getElementById("statusBadgeKey").classList.toggle("hidden", !ON_FIREFOX);
  const settings = await browser.runtime.sendMessage({ type: "getSettings" });
  // The deck select has no option for the stored deck until Anki lists it, and a select given a
  // value it has no option for shows none; the option comes first, the value after.
  renderDeckOptions([], undefined, settings.cardStatusDeck);
  for (const key of FIELDS) {
    const el = document.getElementById(key);
    if (!el) continue;
    setField(el, settings[key]);
    if (typedField(el)) typedBaseline.set(key, readField(el));
  }
  updateOutputs();
  document.getElementById("reset-style").addEventListener("click", resetStyle);
  for (const key of FIELDS) {
    const el = document.getElementById(key);
    if (!el) continue;
    // Text fields act once the edit is done (a URL, a model that would start a download, the
    // known words, whose every save recolours every tab's lines); the font family is the
    // exception, it previews and applies as it is typed, like a slider.
    const live = !typedField(el) || key === "subFontFamily";
    const eventName = live && el.tagName !== "SELECT" ? "input" : "change";
    el.addEventListener(eventName, onChange);
  }
  // The name hint answers while typing, before the change event saves anything.
  document.getElementById("model").addEventListener("input", renderModelHint);
  document.getElementById("start-server").addEventListener("click", startServerFromPopup);
  document.getElementById("update-now").addEventListener("click", updateServerFromPopup);
  document.getElementById("update-later").addEventListener("click", snoozeUpdateFromPopup);
  document.getElementById("update-release").addEventListener("click", openReleasePage);
  document.getElementById("check-updates").addEventListener("click", checkForUpdatesFromPopup);
  browser.storage.onChanged.addListener(onStorageChanged);
  // The popup closes with a click outside it, and the document goes with it (see flushSave).
  window.addEventListener("pagehide", flushSave);
  document.addEventListener("visibilitychange", onVisibilityChange);
  renderModelHint();
  // Anki is asked about its decks only for a viewer who uses the word colours (see refreshDecks).
  if (settings.cardStatus || settings.pitchAccent) refreshDecks();
  await resumeStart();
  // A resumed start or update has already painted its badge; "Checking server" is for a popup that knows nothing.
  checkServer(startFlow.state === "idle" && updateFlow.state === "idle");
  // The toolbar popup is in view for its whole life; the same page as the options page lives on
  // in a tab, and a status line nobody sees is not worth a request every two seconds, nor a deck
  // hint nobody sees a knock at Anki every thirty.
  setInterval(() => {
    if (!document.hidden) checkServer(false);
  }, HEALTH_REFRESH_MS);
  setInterval(() => {
    if (!document.hidden) retryDecks();
  }, DECKS_RETRY_MS);
}

// Out of view: an edit on its way is saved now (the popup closing, a tab switch). Back in view:
// the status line, and a deck verdict that failed, catch up now rather than at the next tick.
function onVisibilityChange() {
  if (document.hidden) flushSave();
  else {
    checkServer(false);
    retryDecks();
  }
}

document.addEventListener("DOMContentLoaded", init);

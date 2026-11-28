"use strict";

/*
 * The server options the popup's "Start server" button may pass, their check, and a line on what
 * each does for the popup's (?) list. Loaded before background.js (manifest.json,
 * service-worker.js) and popup.js (popup.html); the setting that holds them is `startOptions`
 * (settings.js), what the viewer typed.
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
  "--window": {"min": 2, "max": 600},
  "--first-window": {"min": 2, "max": 600},
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

// What each option does, for the list the (?) beside the popup's field opens
// (renderStartOptionsList() in popup.js): one line per option of the table above, in its order,
// and nothing else, naming server.py's default as "word (default)" for a set or "(default N)" for
// a number, with the exceptions to the option's plain rule (the AMD engine beside an NVIDIA GPU,
// what a Kitsune model does not do, the look before each start that only --no-update skips,
// ...). start-options.test.js holds the
// keys to the table's; test_native_host.py reads this as JSON too and holds every default to
// server.py's parse_args() and each exception to the code that makes it, so it stays a plain
// object literal: quoted keys, no comment inside, no trailing comma.
const SHISUKO_START_OPTION_HELP = Object.freeze({
  "--device": "Where the model runs. auto (default): an NVIDIA GPU (the AMD GPU instead with the AMD engine), else the Apple GPU, else the CPU; a Kitsune model runs on an NVIDIA GPU or the CPU only",
  "--compute-type": "The precision the model runs in. auto (default) picks one for the device; a Whisper model gets int8_float16, half the memory, where an NVIDIA GPU (or the AMD engine's, on Linux) has under 4.5 GB free",
  "--beam-size": "How many beams Whisper searches (default 5). 1 decodes greedily: faster, and wrong on a homophone more often",
  "--language-patience": "Seconds of speech Whisper hears in another language before subtitles pause, if YouTube names no language (default 60); a video YouTube itself names another language stops at once, whatever the number. 0: no check at all",
  "--lyrics": "auto (default): songs and speech over music get subtitles too, under stricter checks (not with a Kitsune model). off: a stretch the speech detector hears no speech in stays blank",
  "--human-subtitles": "auto (default): a Japanese track the video's creator uploaded gives a line its text where Whisper heard the same (timed by Whisper) or nothing (on the track's times); elsewhere Whisper's text stays. off: Whisper alone",
  "--sentence-ends": "auto (default): add the sentence mark Whisper left out where a sentence ending (よね, です, か) meets a pause, so a line ends with its sentence. off: Whisper's own punctuation alone",
  "--anki-sync": "auto (default): sync Anki with AnkiWeb a minute after new notes appear. off: never",
  "--mine": "auto (default): a card Yomitan makes from a line gets the line's audio and picture, and Alt+Shift+M fills the newest card. off: nothing is written to Anki, and Alt+Shift+M saves to Downloads",
  "--mine-picture": "on (default): a mined card gets a frame of the video at its line. off: the audio alone",
  "--window": "Seconds of audio transcribed per step (default 30). Shorter reacts faster to a seek",
  "--first-window": "Seconds of the first step after a seek, shorter so that subtitles appear quickly (default 20)",
  "--lookahead": "Seconds to transcribe ahead of the playhead (default 180 on an NVIDIA GPU or the AMD engine's GPU, 900 on the Apple GPU or the CPU). 0: the whole video",
  "--max-cue-chars": "Characters at which a line Whisper heard is cut (default 30; 26 is Netflix's Japanese limit). A word Whisper split may be rejoined past it, and a line of the creator's subtitles is cut only past twice this",
  "--max-cue-seconds": "Seconds of speech at which a line Whisper heard is cut (default 7). The beat after the speech and a word Whisper split may keep it up longer, and a line of the creator's subtitles is not cut by time",
  "--min-cue-seconds": "Lines shorter than this, in seconds, are extended or merged where there is room (default 0.8)",
  "--idle-minutes": "Minutes without a sync before a video's audio is let go (default 30). That happens at the next sync of any video, so while no tab syncs it stays in memory; a live stream's goes on its own",
  "--model-idle-minutes": "Minutes with nothing to transcribe before the model leaves memory (default 1; never under two --window lengths); it loads again when a video needs it. 0: load it at the start and keep it, as the AMD engine always does",
  "--retry-after": "The longest wait, in seconds, before a failed audio download is tried again, and the wait before a model that failed to load is (default 30)",
  "--client-timeout": "Seconds without a sync from a video's tab before transcribing ahead for it stops (default 30). 0: never stop",
  "--cpu-threads": "Threads for decoding on the CPU (default 0: the library's own choice)",
  "--update-check": "on (default): the server asks GitHub once a day, anonymously, whether there is a newer release. off: it never does, though each start still looks for one first unless --no-update",
  "--log-level": "How much the server logs. INFO (default); DEBUG says more, WARNING and ERROR less",
  "--no-update": "Start without looking for a newer version of Shisu-ko first; the popup then offers no Update button, and its banner says the server was not started by run.cmd / run.sh"
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

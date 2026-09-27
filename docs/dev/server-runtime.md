# Server runtime: live streams, models, the Apple GPU, the Start button, the update step

The parts of the server that are not cue building. Read the matching section before changing `LiveFollower`, `switch_model_if_wanted()`, `server/native_host.py`, `server/update.py`, `POST /update` or the launchers.

## How live streams work

yt-dlp reports `is_live`; `Fetcher.download()` then returns None and `Fetcher.follow_live()` runs
`LiveFollower` on the fetch thread instead of downloading. `DashLiveSource` asks yt-dlp (with
`live_from_start`) for the audio format's base URL and fetches `…&sq=N` segments: self-contained
fMP4 whose timestamps are the stream's media clock, verified to be the same clock as the player's
`getProgressState().current` (a DASH segment cross-correlates at 1.0 with the HLS audio at the
`PROGRAM-DATE-TIME` position, and the player's `ingestionTime` matches within ~0.2 s). The live
head comes from the `X-Head-Seqnum` response header; an expired URL (403) is refreshed once a
minute at most. The follower starts one segment before the playhead (`place_cursor()`), runs
forward to the head, waits for new segments, jumps after a seek, pauses while no client has
synced for `--client-timeout`, and exits after `--idle-minutes` (status `evicted`, refetched on
the next sync). Decoded audio lives in `Session.live_audio` (`LiveAudio`, 16 kHz chunks on the
stream clock, trimmed to `LIVE_KEEP_BEHIND` seconds behind the playhead); `audio_slice()`,
`plan_window()` (`plan_live_window()`: a window at the live edge waits until `LIVE_MIN_WINDOW`
seconds are there instead of being marked covered) and `/clip` read it. Live sessions are never
written to the cue cache; when the stream ends and comes back as a video, `Fetcher.fetch()` drops
the live cues and changes the session token so the client starts over on the video's clock.
`server/tests/test_live.py` drives the follower with a fake source and clock.

## How model switching works

The popup's `model` setting names the Whisper model the server should run; `--model` is only the
default. The content script sends it with every `/sync` (`modelForSync()`, trimmed, empty for the
default), and `App.request_model()` stores the wish: an empty name becomes the operator's
`--model` (that is `--model`, else the model chosen at setup in `config.json`, else large-v3,
resolved once in `parse_args()`; not validated, it may be a folder), an invalid name is not
stored (`model_state()` answers that request with `MODEL_NAME_HINT`), a valid one is stored as
its canonical alias (`canonical_model_name()`, built lazily from `faster_whisper.utils._MODELS`:
alias -> repo id -> first alias, so `large` and `Systran/faster-whisper-large-v3` are
`large-v3`), and a name that failed less than `--retry-after` seconds ago is ignored
(`in_cooldown()`), since the client re-sends the setting every second.

`Transcriber.run()` calls `App.switch_model_if_wanted()` before every window, so the swap never
runs while a window is being transcribed. It is a small state machine over `wanted_model`,
`model_name`, `model_preparing`, `model_prepared`, `model_loading` and `model_error`, all under
`App.lock`:

1. Nothing wanted (`wanted == model_name`): drop leftover prepared files, clear `model_loading`.
2. Wanted but nothing in flight: start `prepare_model(wanted)` on a daemon thread
   (`prepare_thread`), set `model_preparing = model_loading = wanted`, keep transcribing with the
   old model. `prepare_model()` runs `download_model_files(wanted, self.device)`:
   `faster_whisper.download_model()` into `MODELS_DIR` plus a `model.bin` check, so a PyTorch
   checkpoint is refused before `WhisperModel()` sees it (the operator's `--model` folder skips the
   download; on the Apple GPU the MLX weights are fetched instead, see "How the Apple GPU works").
   Nothing here
   touches the GPU, so a typo, a missing repo or an offline hub costs only a failed download:
   `model_error = (name, friendly_model_error())`, `model_failed_at`, `wanted_model` reset to the
   loaded model. There is no retry without a new request.
3. A download still running: keep transcribing, report the wanted name as loading (a change of
   mind cannot cancel a download; the new name waits behind it and stale files are dropped).
4. Files prepared for the wanted name: `self.model = None; gc.collect()` first, because on a GPU
   whose memory is mostly held by other programs two models rarely fit side by side, then
   `load_model(args, wanted, path=dir)`. Success: `model_name = wanted`, error cleared,
   `restart_sessions()`. Failure: `model_error`, `wanted_model = previous`, `reload_model(previous)`;
   if even that fails the server has no model left and calls `os._exit(3)` so the launcher
   restarts it on `--model`.

`restart_session()` clears cues, covered ranges, speech and `seg_next`, gives the session a new
token (the client drops everything on a token change, `dropCues()` in `content.js`), sets a
session without audio back to `pending` so `get_session()` fetches it again (the old model's
cache may have marked it covered without ever downloading), and calls `load_cache()` for the new
model. `/health` reports `model` (loaded, canonical), `default_model`, `model_loading`,
`model_error` as `{model, error, names}` (`names` from `model_spellings()`: every alias and the
repo id of the failed model, so the popup can match whatever spelling the viewer typed) and
`models` (`downloaded_models()`, the `models--owner--name` folders under canonical names).
`/sync` adds `model`, `model_loading` and `model_error`, the last judged for the name that
request carried and null for any other. `friendly_model_error()` turns huggingface_hub's
exceptions into one line: unknown size, not found on Hugging Face, could not reach Hugging Face,
no `model.bin`, else the last line of the message cut to 200 characters.

On the client, `content.js` keeps `state.modelLoading` / `state.modelError` from each answer,
shows `Loading model X… (a first use downloads it)` or `Shisu-ko: model X: <error>` (an error is
shown even with progress messages off, both parts capped by `truncate()`), and the storage
listener clears the verdict and syncs at once when the model setting changes. `popup.js` polls
`/health` every two seconds while open and in view (the same page is the options page, and a
hidden tab polls nothing until it is shown again): the badge says "Loading model" during a
switch, and
`modelErrorFor()` puts the server's verdict under the field only when the field's value (or the
default while empty) is one of the failed model's spellings.

Tests: `server/tests/test_model_switch.py` (a faked `faster_whisper`, `switch_model_if_wanted()` called by hand), `server/tests/test_mlx.py` (the same switch on the Apple GPU), `server/tests/test_setup_model.py`, `addon/tests/content.test.js`, `addon/tests/popup-copies.test.js`.

### Downloading a model at setup

Model download with a progress bar, what setup runs after the check: `server.py --download-model
NAME` (`run_download_model()`: validates the name like `/sync` does, resolves the alias,
`huggingface_hub.snapshot_download()` with faster-whisper's five file patterns and its own tqdm
bars, refuses a repo without `model.bin`, writes `config.json`; exit 0, else 2, the code the
launchers end on instead of restarting, so `run.cmd --download-model x` cannot loop; never loads
a model or takes the instance lock). The download runs on a daemon thread that the main thread
joins in half-second steps (`wait_for_thread()`): a Ctrl+C inside `snapshot_download()` would
only surface once its thread pool has finished streaming the current file (model.bin, minutes),
and Windows delivers the signal only between waits. The interrupt prints one line and ends the
process with `os._exit(2)`, since a normal exit would wait for that pool's worker at shutdown;
the partial blob stays as `.incomplete` and the next download resumes it. `setup.cmd`'s pick
line tests `errorlevel 3` before 2: `choice` answers 255 when it cannot read a key (stdin closed
or empty), and that takes large-v3 like `setup.sh`'s EOF fallback.

Which conversion it fetches follows `resolve_device(args.device)`, not the machine alone: on an
Apple GPU it is the MLX build of that name, since the CTranslate2 files would be the whole wait for
weights that backend never loads, and `--device cpu --download-model` still fetches the CTranslate2
ones, so the download and the start that follows it agree.

### YouTube's sign-in

YouTube answers some addresses with "Sign in to confirm you're not a bot" until a download
carries a signed-in browser's cookies, and the popup's Start button starts the launcher without
options, so the browser is a setting in `config.json` (`"cookies_from_browser"`), not only a
flag. `parse_args()` ends in `resolve_default_cookies()`: `--cookies-from-browser NAME` wins,
`none` sends no browser's cookies for that start, `--cookies FILE` is never joined by the
configured browser, and otherwise `configured_cookies_browser()` fills it in (a name from
`COOKIE_BROWSERS`, yt-dlp's `SUPPORTED_BROWSERS`; anything else is ignored with a warning). The
Docker image never takes it (`in_container()` is `SHISUKO_CONTAINER`, which the Dockerfile sets,
and nothing else), since `DATA_DIR` may be the native `~/.shisu-ko` and the image has no browser
to read. Not any container: toolbox and distrobox carry `/.dockerenv` or `/run/.containerenv`
too, but share the home folder and its Firefox profile, so their starts must send the browser
their setup saved; never test those files again. `--save-cookies-from-browser NAME`
(`run_save_cookies()`, exit 0 or 2 like `--download-model`, before the instance lock) reads the
store once through yt-dlp (`youtube_cookies()`: youtube.com cookie names only, never a value) and
refuses a browser that cannot be read or holds no youtube.com cookie (Chrome and Edge on Windows
encrypt theirs so that no other program can decrypt them); `none` drops the key. A `config.json`
that cannot be written is exit 2 as well (`write_cookies_config()`), never 1, which the launcher
would repeat every 5 seconds, reading the store each time. Setup runs `--setup-cookies`
(`run_setup_cookies()`, always exit 0) after the model question: asked only where yt-dlp finds a
Firefox cookie store (`firefox_profile_found()`, through yt-dlp's private
`_firefox_browser_dirs()` / `_firefox_cookie_dbs()`; without them it asks anyway), Y saves
Firefox, N drops an earlier choice only when that is Firefox (the question named Firefox, so a
browser saved with `--save-cookies-from-browser` stays), and no answer leaves the file alone:
an EOF (`setup.sh` gives a stdin that is not a terminal one, since it held the model's answer) or
`SETUP_COOKIES_TRIES` answers that are neither Y nor N (a stdin that never closes, `yes 1`).
`friendly_error()` gets `Fetcher.cookies_note()`: without cookies the sign-in wall names
`run.cmd --save-cookies-from-browser firefox`; with a browser's it asks for a sign-in to YouTube
there; with a `--cookies` file (`COOKIES_FILE_NOTE`) it asks for a fresh export, since a sign-in
leaves an exported file as it was. `run_check()` prints the configured browser. Tests:
`server/tests/test_cookies.py`.
yt-dlp loads the browser's whole store, every site's cookies with their values (no host filter
in its SQL), once per `YoutubeDL`, so every `download_once()` and `DashLiveSource.refresh()`
reads it again. It reads a copy: `_open_database_copy()` writes the database file into a
`yt_dlp*` folder under the system temp folder and deletes it after the read, so a process that
dies during the read (`os._exit()`, a kill) leaves it there. Its Firefox query has no
`originAttributes` filter either (the server passes no container), so the rows of every
Multi-Account Container and every partitioned cookie (a YouTube embed on another site) go into
one jar, where the last row of a domain, path and name wins: with YouTube signed in in two
contexts, a download may carry the other account's session or a mix. The jar sends a request
only its host's cookies, GitHub's with `--allow-remote-ejs` included. `docs/amo/privacy-policy.md`
and the README's "YouTube sign-in" say exactly this, so a change here changes them too. A Nix
install has no setup and no venv, so `run.sh` refuses there; its command is
`nix run . -- --save-cookies-from-browser firefox` (the flake's loop stops on exit 0 and 2 like
`run.sh`).

## How the Apple GPU works

CTranslate2 has no Metal backend, so on Apple Silicon faster-whisper decodes on the CPU. MLX runs
the same Whisper weights on the GPU and leaves the cores to the video that is playing. Hence
`--device auto` prefers it to the CPU. Measured on an M1 Pro over 180 s of Japanese news audio,
identical windows and identical cue pipeline, nothing else running: faster-whisper large-v3 on
`--device cpu` (int8, beam size 5) at 2.3x realtime, MLX large-v3 (float16) at 5.0x with the same
beam and 5.9x greedy.

Measure with a browser playing a video or the number describes nothing, because the GPU's memory is
the browser's memory. One 38 s window of Japanese conversation, M1 Pro, Chrome playing one YouTube
video:

| model | weights | idle | with Chrome playing |
|---|---|---|---|
| large-v3 | 2.9 GB | 13.0 s | 81.0 s (0.47x realtime) |
| large-v3-turbo | 1.5 GB | 4.8 s | 4.8 s (7.9x realtime) |

0.47x is below playback speed, so large-v3 on a Mac never catches the viewer up. The cause is
memory, not GPU contention: compressed memory rose from 1.9 GB to 14.8 GB between the two large-v3
runs, while Chrome works the GPU exactly as hard during the turbo runs, which cost 0.99x of their
idle time. Turbo has 4 decoder layers against large-v3's 32. It pays one word for them: over that
window both models write the same 22 lines, differing in `釣りあたり` where large-v3 heard
`次あたり`.

`MLX_DEFAULT_MODEL` (`large-v3-turbo`) therefore stands beside `DEFAULT_MODEL` (`large-v3`), and
`default_model_for(device="auto")` chooses between them through `resolve_device()`.
`resolve_default_model()` calls it, and a model chosen at setup still wins over the backend's
default: the viewer who asked for large-v3 gets large-v3, on the Apple GPU too. `run_check(device)`
reports whichever is built in, and `server.py --default-model` prints that name alone and exits,
ignoring `config.json`, so `setup.sh` names its first menu choice by asking rather than by keeping
a copy of the rule (`setup.cmd` is unchanged: there is no MLX on Windows). `retranscribe.py`'s
`--model` follows the same default.

`resolve_device()` is the single place `auto` is decided: `cuda` when there is an NVIDIA GPU, else
`mlx` when `mlx_available()` (darwin, `mlx.core`, `mlx_whisper`, `mx.metal.is_available()`), else
`cpu`. `--device mlx` names it outright. Everything downstream reads the resolved name, so a third
backend is a branch here plus a class, not a change in the transcriber.

A backend is two methods. `transcribe(audio, **options)` returns `(segments, info)`, each segment
carrying `start`, `end`, `text` and `words` of `word` / `start` / `end` / `probability`;
`detect_language(audio=...)` returns `(language, probability, every probability)`.
`MlxWhisperModel` presents exactly those over mlx-whisper (`MlxSegment`, `MlxInfo` and the file's
own `Word`), so the transcriber, the cue gates and the language watch never learn which backend
ran.

One option does not survive the crossing, and the wrapper absorbs it rather than the call sites.
mlx-whisper has no `vad_filter`, so given one the wrapper runs the Silero pass faster-whisper
would have run, hands the decoder the speech with the silence cut out (`collect_chunks`) and maps
every timestamp back (`SpeechTimestampsMap`) — the same `VAD_PARAMS`, so the gates in the cue
builder keep judging what they were written for. `--beam-size` is served, by the decoder
`mlx_beam.py` puts into the library: `MlxWhisperModel.transcribe()` passes the size on only when
the patch took and the caller asked for more than 1, and mlx-whisper, like faster-whisper, drops
it above temperature 0 by itself, so the beam runs on the first pass alone.

`canonical_model_name()` is what keeps one name per set of weights across backends. `MLX_REPOS`
maps a faster-whisper size to the mlx-community repo holding it converted and `MLX_ALIASES`
reverses that, so `large`, `large-v3`, `Systran/faster-whisper-large-v3` and
`mlx-community/whisper-large-v3-mlx` all canonicalise to `large-v3`. One name means one cue cache,
so a machine's CPU cues and its GPU cues are the same file, and one name in the popup whichever
backend is loaded. `model_spellings()` appends the MLX repo, so the popup still matches whatever
the viewer typed.

`download_model_files(name, device)` branches on the device: CTranslate2 files for `cuda` and
`cpu`, `download_mlx_model_files()` for `mlx` (`snapshot_download()` into the same `MODELS_DIR`,
refusing a repo without `config.json` beside `weights.safetensors` / `weights.npz`, the way a
missing `model.bin` refuses a PyTorch checkpoint). `App.prepare_model()` passes `self.device`, so
the state machine above is unchanged and runs on the Apple GPU as documented: nothing in the
download touches the GPU, so it still runs beside the working model, and a size nobody converted
fails as a failed download rather than a crash. `--compute-type` on MLX is `float16` (the default)
or `float32`; anything else is ignored with a warning instead of being reported as loaded, because
`/health` must not name a precision the GPU never ran.

`server/requirements.txt` installs `mlx-whisper` only under `sys_platform == "darwin" and
platform_machine == "arm64"`. faster-whisper stays required everywhere: the MLX path still uses its
Silero VAD, its audio decoding and its alias table. `run_check()` prints `MLX <version>: Metal
available` on macOS and `Backend for --device auto: <device>` on every platform.
`server/tests/test_mlx.py` fakes `mlx.core`, `mlx_whisper`, `huggingface_hub` and `faster_whisper`
in `sys.modules`, so the device choice, the names, the download, the option translation, the VAD
emulation, `detect_language()` and a switch on the Apple GPU are covered without a Mac.

### The beam search (`server/mlx_beam.py`)

mlx-whisper has everything a beam needs and no decoder to use it: `DecodingTask.n_group` sizes the
batch, `MaximumLikelihoodRanker` makes the length-penalised final choice,
`Inference.rearrange_kv_cache()` reorders the cache when the beams are shuffled, and the decoder
itself is a `NotImplementedError`. `mlx_beam.py` fills that hole with a port of openai-whisper's
`BeamSearchDecoder` (MIT) onto MLX, implementing the library's own `TokenDecoder` contract
(`reset` / `update` / `finalize`). It earns its keep in Japanese because greedy decoding commits to
a homophone before the words that would disambiguate it arrive: `敬老` comes out `経老`.

Two things differ from the PyTorch original. `update()` returns `sum_logprobs` rather than mutating
it, since an MLX array is a value. And it returns `completed` as an `mx.array`, because
`_main_loop` hands that straight to `mx.async_eval` beside the tensors. Only each beam's best
`beam_size + 1` continuations cross back from the GPU (`mx.argpartition`, then
`mx.take_along_axis`): no more than that can survive the cut below, and the whole row is the
51 866-wide vocabulary, which costs more to copy than the search it feeds.

`rearrange_self_attention_only()` replaces `Inference.rearrange_kv_cache`, and it is what makes the
beam affordable. The library's version reorders both halves of every layer's `(self_kv, cross_kv)`.
Cross-attention keys and values are computed from the audio, not from the tokens, so all five beams
of one audio hold identical copies of them: permuting those rows changes nothing and, on large-v3,
copies about 1.2 GB per decoded token. Skipping it is exact, because a candidate never comes from
another audio's beam group. Measured: that one change took beam search from 1.1x realtime to 4.1x.

`enable_beam_search()` wraps `DecodingTask.__init__` instead of rewriting it: it runs the original
with `beam_size` and `patience` stripped out, because the original raises on a beam size it cannot
serve, then puts `self.options` back (what `DecodingResult` reports), sets `self.n_group = beam` and
installs the decoder. Carrying the group size through as `best_of` would have been the smaller
patch and does not work: `_verify_options()` refuses `best_of` at temperature 0, the only
temperature beam search runs at. It is idempotent, and it stands aside when `decoding` already has
a `BeamSearchDecoder`, so a future mlx-whisper that grows its own keeps it — that one knows the
library's internals better than a patch does.

`enable_mlx_beam_search()` in `server.py` loads the file by path, the way `run_check()` loads
`native_host.py`, and caches whether it took, so `server.py` stays the one file and a machine
without MLX never reads it. A failure warns and answers False, and the wrapper decodes greedily,
which is what it did before the decoder existed: a patch that cannot be applied must cost accuracy,
never the backend.

The beam does not buy parity with the CPU. In those 180 s it fixes two of greedy decoding's three
slips (`経老` becomes `敬老`, the wrong figure `0.5%に上昇` becomes `0.2ポイント上昇`) and leaves
one (`線上降水帯` where the CPU writes `線状降水帯`).

What the beam costs depends on the audio, and one figure for it is a lie. It is about 15% on the
read news speech above and about 90% on dense conversation: 15.1 s against 8.0 s greedy over the
38 s window. The cost is per decoder step, and a conversational window holds roughly three times as
many segments as a news one. The temperature fallback is the obvious suspect and it is innocent:
pinning `temperature=[0.0]` left that window at 15.1 s against 17.0 s.

`server/tests/test_mlx_beam.py` needs neither a Mac nor MLX: the decoder's `update()` and
`finalize()` against scripted logits where greedy takes the locally better token and loses, the
cache reordering that follows the beams through the self-attention half and leaves the
cross-attention half untouched, the wrapped constructor with a beam and without one, its
idempotence and its retreat before a library that brought its own decoder, and `server.py` falling
back to greedy when the patch cannot be applied. Its `mlx.core` is numpy wearing MLX's names, which
is only safe as long as it does not drift from the library: every decoder test therefore runs a
second time against the installed `mlx.core` when there is one, and
`test_the_library_still_contracts_what_the_fakes_copy` pins the parts of `mlx_whisper.decoding` the
patch reaches into, so an upstream change that moves them fails here rather than on a Mac.

## How the Start server button works

A WebExtension cannot spawn a process, so the popup's button goes through native messaging:
`background.js` sends `{cmd: "start"}` to the native host `shisuko`, and the host,
`server/native_host.py`, runs the checkout's own launcher. Firefox and Chrome: `register()`
writes a host manifest for each, and Chrome's has to name the extension by an id that is fixed
only for the Chrome Web Store install (`CHROME_EXTENSION_ID`, `ecenifonpkaiccmmknpbllbebbfigjnm`);
an unpacked build's id is derived from the path of the folder it was loaded from. So `popup.js`
shows the button only when `browser.runtime.getURL("")` is `moz-extension:` (`ON_FIREFOX`) or
exactly `chrome-extension://<CHROME_STORE_ID>/` (`START_AVAILABLE`); `ON_FIREFOX` alone decides
the Firefox-only rest (the Alt+Shift+H hint Chrome's build has no key for, the signed-.xpi wait
of the update banner). Another Chromium-based browser holding the store install can give that
URL, so one that reads host manifests from a folder `browsers()` does not write (Chromium
outside Linux; on Linux and macOS Chrome Beta, Dev and Canary, whose folders carry the channel's
name; Brave, Edge and the like) can show the button and get "launcher not registered". On
Windows every Chrome channel reads the one `HKCU` key. The host is stdlib only, so the wrapper can fall back to the
system Python before setup ran, and it never imports `server.py` (`VERSION` is read from it with
a regex). On Chrome the background reaches the host through `browser-api.js`, whose
`runtime.sendNativeMessage` is a getter over `chrome.runtime`: Chrome adds the method only once
the popup's grant lands, while the service worker runs.

Protocol (the browsers' own: 4-byte little-endian length, UTF-8 JSON, one request per message,
answered in order until stdin closes; `read_message()` / `write_message()`, `serve()`, `handle()`):
`{"cmd": "status"}` -> `{ok, running, version, root}`, `running` being a `/health` answer within
1.5 s (`server_running()`); `{"cmd": "start"}` -> `{ok: true, already: true}` for a running
server, `{ok: true, already: true, starting: true}` for one that holds the instance lock but
does not answer yet (its model is loading; `server_starting()`), else `launch()` and
`{ok: true, started: true, log}` (`log` null on Windows, the path of `~/.shisu-ko/server.log`
elsewhere) or `{ok: false, error}` in one line. Anything else, a request with extra keys
included, is `{ok: false, error: "unknown command"}`; a frame over 1 MiB is answered and ends
the host. The instance lock is `APP_DIR/server-<port>.lock`: `server.py` takes it in
`hold_instance_lock()` before `load_model()` and holds it until the process ends, a second
server exits 2. `try_lock()` exists in both files (`msvcrt.locking` / `fcntl.flock`); only
`LOCK_HELD_ERRNOS` mean "held", a filesystem that cannot lock at all counts as no lock.
`launch()` on Windows runs `cmd.exe /c start "Shisu-ko server" .\run.cmd` with `cwd=server/`
(cmd.exe splits a full path holding `&` or `(` even when quoted), `DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP` and first `CREATE_BREAKAWAY_FROM_JOB`, retrying without it on
`PermissionError`; on POSIX `bash run.sh` with `start_new_session=True` and stdout/stderr
appended to the log (bash, not the file itself: a zip install has no mode bits). The browser
starts the host with arguments of its own (Firefox: manifest path and extension id; Chrome: the
extension's origin and, on Windows, `--parent-window=<handle>`); `main()` serves whenever no
action flag is given and stdin is not a terminal.

Registration (`native_host.py --register | --unregister | --status [--verbose]`, exit 0 on
success, quiet unless `--verbose`) writes one manifest per browser in `browsers()`: Firefox and
Chrome everywhere, Chromium too on Linux. Each is `{name: "shisuko", description, path:
<wrapper>, type: "stdio"}` plus, for Firefox, `allowed_extensions:
["shisu-ko@multysquid.github.io"]` and, for Chrome and Chromium, `allowed_origins:
["chrome-extension://ecenifonpkaiccmmknpbllbebbfigjnm/"]` (`manifest()`). They go to
(`manifest_path()`): on Windows `%USERPROFILE%\.shisu-ko\native-messaging\shisuko.json` and
`shisuko-chrome.json` beside it (`SHISUKO_HOME` respected), each named by the default value of
its key under `HKCU` (`REGISTRY_KEYS`: `Software\Mozilla\NativeMessagingHosts\shisuko`,
`Software\Google\Chrome\NativeMessagingHosts\shisuko`); on Linux
`~/.mozilla/native-messaging-hosts/shisuko.json`,
`~/.config/google-chrome/NativeMessagingHosts/shisuko.json` and
`~/.config/chromium/NativeMessagingHosts/shisuko.json` (`config_home()`: `$CHROME_CONFIG_HOME`,
else `$XDG_CONFIG_HOME`, replaces `~/.config` when it is set and not empty, the order Chrome's
own `chrome_paths_linux.cc` uses; Firefox's folder follows neither); on macOS
`~/Library/Application Support/Mozilla/NativeMessagingHosts/shisuko.json` and
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/shisuko.json`. Every
browser is tried: one that cannot be written does not keep the others from the button, and
`register()` then raises `RegistrationError` with the failures and the ones that were written
(`--register` exits 1 and, with `--verbose`, still lists those). `unregister()` removes every
manifest and key; `registered()` asks per browser (on Windows the registry value must name an
existing file); `status_text()` is one line, per browser "<Browser> registered at <path>", with
"(points at ...)" for another checkout's wrapper and "(out of date ...)" for a manifest that
differs from what `--register` would write, "<Browser> not registered (run setup or start the
server once)", or "<Browser> could not be checked (<error>)" when `registered()` raises (a
folder this user cannot enter, such as a profile a browser once started through sudo left to
root), which leaves the other browsers reported. Only when every browser was checked and none
has it is the line the bare "not registered (run setup or start the server once)". The wrapper
is `server/native-host.cmd` (CRLF) or `server/native-host.sh` (LF, mode 755; `register()`
restores the bit a zip install drops): the venv's Python, else the system one, on
`native_host.py`. `setup.cmd` / `setup.sh`
register and then run `--check`, which reports it; `run.cmd` / `run.sh` register on every
start, so an install that never re-ran setup gets the button after a manual start, with one
exception: the start that updates an older checkout to a version with the button does not
register, because the old launcher is what runs (`run.cmd`'s old `update.py ... & goto loop`
jumps to `:loop` in the new file, below the register line; `run.sh`'s already-parsed old
`main()` has no register call), so it is the start after the update, or setup, that registers.
In `run.cmd` the call sits on its own line before the `update.py ... & goto loop` line; in
`run.sh` after the update, which rewrites the wrapper. So an install that already has the button
gets Chrome's registration from the start that updates it on Linux and macOS (`main()` and the
code-4 branch start the updated `native_host.py --register` after `update.py`), and on Windows
only from the next `run.cmd` start (its register line runs before the update, and `:update`
does not register).
`launch()` passes no arguments to the launcher: a server the button started runs on `server.py`'s
defaults (`--model` from `config.json`, else large-v3; `--device auto`; `--cookies-from-browser`
from `config.json`, see "YouTube's sign-in"), and the popup's model
setting only takes effect after that default model is loaded.
`run_check()` in `server.py` loads `native_host.py` by path and prints `status_text()`.

Extension side: the popup's flow is `startFlow.state`, `idle -> requesting -> starting ->
waiting -> idle` once `/health` answers, or `failed` with the reason on the detail line and the
button back; the permission request is issued in the click handler before its first `await`
(it needs the user gesture). A server-address edit (`checkServer(true)`) puts a spent flow
(`failed`, and the update flow's `done` / `stale` / `failed` / `lost`) back to `idle`, since
its hint was judged at the old address, and overtakes a `/health` request still under way
(`healthAsked`: the old address may hold it up for the full timeout, and its answer is
dropped). The background owns the launch (`startServer()`): one
`sendNativeMessage` at a time (`startInFlight`), raced against `NATIVE_TIMEOUT_MS` (15 s), the
answer recorded with a `deadline` of `START_WINDOW_MS` (90 s, past any healthy model load) in
memory and in `browser.storage.session` (Firefox ends an idle event page after 30 s), so a
reopened popup resumes at "waiting" (`startServerStatus`) instead of offering a second start
while the first still loads; a `/health` answer through `apiRequest()` forgets the record.
`nativeError()` maps the browser's exceptions: "No such native application" / "not found" /
"forbidden" -> `launcher not registered` with `LAUNCHER_HINT`; permission wording, or no
`sendNativeMessage` at all -> `permission missing`; timeout -> `the launcher did not answer`;
the host's own `{ok: false, error}` passes through. The popup keeps `already` and the host's
`starting` (as `loading`) for the hint at the deadline: `START_ELSEWHERE_HINT` when the launcher
saw a server answering that the popup cannot reach, else `startNotUpHint(log)`. Badge texts:
`Checking server`, `Server offline`, `Starting server`, `Updating server`, `Loading model`,
`Server online`. A start and an update exclude each other: `requestStart()` refuses while
`pendingUpdate()` holds a record (`UPDATE_RUNNING_HINT`), `startStatus()` carries that record
as `updating` so a reopened popup hides the button before its first paint, and the popup
disables Update while `START_BUSY` and hides Start while `UPDATE_BUSY`.

Tests: `server/tests/test_native_host.py`, `server/tests/test_server.py` (`hold_instance_lock()`), `addon/tests/background.test.js`, `addon/tests/popup.test.js`, `addon/tests/browser-api.test.js`.

## How the update step works

`server/update.py` (stdlib only) runs first in `run.cmd` / `run.sh`, never in Docker or Nix. In a
git checkout it fetches the tracked upstream and fast-forwards (`merge --ff-only`); a diverged
branch, local changes git would overwrite, a detached HEAD or an unreachable remote leave the
tree alone with a message. In a folder without `.git` it compares `VERSION` with the newest
GitHub release tag (`v<VERSION>`, so keep bumping `VERSION`, the manifest and the tag together)
and unpacks the release zip over the folder, staging each file next to its target and
`os.replace()`-ing it, without deleting anything. Both paths reinstall `requirements.txt` into
the running interpreter when it changed (only inside a venv) and point out a changed
`addon/manifest.json` version. It always exits 0: the server must start even when the update
fails. `--no-update` or `SHISUKO_NO_UPDATE=1` skips it; `server.py` accepts `--no-update` too
(`args.no_update`) so the launchers can pass all arguments through, and reads it, like the
variable, as a reason to refuse `POST /update`.

The launchers also run the update on demand, for the popup's Update button: `run.cmd` /
`run.sh` set `SHISUKO_LAUNCHER=1` for the server they start, `POST /update` then marks
`App.exit_code = EXIT_UPDATE` (4), answers, and `stop_server_later()` calls `httpd.shutdown()`
from a helper thread after `SHUTDOWN_DELAY` (0.5 s, so the answer leaves the socket;
`shutdown()` blocks until `serve_forever()` returns, so the handler thread cannot call it),
and `main()` runs `server_close()` and then `sys.exit(app.exit_code)`; every worker is a daemon
thread, so nothing waits for a window. Exit code 4 means "run `update.py`, then start again":
`run.cmd` has `if "%CODE%"=="4" goto update` after the 0 and 2 branches, `run.sh`
`[ "$code" -eq 4 ] && { update.py "$@"; native_host.py --register; continue; }` (the register
call restores the wrapper's mode bits, which the zip update drops). Codes 0 and 2 keep their
meaning, every other code keeps the 5 s restart.

The update can replace the launcher that is running it. cmd.exe reads batch files incrementally,
so in `run.cmd` the update call and `goto loop` must stay on one line and the `:loop` label must
keep its name; `run.sh` keeps everything in `main()` and ends with `main "$@"; exit` for the same
reason. The code-4 path adds two rules. The `:update` label sits directly above that one-line
update call, so `goto update` lands on it: the lines between the server's exit and `goto update`
are read from the old file, which nothing changed since the jump to `:loop`, and the already
parsed `goto loop` looks its label up in the new file, so no line is ever read from a stale
offset. And `set "SHISUKO_LAUNCHER=1"` sits directly after `:loop`, not at the top: a launcher
from before the variable that has just updated itself arrives in the new file through its own,
already parsed `goto loop`, so only the lines after `:loop` run for it, and its server would
otherwise refuse the button. `run.sh` cannot help itself the same way (bash parsed the old
`main()`, which has no code-4 branch, before the update), so `export SHISUKO_LAUNCHER=1` sits
inside `main()` before the loop and the server's 409 text names "an older launcher that has not
been restarted since it was updated"; a restart by hand fixes it. 
Tests: `server/tests/test_update.py` drives the real git against a bare repository in a temp directory and feeds a locally built zip in place of the GitHub download; `server/tests/test_update_endpoint.py` covers the endpoint over a real socket and the launcher texts.

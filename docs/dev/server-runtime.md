# Server runtime: live streams, models, the Apple GPU, the Start button, the update step

The parts of the server that are not cue building. Read the matching section before changing `LiveFollower`, `switch_model_if_wanted()`, the Kitsune engine (`server/kitsune_engine.py`, `load_kitsune_model()`, `server/kitsune_setup.py`), `server/native_host.py`, `server/update.py`, `POST /update`, the launchers, the experimental AMD engine (`rocm_engine()`, `server/amd_setup.py`) or any way out of the server process (`hard_exit()`, `finish()`).

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

The popup's `model` setting names the model (Whisper or Kitsune) the server should run; `--model` is only the
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
   if even that fails the server has no model left and calls `hard_exit(3)` (`os._exit(3)`
   except with the AMD engine on Windows) so the launcher restarts it on `--model`. With the AMD
   engine on Windows this step is a restart instead, since freeing a model can hang there: see
   "A model switch on Windows" under "How the AMD engine works".

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
process with `hard_exit(2)`, since a normal exit would wait for that pool's worker at shutdown;
the partial blob stays as `.incomplete` and the next download resumes it. `setup.cmd`'s pick
line tests `errorlevel 3` before 2: `choice` answers 255 when it cannot read a key (stdin closed
or empty), and that takes large-v3 like `setup.sh`'s EOF fallback. After the model download
both setups run `amd_setup.py` without arguments, on a line of its own whose exit code nothing
reads (`|| true` in `setup.sh`, under `set -e`), before "Setup is complete": it offers the
experimental AMD engine where it finds an AMD card and never fails the setup (see "How the AMD
engine works").

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

## How the Kitsune-Transcribe models work

Kitsune-Transcribe's students (github.com/Multysquid/Kitsune-Transcribe) are not Whisper models:
the Transcribe family is `CohereAsrForConditionalGeneration` (FastConformer encoder, Transformer
decoder, `family "aed"`), the Parakeet family `ParakeetForCTC` (FastConformer encoder, CTC head,
`family "ctc"`). CTranslate2 converts neither, so they run on PyTorch and transformers, in
`server/kitsune_engine.py`. `server.py` stays importable without torch: it loads the engine by
path (`kitsune_engine()`, registered in `sys.modules` before `exec_module` for its dataclasses),
and the engine imports torch only inside the functions that load and run a model. Its pure parts
(unpacking, timings, chunks, segments) are numpy only and tested in `test_kitsune.py` without torch.

**Names.** `KITSUNE_REPOS` maps `kitsune-0.6b` / `-0.3b` / `-0.1b` to their repos
(`Multy123/kitsune-transcribe-<size>`). A repo's root holds the training run's bf16 export (the
bare name), each precision a folder of its own as `python -m kitsune.quant export` writes it
(`KITSUNE_FORMATS`: `fp16`, `int8` -> `int8-w8a16`, `fp8` -> `fp8-w8a8`, `nvfp4` -> `nvfp4-w4a16`,
`mxfp4` -> `mxfp4-w4a4`). `kitsune_name()` parses a name into (base, short precision);
`canonical_model_name()` gives `base` or `base-<short>`, so `-int8-w8a8` and `-int8-w8a16` are one
model: kitsune.quant writes their files byte-identical, and the server keeps activations 16-bit,
so the two formats run the same here. The canonical name is also the cue cache's model, so every
precision keeps cues of its own. All names pass `MODEL_NAME_RE`; the popup's copy is unchanged.

**Download.** `download_model_files()` refuses a Kitsune name while `kitsune_runtime_missing()`
(torch, transformers, safetensors not importable): the download would be gigabytes for a model
that cannot load, and the popup shows `KITSUNE_INSTALL_HINT`. Otherwise `kitsune_download_plan()`
names the repo, the folder and `KITSUNE_FILES` in it, and `snapshot_download()` fetches only
those; `require_kitsune_files()` refuses a folder the repo does not have ("may not be published
yet"). `run_download_model()` (setup) takes Kitsune names like sizes: the choice goes to
`config.json` before the download, with `kitsune_size()` in the announcement. `downloaded_models()`
lists a Kitsune repo's root and folders that hold a package (`kitsune_downloaded()`).

**Load.** `load_model()` hands a Kitsune name, or a folder whose `config.json` names one of the
two architectures (`is_kitsune_model()`, the operator's `--model` may be one), to
`load_kitsune_model()`: `--language` must be `ja`, the files come from `download_model_files()`
(cached), `kitsune_engine.load()` builds the model, a GPU failure falls back to the CPU, and a
2-second warm-up runs. `compute_type` in `/health` reads `bfloat16`, or `int8-w8a16 weights,
bfloat16`; `/health` also says `engine: "kitsune"`. A switch away frees the model and calls
`release_torch_memory()` (PyTorch caches freed GPU memory).

In the engine, `read_package()` checks the folder before torch is imported. A plain export (or
the fp16 variant, whose keys are HF's) loads with `from_pretrained()`. A quantised variant is
built from its config under `no_init_weights()`, and `variant_state_dict()` unpacks every layer
`quantization.json` names (`dequantize()`: int8 and fp8 per output row, nvfp4 E2M1 codes times an
E4M3 scale per 16 times the FP32 tensor scale, mxfp4 E2M1 times a power of two per 32; fp8 and E4M3
scales read as their bytes through `E4M3_VALUES`, since numpy has no float8); a pointwise conv's
`.linear` layer goes back to its Conv1d key and shape; tied keys may be missing, nothing else. The
weights then go to the compute dtype (`pick_dtype()`: bf16 on a GPU that has it, fp16 for the
fp16 variant, fp32 on the CPU; `--compute-type bfloat16/float16/float32` overrides) with norms and
BatchNorm left fp32 under autocast (`cast_for_inference()`, kitsune.quant's fp16 recipe). So every
format runs everywhere with the numbers its weights hold; activation quantisation is not
emulated, and GPU memory and speed are the 16-bit model's.

**Decoding.** `KitsuneModel.transcribe()` has `WhisperModel.transcribe()`'s shape: it runs the same
Silero detector (`vad_parameters`, capped at `MAX_CHUNK_S` = 28 s per interval), joins speech
across pauses up to `CHUNK_GAP_S` into chunks of at most 28 s (`plan_chunks()`; the Cohere
feature extractor splits audio above 30 s, and the students never saw more), and decodes each
chunk greedily, as the students were evaluated. The features are the package's own processor's.
- CTC: encoder, the CTC head in fp32 outside autocast, log-softmax as `z - logsumexp(z)`
  (`kitsune.ctc_student.ctc_log_probs`), then `ctc_spans()`: a run per token of the greedy path.
  A word starts at its first frame and ends `CTC_TAIL_S` after its last, never past the next word.
- AED: encoder once, `generate()` with the teacher pass's decoder prompt (`AED_PROMPT`), max
  new tokens `16 + 10 x seconds` and the repetition stop, then one teacher-forced pass over the
  result. Its cross-attention (recomputed from the q/k projections through hooks, so the attention
  backend does not matter; the upper half of the layers) is normalised per head, median filtered
  and aligned by DTW (`attention_boundaries()`, Whisper's `find_alignment`), and
  `cap_durations()` shortens a word that spans a pause (`WORD_BASE_S` + `WORD_CHAR_S` per
  character: a first word or one after punctuation keeps its end, any other its start).
Words are groups of tokens (`group_tokens()`: a character split over byte-fallback tokens stays
whole; the text is the decoded prefix's increment). `make_segments()` cuts them at sentence marks
and pauses of `SEGMENT_GAP_S`; `no_speech_prob` is 0 and `avg_logprob` the words' mean log
probability.

**What the transcriber does differently.** A Kitsune model says `detects_language = False`,
`sings = False`, `takes_prompt = False`. `process()` skips the language watch for it (no language
head; `detect_language()` raises), never takes the lyrics path or leaves unsung stretches
uncovered (the lyrics gates read Whisper's confidence figures), and `transcribe_options()` passes
no initial prompt, so `retry_prompt_skips()` never decodes a window twice. `dump_words.py` takes
its options and its lyrics decision from the same helpers and loads a Kitsune model through
`load_kitsune_model()`.

**Installing.** `server/kitsune_setup.py` (stdlib only, never imports `server.py`) installs
PyTorch from its own index (`cu128` where `nvidia-smi -L` lists a GPU, `cpu` elsewhere, PyPI on
macOS) and `server/requirements-kitsune.txt` into the interpreter running it; exit 1 when they do
not import afterwards. `setup.cmd` / `setup.sh` run it only for a Kitsune pick, before the
download, and a failure ends setup. `update.py` installs a changed `requirements-kitsune.txt`
only where torch is importable. The Docker image installs torch (`TORCH_INDEX` build argument,
cu128 by default) and copies `kitsune_engine.py`; the Nix package has no torch.

**Measured on real weights** (a scratch venv, CPU, a 49 s clip of five eval utterances): the
Cohere teacher through the AED path wrote every Japanese line correctly with plausible word
times; the Parakeet anchor packaged as `ParakeetForCTC` through the CTC path likewise; int8, fp8,
nvfp4 and mxfp4 variants of the anchor, packed per the WP5 recipes, decoded an 18 s passage
word for word like the bf16 export, and a T-0.6B nvfp4 variant loaded with its 262 layers and
its tied head. Not yet measured: a trained student, the cue geometry `cue_stats.py` reports for
Kitsune against large-v3, and the gates' word-probability thresholds (`VAD_GATE_PROB` and the
like were tuned on Whisper's probabilities).

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
and `main()` runs `server_close()` and then `finish(app.exit_code)` (`sys.exit()` except with the
AMD engine on Windows); every worker is a daemon thread, so nothing waits for a window. Exit
code 4 means "run `update.py`, then start again": `run.cmd` has `if "%CODE%"=="4" goto update`
after the 0 and 2 branches, `run.sh`
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

`update.py` never touches the AMD engine's side folder (below), and no launcher runs
`amd_setup.py`: an engine installed from older pins keeps running until setup, or
`amd_setup.py` run by hand, offers to update it.

## How the AMD engine works (experimental)

Experimental, and not yet tested on AMD hardware by the maintainer, whose PC has an NVIDIA GPU;
CI has no GPU at all. CTranslate2 has published a build for AMD GPUs (ROCm/HIP) since 4.7, and
faster-whisper 1.2.1 uses it unchanged: to both a HIP build is still `device="cuda"`, and
`ctranslate2.get_cuda_device_count()` counts HIP devices, so `cuda_available()`, `--device auto`
and `load_model()` work as they are, and `/health` reports device `cuda`. The build is not on
PyPI but in zips of wheels on CTranslate2's GitHub release, whose wheels carry the PyPI wheels'
file names, so pip takes them for the same distribution. On Windows it needs AMD's runtime
wheels `rocm_sdk_core` and `rocm_sdk_libraries_custom` from repo.radeon.com as well; they install
as the folders `_rocm_sdk_core` and `_rocm_sdk_libraries_custom`, and CTranslate2's own
`__init__` adds their `bin` folders as DLL directories relative to its package folder, so the
three must sit side by side. On Linux it links against a system ROCm 7.2.x (`libamdhip64.so.7`,
`libhipblas.so.3` and `libhiprand.so.1` under `$ROCM_PATH/lib`, default `/opt/rocm`; OpenMP under
`lib/llvm/lib`) and needs `/dev/kfd` and a user in the render and video groups. The NVIDIA and
CPU paths stay exactly as they were: while the engine is off, nothing below changes a call.

What it runs on. The build is compiled for gfx1030, gfx1100, gfx1101, gfx1102, gfx1150, gfx1151,
gfx1200 and gfx1201, and AMD's Windows runtime carries rocBLAS kernels for all of them but
gfx1030. On Windows that is the Radeon RX 7000 and RX 9000, the Radeon PRO W7000 and W9000 and
the Radeon AI PRO R9700, the Radeon 890M/880M of Ryzen AI 300 and the Radeon 8060S/8050S/8040S of
Ryzen AI Max, with AMD Software: Adrenalin Edition 26.2.2 or newer; not the RX 6000 or anything
older. On Linux it is the same cards plus the RX 6800/6900 (gfx1030), on a ROCm 7.2.x the user
installs (https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.4/, which also has the
user join the render and video groups; AMD's unversioned `latest` guide now installs ROCm 10.0 and
asks that ROCm 7.2.4 or older be uninstalled first); the RX 6600/6700 (gfx1032/gfx1031) is no
target and runs, if at all, at the user's own risk with `HSA_OVERRIDE_GFX_VERSION=10.3.0`. It is
never used in the Docker image or under Nix: the image holds `server.py` alone and runs no setup,
a Nix install has no setup and no venv, and `rocm_engine()` refuses both even when a data folder
shared with a native setup, or `SHISUKO_ENGINE=rocm`, asks for the engine. No build exists for
macOS, ARM or a 32-bit Python.

### Where it lives

The side folder `ROCM_DIR`, `~/.shisu-ko/rocm`, holds CTranslate2's package and, on Windows,
AMD's two runtime packages beside it, installed by `amd_setup.py` with `pip install --target`,
plus the marker `shisuko-rocm.json` (`ROCM_MANIFEST`), a JSON object with `ctranslate2` (4.8.2),
`rocm` (7.2.1 on Windows, `system` on Linux), `python` (the wheel's tag, such as `cp312`),
`platform` (`win_amd64` or `linux_x86_64`) and `installed` (the time, ISO 8601). The venv's own
CTranslate2 is never touched: an
install into the venv would replace the NVIDIA build, and `update.py`'s `pip install -r
requirements.txt` could later put the PyPI build back over it. `config.json`'s `"engine": "rocm"`
turns the engine on. Only a `server.py --probe-gpu` that passed writes it; a probe drops it
before it loads anything, and `amd_setup.py` drops it after a failed or cancelled test, before
it replaces an installed engine, when it finds no AMD GPU, and on `--remove`.
`SHISUKO_ENGINE=rocm|default` in the environment decides over `config.json`: the probe runs with
`rocm`, before `config.json` says anything, and `default` keeps a start on the default engine
whatever `config.json` says. Beside the side folder live `rocm-starts` (the crash guard),
`next-model` (the model a switch on Windows restarts into) and, while `amd_setup.py` installs,
`rocm.new`, `rocm.old` and `cache/rocm-download`.

### The import: rocm_engine()

`rocm_engine()` runs at import time, right after `NVIDIA_DIRS = add_nvidia_dll_dirs()` and before
anything imports ctranslate2 or faster_whisper, never raises, and sets `ROCM_ACTIVE` and
`ROCM_REASON` (why it is on or off, for `--check` and the start's log, `rocm_engine_line()`). The
decision is `rocm_engine_state()`, pure, in this order: never in the Docker image
(`in_container()`: the image may share the data folder with the native setup, side folder and
crash guard included, but has no ROCm runtime, and a failed start there would count against the
native server's guard); never under a Python from the Nix store (`sys.base_prefix` under
`/nix/store/`: the manylinux build needs the system's libraries, which Nix's loader does not
search, and `nix run .` shares `~/.shisu-ko` too); `SHISUKO_ENGINE`, else `config.json`; a
`ctranslate2/__init__.py` in the side folder; a marker whose `python` is
this interpreter's tag (`python_tag()`: `cp312`, or `cp314t` for a free-threaded build, so a venv
rebuilt on a newer Python falls back to its own CTranslate2) and whose `platform` is this one's
(`platform_key()`, `ROCM_PLATFORMS`); and a crash guard below its limit. `rocm_engine()` adds
three rules. A program that only imports `server.py` (the tools in `server/tools`,
`script=False`) keeps the default engine on Windows in every case, since it ends through the
interpreter's own exit (see "Ending a process on Windows"), and on Linux unless
`SHISUKO_ENGINE=rocm` asks for the engine and `$ROCM_PATH/lib` is on its `LD_LIBRARY_PATH`
already. On Linux the engine stays off while `missing_rocm_libraries()` finds one of
`ROCM_LINUX_LIBRARIES` missing from `$ROCM_PATH/lib` (ROCm removed or upgraded past 7.2, or a
`ROCM_PATH` exported in the setup's terminal that the Start button's environment lacks); every
start would otherwise fail on the import, and the server takes the engine again by itself once
the libraries are back. And a folder that cannot be read leaves the engine off with the error as
its reason.

Turned on, `rocm_engine()` puts `ROCM_DIR` first on `sys.path` and sets
`CT2_CUDA_ALLOCATOR=cub_caching` (`setdefault`, so an operator's own value stays) before the first
allocation: CTranslate2's default allocator on Linux loses text silently or aborts on AMD cards
(CTranslate2 #2090, up to 95 % of the text gone on gfx1030 and gfx1151; #2021, a crash on
gfx1201), and the Windows HIP build uses cub_caching already, so there it changes nothing. On
Linux the dynamic loader reads `LD_LIBRARY_PATH` only when a process starts, and not every ROCm
install put its libraries in `ld.so.conf`: when `$ROCM_PATH/lib` is not on it,
`rocm_library_path()` puts `$ROCM_PATH/lib` and `$ROCM_PATH/lib/llvm/lib` in front, and the
server starts itself once more with `os.execv(sys.executable, [sys.executable] + sys.argv)`,
stdout and stderr flushed first. `SHISUKO_ROCM_REEXEC=1` in the environment is the guard that
keeps the second process from starting a third; an `execv` that fails puts both variables back
and leaves the engine off. `run.sh` stays as it is, and `amd_setup.py` removes
`SHISUKO_ROCM_REEXEC` from its probe's environment, since the guard belongs to the process that
set it.

### The crash guard and the hand-over

A card or driver the build cannot use can end the process where no except clause sees it (a C++
terminate, "Memory access fault by GPU"): `load_model()`'s CPU fallback never runs, and the
launcher would restart the server into the same crash for ever. `rocm-starts`
(`ROCM_STARTS_PATH`, an integer; a missing or garbled file is 0) counts the starts on the AMD
engine since the last one that got a model onto the GPU. `main()` counts one
(`count_rocm_start()`) after the instance lock and before the model loads, unless `--device cpu`
keeps the start off the GPU; `load_model()` deletes the file once a model is loaded and warmed up
with device `cuda` (`clear_rocm_starts()`); `start_app()` takes the count back
(`uncount_rocm_start()`, one step down, so that an earlier crash stays counted) when
the load ends on a Python exception other than an `ImportError`, which is the model's fault (a
typo, no connection, a missing file) and ends the start on code 2, which the launcher does not
retry; a Ctrl+C during the load takes it back too. At `ROCM_GUARD_LIMIT` (2) the next start keeps
the engine off (`rocm_guard_allows()`), with a reason that names `server/amd_setup.py --probe`.
`--probe-gpu` ignores the guard, since it is how a guarded engine gets tested again, and a passed
probe deletes the file. The one-shot commands (`--check`, `--download-model`,
`--save-cookies-from-browser`, `--setup-cookies`, `--probe-gpu`) never count, and a guard file
that cannot be written costs a warning, not the start.

`rocm_engine()` sees files, not whether they load. So before the model loads,
`check_rocm_import(device)` imports ctranslate2 and looks up `get_cuda_device_count` (the
compiled part: without it the package imports empty), and for a start that is to use the GPU it
asks for the device count. A build that does not load (a system ROCm removed or upgraded past
7.2, a DLL missing from the side folder, on Linux a `ROCM_PATH` the Start button's environment
lacks) or that sees no AMD GPU (a card removed or replaced, a driver it cannot use: on Windows
Adrenalin 26.2.2 or newer, on Linux `/dev/kfd` and the render and video groups) goes to
`leave_rocm()`. It logs one error line that says what happens next and names
`server/amd_setup.py --probe` and `--remove`, sets the guard to its limit and ends with
`hard_exit(3)`: `run.cmd` / `run.sh` start the server again five seconds later, on the default
engine; a start by hand just ends, and its next start uses the default engine. Without the
hand-over a failed import would end the start on code 2, which the launcher never retries,
without a word about the engine, and `--device auto` without an AMD GPU would run the model on
the processor while an NVIDIA GPU beside it stayed idle. A guard that cannot be written ends the
start on code 2 after all, since a restart would meet the same failure for ever.

### Ending a process on Windows

CTranslate2's ROCm build on Windows can hang the way out of a process: freeing a model (#2038),
the interpreter's exit (#2085), and destructors or even `os._exit()` inside the runtime's DLL
detach, even on the CPU (#2101). All three were open on 2026-09-26, and what helped there is
`TerminateProcess`, which runs no DLL detach. The maintainer's PC reproduced it with the real
4.8.2 Windows build running `tiny` on the CPU (no AMD GPU): after the model had run, a plain
interpreter exit hung until it was killed after 90 s and `del model; gc.collect()` hung for
60 s, while the same script on the default engine ended normally; a passing probe that returned,
and so freed its model, hung the same way and would have been stopped by `amd_setup.py` after
fifteen minutes and switched off. A launcher waiting on such a process never restarts it. So
where `rocm_on_windows()` holds (`ROCM_ACTIVE` and `os.name == "nt"`), no exit is left to Python:

- `hard_exit(code)` stands for every `os._exit()` (a broken GPU context, a lost model, the
  hand-over and a switch's restart, code 3; the interrupted setup download, code 2; the probe's
  end) and `finish(code)` for every `sys.exit()` of `main()`, `--check` and the normal end
  (`finish(None)`, 0 there) included. Everywhere else they are `os._exit()` and `sys.exit()` (or
  a plain return) as before; with the engine on Windows both call `terminate_process()`:
  `flush_output()` (every logging handler, stdout, stderr), then
  `kernel32.TerminateProcess(GetCurrentProcess(), code)` through ctypes, with its argument types
  set.
- `KEPT_MODELS` holds every model `load_model()` builds there, appended before the warm-up, so
  no model is ever freed: not by a function that returns (`run_probe_gpu()`), nor by the
  traceback of a failed warm-up. The process ends through `TerminateProcess` anyway, and its
  model switch is a restart, so nothing is held longer than before.
- `end_on_ctrl_c()`: a model load is one long call into CTranslate2, and a Ctrl+C during it
  raises `KeyboardInterrupt` the moment the call returns, before faster-whisper has stored the
  model, whose unwinding would free it. While the server's model loads, and for the whole probe,
  Ctrl+C and Ctrl+Break get a handler that ends the process instead: `finish(0)` for the server,
  which takes this start's count back first, `hard_exit(1)` for the probe. Ctrl+Break gets it too
  because Python leaves that signal to the console, whose default handler ends the process
  through `ExitProcess` and its DLL detach. Once the model is loaded both get Python's own handler
  back, so a Ctrl+C or Ctrl+Break while serving raises `KeyboardInterrupt`, `server_close()` runs
  and `finish()` ends the process.
- `entry_point()` is what `python server.py` runs: `main()` inside a catch that passes
  `SystemExit` on and, with the engine on Windows, ends a `KeyboardInterrupt` with `finish(0)` and
  any other exception with its traceback in the log and `finish(1)`, the code after which
  `run.cmd` restarts the server; everywhere else it re-raises, as before. Two such exceptions are
  stopped earlier as well: `--port` refuses a number outside 1-65535 while the arguments are read
  (`port_number()`), before the instance lock and the model load, since the listening socket
  raises `OverflowError` for one, not `OSError`; and the listen step catches `OverflowError`,
  `ValueError` and `TypeError` (a host that cannot be encoded) beside `OSError`, ending on 2.

`test_rocm.py` holds by the syntax tree that the `os._exit()` in `hard_exit()` and the
`sys.exit()` in `finish()` are `server.py`'s only exit calls (no other `os._exit()`, `sys.exit()`,
`raise SystemExit`, `exit()`, `quit()`, `os.abort()` or `os.kill()`) and that the script runs
`entry_point()`. The maintainer has no AMD card to see such a hang, so these tests are the only
guard: a new way out of the server goes through `hard_exit()` or `finish()`.

### A model switch on Windows

Since freeing a model can hang there, `switch_model_if_wanted()` never frees one with the engine
on Windows. The download runs as for every switch; once `prepare_model()` has the files,
`restart_for_model()` writes the canonical name to `next-model` (`NEXT_MODEL_PATH`), logs that a
switch restarts the server with the AMD engine on Windows, and ends with `hard_exit(3)`. `run.cmd`
says that the server stopped unexpectedly and starts it again five seconds later; the popup's
Start button runs `run.cmd`, so a server it started switches the same way. `start_app()` then
takes the file (`take_next_model()`: the file goes first, whatever it holds, so that a model that
kills the process while it loads is not asked for by every restart after it; a name that cannot
be removed with it is not used, and only a name `valid_model_name()` accepts counts, in its
canonical form), fetches its files through `download_model_files()` as a switch does, so that a
client's name never reaches `WhisperModel()` as a folder, and passes it to
`App(model_name=...)`. A model that does not load gives way to `--model`, and the failure is
reported as a failed switch is. Only `run.cmd`'s loop starts the server again, which it says
through `SHISUKO_LAUNCHER`: under a plain `python server.py` the switch is refused before its
download (`restart_blocker()`, whose `model_error` names `run.cmd`, the Start button and
`--model`), and unlike for `update_blocker()` `--no-update` does not matter, since `run.cmd`
restarts after code 3 all the same. A `next-model` that cannot be written is a failed switch that
keeps the old model. Everywhere else the switch stays in the process, exactly as above.

### The probe: server.py --probe-gpu

`--probe-gpu` (hidden from `--help`) is how `amd_setup.py` tests the engine, in a process of its
own, so that a card that aborts the process takes the probe down and nothing else. It runs before
the instance lock, like `--download-model`, with `SHISUKO_ENGINE=rocm` from its caller, and
refuses (exit 1, `ROCM_REASON` printed) when the engine is not active. `run_probe_gpu()` first
takes `"engine"` out of `config.json`, so that a probe that dies on the way leaves the default
engine; imports ctranslate2 and prints its version, its folder and the device count; fails for a
CTranslate2 that is not the side folder's and for 0 devices ("no AMD GPU visible to the ROCm
engine"); loads `--model` (the model chosen at setup, else large-v3) with device `cuda` through
`load_model(strict=True)`, which has no CPU fallback and raises on a failed warm-up; and requires
device `cuda`. Then `write_config({"engine": "rocm"})`, the crash guard deleted, and `AMD GPU
engine works: <the gfx target on Linux, else "device 0"> (<compute type>)`, exit 0. Every failure
prints `The AMD GPU engine does not work here: <reason>` and exits 1. Every exit is `hard_exit()`
after `flush_output()`, and a Ctrl+C or Ctrl+Break prints `The AMD GPU engine test was
cancelled` and ends with `hard_exit(1)`.

### What else changes with the engine on

`gpu_memory_mb()` asks amdgpu instead of nvidia-smi: on Linux `amd_vram_mb()` reads
`device/mem_info_vram_total` and `mem_info_vram_used` (bytes) of the AMD card (`device/vendor`
`0x1002`) with the most memory under `/sys/class/drm`, since an AMD processor's own graphics shows
up beside a card with a small carve-out of system memory; on Windows there is nothing to ask, so
`float16`, as on an NVIDIA machine without nvidia-smi. `load_model()`'s line says "on the AMD GPU
(ROCm)". `gpu_context_broken()`, the old `cuda`/`cudnn`/`cublas` test after a failed decode
widened, also knows `hipblas`, `rocblas`, `hiprand`, `hip error` at the start of a word,
`hsa_status` and `memory access fault`, in any case; the build mostly still says "CUDA failed",
and every message that broke the context before still does. `run_check()` adds one line
(`rocm_engine_line()`): `GPU engine: AMD ROCm (CTranslate2 <version> from <folder>)` in use,
`GPU engine: AMD ROCm installed but not used: <reason>` for a side folder it leaves off, and
nothing without one, which is every NVIDIA and CPU machine; with the engine on and 0 devices it
says that a start hands over to the default engine, in place of the CPU-fallback hint. A start
logs the same engine line. `--device` keeps its three choices.

### server/amd_setup.py

```
amd_setup.py            look for a card, ask, download, install, test (what setup runs)
amd_setup.py --yes      the same without the question
amd_setup.py --probe    test the installed engine again
amd_setup.py --status   what is detected, installed and switched on; no network
amd_setup.py --remove   switch the engine off and delete the side folder
```

Stdlib only, like `update.py` and `native_host.py`, and it never imports `server.py`: it must run
when the server's own requirements are broken, so it runs `server.py` only as the probe's child.
It runs with the venv's Python (a venv under `~/.shisu-ko` and another interpreter: it names the
venv's and stops, since the engine is built for one Python and the probe needs the venv's
packages). Exit codes: without `--yes` or `--probe` always 0, whatever happened, so the setups
can never fail on it. `--yes` exits 1 unless the engine ends up working (installed, tested and
switched on, or found so already): no AMD GPU, adapters that could not be listed, a platform
without a build, what Linux still lacks, the wrong Python, too little space, a failed download,
install or test, or a Ctrl+C. `--probe` exits 1 without an installed engine, under the wrong
Python, or when the test fails. `--status` and `--remove` exit 0. An unexpected error is one line,
`the AMD engine setup failed (...)`.

Detection. On Windows, Windows PowerShell by its full path under `%SystemRoot%` (not whatever
`powershell` the folder or `PATH` holds first) runs `Get-CimInstance Win32_VideoController |
Select-Object Name, PNPDeviceID | ConvertTo-Json -Compress` with a 20 s timeout; an adapter is AMD
when its PNPDeviceID holds `VEN_1002`, NVIDIA for `VEN_10DE`. A query that cannot start, takes too
long or fails without output is not "no AMD GPU" (`DetectError`): it says so, changes nothing
and names the command to look again later. An AMD card that Windows shows as "Microsoft Basic
Display Adapter" has lost its driver, and the line names Adrenalin 26.2.2 or newer. On Linux the
cards are `/sys/class/drm/card*/device/vendor`, and the GPU targets come from
`/sys/class/kfd/kfd/topology/nodes/*/properties` (`gfx_target_version`, major*10000 +
minor*100 + stepping, the last two in hex: 110000 is gfx1100, 90010 gfx90a; 0 is a processor's
node); an AMD card without a kfd target is an unknown one. An NVIDIA GPU is looked for on the PCI
bus (display controllers, class `0x03`, under `/sys/bus/pci/devices`), which lists a headless one
too, and not by nvidia-smi, which stays installed after the card is gone. Anywhere else nothing is
looked for, and nothing is said.

`classify()`: on Linux by the gfx target (the eight targets are supported, gfx1031/1032 are
unsupported with the override named, anything else unsupported); on Windows by the adapter's
name, the supported patterns first (RX 7xxx, RX 9xxx, PRO W7xxx/W9xxx, R9700, 890M/880M,
8060S/8050S/8040S), then the unsupported ones (RX 6xxx and 5xxx, PRO W6xxx and W5xxx, Vega,
780M/760M/740M, 680M/660M/610M, 860M/840M/820M and older Radeons), for which AMD's Windows runtime
has no kernels; any other name is unknown and offered with a warning. `best_card()` offers the
engine for the first supported card, else the first unknown one.

The flow (`setup()`), in this order. No AMD card: one line, and an engine that is switched on is
switched off (left on, every start would load it, see no AMD GPU and hand over; a `--probe` that
passes turns it on again). An installed engine that is switched on: on Linux what the system
still lacks, on Windows a card that lost its driver, a crash guard at its limit (`--yes` tests it
again), else "installed and switched on" (beside an NVIDIA GPU, with `--remove` named as the way
back to it) and, for one from older pins, that `--yes` updates it. An AMD card next to an NVIDIA
GPU: not offered without `--yes`, since the server uses the NVIDIA GPU. An unsupported card: one
line (for an RX 6600/6700 on Linux, how to try it with the override in the login shell's
profile), unless `--yes` installs it anyway. On Linux without `/dev/kfd`, access to it, or ROCm
7.2's libraries: what is missing, AMD's install guide and `sudo usermod -aG render,video "$USER"`.
The wrong Python: the command to run with the venv's. An engine from these pins that is switched
off: without `--yes` a line naming `--probe`, with it the test. Otherwise the offer: the card,
"This is experimental, not yet tested on AMD hardware by the maintainer.", a warning for an
unknown card, and what it takes (Windows: Adrenalin 26.2.2 or newer and a download of about
1.27 GB, CTranslate2 4.8.2 and AMD's ROCm 7.2.1 runtime, about 3.90 GB once installed and about
5.31 GB free while it installs; Linux: a download of about 284 MB), then `Download the AMD engine?
[y/N]`, or `Update the AMD engine? [y/N]` over an engine from older pins. Only y or yes is a yes;
an EOF, which an unattended `setup.sh` gives, is a no.

The install (`install()`). `space_shortfall()` checks the room before the first download: on the
data folder's disk the pinned files not downloaded yet, the wheel taken out of its zip (counted
as the zip, which is larger) and the installed size, which `PINS` has for Windows only (measured:
3,902,512,758 bytes); on the temporary folder's disk the installed size. Folders on one disk share
its room, where the largest need counts, and a disk whose free space cannot be read counts as
having room. Every pin is checked to be 64 hex characters before the first byte is fetched, so a
pin that was never filled in can never pass. Each file is downloaded with urllib into
`cache/rocm-download` through a `.part` file (a 60 s socket timeout, a line every 5 %, one more
try after a failed download); more bytes than pinned, a SHA-256 that does not match (the file is
deleted) and a full disk fail at once and are never tried again, and a file already there with
its pinned size and digest is used as it is. The wheel for this Python's tag and platform comes
out of the zip (a free-threaded Python takes the `cp314t` wheel, never the GIL build of the same
version; no wheel for this Python fails with its tag named). Before pip the room is checked again
for what the wheels unpack to, plus 1 %, on the side folder's disk and the temporary folder's,
since pip unpacks into the temporary folder first and a disk that fills halfway leaves only its
exit code. `pip install --no-deps --no-index --disable-pip-version-check --target rocm.new
<wheels>`, with the running Python, installs the files as they are and nothing else from
anywhere; a failed pip, or no `ctranslate2/__init__.py` afterwards, removes `rocm.new`. Then the
marker, `config.json`'s engine switched off (a new engine is untested until its probe passes),
`replace_dir()` (the old `rocm` moves to `rocm.old`, `rocm.new` takes its place, `rocm.old` is
removed last and moves back when the new folder cannot take the place), and the downloads
deleted. A failed install keeps the finished downloads for the next try and says which engine
the server keeps using.

The probe's child (`run_probe()`): `[sys.executable, server.py, --probe-gpu]` with
`SHISUKO_ENGINE=rocm`, `SHISUKO_HOME` passed on and `PYTHONUNBUFFERED=1`, its output shown as it
comes, for up to 900 s, waited for in one-second steps so that a Ctrl+C reaches `amd_setup.py`
on Windows, and killed at the deadline. A pass says "The server will use the AMD GPU from its
next start." and, on Linux, which variables of this shell every start needs too
(`HSA_OVERRIDE_GFX_VERSION`, or a `ROCM_PATH` without which the libraries are not found), with the
line for the login shell's profile (`~/.bash_profile`, `~/.zprofile` or `~/.profile`), since the
Start button starts the server without this shell's environment. A failure (a crash named by its
signal on Linux or its NTSTATUS on Windows) switches the engine off, since a child that crashed
could not, and says that the server keeps using the NVIDIA GPU or the CPU, and that `--probe`
tests again and `--remove` deletes the engine and frees its space; the side folder stays, so a
new test needs no download. A cancelled test switches the engine off too, unless the child
passed just before.

`--status` prints the Python and platform, each AMD card with its verdict, whether an NVIDIA GPU
is there, on Linux what is missing, the marker, whether the engine is switched on and by what,
the crash guard, and whether the next start uses the engine as the files have it (`server.py
--check` says what the server itself decides). `--remove` switches the engine off in
`config.json` first, so that no start reaches for a folder that is half gone, then deletes
`rocm`, `rocm.new`, `rocm.old`, `cache/rocm-download` and `rocm-starts`; a folder still in use (a
running server on Windows) is named with the advice to stop the server and run `--remove` again.
Every line that names a command (`rerun_line()`) gives the running interpreter bare where Command
Prompt and PowerShell both take it, else in PowerShell's `& "..."` form with a note for Command
Prompt, and in two forms for a path that holds `$` or a backtick.

The pins live in one place, `PINS` at the top of `amd_setup.py`: CTranslate2 4.8.2's
`rocm-python-wheels-Windows.zip` (137,538,158 bytes) and `rocm-python-wheels-Linux.zip`
(284,315,912 bytes) with the SHA-256 digests GitHub lists for the release assets, and AMD's
`rocm_sdk_core` and `rocm_sdk_libraries_custom` 7.2.1 wheels for Windows (644,793,492 and
489,964,648 bytes), whose digests the maintainer pinned from a download, since AMD publishes
none. Nothing else holds a pin. What the two files must agree on is written in both, since
`amd_setup.py` never imports `server.py`: `ROCM_GUARD_LIMIT` and `GUARD_LIMIT`,
`ROCM_LINUX_LIBRARIES` and `LINUX_LIBRARIES`, `ROCM_PLATFORMS` and `platform_key()`, the two
`python_tag()`, `gfx_target()` and `gfx_name()`, and the marker check of `rocm_engine_state()` and
`marker_usable()`; `test_amd_setup.py` and `test_rocm.py` hold each pair equal. New pins change
`test_pins_hold_the_published_and_the_maintainers_digests` and the sizes the offer announces; an
engine installed from older pins keeps running (the server asks only for its Python and
platform) until setup offers the update.

### When it fails, and the way back

- At setup: a failed download, install or test is a few lines that end in the engine the server
  keeps using and the commands to try again, and the setup goes on to "Setup is complete".
- At a start: the log's `GPU engine:` line says whether the engine is in use and why not. A
  build that does not load or sees no AMD GPU: one error line, the guard at its limit, exit 3,
  and the launcher's next start runs on the default engine. A crash: after the second start
  that did not get a model onto the GPU, the server leaves the engine off (`GPU engine: AMD ROCm
  installed but not used: the AMD engine did not get a model onto the AMD GPU at its last start
  ...`), and `--check` and `amd_setup.py --status` say the same.
- Back to the default engine: `amd_setup.py --remove` switches the engine off and deletes it
  (beside an NVIDIA GPU that is the way back to the NVIDIA GPU), and `SHISUKO_ENGINE=default`
  keeps it off for the starts that have the variable. `amd_setup.py --probe` tests a guarded or
  switched-off engine again and switches it on when it passes.

Known limitations: the engine runs on the first AMD GPU, HIP device 0 (`load_model()` passes no
device index), so on a machine with an AMD processor's graphics and an AMD card the model may
land on the integrated one; `HIP_VISIBLE_DEVICES` set to the card's index where every start sees
it (a user environment variable on Windows, the login shell's profile on Linux, and in the shell
that runs `--probe`) leaves only the card in sight, while `gpu_memory_mb()` reads the card with
the most memory either way. On Windows a model switch needs `run.cmd` or the Start button. Linux
needs a system ROCm 7.2.x, whose libraries the wheel is linked against. The installed size on
Linux is not measured, so its first space check counts the download and the wheel taken out of
it, and only the check before pip counts what it unpacks to.

### How it was tested

Nothing has run on AMD hardware yet. `server/tests/test_rocm.py` covers `server.py`'s side and
`server/tests/test_amd_setup.py` `amd_setup.py`, with stand-ins for everything that would reach a
GPU, the network, PowerShell, pip or the real data folder (`build-and-test.md`, "Server tests");
they run in CI on Linux and Windows without a GPU. `test_setup_model.py` and
`scripts/launcher-smoke.sh` hold where the setups call `amd_setup.py`, and that one that fails
does not stop `setup.sh`. On the maintainer's Windows PC (an NVIDIA RTX 4070 Laptop GPU, no AMD
GPU) the real Windows engine was installed from the pinned files into a scratch data folder
(through `install()` directly, since setup finds no AMD GPU there; pip took about 30 s, and the
side folder held 2,433 files and 3.90 GB; its longest path is 163 characters inside the data
folder, which leaves room under Windows' 260).
`--check` named the engine and saw 0 devices where the venv's build sees the NVIDIA GPU, so the
ROCm build was the one imported, with `CT2_CUDA_ALLOCATOR=cub_caching` set; `--probe` failed
cleanly with "no AMD GPU visible to the ROCm engine" and left `config.json` without the engine.
Server starts on it ran `tiny` on the processor, since the build saw no GPU, and ended on Ctrl+C
and Ctrl+Break without hanging; such a start is what the hand-over of `check_rocm_import()` now
prevents. `--remove` then deleted the folder and the guard. The exit hang above was reproduced
on the same PC, and the passing probe was simulated (a device count of 1, `tiny` on the CPU
reported as `cuda`): as first written it hung after `AMD GPU engine works`, and with
`KEPT_MODELS` it ended through `hard_exit(0)` after about 10 s.

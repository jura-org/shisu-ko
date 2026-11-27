# The Shisu-ko server

How to start the server with options, give it your YouTube sign-in, run it on an Apple Silicon
Mac, and use the Kitsune-Transcribe models. Installing the server is in
[install.md](install.md); AMD graphics cards have [a page of their own](amd.md).

- [Server options](#server-options)
- [YouTube sign-in](#youtube-sign-in)
- [Apple Silicon](#apple-silicon)
- [Kitsune-Transcribe models](#kitsune-transcribe-models)

## Server options

Append options to `run.cmd` / `run.sh` (with Nix, after `--`:
`nix run github:Multysquid/shisu-ko -- --device cpu`, or `nix run . -- --device cpu` in a
checkout), or put them in the `command:` line of `compose.yaml`. The popup's **Start server**
button runs `run.cmd` / `run.sh` with the options in the popup's **Start server options** field
(empty by default, so it starts the defaults below; the model can still be switched from the
popup afterwards), and the **?** beside the field lists every option it takes, with the values
each allows, its default and what it does. That field takes the options of the table below
except `--model` (use the popup's model field), `--port`, `--cookies-from-browser`,
`--save-cookies-from-browser`, `--cookies`, `--anki-url`, `--initial-prompt`, `--js-runtime`,
`--allow-remote-ejs`, `--check`, `--default-model` and `--download-model`, and `--language`,
`--host` and `--setup-cookies` from
`server.py --help`: those take an address, a path, a program, a browser's cookies, code from the
network or free text, or exit instead of serving, and the launcher refuses them from the
browser. Each value must be a number in a sensible range or one of the option's words, as in
the table; the line under the field says what the button will pass. A server that needs one of
the others is started by hand. Without `--model` the server runs the model chosen at setup
(`~/.shisu-ko/config.json`), else large-v3, or large-v3-turbo on an Apple GPU;
`bash server/run.sh --default-model` prints which of the two this machine is. Without
`--cookies-from-browser` or `--cookies` it
sends the YouTube cookies of the browser chosen there (see [YouTube sign-in](#youtube-sign-in)).

| Option | Effect |
|---|---|
| `--model kotoba-tech/kotoba-whisper-v2.0-faster` | Default model (here the Japanese-specialised distilled one, about 6x faster and lighter on memory than large-v3). The popup overrides the default with any faster-whisper size or Hugging Face repo id, without a restart (with the AMD engine on Windows, through one) |
| `--model large-v3-turbo` | OpenAI's faster large model as the default; already the default on an Apple GPU |
| `--model small --device cpu` | CPU-only operation |
| `--download-model small` | Download the model now, with a progress bar, and make it the default of later starts (what setup runs after its environment check); exits instead of starting the server, with code 2 on a failure or Ctrl+C, which `run.cmd` / `run.sh` do not restart on |
| `--device mlx` | Decode on an Apple Silicon GPU through MLX, about twice the CPU speed at the same beam size; the default model there is large-v3-turbo. `auto` already picks it when there is no NVIDIA GPU and mlx-whisper is installed |
| `--beam-size 1` | Decode greedily instead of searching five beams. About 15% faster on read news speech and about 90% on dense conversation, and wrong on a homophone rather more often |
| `--compute-type int8_float16` | Halves GPU memory use; chosen by itself when less than 4.5 GB is free. MLX knows `float16` (the default there) and `float32` only, so on an Apple GPU the smaller default model is what keeps memory down |
| `--default-model` | Print the model a bare start would load on this machine, ignoring `~/.shisu-ko/config.json`, and exit. What `setup.sh` asks to name its first choice |
| `--cookies-from-browser firefox` | Send that browser's YouTube cookies for this start: when YouTube asks for a sign-in, and for age-restricted or members-only videos. `none` sends no browser's, whatever setup chose |
| `--save-cookies-from-browser firefox` | Make that browser's YouTube cookies the default of every later start, the popup's **Start server** button included, after checking that they can be read; `none` forgets it. Exits instead of starting the server (see [YouTube sign-in](#youtube-sign-in)) |
| `--cookies /path/cookies.txt` | Same as `--cookies-from-browser`, with an exported cookies file (use this inside Docker) |
| `--lookahead 0` | Transcribe to the end of the video instead of stopping ahead of you (default 180 s on an NVIDIA GPU, 900 s on an Apple GPU or the CPU) |
| `--window 60` | Longer windows are slightly more efficient, shorter ones react faster to seeking (default 30, faster-whisper's own chunk: a longer window decodes its tail without the initial prompt) |
| `--max-cue-chars 26` | Characters per cue before it is split (default 30; 26 is the Netflix Japanese limit) |
| `--max-cue-seconds 7` / `--min-cue-seconds 0.8` | Longest and shortest cue (defaults 7, Netflix's own maximum, and 0.8); shorter ones are extended or merged |
| `--initial-prompt ""` | Turn off the prompt that asks Whisper for punctuation. Japanese gets one by default (`はい、そうですね。今日はよろしくお願いします。それで、どう思いますか？`), because a window is decoded with nothing in front of it and an unprompted decode writes a sentence mark at about half of the sentence ends; any other text replaces it, and a language other than Japanese has none. The prompt now and then makes Whisper skip the first seconds of speech in a window; such a window is decoded once more without it and the skipped lines filled in |
| `--language-patience 60` | Seconds of speech in another language before a video's subtitles stop. It governs YouTube's declaration as well: 0 means neither the declaration nor the speech is checked and everything is transcribed |
| `--lyrics off` | Transcribe a window in which the speech detector hears under a second of speech with the detector as before (blank when it heard nothing). The default `auto` transcribes such a window without the detector when its audio is not silent (sung lyrics, speech over music) and Whisper hears the target language in it, under stricter gates |
| `--human-subtitles off` | Build every cue from Whisper alone and ask YouTube for no subtitle track. The default `auto` uses a Japanese track the video's creator uploaded: a line Whisper heard the same way takes the creator's text and line breaks with Whisper's timing, any other stretch stays Whisper's. Cues already cached stay as they are |
| `--sentence-ends off` | Cut and merge lines on Whisper's own punctuation alone. The default `auto` writes the sentence mark Whisper left out where a word ending in a sentence-final expression (よね, です, ます, か, or a plain form) is followed by a pause, so a run-on line breaks where the speaker ended the sentence and a mined card gets that sentence and no more |
| `--anki-sync off` | Never sync Anki. The default `auto` watches Anki through AnkiConnect and syncs it with AnkiWeb a minute after new notes appear, then every 30 s while they keep coming (see [Sentence mining](guide.md#sentence-mining)); `/health` reports how the last sync went. Off by default inside the Docker image, where 127.0.0.1 is the container, unless `--anki-url` is given |
| `--anki-url http://127.0.0.1:8765` | AnkiConnect's address for that sync, for mining and for the word colours' deck (the default shown) |
| `--mine off` | Never write to Anki. The default `auto` gives a card Yomitan makes from a subtitle line that line's audio and picture, in the fields it left empty (and grows a sentence Yomitan cut short to the whole line), and takes the extension's Alt+Shift+M (see [Sentence mining](guide.md#sentence-mining)). Off by default inside the Docker image unless `--anki-url` is given |
| `--mine-picture off` | A mined card gets the audio alone. The default `on` adds a frame of the video at the line, from YouTube's stream (one yt-dlp resolve per video, kept 4 hours) |
| `--idle-minutes 30` | Release the decoded audio of a video nobody has synced for this long |
| `--model-idle-minutes 1` | The default: the server starts without its model, loads it when a video needs transcribing (beside the audio download, so a new video waits little or no longer), and frees it again after this many minutes without anything to transcribe. A video you are watching keeps it, paused or not, until it is transcribed to its end or you close its tab. Meanwhile it shows the subtitles it has already made, from its cache, and holds no model in GPU memory (until the first load none at all; after it, about 0.1 GB that the CUDA runtime keeps until the server ends); large-v3 holds about 4 GB and loads in 3 to 7 s on a laptop RTX 4070. A model switch loads the new model at once, which then waits out the idle minutes like any other. `0` loads the model at the start and keeps it, as before 0.28.0. The AMD engine always keeps its model |
| `--retry-after 30` | The longest wait between two attempts at a failed audio fetch (the waits start at 2 s and double; a private or removed video is not retried), and the wait before a model name that failed to download or load is tried again |
| `--js-runtime deno` | JavaScript runtime for yt-dlp: auto, node, deno, bun, or name:path |
| `--allow-remote-ejs` | Lets yt-dlp fetch its YouTube challenge-solver script from GitHub when the `yt-dlp-ejs` package is missing or does not match, in the version the installed yt-dlp expects. It is no substitute for updating yt-dlp |
| `--port 8791` | Listen on another port (default 8790; leave 8765 to AnkiConnect), and set the popup's **Shisu-ko server URL** to match. The **Start server** button always starts a server on 8790 |
| `--cpu-threads 8` | Threads for decoding on the CPU (default 0: the library's own choice) |
| `--log-level DEBUG` | Log more, for example why the server left a new Anki card alone (default `INFO`) |
| `--check` | Print environment diagnostics (CUDA, the AMD engine where it is installed, MLX and the backend `--device auto` would pick, yt-dlp's JavaScript runtime, downloaded models, the model a bare start would run, whether the popup's Start button has its launcher registered) and exit |
| `--update-check on\|off` | `on` (default, Docker and Nix included): ask GitHub once a day, anonymously, for the newest release and report it in `/health` for the popup's badge and banner; nothing is downloaded. `off`: never (so does `SHISUKO_NO_UPDATE_CHECK=1`) |
| `--no-update` | Start without looking for a newer version of Shisu-ko first (`run.cmd` / `run.sh`). The popup's **Update** button is refused too, since the launcher would restart the server without updating |

`run.cmd` / `run.sh` set `SHISUKO_LAUNCHER=1` for the server they start. Only with it does
`POST /update` (the popup's **Update** button) answer yes: the server then exits with code 4,
which the launchers read as "run `update.py`, then start again" (0 stops the loop, 2 is a
startup error, anything else restarts after 5 s). Under Docker, Nix or a `python server.py` by
hand the variable is missing, `/health` reports `launcher: false` and `/update` answers 409;
those servers are updated the way they were started.

Endpoints, for anyone building on the server: `GET /health` (`model`, `default_model`,
`model_loading`, `model_error` as `{model, error, names}` or null, where `names` lists every
spelling of the failed model (aliases and repo id), `model_loaded`, false while the model is out
of memory (`--model-idle-minutes`), `models` with the models downloaded so far, device, compute
type (null until a model has loaded), language), `POST /sync` (`{video_id, url, t, paused, since, model}`
returns new cues and covered ranges plus `model` (the one in use, loaded or to load), `model_loading` (the name being
prepared, swapped in or loaded for a video) and `model_error`, the last judged for the name this request asked for
and null for any other; a model switch answers with a new session token, so the client starts
over; for a live stream `t` is the stream's media clock, `getProgressState().current` in
YouTube's player, and the reply carries `live: true`), `GET /clip?video_id=&start=&end=&format=mp3|wav`,
`GET /sessions` for debugging, and `POST /update` (body ignored), which answers
`{ok: true, restarting: true, version}` and then exits with code 4 for the launcher, or 409
`{ok: false, error}` with the reason when nothing would update it (no launcher, `--no-update`,
`SHISUKO_NO_UPDATE`); `/health` carries `version`, `launcher`, true when `/update` would
work, and `latest`, the server's update check (`{version, url, xpi, published, checked, error}`,
or null when the check is off or has not run yet). The server only listens on 127.0.0.1 and answers browser requests
only from browser extensions (Shisu-ko and any other installed one) or from pages served on this
machine, so an arbitrary website cannot drive downloads and transcription; `/update` is narrower
still and takes browser requests from extensions alone, never from a page, so nothing served on
this machine can restart the server. A model name is validated (a Kitsune name, a size alias or
`owner/name`, never a path) and resolved through the server's Kitsune table or faster-whisper's
own download before anything is loaded, so a request can never point the server at a local folder.

## YouTube sign-in

YouTube answers some addresses with "Sign in to confirm you're not a bot" and refuses to let
yt-dlp download anything until the request carries a signed-in browser's cookies. The server can
send them: setup asks once, where Firefox is installed, and
`server\run.cmd --save-cookies-from-browser firefox` (`bash server/run.sh ...` on Linux/macOS;
`nix run github:Multysquid/shisu-ko -- --save-cookies-from-browser firefox` with Nix, which has
no setup, or `nix run . -- ...` in a checkout) sets it at any time. That reads Firefox's cookie
store once to check that it holds YouTube cookies (only their names are looked at), writes
`"cookies_from_browser": "firefox"` to `~/.shisu-ko/config.json` and exits; from the next start
on, every download sends Firefox's YouTube cookies, whether the server was started by hand or
with the popup's **Start server** button. `--save-cookies-from-browser none` forgets it, and
`--cookies-from-browser none` leaves it out for one start. `run.cmd --check`
(`nix run github:Multysquid/shisu-ko#check` with Nix) says which browser is used.

Firefox is the browser to use on Windows: Chrome and Edge keep their cookies from other programs
there, so yt-dlp finds none and the command refuses them. The cookies are those of whatever
account is signed in to YouTube in that browser. yt-dlp does not keep Firefox's contexts apart,
though: the YouTube cookies of every Multi-Account Container, and those that YouTube players
embedded in other sites keep, go into one set, so an account signed in in a container can take
the place of the default one or mix with it. yt-dlp reads the browser's whole cookie store, every
site's cookies, for each download, through a copy of the store's file that it writes into the
system's temp folder and deletes after reading, and sends a site only its own: YouTube's to
YouTube, and GitHub's to GitHub when `--allow-remote-ejs` fetches the solver script there.
yt-dlp's authors warn that an account used this way may be rate-limited or flagged, so a
secondary account is the careful choice. The Docker image never takes the browser from
`config.json` (it has none to read): Docker users export a `cookies.txt` into the data folder and
add `--cookies /data/cookies.txt` to the `command:` line. Toolbox and distrobox share your home
folder and its Firefox, so there the saved browser is used as on the host.

## Apple Silicon

**On Apple Silicon** the transcription runs on the Mac's own GPU, with no option to set:
CTranslate2 has no Metal backend, so the server decodes through MLX instead and `--device auto`
picks it as soon as `setup.sh` has installed mlx-whisper. Model names do not change — `large-v3`
in the popup is the same weights either way, setup downloads the MLX build of whichever model you
pick, and both backends share one cue cache. On an idle M1 Pro, over three minutes of Japanese news
audio, large-v3 transcribed at 5.0x realtime on the GPU against 2.3x on the CPU at the same beam
size, and left the cores to the video.

A Mac's default model is large-v3-turbo, not large-v3, because the GPU's memory is the browser's
memory. large-v3 is 2.9 GB of weights. With Chrome playing one YouTube video beside it, an M1 Pro
took 81 seconds over a 38-second window — half the speed you are watching at, which is no use to
anyone; the same window took 13 seconds with the browser closed. Turbo is 1.5 GB and takes 4.8
seconds either way. It costs about one word in twenty lines. Type `large-v3` into the popup's model
field, or pick it at setup, if you want it anyway.

mlx-whisper itself has no beam search; Shisu-ko adds one, so the GPU searches five beams as the
CPU does. It is not quite the CPU's transcript: of the three words greedy decoding got wrong in
those three minutes of news, the beam fixes two and still misses one homophone (線上降水帯 for
線状降水帯). What it costs depends on the speech: about 15% on read news, about 90% on dense
conversation, where one window holds three times as many sentences to decode. `--beam-size 1`
takes that back and mishears a homophone rather more often.

## Kitsune-Transcribe models

[Kitsune-Transcribe](https://github.com/Multysquid/Kitsune-Transcribe) distils Cohere Transcribe
into small Japanese-only speech recognisers that run on an 8 GB consumer GPU, or on a CPU. The
server runs them next to Whisper; pick one at setup or type its name in the popup's
Transcription model field.

| Name | What it is |
|---|---|
| `kitsune-0.6b`, `kitsune-0.3b`, `kitsune-0.1b` | the student of that size, in bf16 (about 1.2 GB, 600 MB, 200 MB) |
| `kitsune-0.6b-fp16` | the same weights in fp16 |
| `kitsune-0.6b-int8` (also `-int8-w8a16`, `-int8-w8a8`) | 8-bit integer weights, about half the download |
| `kitsune-0.6b-fp8` (also `-fp8-w8a8`) | 8-bit float weights |
| `kitsune-0.6b-nvfp4` (also `-nvfp4-w4a16`, `-nvfp4-w4a4`) | 4-bit NVFP4 weights, about a third of the download |
| `kitsune-0.6b-mxfp4` (also `-mxfp4-w4a4`) | 4-bit MXFP4 weights |

Every precision works for every size, on an NVIDIA GPU and on the CPU (an AMD or Apple GPU is not
used: there the model runs on the CPU, the AMD engine being Whisper's). The server unpacks the weights
into 16 bits when it loads them (bf16 on a GPU that has it, else fp16; fp32 on the CPU), so a
smaller precision saves download and disk, not GPU memory or time, and the W8A8/W4A4 formats run
with 16-bit activations. Two spellings that differ only in the activations (`-int8-w8a16` and
`-int8-w8a8`) load the same files and count as one model.

What to know:

- **PyTorch.** Kitsune models are not Whisper models and do not run on CTranslate2; the server
  runs them on PyTorch and transformers, with librosa. Setup installs them when you pick a Kitsune
  model (`server/kitsune_setup.py`: PyTorch's CUDA build where it finds an NVIDIA GPU, about 3 GB,
  else the CPU build). To add them later, run
  `%USERPROFILE%\.shisu-ko\venv\Scripts\python server\kitsune_setup.py` in the Shisu-ko folder
  in the Windows Command Prompt (in PowerShell, start it with `~\.shisu-ko\venv\Scripts\python`;
  on Linux/macOS, `~/.shisu-ko/venv/bin/python server/kitsune_setup.py`); `--status` says what is
  installed. Without them the popup says so when you pick a Kitsune
  model, and nothing is downloaded. `run.cmd --check` reports it too. The Docker image includes
  PyTorch; the Nix package does not.
- **Japanese only.** A Kitsune model refuses to start with another `--language`. It has no
  language detector, so the language watch (`--language-patience`) does not pause a video in
  another language, and it has no lyrics mode (`--lyrics`): music without detected speech stays
  blank. `--initial-prompt`, `--beam-size` and the other decoding options are Whisper's; a
  Kitsune model decodes greedily, as it was evaluated.
- **Word timings.** The cue builder needs every word's start and end. A Parakeet-family student
  (a CTC model) reads them off its 80 ms frames; a Transcribe-family student gets them from its
  decoder's attention, aligned as Whisper aligns its own word timestamps.
- **Licence.** The Kitsune students are released for non-commercial use (their training data
  requires it); see the model card in each model's repo.

`--compute-type` takes `bfloat16`, `float16` or `float32` for a Kitsune model. The models live in
the repos `Multy123/kitsune-transcribe-t0.6b-ja` (kitsune-0.6b), `-p0.3b-ja` (kitsune-0.3b) and
`-p0.1b-ja` (kitsune-0.1b), the bf16 export at the root and each precision in a folder of its
own. `--model` and the popup also take such a repo id, and `--model` a local folder holding such
a package.

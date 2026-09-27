# AGENTS.md

Guidance for AI coding agents (and new contributors) working in this repository.
Read this before changing code; the README is the user-facing document.
The detail behind every section lives in `docs/dev/` (index: `docs/dev/README.md`).

## What this project is

Shisu-ko shows live Japanese subtitles on YouTube in Firefox and Chrome. A local Python server transcribes
the video's audio with Whisper (faster-whisper / CTranslate2) a little ahead of the playhead; the
extension renders the cues as real DOM text so Yomitan can scan them, and can mine a screenshot
plus sentence audio into the newest Anki card via AnkiConnect.

```
addon/        Firefox source extension, Manifest V3, plain JS; directly loadable without a build
              (match.js and words.js are shared by background.js and content.js; loaded before
              both, in that order)
server/       server.py (single file) + setup/run scripts + update.py; runtime data in ~/.shisu-ko
              native_host.py: the native-messaging host behind the popup's "Start server" button
              (stdlib only); Firefox and Chrome run it through native-host.cmd / native-host.sh
docker/       Windows wrappers for docker compose, WSL Docker Engine installer
docs/dev/     developer docs: the full design of each subsystem, its reasons and measurements
docs/cws/     Chrome Web Store setup: the service account and publisher id cws-listing.yml needs
Dockerfile, compose.yaml, compose.cpu.yaml, .env.example
flake.nix        Nix package/app/dev shell for the server and the extension build
sign-addon.cmd   signs a local build through addons.mozilla.org, unlisted (manual fallback, owner's API key)
publish-addon.cmd  submits a release to the public AMO listing by hand, the fallback for
                 amo-listing.yml (see "Release"); docs/amo/ holds the listing text and assets
```

## Invariants (do not break these)

Full text and reasons: `docs/dev/invariants-and-gotchas.md`.

- Subtitles stay ordinary DOM text (`textContent`): never canvas, `<track>` cues or shadow DOM, since
  Yomitan depends on it. The one markup allowed in a cue's text is `<span class="shisuko-word">`
  holding a text node, with `data-status` and `data-pitch` only; `renderText()` in `content.js` is
  its one writer.
- Never use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval` or `script.src` in the content
  script. youtube.com enforces Trusted Types.
- Settings are untrusted input before they reach CSS: every style value goes through a sanitiser in
  `applyStyleSettings()` (`clampNumber`, `oneOf`, `hexColor`, `fontStack` with `FONT_FAMILY_RE`). No
  quote, semicolon or `url(` reaches the stylesheet. `popup.js` keeps copies of `FONT_FAMILY_RE`,
  the preset stacks and `MODEL_NAME_RE`; `addon/tests/popup-copies.test.js` keeps them equal.
- Application code uses the `browser.*` promise API; only `browser-api.js`, the Chrome adapter,
  calls `chrome.*`.
- All overlay classes and flags use the `shisuko-` / `__shisuko` prefix.
- Settings defaults live once in `addon/settings.js` (`SHISUKO_DEFAULT_SETTINGS`). A new setting goes
  there plus a popup input with the same id; `addon/tests/settings.test.js` enforces both.
- The server listens on `127.0.0.1:8790`. Port 8765 belongs to AnkiConnect; never use it.
- The server never exposes anything beyond `/health`, `/sync`, `/clip`, `/sessions` and
  `POST /update`. It validates `video_id` against `^[A-Za-z0-9_-]{6,20}$` and answers only the
  extension's origin or loopback pages (`origin_allowed()`). `/update` is narrower
  (`update_origin_allowed()`: the extension or no `Origin` at all) and answers 409 unless the server
  was started by `run.cmd` / `run.sh` (`SHISUKO_LAUNCHER=1`) without `--no-update` or
  `SHISUKO_NO_UPDATE` (`App.update_blocker()`); else it exits with `EXIT_UPDATE` (4). The server
  never spawns `update.py` and never downloads a release itself; the launcher does.
- The add-on never installs itself: no `update_url`, no `.xpi` download or install
  (`addon/tests/settings.test.js`); its updates come from addons.mozilla.org. Its one remote
  request is the anonymous `GET` of GitHub's `releases/latest` in `fetchLatestRelease()`, never with
  a token, cookie or identifier. `notifications` stays a required permission. `docs/amo/` states
  exactly this, so a change here changes them too. On Chrome, the package carries no `key` or
  `update_url` (`scripts/tests/build.test.mjs`), a Web Store install is updated by the store
  (`CHROME_STORE_ID` in `popup.js`) and an unpacked build (the release's zip, `dist/chrome`) only
  by the viewer loading a newer one; this has no counterpart in `docs/amo/`, which is the AMO
  listing's text.
- The native host (`server/native_host.py`, name `shisuko`) answers only `status` and `start`. It
  never takes a path, program or argument from a message; it runs only the checkout's own
  `server/run.cmd` / `server/run.sh` and registers only under the user's own profile, for Firefox
  (`allowed_extensions`: the gecko id) and Chrome, plus Chromium on Linux (`allowed_origins`: the
  Chrome Web Store id `CHROME_EXTENSION_ID` alone, never a wildcard or an unpacked build's id).
  The popup offers the button on Firefox and under that store id (`START_AVAILABLE`,
  `CHROME_STORE_ID` in `popup.js`; `test_native_host.py` keeps the two ids equal), never to an
  unpacked build. Other Chromium browsers holding the store install read host folders of their
  own and are deliberately left out.
  `nativeMessaging` stays in `optional_permissions`, requested by the popup's click handler before
  its first `await` and by nothing else.
- A client's model name (`model` in `/sync`) must match `MODEL_NAME_RE` and contain no `..`, else it
  gets `MODEL_NAME_HINT` and is never stored. A valid name is reduced by `canonical_model_name()`
  and resolved through `faster_whisper.download_model()`. A raw client string must never reach
  `WhisperModel()`; only the operator's `--model` may be a folder.
- `enabled` is the master switch (popup header toggle, Alt+Shift+S). Off means nothing happens on
  YouTube pages: no `/sync`, no overlay, no native-caption hiding, no arrow keys, no Anki polling,
  no mining, no known word marked, no status badge switched, no `cardStatus` ask
  (`wordColoursOn()` includes `enabled`). Only the toggle command keeps working.
- Overlay handlers (`onMineClick`, `onTranscriptClick`, `onSubtitleEnter`, `onSubtitleLeave`, the
  transcript's close button) act only on `ev.isTrusted` events. Server text reaches Anki's HTML
  fields only through `escapeHtml()` in `background.js`; the overlay shows it through `textContent`.
- Live streams run on the stream's media clock (`getProgressState().current`), never on
  `video.currentTime`. Every read or seek of the playhead goes through `playhead()` /
  `seekPlayhead()`.
- Runtime data lives in `~/.shisu-ko` (`SHISUKO_HOME` overrides it): `venv/`, `models/`, `cache/`,
  `config.json` (`write_config()`, `read_config()`, `resolve_default_model()`,
  `resolve_default_cookies()`: `model` and `cookies_from_browser`, the browser whose YouTube
  cookies every download sends; the Docker image never takes the latter, `in_container()` is
  `SHISUKO_CONTAINER` only, since toolbox and distrobox share the home folder's Firefox),
  `server-<port>.lock`, `server.log` and, on Windows, `native-messaging/shisuko.json` (Firefox's
  host manifest) and `native-messaging/shisuko-chrome.json` (Chrome's). Detail:
  `docs/dev/cue-building.md`, "Runtime data and the cue cache".
- The cue cache is forever. `CACHE_FORMAT` (6) is the only migration: `load_cache()` drops every
  older record whole before reading anything out of it, so a geometry change bumps it. Cue caches
  are reused only when model (compared canonically) and language match. `covered` always means
  "Whisper has seen this"; what a language-paused session only listens to goes into
  `Session.probed`, which is never written to `covered` or the cache.
- No absolute personal paths, no secrets and no `.env` in tracked files. `.env.example` documents it.
- Line endings: LF everywhere, CRLF only for `*.cmd` (`.gitattributes` enforces this).

## Subsystem map

**Cue building** (`server/server.py`; read `docs/dev/cue-building.md` first). `plan_window()` picks
the next window; `process()` runs `detect_speech()`, the language watch, `wants_lyrics()`,
Whisper, `retry_prompt_skips()` and `build_window_cues()`. Inside it `repair_lead_words()`
runs first, then `punctuate_words()`, the gates (`hallucination_reason()`, or `lyrics_reason()` on
a lyrics window), `build_cues()` and `merge_segments()`. Rules that must not regress:
- `repair_lead_words()` only ever slides a head **forward**.
- `merge_segments()` runs **before** `normalise_gaps()`.
- The merges ask `breaks_word()`, never `may_break()`: wired to `may_break()`, the merge forced a
  41-character, 8.1-second cue.
- A sentence-mark seam never gives way (`ends_sentence()`), in `merge_adjacent()` and
  `merge_segments()`; a merge that cannot give both a row is refused.
- `server/tools/cue_stats.py`, `retranscribe.py`, `dump_words.py` and `replay_cues.py` measure a
  change; keep them working (`dump_words.py` decides every window as `process()` does).

**Server runtime** (`docs/dev/server-runtime.md`). Live streams: `Fetcher.follow_live()`,
`LiveFollower`, `DashLiveSource`, `Session.live_audio`; live sessions are never cached. Model
switching: `App.request_model()` and `App.switch_model_if_wanted()`, run before every window,
never during one. The Start button: `startServer()` in `background.js`, the native host's
`handle()` and `launch()`, the instance lock `hold_instance_lock()` / `try_lock()`. The update
step: `server/update.py`, `POST /update`, exit code 4. Launcher rules that must not regress:
- `run.cmd`: the update call and `goto loop` stay on one line, `:loop` keeps its name, `:update`
  sits directly above that line, and `set "SHISUKO_LAUNCHER=1"` sits directly after `:loop`.
- `run.sh`: everything stays in `main()`, the file ends with `main "$@"; exit`, and
  `export SHISUKO_LAUNCHER=1` sits inside `main()` before the loop.
- `update.py` always exits 0: the server must start even when the update fails.

**Mining** (`docs/dev/mining.md`). `electSyncTab()` in `background.js` picks the one tab whose
`/sync` reaches the server; it never names a tab that did not ask. `ankiPoll()` watches for the note
Yomitan just created; `addToAnki()` refuses a note whose sentence does not match (`MIN_SIMILARITY`).
A mined card gets exactly the cue on screen: `sentenceForCue(cue)` in `content.js` is the one
place that decides it, and cues are never rejoined by `seg`. `SHISUKO_MATCH` (`addon/match.js`)
scores cards against cues; `premined` in `background.js` holds frames and clips captured while a
line played.

**Word colours** (`docs/dev/word-colours.md`). `addon/words.js` (`SHISUKO_WORDS`, pure) parses
fields and marks words (`buildIndex()`, `markWords()`); `cardStatus()` / `fetchDeckIndex()` in
`background.js` build the deck's entries; `pollWordIndex()`, `renderText()` and
`refreshWordMarks()` in `content.js` draw them. A particle never takes the colour of the word
before it: 領域まで reads `[領域]まで`. That is the owner's ruling; never re-add the rule that
coloured particles with the word before them.

**Updates and release** (`docs/dev/updates-and-release.md`). `checkForUpdate()`, `decideUpdate()`
and `requestUpdate()` in `background.js`; `renderUpdate()` in `popup.js`; the workflows under
`.github/workflows/`, the Chrome Web Store's `cws-listing.yml` and `cws-schedule.yml` among them,
and `scripts/cws.mjs`.

**Build and tests** (`docs/dev/build-and-test.md`). `scripts/build.mjs` derives Chrome from the
Firefox source; `_serverlib.py`, `_loadBackground.js` and `_loadContent.js` load the code under test.

## Commands

Nix (any Linux with flakes, NixOS): `nix run . -- [options]` starts the server with CUDA
(`flake.nix`; CTranslate2 comes prebuilt from `cache.nixos-cuda.org`, onnxruntime is the CPU build
because only the VAD uses it). `nix run .#check`, `nix run .#tests`, `nix build .#addon`,
`nix develop` for a shell with Python, web-ext, Node and Deno. `.#server-cpu` is the CUDA-free variant.
Native server (Windows): `server\setup.cmd` once (it asks for large-v3 or small and downloads it),
then `server\run.cmd [options]`.
Native server (Linux/macOS): `bash server/setup.sh`, then `server/run.sh`.
Diagnostics: `server\run.cmd --check` (also says whether the Start button's launcher is registered
and which model a bare start runs).
Music videos: `--lyrics auto` (default) or `--lyrics off`; see `docs/dev/cue-building.md`.
Model download with a progress bar: `server.py --download-model NAME`; see
`docs/dev/server-runtime.md`.
YouTube's sign-in: `run.cmd --save-cookies-from-browser firefox` (or `none`; on Nix
`nix run . -- --save-cookies-from-browser firefox`) remembers the browser whose cookies every
start sends; setup asks through `server.py --setup-cookies`.
Start-button launcher, with the venv's Python (`run.cmd` / `setup.cmd` and their `.sh` twins do
this themselves): `~/.shisu-ko/venv/Scripts/python server/native_host.py --register --verbose`
(`venv/bin/python` on Linux/macOS), `--status`, `--unregister`.

Docker: `docker\up.cmd`, `docker\logs.cmd`, `docker\down.cmd` (or `docker compose up -d` etc.).
`up.cmd` keeps a minimized "Shisu-ko WSL keep-alive" window open when Docker Engine runs inside
WSL, because WSL stops the distro (and Docker) seconds after the last WSL session ends.

Extension checks and browser packages:

```
for file in addon/*.js; do node --check "$file"; done
npx web-ext lint --source-dir addon
npx web-ext build --source-dir addon --artifacts-dir dist --overwrite-dest --ignore-files "tests/**"

npm ci
npm test
npm run build
npx playwright install --with-deps chromium
npm run test:browser
```

`addon/manifest.json` is the Firefox source and remains directly loadable from `about:debugging`.
`node scripts/build.mjs` derives Chrome from it into `dist/chrome`; it never maintains a second
application copy. Chrome keeps only the first four suggested shortcuts, so a new command goes last.

Server check: `python -W error -c "import ast; ast.parse(open('server/server.py', encoding='utf-8').read())"`.

Automated tests (also run in CI via `.github/workflows/tests.yml`, no GPU/network/Firefox needed):

```
pip install -r server/requirements-test.txt && python -m pytest server/tests
node --test addon/tests/*.test.js
```

Test `server.py`'s pure functions by importing the module; register it in `sys.modules` before
`exec_module` because of `from __future__ import annotations` (`server/tests/_serverlib.py` does).
When adding a new setting or a new pure helper, add a matching test rather than only exercising
it manually.

Load the extension for manual testing via `about:debugging#/runtime/this-firefox` > Load Temporary
Add-on > `addon/manifest.json`. The content script can also be exercised outside Firefox by
concatenating a small `browser.*` shim with `content.css` and `content.js` and running it on a page
that contains `#movie_player.html5-video-player > video` with `?v=<video id>` in the URL.

## Gotchas learned the hard way

Full text: `docs/dev/invariants-and-gotchas.md`.

- AMO checks the listing texts only when a version is submitted to the listing; release notes and
  reviewer notes over 3,000 characters each are refused then, as 0.14.0 was.
- Chrome Web Store: API v2 only (v1.1 stops on 2026-10-15); a store version must be higher than
  the one before, and the store refuses the same version twice (a draft holding it is submitted by
  hand in the Developer Dashboard).
- Windows command lines are limited to about 32 KB. Put long scripts in files.
- Hugging Face's xet backend stalled on Windows; the server sets `HF_HUB_DISABLE_XET=1`, plus
  `HF_HUB_VERBOSITY=error` and `HF_HUB_DISABLE_SYMLINKS_WARNING=1` before the import.
- yt-dlp needs a JavaScript runtime (Deno preferred, Node 20+ works) for YouTube.
- YouTube's "Sign in to confirm you're not a bot" needs a signed-in browser's cookies: the Start
  button passes no options, so the browser lives in `config.json` (`cookies_from_browser`), never
  taken in the Docker image (`SHISUKO_CONTAINER`; toolbox and distrobox do take it). Offer
  Firefox: Chrome and Edge on Windows lock their cookies away.
- On Windows the CUDA libraries come from the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels;
  `add_nvidia_dll_dirs()` must run before `ctranslate2` is imported.
- GPU memory is often shared. `load_model()` picks `int8_float16` below 4.5 GB free VRAM. Exit codes:
  2 is a startup error not to retry, 3 asks for a restart (broken GPU context, or a failed switch
  with no model left), 4 (`EXIT_UPDATE`, only from `POST /update`) runs `update.py` first.
- AnkiConnect: send requests without a `Content-Type` header, call `requestPermission` first, find
  the newest card with `findNotes("added:1")`.
- `data_collection_permissions` in the manifest requires `strict_min_version` 140 or later.
- `notifications` is required, not optional: the start-up check has no popup to ask for a grant.
  GitHub allows sixty unauthenticated API requests an hour per address, hence one check a day.
  Never add a token.
- Regular Firefox only keeps signed add-ons; unsigned builds are temporary installs only.
- Screenshots fail on DRM-protected videos (tainted canvas); the audio clip still works.
- The native server and the container both use port 8790; run one at a time.
- Firefox and Chrome run a `.cmd` native host through `cmd.exe`: the wrapper must print nothing
  (`@echo off` first), the host must accept the browsers' arguments (Firefox's two, Chrome's origin
  and `--parent-window=`), a child must `CREATE_BREAKAWAY_FROM_JOB`, and the launcher is named
  relative to `cwd` (cmd.exe splits a quoted path holding `&`, `(`, `^`).
- Chrome adds `runtime.sendNativeMessage` only once `nativeMessaging` is granted, while the
  service worker runs: `browser-api.js` looks it up on `chrome.runtime` at each use, never at load.
- `scripts/browser-smoke.mjs` waits for `#server-status` to read exactly `Server offline` and then
  `Server online`; keep those badge texts.
- `browser.downloads.download()` refuses `data:` URLs. Build a `Blob`, pass
  `URL.createObjectURL()` from the background page, revoke it later.
- The toolbar popup dies with the click that closes it: `flushSave()` sends before any `await`,
  sends only the `dirty` fields, and a focused field takes outside changes unless the viewer is
  typing (`typing()`). `saveSettings()` in `background.js` runs one save at a time (`saveChain`).
- YouTube's SPA keeps the watch page in the DOM: `findPlayer()` asks for `#shorts-player` on
  `/shorts/` first. `/sync` keeps going out during an ad, carrying `state.contentPlayhead`.
- The player's focused controls take the arrow keys (`KEY_SKIP_SELECTOR`). `jumpTarget()` answers
  null when there is no cue or the target lies in another covered range (`coveredRange()`).
- Left steps one line back wherever the playhead sits in the line. `leadIn()` takes the previous
  cue's end as a floor: the lead-in may eat silence and nothing else.

## Making changes

1. Keep `server.py` a single dependency-light file (stdlib + numpy + faster-whisper + yt-dlp + PyAV).
2. Bump `version` in `addon/manifest.json` and `VERSION` in `server/server.py` together, inside
   the change's last commit, with its entry in `docs/amo/release-notes.md`; the release notes and
   `reviewer-notes.md` must each stay within AMO's 3,000 characters (see "Release"; `npm test`
   fails otherwise).
3. Run the checks above, then test manually on a real YouTube video: subtitles appear, hover
   pauses, transcript panel works, Alt+Shift+M produces a toast and (with Anki running) fills the card.
4. Update README sections that describe changed behaviour; keep this file's invariants current,
   and the matching `docs/dev/` file.
5. Commit with a descriptive message; do not commit `.env`, `dist/` or anything from `~/.shisu-ko`.

## Release

Full process: `docs/dev/updates-and-release.md`.

- Pushing a tag `v<version>` (on the merge commit, matching `addon/manifest.json`) runs
  `.github/workflows/release.yml`, which makes the release and its `.xpi` and nothing more; the
  finished release then starts `cws-listing.yml`, which submits it to the Chrome Web Store (below).
- `make_metadata.py` refuses a `release-notes.md` that does not mention the manifest's version, so
  bump the version and write its notes in the same change.
- **AMO's 3,000-character limit is a must-test.** `make_metadata.py` refuses release notes or
  reviewer notes over `NOTES_LIMIT`, and `scripts/tests/amo-metadata.test.mjs` holds the limit on
  every push. 0.14.0 was refused at submission (8,920 and 18,844 characters) and reached the
  listing as 0.14.1. The full reviewer text lives in `docs/amo/reviewer-guide.md`.
- A tag AMO has signed is never moved: fix forward with the next patch version.
- The public listing is `.github/workflows/amo-listing.yml`, run by hand
  (`gh workflow run amo-listing.yml -f tag=v<version>`) for the newest release only, never by a tag.
- web-ext runs pinned to one exact version (`web-ext@10.7.0`) in every workflow and in
  `publish-addon.cmd` / `sign-addon.cmd`.
- The actions run on Node 24, one major each across the workflows: `actions/checkout@v7`,
  `actions/setup-node@v7`, `actions/setup-python@v7`, `actions/upload-artifact@v7`,
  `softprops/action-gh-release@v3`. A new action gets its first Node 24 major in
  `release-workflows.test.mjs`. setup-node sets `package-manager-cache: false` in every job that
  holds a key or writes to the releases.
- Every release goes to the Chrome Web Store by itself: `.github/workflows/cws-listing.yml` uploads
  the release's own Chrome zip (the highest release, never rebuilt) through `scripts/cws.mjs`,
  store API v2 only, with the repository secret `CWS_SERVICE_ACCOUNT_JSON` and the repository
  variable `CWS_PUBLISHER_ID` (`docs/cws/README.md`); `cws-schedule.yml` runs it every three hours
  for a release that an older review held back.
- `cws-listing.yml` finds the release workflow by `release.yml`'s `name:` (`workflow_run`): rename
  both or neither.
- `scripts/tests/release-workflows.test.mjs` holds the split between the tag and listing workflows,
  and what `cws-listing.yml` and `cws-schedule.yml` run and do for each word the store gives.
- Rebuild the Docker image with `docker compose build`.

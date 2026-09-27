# Invariants and gotchas in full

AGENTS.md states each rule in a line or two; this is the full text of each, with its reasons.

## Invariants (do not break these)

- Subtitles must stay ordinary DOM text (`textContent`), never canvas, never `<track>` cues,
  never shadow DOM. Yomitan and other popup dictionaries depend on it. The one markup allowed
  inside a cue's text is the word colours' inline `<span class="shisuko-word">` holding a text
  node, with `data-status` and `data-pitch` and nothing else (`renderText()` in `content.js`
  is the one writer, on the subtitle and in the transcript alike); no other element, attribute
  or wrapper goes into a line.
- Never use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval` or `script.src` in the content
  script. youtube.com enforces Trusted Types; only `textContent`/`createElement` style DOM code works.
- Settings are untrusted input before they reach CSS: every style value goes through a sanitiser
  in `applyStyleSettings()` (`clampNumber`, `oneOf`, `hexColor`, `fontStack` with
  `FONT_FAMILY_RE`, which admits only letters, digits, spaces, dots, hyphens and underscores). A
  font family that fails the rule falls back to the preset; no quote, semicolon or `url(` reaches
  the stylesheet. `popup.js` keeps copies of `FONT_FAMILY_RE`, the preset stacks and
  `MODEL_NAME_RE`; `addon/tests/popup-copies.test.js` keeps them equal to the originals.
- Application code uses the `browser.*` promise API. Firefox provides it natively; the shared
  `browser-api.js` adapter supplies it on Chrome, where its internal bridge necessarily calls
  `chrome.*` callbacks. Application code must not call `chrome.*` directly.
- All overlay classes and flags use the `shisuko-` / `__shisuko` prefix.
- Settings defaults live once in `addon/settings.js` (`SHISUKO_DEFAULT_SETTINGS`), loaded before
  `background.js`, `content.js` and `popup.js`. To add a setting, add it there and add the popup
  input with the same id; `addon/tests/settings.test.js` enforces both.
- The server listens on `127.0.0.1:8790`. Port 8765 belongs to AnkiConnect; never use it.
- The server never exposes anything beyond `/health`, `/sync`, `/clip`, `/sessions` and
  `POST /update`; it binds to localhost, validates `video_id` against `^[A-Za-z0-9_-]{6,20}$`,
  and answers browser requests only from the extension's own origin or from pages on loopback
  hosts (`origin_allowed()`), so arbitrary websites cannot drive downloads and transcription.
  `/update` is narrower (`update_origin_allowed()`: the extension's origin or no `Origin` header
  at all, never a page, since ending the server and making the launcher run git and pip is a
  capability only the popup has a use for) and answers 409 `{ok: false, error}` unless the
  server was started by `run.cmd` / `run.sh` (`SHISUKO_LAUNCHER=1`) without `--no-update` and
  without `SHISUKO_NO_UPDATE` (`App.update_blocker()`); otherwise it answers
  `{ok: true, restarting: true, version}` and the process exits with `EXIT_UPDATE` (4). The
  server never spawns `update.py` and never downloads a release itself; the launcher does.
- The add-on never installs itself: no `update_url` in the manifest, no `.xpi` download or
  install (`addon/tests/settings.test.js` enforces the manifest); its updates come from
  addons.mozilla.org. Its one remote request is the anonymous `GET` of
  `https://api.github.com/repos/Multysquid/shisu-ko/releases/latest` in `fetchLatestRelease()`,
  never with a token, cookie or identifier, and there is no second remote endpoint;
  `notifications` stays a required permission (the start-up check notifies with no popup open
  to ask for a grant). The privacy policy, description and reviewer notes in `docs/amo/` state
  exactly this, so a change here changes them too. On Chrome, the package carries no
  `update_url` and no `key`: the Chrome Web Store keeps the item's own key, and refuses a key on
  a new item and one that is not the item's own on an update (`scripts/tests/build.test.mjs`
  holds the built Chrome package the store receives, `addon/tests/settings.test.js` the source
  manifest). A Chrome Web Store install (`CHROME_STORE_ID` in `popup.js`) is updated from the
  store, which adds its own update URL to the package it serves, and an unpacked Chrome build
  (the release's zip, `dist/chrome`) only by the viewer loading a newer one. That has no
  counterpart in `docs/amo/`: those texts go to addons.mozilla.org, which lists the Firefox build
  only.
- The native host (`server/native_host.py`, name `shisuko`) answers only `status` and `start`.
  It never takes a path, a program or an argument from a message: the only thing it can run is
  the checkout's own `server/run.cmd` / `server/run.sh` (root = the parent of the folder the
  host file lives in), and it registers only under the user's own profile, never system-wide:
  for Firefox (`HKCU\Software\Mozilla\NativeMessagingHosts`, `~/.mozilla/native-messaging-hosts`,
  `~/Library/Application Support/Mozilla/NativeMessagingHosts`) and for Chrome
  (`HKCU\Software\Google\Chrome\NativeMessagingHosts`, `~/.config/google-chrome/NativeMessagingHosts`,
  `~/Library/Application Support/Google/Chrome/NativeMessagingHosts`), plus Chromium on Linux
  (`~/.config/chromium/NativeMessagingHosts`; `$CHROME_CONFIG_HOME`, else `$XDG_CONFIG_HOME`,
  replaces `~/.config` for both, as it does for Chrome itself). Firefox's manifest allows the
  gecko id (`allowed_extensions`), Chrome's the Chrome Web Store install's origin alone
  (`allowed_origins`, `CHROME_ORIGIN`): never a wildcard, and never an unpacked build's id,
  which Chrome derives from the folder it was loaded from. The popup shows the button for
  Firefox and for the store id (`START_AVAILABLE`: `runtime.getURL("")` is `moz-extension:` or
  exactly `chrome-extension://<CHROME_STORE_ID>/`, and `test_native_host.py` keeps
  `CHROME_STORE_ID` equal to `CHROME_EXTENSION_ID`), never for an unpacked build. Another
  Chromium-based browser holding the store install can give the same URL, and the host is
  registered only in the folders above, so a browser that reads a folder of its own (Chromium
  outside Linux; on Linux and macOS Chrome Beta, Dev and Canary, whose folders carry the
  channel's name, `google-chrome-beta` or `Google/Chrome Beta`; Brave, Edge and the like; on
  Windows every Chrome channel reads the one key) can show the button and answer "launcher not
  registered" with a hint that cannot help: those browsers are deliberately left out.
  `nativeMessaging` stays in `optional_permissions`, requested by the popup's click handler
  before its first `await` and by nothing else; the button is not a setting.
- A model name from a client (`model` in `/sync`) must match `MODEL_NAME_RE`
  (`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$`) and contain no `..`;
  anything else is answered with `MODEL_NAME_HINT` and never stored. A valid name is reduced to
  its canonical alias (`canonical_model_name()`: `large`, `Systran/faster-whisper-large-v3` and
  `large-v3` are one model) and resolved through `faster_whisper.download_model()` before it is
  loaded. A raw client string must never reach `WhisperModel()`, which also opens local
  directories; only the operator's `--model` may be a folder, and it skips the download.
- `enabled` in the settings is the master switch (the header toggle in the popup, Alt+Shift+S). Off
  must mean nothing happens on YouTube pages: no `/sync`, no overlay, no native-caption hiding, no
  arrow-key handling, no Anki polling, no mining (the cues outlive the switch, so Alt+Shift+M would
  still find one), no known word marked (Alt+Shift+K, `markKnown()`, would find one in them too and
  write the setting), no status badge switched (Alt+Shift+H), no `cardStatus` asks for the word
  colours' deck index (`wordColoursOn()` in `content.js` includes `enabled`, and the poll runs
  from `syncTick()`), so no request reaches Anki from a YouTube tab. Only the toggle command
  itself keeps working: the command listener in `content.js` returns for every other command
  while `enabled` is false.
- The overlay lives in the page's DOM, where any script on youtube.com can dispatch events on
  it, so its handlers (`onMineClick`, `onTranscriptClick`, `onSubtitleEnter`, `onSubtitleLeave`,
  the transcript's close button) act only on trusted events (`ev.isTrusted`): a synthetic click
  must not write a card, save a file, seek or pause. The keyboard commands arrive through the
  browser, not the page. Text from the server is written into Anki's HTML fields only through
  `escapeHtml()` in `background.js`; the overlay shows it through `textContent`.
- Live streams run on the stream's media clock, `getProgressState().current` of YouTube's player
  (read through `wrappedJSObject`, Firefox only), never on `video.currentTime`, which restarts at
  an arbitrary point on every page load. Every place the content script reads or seeks the
  playhead goes through `playhead()` / `seekPlayhead()`.
- Runtime data: see `cue-building.md`, "Runtime data and the cue cache".
- No absolute personal paths, no secrets and no `.env` in tracked files. `.env` is machine-specific
  and ignored; `.env.example` documents it.
- Line endings: LF everywhere, CRLF only for `*.cmd` (`.gitattributes` enforces this).

## Gotchas learned the hard way

- addons.mozilla.org checks the listing texts only when a version is submitted to the listing,
  which `amo-listing.yml` does when it is run for a released tag: release notes and reviewer
  notes over 3,000 characters each are refused then ("Ensure this field has no more than 3000
  characters"), as 0.14.0 was when the tag workflow still submitted every tag. `make_metadata.py` and `scripts/tests/amo-metadata.test.mjs` hold
  that limit on every push; the long reviewer text lives in `docs/amo/reviewer-guide.md`.
- The Chrome Web Store API v1.1 stops on 2026-10-15. `scripts/cws.mjs` speaks v2 alone
  (`chromewebstore.googleapis.com/v2/` and `/upload/v2/`, the item under the publisher's path),
  and `scripts/tests/cws.test.mjs` holds those URLs; nothing may go back to v1.1.
- The Chrome Web Store takes a version only above the one before, compared as numbers part by
  part (0.14.6 is above 0.5.0, which a string comparison gets wrong), and it refuses the same
  version twice. When an upload went in and its publish failed, the draft holds that version and
  every later upload of it is refused: submit that draft in the Developer Dashboard by hand. So
  `cws-listing.yml` uploads only the release with the highest version, and a version the store
  rejected is fixed forward with the next patch version.
- The Chrome Web Store takes one service account per publisher (Developer Dashboard, Account): the
  one whose JSON key is the repository secret `CWS_SERVICE_ACCOUNT_JSON` (`docs/cws/README.md`).
- The Developer Dashboard allows six review cancellations a day. `cws-listing.yml` withdraws a
  waiting review only when a run by hand asks for it (`cancel_review`), never by itself, and
  `cws.mjs submit --cancel-review` cancels nothing when no review waits.
- `workflow_run` knows the release workflow only by its `name:` ("Release extensions"): renamed in
  `release.yml` alone, every release would wait for the next run of `cws-schedule.yml`, up to
  three hours, without a word, so `scripts/tests/release-workflows.test.mjs` holds the two equal.
  It matches by the name alone, and a pull request from a fork can give a workflow of its own the
  same one: `cws-listing.yml` acts only on a successful tag push in this repository.
- GitHub switches off a public repository's workflow that has a schedule after 60 days without
  activity in the repository, and a workflow switched off starts for nothing, not even for a
  `workflow_run`. That is why the store's schedule is `cws-schedule.yml` and `cws-listing.yml` has
  none: a release after a long pause still reaches the store, and only the catch-up of releases
  held back behind an older review waits until `gh workflow enable cws-schedule.yml`.
  `amo-xpi.yml`'s schedule stops the same way.
- Windows command lines are limited to about 32 KB. Put long scripts in files instead of
  inline heredocs when running tools from a shell.
- Hugging Face's xet transfer backend stalled on Windows; the server sets `HF_HUB_DISABLE_XET=1`.
  It also sets `HF_HUB_VERBOSITY=error` and `HF_HUB_DISABLE_SYMLINKS_WARNING=1` before the
  library is imported: the Hub's "set a HF_TOKEN" nag arrives as an `X-HF-Warning` header that
  huggingface_hub logs through its own bare handler and ours (twice on screen), and Windows
  without Developer Mode gets a symlink `UserWarning` per model. Download failures still reach
  the viewer through `friendly_model_error()`.
- yt-dlp needs a JavaScript runtime (Deno preferred, Node 20+ works) for YouTube. The Docker image
  ships Deno; the native setup relies on what is installed.
- YouTube answers some addresses (the owner's, since September 2026) with "Sign in to confirm
  you're not a bot" for every video until the download carries a signed-in browser's cookies.
  The popup's Start button passes no options, so the browser is `config.json`'s
  `cookies_from_browser` (setup, `--save-cookies-from-browser`), the default of every start.
  Firefox is the one to offer on Windows: Chrome and Edge encrypt their cookies there so that
  yt-dlp decrypts none. The Docker image never takes the configured browser (`in_container()`,
  which is `SHISUKO_CONTAINER` only); toolbox and distrobox do, since they share the home folder
  and its Firefox profile, so `/.dockerenv` and `/run/.containerenv` must not count.
- On Windows the CUDA libraries come from the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels;
  `add_nvidia_dll_dirs()` must run before `ctranslate2` is imported.
- GPU memory is often shared with games or wallpaper apps. `load_model()` reads free VRAM with
  `nvidia-smi` and picks `int8_float16` below 4.5 GB; a driver reset shows up as a process death
  without a traceback (Windows LiveKernelEvent 141). The launchers restart the server; exit code 2
  means a startup error that must not be retried. Exit code 3 asks for a restart: a broken GPU
  context, and also a failed model switch after which the previous model could not be reloaded,
  which would leave the server running without any model. Exit code 4 (`EXIT_UPDATE`) asks the
  launcher to run `update.py` before starting again; only `POST /update` produces it.
- AnkiConnect: send requests without a `Content-Type` header (a "simple" request needs no CORS
  preflight), call `requestPermission` first, find the newest card with `findNotes("added:1")`.
- `data_collection_permissions` in the manifest requires `strict_min_version` 140 or later.
- `notifications` is a required permission, not an optional one: the update notification is
  created from the start-up check, where no popup is open to ask for a grant. The GitHub check
  needs no host permission (`Access-Control-Allow-Origin: *`), but GitHub allows sixty
  unauthenticated API requests an hour per address, shared with everything else on the
  connection: hence one check a day, cached in `storage.local`, and the 403/429 text that asks
  to try again in an hour. Never add a token.
- Regular Firefox only keeps signed add-ons; unsigned builds are temporary installs only.
- Screenshots fail on DRM-protected videos (tainted canvas); the audio clip still works.
- The native server and the container both use port 8790; run one at a time.
- Firefox runs a `.cmd` native host through `cmd.exe /s /c "<host> <manifest path> <extension id>"`,
  Chrome through `cmd.exe` too, with the extension's origin and, on Windows,
  `--parent-window=<handle>` as its arguments. stdout is the protocol, so the wrapper must not
  `echo`, `pause` or print anything (`@echo off` first), and the host has to accept those
  arguments. Firefox keeps the host in a job object: a child that does not
  `CREATE_BREAKAWAY_FROM_JOB` dies with the host. And cmd.exe splits a quoted path holding `&`,
  `(` or `^`: name the launcher relative to `cwd`.
- Chrome adds `chrome.runtime.sendNativeMessage` only once the optional `nativeMessaging` is
  granted, which the popup does while the service worker runs, possibly on a `chrome.runtime`
  rebuilt for it. `browser-api.js` therefore looks the method up on `chrome.runtime` at each use
  (a getter); a copy taken at load read "permission missing" until the worker restarted.
- `scripts/browser-smoke.mjs` waits for `#server-status` to read exactly `Server offline` and
  then `Server online`; keep those badge texts.
- `browser.downloads.download()` refuses `data:` URLs ("Access denied for URL data:...", thrown
  synchronously before any promise exists): an extension may not load a URL that inherits its
  principal. Build a `Blob`, pass `URL.createObjectURL()` from the background page, revoke it later.
- The toolbar popup's document dies with the click outside it that closes it, and a text field's
  `change` event fires on that very close: `flushSave()` in `popup.js` sends the save before any
  `await`, from `pagehide` and `visibilitychange` as well as the 150 ms debounce, and sends only
  the fields edited since the last save (`dirty`), because the form is not the only writer (the
  content script saves `enabled`, `showTranscript` and `knownWords` for the commands) and a
  whole-form save would put back what another writer changed; `browser.storage.onChanged` brings
  such changes into the form, a focused text field included unless the viewer is typing in it
  (`typing()`, `typedBaseline`). `saveSettings()` in `background.js` is a read-modify-write, so it
  runs one save at a time (`saveChain`): two in flight would drop a patch.
- YouTube's SPA keeps the watch page in the DOM when it leaves it: on a Short reached from a
  watch page `#movie_player` still exists, hidden, with its video, so `findPlayer()` asks for
  `#shorts-player` on `/shorts/` addresses first, or the Short would be fetched and transcribed
  on the hidden video's clock. An ad runs on a clock of its own: `/sync` still goes out during
  one (the server hears of a new video only through it, and a pre-roll ad used to hold up the
  fetch by its whole length), carrying `state.contentPlayhead`, the video's position as of the
  last request outside an ad, or the link's `t=` before the first.
- The player's focused controls (the volume slider, the settings menu, the radios and lists of
  its dialogs) take the arrow keys themselves; `KEY_SKIP_SELECTOR` names them by ARIA role,
  except the progress bar, whose arrows are the five second seek the subtitle jump replaces.
  `jumpTarget()` answers null, leaving YouTube's own seek alone, when there is no cue at all or
  the playhead and the target do not sit in the same covered range (`coveredRange()`): the last
  known line before an untranscribed stretch is not the previous line.
- Left steps one line back wherever the playhead sits in the current line: inside a line, the line
  before it; in the gap after one, that line again. It used to replay the current line past
  `CUE_REPLAY_S` (1 s) into it, asbplayer's rule, but a line runs three to six seconds, so that
  was nearly always and Left restarted what was already playing. And `leadIn()` takes the previous
  cue's end as a floor: `normalise_gaps` closes every gap under 0.5 s to 0.1 s, shorter than the
  0.15 s lead-in, so the old unclamped seek landed inside the previous line and flashed its last
  frames before sweeping back into the line the viewer had just left. The lead-in may eat silence
  and nothing else.

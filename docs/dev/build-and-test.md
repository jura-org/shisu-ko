# Build and test harness

How the Chrome build is derived, how the test loaders run the add-on scripts outside a browser,
and how the server tests keep away from the real data folder.

## The Chrome build

`addon/manifest.json` is the Firefox source and remains directly loadable from
`about:debugging`. `node scripts/build.mjs` derives Chrome from that source into `dist/chrome`;
it never maintains a second application copy. `npm run watch` rebuilds after edits; reload the
unpacked extension in `chrome://extensions` and reload the YouTube tab. The build writes
versioned Firefox and Chrome ZIPs and excludes `addon/tests`, dotfiles, and development metadata.
Chrome's `service-worker.js` loads `browser-api.js`, `settings.js`, `match.js`, `words.js` and
`background.js` in that order with classic `importScripts`, so settings globals retain the same
behavior as Firefox; `addon/tests/settings.test.js` and `scripts/tests/build.test.mjs` hold that
order. Chrome refuses an extension whose `commands` suggest more than four shortcuts, so
`chromeManifest()` keeps the first four in manifest order (`CHROME_MAX_SUGGESTED_KEYS`) and drops
the default key of the rest (Alt+Shift+H, `toggle-status`, the fifth), which a Chrome user binds at
`chrome://extensions/shortcuts`; `build.test.mjs` holds that too. A new command goes last.

The release's Chrome zip, `shisu-ko-<version>-chrome.zip`, is what the Chrome Web Store receives,
byte for byte: `cws-listing.yml` downloads the release's asset and uploads it without building it
again (`updates-and-release.md`, "Release"). So the build must write a package the store takes at
upload, and `build.test.mjs` ("the Chrome package is one the Chrome Web Store takes at upload")
holds it: no `key` (the store keeps the item's own, and refuses a key on a new item and one that
is not the item's own on an update) and no `update_url` in the manifest, a name of at most 75
characters and a description of at most 132 (it is 131), a version of one to four integers up to
65535 without leading zeros, and every icon the manifest names, `action.default_icon` included, a
PNG file in the package.

## Test loaders

`server/tests/_serverlib.py` loads `server.py` the way `cue-building.md` recommends
(`sys.modules` registration before `exec_module`), with `SHISUKO_HOME` pointed at a temporary
folder first ("Server tests" below). `addon/tests/_loadBackground.js` runs
`background.js` in a Node `vm` sandbox with `browser`/`fetch`/`btoa` stubbed out — top-level
`function` declarations become sandbox properties, but `const`/`let` (`DEFAULT_SETTINGS`,
`REQUEST_TIMEOUT_MS`, `CARD_STATUS_TTL_MS`, `DECK_SEEN_KEY`, `DECK_NOTES_KEY`, `DECK_NOTES_FORMAT`) need an extra
script run in the same context to expose them, since they live in the global lexical environment
rather than as globalThis properties; it loads `settings.js`, `match.js` and `words.js` first,
like the manifest. `addon/tests/_loadContent.js` does the same for `content.js` by rewriting
its IIFE to return its pure helpers (`shouldSync`, `mergeCues`, `findActiveCue`, `jumpTarget`,
`fontStack`, `modelForSync`, ...), the handlers that drive them (`sync`, `premineNow`,
`onKeyDown`, `onMineClick`, `discover`, ...), the word-colour functions (`renderText`,
`refreshWordMarks`, `pollWordIndex`, `wordColoursOn`, `syncTick`, `setSubtitle`,
`transcriptLine`, `mineCue`), the `browser.storage.onChanged` listener as `onSettingsChanged`
and the `runtime.onMessage` listener as `onCommand`; it throws if the file's shape changes. It
declares words.js's export with `var` instead of `const`, so a test can put a counting wrapper
in `sandbox.SHISUKO_WORDS` and see how often content.js asks the matcher. Its
`stubElement(tag)` keeps a child list (`appendChild`, `insertBefore`, `replaceChildren`, a
fragment that empties into its target), so a test can count the nodes a render makes and read
the transcript panel's order back, and a stub canvas yields no blob. `addon/tests/popup.test.js`
builds its fake document from `popup.html` (tags, types, range bounds, listeners a test can
fire) and plays the background with a `getSettings` / `saveSettings` pair that merges like
`background.js` and echoes the write to the storage listener.
When adding a new setting or a new pure helper, add a matching test rather than only exercising
it manually.

## Server tests

`python -m pytest server/tests` needs only `server/requirements-test.txt` (pytest and numpy): the
tests fake faster-whisper, CTranslate2 and yt-dlp where they need them. CI runs them on Ubuntu
with Python 3.10, 3.12 and 3.13 and on Windows with 3.12, none of them with a GPU.

They never touch the real data folder. `server.py` fixes `APP_DIR` and every path under it (the
cache, `config.json`, `next-model`, `rocm-starts`, the instance locks) at import, from
`SHISUKO_HOME`, else the real `~/.shisu-ko`. So `_serverlib.isolate_home()` sets `SHISUKO_HOME` to
a fresh temporary folder (`shisuko-tests-*`) for the whole process unless the caller set one (an
empty value counts as unset, as it does in `server.py`), and an `atexit` hook
(`remove_temporary_homes()`) deletes it again; `conftest.py` calls that hook itself before its
`os._exit()` on CI. `load_server()` calls `isolate_home()` before the import, and `conftest.py`
calls it before any test module is collected, so the child processes the tests start inherit the
folder too. `load_server()` also imports `server.py` with `SHISUKO_ENGINE=default`, since
`rocm_engine()` decides at import whether the AMD engine is on, and an engine installed on the
machine must not change what the tests see; `test_rocm.py` sets `ROCM_ACTIVE` itself where it
needs it. A test that needs a data folder takes `tmp_path` and points the paths there, as the
`home` fixtures of `test_rocm.py` and `test_amd_setup.py` do for every test.

`conftest.py` then checks that it held. Before the session it lists the real `~/.shisu-ko` (from
`Path.home()`, before any test changes `HOME` or `USERPROFILE`): the names at the top level, a
file with its mtime and a folder by its name, and one level into `rocm`, `rocm.new`, `rocm.old`
and `cache/rocm-download`, the folders `amd_setup.py` installs, swaps and removes, where a folder
counts by its mtime too. `server.log` and `server-<port>.lock` are left out: the viewer's own
server, which may run while the tests do, writes those. After every test and fixture teardown
(`pytest_sessionfinish`, `trylast`) it lists the folder again, and a change fails a run that
passed, with a "real ~/.shisu-ko changed" section naming each new, removed or modified entry. It
only lists and stats; no file there is opened.

`server/tests/test_rocm.py` covers `server.py`'s side of the AMD engine (`server-runtime.md`, "How
the AMD engine works"): `rocm_engine_state()` and `rocm_engine()` fed tmp paths and dict
environments (`config.json` and `SHISUKO_ENGINE`, the marker, the Python tag and platform, the
Docker image, Nix's Python, importers, the Linux libraries and the re-exec), the crash guard and
the hand-over, `gpu_context_broken()` (every message that broke the context before still does),
`amd_vram_mb()` and the gfx targets, the exits, the model switch that restarts on Windows,
`--probe-gpu`, `--check`'s line, and `conftest.py`'s real-folder check. ctranslate2,
faster_whisper, ctypes and `signal` are stand-ins, so nothing loads a model or installs a Ctrl+C
handler in pytest's process, and the autouse fixture turns any call of the real
`terminate_process()` into a failure. Its syntax-tree tests hold that `hard_exit()` and `finish()`
hold `server.py`'s only exit calls and that the script runs `entry_point()`; with the engine off,
the NVIDIA and CPU paths are checked to stay as they were. `server/tests/test_amd_setup.py` covers
`amd_setup.py` without the network, PowerShell, pip, a GPU or the real data folder: downloads go
through a fake opener, pip and the probe's child through fake runners, detection is faked or reads
a sysfs tree built under `tmp_path`, and an autouse fixture fails any test that still reaches a
real process or download. It holds the pins and the sizes the offer announces, that the file is
stdlib only and never imports `server.py`, each value `server.py` repeats, and the exit code of
every flow. `test_setup_model.py` holds where `setup.cmd` and `setup.sh` call `amd_setup.py` (a
line of its own after the download's failure block, before "Setup is complete", with nothing
after it that reads its exit code), and `scripts/launcher-smoke.sh` runs `setup.sh` against an
`amd_setup.py` stub that asks its question, gets the EOF and fails.

On Windows the server tests' Linux side (the re-exec, sysfs, `fcntl`, the POSIX paths) runs
through WSL: a venv of its own inside the distribution, outside the checkout (`python3 -m venv`
under `~/.cache`, then `pip install -r server/requirements-test.txt`), and pytest from the
checkout under `/mnt/c/...` with `python -m pytest -p no:cacheprovider server/tests`, so that
pytest writes no cache folder into the Windows tree. The real-folder check there watches the
distribution's own `~/.shisu-ko`.

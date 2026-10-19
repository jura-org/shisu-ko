# Build and test harness

How the Chrome build is derived, how the test loaders run the add-on scripts outside a browser,
and how the server tests keep away from the real data folder.

## The Chrome build

`addon/manifest.json` is the Firefox source and remains directly loadable from
`about:debugging`. `node scripts/build.mjs` derives Chrome from that source into `dist/chrome`;
it never maintains a second application copy. `npm run watch` rebuilds after edits; reload the
unpacked extension in `chrome://extensions` and reload the YouTube tab. The build writes
versioned Firefox and Chrome ZIPs and excludes `addon/tests`, dotfiles, and development metadata.
Chrome's `service-worker.js` loads `browser-api.js`, `settings.js` and
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
`background.js` in a Node `vm` sandbox with `browser`/`fetch` stubbed out — top-level
`function` declarations become sandbox properties, but `const`/`let` (`DEFAULT_SETTINGS`,
`REQUEST_TIMEOUT_MS`, `SERVER_TOO_OLD_TEXT`, `deckState`) need an extra
script run in the same context to expose them, since they live in the global lexical environment
rather than as globalThis properties; it loads `settings.js` first, like the manifest. `addon/tests/_loadContent.js` does the same for `content.js` by rewriting
its IIFE to return its pure helpers (`shouldSync`, `mergeCues`, `findActiveCue`, `jumpTarget`,
`fontStack`, `modelForSync`, ...), the handlers that drive them (`sync`, `takeMined`, `pendingMine`,
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

`scripts/browser-smoke.mjs` (`npm run test:browser`, Playwright's Chromium) drives the packaged
Chrome build against local fixtures: a fixture server answering `/health` and `/sync`, and a
fixture AnkiConnect. Since 0.22.0 its fixture server keeps a mining log the way `server.py` does
(one entry per mine nonce, `mined` / `mined_next` from the cursor): a line's pickaxe must put
`mine` on the next `/sync` with the popup's `anki` settings, stop sending it once the log answers
it, and toast the answer, and the popup's field names must reach the server in `/sync`. Since
0.23.0 the fixture server also answers `/sync`'s `deck` (every row, then a delta) and
`/health?decks=1`: the line must show the words in their card's colours and follow the delta, and
the popup's deck list and hint must come from the server. The fixture AnkiConnect refuses every
call, the stored settings name it under 0.22.0's `ankiUrl`, and the run fails on any request to
it (the 0.22.0 extension fails there). Since 0.24.0 the fixture `/health` carries `latest`, the
server's update check, naming a release far ahead: the popup's banner and result line and the
toolbar badge must show it. The run also logs every request the browser makes, the service
worker's included (it must see a `/health`), and fails on any host but 127.0.0.1 and the routed
YouTube page. The attach
itself is the server's and is tested in `server/tests/test_mining.py` and `test_mining_sync.py`.

## Server tests

`python -m pytest server/tests` needs only `server/requirements-test.txt` (pytest, pytest-xdist and
numpy): the tests fake faster-whisper, CTranslate2, PyTorch, yt-dlp and AnkiConnect where they need them;
the frame decode test (`test_mining.py`) runs on a tiny MPEG-TS file it writes with PyAV, and is
skipped without PyAV. CI
runs them on Ubuntu with Python 3.10, 3.12 and 3.13 and on Windows with 3.12, none of them with a
GPU.

They never touch the real data folder. `server.py` fixes `APP_DIR` and every path under it (the
cache, `config.json`, `next-model`, `rocm-starts`, the instance locks) at import, from
`SHISUKO_HOME`, else the real `~/.shisu-ko`. So `_serverlib.isolate_home()` sets `SHISUKO_HOME` to
a fresh temporary folder (`shisuko-tests-*`) for the whole process unless the caller set one (an
empty value counts as unset, as it does in `server.py`; so does a folder another test process
made, which `SHISUKO_TESTS_HOME` names: a pytest-xdist worker inherits its controller's and makes
its own, "Parallel runs" below), and an `atexit` hook
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
only lists and stats; no file there is opened. Under xdist the controller makes that check, once
every worker has finished; a worker's exit status is not the run's, so the workers skip it.

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

### Parallel runs

With pytest-xdist installed, `conftest.py` (`pytest_cmdline_main()`, run before the plugin's own
hook) runs a whole folder on one worker per core, at most `XDIST_WORKERS` (8), unless the command
line names a count: `-n 0` runs everything in one process, `-n auto` on every core, and
`PYTEST_XDIST_AUTO_NUM_WORKERS` is taken as it stands. A command line that names only files or
test ids (`names_a_folder()`) runs in one process, where the workers would cost more than they
save: one file took 0.3 s alone and 6.7 s on 8 workers. `--pdb` keeps one process, and without
the plugin (or with `-p no:xdist`) the suite runs as it always did. Workers are pytest processes
of their own that load `conftest.py` again with `PYTEST_XDIST_WORKER` set, which keeps them from
starting workers in turn. Why at most 8: xdist starts its workers one after another, about half a
second each on Windows, and hands out no test before every one has collected (some 4 s). On 24
cores (Windows, Python 3.12, 2026-10-18) the suite took 74 s in one process, 27 s on 4 workers,
22-23 s on 8 to 12 and 32-35 s on 24. CI's runners have four cores, so four workers.

Every `server.App` starts a `Transcriber` thread (`App.__init__`) whose loop never ends. Left
alone, the thread went on picking work from its finished test's sessions while later tests ran,
with whatever those had patched into `server.py` (`CACHE_DIR`, `detect_speech`, ...): over a
hundred tests left one, about a dozen of them with a session still to transcribe. In one process
the order of the tests was always the same and nothing showed; xdist changes the order from run
to run, and `test_declared_language.py` then failed now and again on a pause a stray thread had
lifted. The autouse fixture `stop_transcribers` in `conftest.py` is set up before any other
fixture of a test, so it is torn down after them all, its monkeypatch included. It hands every
`Transcriber` thread the test started a `StoppedApp` (no model to switch, no work, no sweep) and
waits, up to 10 s, until the thread has looked at it once: from then on the thread only sleeps.
It ends no thread, since pytest reports an exception that ends one, `SystemExit` included. The
leftover threads had slowed the run in one process as well: 108 s before, 74 s after.

`run_check()` (`--check`) prints a line about the Kitsune models' runtime
(`kitsune_runtime_line()`), which imports torch and transformers wherever they are installed and
asks the CUDA driver for its GPU. The `--check` tests of `test_setup_model.py` and `test_rocm.py`
put stand-ins for both in `sys.modules`, as they do for CTranslate2: on a Windows machine with
both installed, the two real imports took over a minute and made one test 57 s of a 172 s run.
CI has neither, so it never noticed. `test_kitsune.py`'s package test, which needs the real
torch and safetensors (`importorskip`), still imports them: some 10 s on that machine.

One test opens windows: `test_launch_on_windows_runs_the_launcher_from_a_path_cmd_would_split` in
`test_native_host.py` runs a real `start` three times, and each one opens a console window, in
Windows Terminal on Windows 11, in front of whatever the viewer is doing. Nothing the test could
pass hides it: `cmd.exe` makes that console, and one handed over hidden (`SW_HIDE` in the
`STARTUPINFO`) was dropped by Windows Terminal before `run.cmd` ran. It runs on CI (`CI=true`) and
locally with `SHISUKO_TEST_CONSOLES=1` (`opens_consoles`). Every other process the tests start
shares the console pytest runs in, whose window is the terminal's own or none.

## Extension tests

`npm test` runs `addon/tests` and `scripts/tests` in one `node --test`, which starts one process
per test file and runs up to one file per core at a time. `addon/tests/popup.test.js` waits on
real timers (the 150 ms save debounce, `wait(200)`) and takes some 12 s alone, every other file
under 2.5 s, so it sets the length of the run. CI runs the two folders in separate steps.

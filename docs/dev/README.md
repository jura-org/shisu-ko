# Developer docs

The detail behind `AGENTS.md`. Read the file for a subsystem before changing it.

- `cue-building.md`: window planning, the initial prompt and its skip retry, lead-word repair, sentence marks, the gates, `build_cues()`, the merges and seams, sung lyrics, the language watch, the preview decode, the creator's own subtitles (fetch, cleaning, alignment), the runtime data and the cue cache.
- `server-runtime.md`: live streams, model switching and the setup download, the Kitsune-Transcribe models (their engine on PyTorch, names, quantised weights, word timings, installer), the Start server button and its native host, the server update step (`update.py`, `POST /update`, exit code 4, the launchers), the Anki sync after mining (`AnkiWatcher`), the experimental AMD engine (its side folder, the crash guard, the exits on Windows, `--probe-gpu`, `server/amd_setup.py`).
- `mining.md`: the one-tab election, the server's attach to new Anki notes (`Miner`), the `/sync` mining fields, what a mined card gets, matching a card to its subtitle, the picture (`FrameGrabber`).
- `word-colours.md`: `addon/words.js`, the server's deck index and the golden test, the rows in the background, the drawing in the content script and the portal, the popup controls.
- `updates-and-release.md`: the server's update check, the add-on's badge and the popup's Update button, tags, signing, the AMO listing workflow, the Chrome Web Store workflows (`cws-listing.yml`, `cws-schedule.yml`) and `scripts/cws.mjs`.
- `build-and-test.md`: the Chrome build, the test loaders that run the add-on scripts in Node, and the server tests (kept away from the real data folder, the AMD engine's tests, running them through WSL).
- `portal.md`: the phone portal: its access modes, pairing and threat model, the card setup, the HLS proxy, timing sources and language, retries, the Anki sync line in Settings, positions, the YouTube watch history.
- `invariants-and-gotchas.md`: the full text of every invariant and gotcha `AGENTS.md` states in short.

## Repository layout

```
addon/                Firefox extension (Manifest V3, plain JS, no build step)
  content.js          overlay, sync loop, hover-pause, transcript, live clock, mine requests in /sync
  background.js       server proxy, settings, server start, the server's release check (from
                      /health), badge and notification, the server's update, the no-Anki
                      fallback's downloads, the word colours' rows from /sync
  settings.js         the one place settings and their defaults are declared
  words.js            the word colours' matcher (finding words in a line), for content.js and the
                      phone portal; its note reading has a twin in server.py, held equal by the
                      golden test
  popup.*             settings UI with the server status, the Start and Update buttons and the update
                      banner, also the preferences page
  tests/              Node tests for background.js, popup.js, words.js and the pure helpers
                      of content.js
server/
  server.py           HTTP server: yt-dlp + faster-whisper (MLX on Apple Silicon) + live
                      follower + clip cutting + mining (attaching audio and a frame to Anki notes)
  mlx_beam.py         the beam search mlx-whisper lacks; server.py loads it on the Apple GPU
  kitsune_engine.py   the Kitsune-Transcribe models on PyTorch behind faster-whisper's interface:
                      packages, quantised weights, CTC and cross-attention word timings
  kitsune_setup.py    installs PyTorch, transformers and librosa for them, run by setup (stdlib only)
  setup.cmd/.sh       setup, downloads the model     run.cmd/.sh   start (with auto-restart)
  amd_setup.py        the experimental AMD engine, run by setup: looks for a card, installs
                      CTranslate2 for ROCm into ~/.shisu-ko/rocm and tests it (stdlib only)
  update.py           self-update run by run.cmd/.sh, first and after the server exits with code 4
                      (POST /update): git fast-forward or newest release
  native_host.py      native-messaging host behind the popup's Start server button (stdlib only);
                      native-host.cmd/.sh wrap it for Firefox and Chrome; --register writes a
                      host manifest for each browser
  tests/              pytest suite                   tools/        cue statistics, re-transcription
portal/               the phone portal: portal.py, qr.py, static/ (the page), tests/; started by
                      server/portal.cmd / portal.sh or nix run .#portal
docker/               Windows wrappers for docker compose and the WSL engine installer
docs/                 the user pages (install.md, guide.md, server.md, amd.md, troubleshooting.md,
                      how-it-works.md), subtitle-quality.md, screenshots, the demo recording, amo/ (the listing on
                      addons.mozilla.org), cws/ (the Chrome Web Store: how releases get there,
                      the key's one-time setup), dev/ (developer docs: the design of each subsystem)
scripts/cws.mjs       asks the Chrome Web Store what it holds, uploads a release's Chrome zip and
                      submits it for review
.github/workflows/    cws-listing.yml takes every release to the Chrome Web Store; cws-schedule.yml
                      runs it every three hours for a release held back behind an older review
Dockerfile, compose.yaml, compose.cpu.yaml, .env.example, flake.nix
sign-addon.cmd        signs a local build through addons.mozilla.org (unlisted; manual fallback)
publish-addon.cmd     submits a release to the public listing by hand (fallback for amo-listing.yml)
AGENTS.md             architecture notes, invariants and gotchas for contributors and coding agents
```

# Developer docs

The detail behind `AGENTS.md`. Read the file for a subsystem before changing it.

- `cue-building.md`: window planning, the initial prompt and its skip retry, lead-word repair, sentence marks, the gates, `build_cues()`, the merges and seams, sung lyrics, the language watch, the preview decode, the creator's own subtitles (fetch, cleaning, alignment), the runtime data and the cue cache.
- `server-runtime.md`: live streams, model switching and the setup download, the Kitsune-Transcribe models (their engine on PyTorch, names, quantised weights, word timings, installer), the Start server button and its native host, the server update step (`update.py`, `POST /update`, exit code 4, the launchers), the Anki sync after mining (`AnkiWatcher`), the experimental AMD engine (its side folder, the crash guard, the exits on Windows, `--probe-gpu`, `server/amd_setup.py`).
- `mining.md`: the one-tab election, the server's attach to new Anki notes (`Miner`), the `/sync` mining fields, what a mined card gets, matching a card to its subtitle, the picture (`FrameGrabber`).
- `word-colours.md`: `addon/words.js`, the server's deck index and the golden test, the rows in the background, the drawing in the content script and the portal, the popup controls.
- `updates-and-release.md`: the server's update check, the add-on's badge and the popup's Update button, tags, signing, the AMO listing workflow, the Chrome Web Store workflows (`cws-listing.yml`, `cws-schedule.yml`) and `scripts/cws.mjs`.
- `build-and-test.md`: the Chrome build, the test loaders that run the add-on scripts in Node, and the server tests (kept away from the real data folder, the AMD engine's tests, running them through WSL).
- `portal.md`: the phone portal: its access modes, pairing and threat model, the card setup, the HLS proxy, timing sources and language, retries, the Anki sync line in Settings, positions.
- `invariants-and-gotchas.md`: the full text of every invariant and gotcha `AGENTS.md` states in short.

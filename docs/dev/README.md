# Developer docs

The detail behind `AGENTS.md`. Read the file for a subsystem before changing it.

- `cue-building.md`: window planning, the initial prompt and its skip retry, lead-word repair, sentence marks, the gates, `build_cues()`, the merges and seams, sung lyrics, the language watch, the preview decode, the runtime data and the cue cache.
- `server-runtime.md`: live streams, model switching and the setup download, the Start server button and its native host, the server update step (`update.py`, `POST /update`, exit code 4, the launchers).
- `mining.md`: the one-tab election, automatic mining through AnkiConnect, what a mined card gets, matching a card to its subtitle, pre-mined sentences.
- `word-colours.md`: `addon/words.js`, the deck index in the background, the drawing in the content script, the popup controls.
- `updates-and-release.md`: the add-on's update check and the popup's Update button, tags, signing, the AMO listing workflow, the Chrome Web Store workflows (`cws-listing.yml`, `cws-schedule.yml`) and `scripts/cws.mjs`.
- `build-and-test.md`: the Chrome build and the test loaders that run the add-on scripts in Node.
- `invariants-and-gotchas.md`: the full text of every invariant and gotcha `AGENTS.md` states in short.

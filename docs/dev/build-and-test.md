# Build and test harness

How the Chrome build is derived and how the test loaders run the add-on scripts outside a browser.

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
(`sys.modules` registration before `exec_module`). `addon/tests/_loadBackground.js` runs
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

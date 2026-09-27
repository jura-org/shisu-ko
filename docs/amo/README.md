# Publishing on addons.mozilla.org

Everything the public listing needs, ready to paste or to send through the API. The listing is
live at https://addons.mozilla.org/firefox/addon/shisu-ko/ (id `shisu-ko@multysquid.github.io`;
0.7.0 was its first listed version and 0.14.1 its second; 0.2.0 to 0.4.0, 0.7.1 and 0.8.0 to
0.13.0 are unlisted builds). From 0.14.2 on, every tag is signed for self-distribution and
its `.xpi` attached to the GitHub release (*Every release* below), and the listing gets a release
only when it is published there by hand (*Publishing a release on the listing*).

| File | Used for |
|---|---|
| `summary.txt` | Listing summary (max 250 characters, no URLs) |
| `description.md` | Listing description (AMO renders this Markdown subset: bold, lists, links, code) |
| `release-notes.md` | Version notes shown on the listing; edit per release. **At most 3,000 characters** (AMO refuses more) |
| `reviewer-notes.md` | Notes to the reviewer, submitted with each version: a summary, a quick test, the permissions, and a link to the full guide at the tag. **At most 3,000 characters** with `<version>` filled in. Not public |
| `reviewer-guide.md` | The full reviewer guide: every feature's test steps, every permission and request. Linked from the notes; no length limit |
| `privacy-policy.md` | Privacy policy; Developer Hub only |
| `icon-128.png`, `icon-256.png` | Listing icon (rendered from `addon/icons/icon.svg`); Developer Hub only |
| `screenshots/` | Listing screenshots with the captions below; Developer Hub only |
| `make_metadata.py` | Builds `amo-metadata.json` from the files above for `amo-listing.yml` and `publish-addon.cmd` |

Listing values that are not in a file: categories **Language Support** and **Photos, Music &
Videos**; tags **youtube**, **streaming** (AMO's tag list is fixed); license **MIT License**;
homepage `https://github.com/Multysquid/shisu-ko`; support site
`https://github.com/Multysquid/shisu-ko/issues`; not experimental, no payment; compatible with
**Firefox** only (the server has to run on the same computer, so untick Firefox for Android).

Screenshot captions, in order:

| File | Caption |
|---|---|
| `01-subtitles.png` | Live subtitles drawn over the player as real text, a few seconds behind the transcription running on your own machine. |
| `02-yomitan-lookup-anki.png` | Hover a line and the video waits. Look the word up with Yomitan, add the card, and Shisu-ko attaches the frame and the sentence audio. |
| `03-hover-pickaxe.png` | The pickaxe on a hovered line (or Alt+Shift+M) mines the sentence by hand, into the newest Anki card or into Downloads. |
| `04-transcript-and-style.png` | The transcript panel lists every line so far; timestamps seek, pickaxes mine. Font, position, colour and outline are adjustable. |
| `05-settings-popup.png` | Every setting lives in the toolbar popup; the switch in its header turns the whole extension off and on. |

## Before every release

1. Bump `version` in `addon/manifest.json` and `VERSION` in `server/server.py` together. AMO
   refuses a version number that was uploaded before, in either channel (0.2.0 to 0.4.0, 0.7.1 and
   0.8.0 to 0.13.0 are taken by unlisted builds; every release takes its own number unlisted,
   and its number plus `.1` listed once it is published on the listing).
2. Run the checks: `for f in addon/*.js; do node --check "$f"; done` (`node --check` takes one file),
   `npx --yes web-ext@10.7.0 lint --source-dir addon --ignore-files "tests/**"` (the version the workflows use),
   `node --test addon/tests/*.test.js`, `python -m pytest server/tests`.
3. Build: `npx web-ext build --source-dir addon --artifacts-dir dist --overwrite-dest --ignore-files "tests/**"`
   gives `dist/shisu-ko-<version>.zip`. The zip is the source: there is no build step, so answer
   **No** when AMO asks whether source code needs to be submitted.
4. Update `release-notes.md` (it has to mention the new version, since any release may be
   published on the listing with the texts its tag holds, and both it and `reviewer-notes.md` must
   stay within AMO's 3,000 characters: `make_metadata.py` refuses them otherwise, and `npm test`
   and the Tests workflow check it on every push, since AMO itself only says so once the version
   is submitted), and `reviewer-guide.md` (then the summary in `reviewer-notes.md`) if permissions
   or the test steps changed.
   A changed `privacy-policy.md` is pasted into the Developer Hub by hand (the listing's **Edit**
   pages): only the first submission takes it from the file, `publish-addon.cmd` never uploads
   it. 0.9.0 changes it (the release check against GitHub), and so does the word colours
   release (what is read from Anki and stored, and the model download at setup).
5. Merge, then tag the merge commit (`v<version>`); pushing it releases the version and has it
   signed for self-distribution (below). Do not also run `sign-addon.cmd` for it: the workflow
   uploads only a number AMO does not have yet, and attaches a signed file only when it holds
   the tag's build, so a hand-signed file of other code fails the release. A tag AMO has signed
   is never moved; fix forward with the next patch version. Publishing it on the listing is a
   step of its own, taken for the releases the listing should get; the reviewer notes point to
   the tag. The Chrome Web Store gets every release by itself once the release is made;
   `docs/cws/README.md` describes that side.

## Developer Hub, by hand in the browser

How 0.7.0, the first listed version, was submitted; later listed versions go through
`amo-listing.yml` or `publish-addon.cmd` (below). Log in at https://addons.mozilla.org/developers/,
open **My Add-ons** > **Shisu-ko** > **Upload New Version** (for an add-on that is not on AMO yet,
**Submit a New Add-on** shows the same screens).

1. **Distribution**: On this site.
2. **Upload**: the listed build of the newest release, never the zip of step 3 above, whose
   number the release workflow has taken already: `git checkout v<version>`,
   `node scripts/build.mjs --browser firefox`, `node scripts/amo-xpi.mjs listing dist/firefox`,
   then `npx web-ext build --source-dir dist/firefox --artifacts-dir dist --overwrite-dest` gives
   `dist/shisu-ko-<version>.1.zip`. The validator should report no errors; one warning about
   `strict_min_version` and Firefox for Android is expected and harmless.
3. **Compatibility**: Firefox only.
4. **Source code**: No.
5. **Describe add-on**: name `Shisu-ko`; add-on URL (slug) `shisu-ko`; summary from
   `summary.txt`; description from `description.md`; the categories, tags, license, homepage and
   support site listed above; privacy policy from `privacy-policy.md`; notes to reviewer from
   `reviewer-notes.md` (replace `<version>` with the release's number); release notes from
   `release-notes.md`.
6. **Submit**, then in the listing's **Edit** pages upload `icon-128.png` (or the 256 one) as the
   icon and the five screenshots with their captions, in order.

A listed version is public only once AMO's review has passed, which can take days, a first
submission up to a few weeks. Reviewers also look at published versions afterwards. Their
questions arrive by email and on the version's page in the Developer Hub, and are answered there.

## Every release: the release workflow

Pushing the tag runs `.github/workflows/release.yml`: the checks, the `.xpi` and the GitHub
release, nothing more. It signs `dist/firefox` as the releases up to 0.13.0 were signed:
`web-ext sign --channel unlisted` (no listing texts; the AMO API key lives in the repository
secrets `WEB_EXT_API_KEY` and `WEB_EXT_API_SECRET`) uploads it to AMO's unlisted channel, waits
up to 15 minutes for AMO to sign it for self-distribution (usually a few minutes; AMO allows
itself up to a day, longer for a version it picks for a manual review) and downloads the signed
`shisu_ko-<version>.xpi`, and the GitHub release is then made with the zips and the `.xpi`: a
permanent install for regular Firefox that does not wait for any listing review.

Every release carries an `.xpi`. AMO holds some versions for a human review, which can take
days (0.14.2 was one): when AMO has not signed the version within the 15 minutes, the release
still goes out, with `shisu-ko-<version>-firefox-unsigned.xpi`, the Firefox zip under a name
that says what it is (it installs in Firefox Developer Edition, Nightly and ESR with
`xpinstall.signatures.required` set to `false`, or for the session from `about:debugging`).
`.github/workflows/amo-xpi.yml` (every three hours for the newest release, or by hand:
**Actions** > **Attach the signed Firefox package** > **Run workflow**, with a tag) then puts
AMO's signed `shisu_ko-<version>.xpi` on the release as soon as AMO has signed it, checked
against the release's own Firefox zip (`amo-xpi.mjs same-build`), and deletes the unsigned
stand-in. An upload AMO never took fails the release job: fix the cause and re-run it. AMO takes
a number once, so a re-run takes AMO's signed file of the first upload instead, when it holds the
tag's build (a moved tag fails here). A version AMO rejects never gets a signed file: answer the
reviewer in the Developer Hub and release the next patch version. 0.14.1's only possible file
is its listed one. Nothing of this touches the listing.

`node scripts/amo-xpi.mjs status <version>` (with the API key in the environment) says what AMO
knows of a version: `missing`, `pending`, `public` (signed; for a listed version also approved),
or `disabled` for a rejected one.

## Publishing a release on the listing: `amo-listing.yml`

The listing is a workflow of its own, run on purpose and never by a tag: **Actions** >
**Publish a release on addons.mozilla.org** > **Run workflow** with the newest release's tag, or

    gh workflow run amo-listing.yml -f tag=v<version>

Publish the releases worth an update for the listing's users (a feature, a fix for something
broken): every listed version goes through AMO's review, which can take days, while the GitHub
`.xpi` of every release is usually there within minutes. Only the newest release can be
published, and the workflow refuses any other tag, one whose release workflow has not made its
GitHub release yet, and any tag before v0.14.2 (0.14.1 is a listed version of its own): AMO takes a listed version only when it is greater than the last
approved listed version (the unlisted ones do not count), and a new listed submission disables
every older listed version still waiting for its review, whatever its number. Publishing a
release while an earlier one waits therefore replaces that one in the queue; its number is gone
for good.

The workflow takes its scripts from its own commit and the add-on from the tag: it builds the
tag's `dist/firefox` and rewrites its manifest's version to `<version>.1`
(`scripts/amo-xpi.mjs listing`, that one line and nothing else), since the release's own number
belongs to its self-distributed build and AMO takes a number once, in either channel. It lints
that package and submits it to the listed channel with the tag's metadata from `make_metadata.py`
(`web-ext sign --channel listed --amo-metadata ... --approval-timeout 0`), which also rewrites the
listing texts from the files here, with `<version>` in the reviewer notes the tag's own number.
It returns once the version exists; the review follows, and its questions arrive by email and in
the Developer Hub. A run for a version AMO already has uploads nothing and ends green, so it can
be re-run, except for a version AMO has disabled (rejected, or replaced while it waited): that
number is never taken again, so the run fails and says to release the next patch version and
publish that. Firefox offers `<version>.1` to the GitHub installs of `<version>` too once it is
approved: the same code, now from the listing.

## By hand: `publish-addon.cmd` and `sign-addon.cmd`

`publish-addon.cmd` is the fallback when `amo-listing.yml` cannot run. Check out the newest
release's tag first (`git checkout v<version>`), then run it (it needs the AMO API key like
`sign-addon.cmd`): it refuses a tag that is not the newest and a tree that differs from the tag
(`addon/`, `docs/amo/` and the build script, uncommitted edits and untracked files included),
regenerates `amo-metadata.json`, builds `dist/firefox`, rewrites its version to `<version>.1`
like the workflow (and stops unless it reads that back) and runs `web-ext sign --channel listed`,
which uploads the build, creates the version with the release notes and reviewer notes, and
rewrites the listing text from the files here. It returns as soon as the version exists; approval
happens later. The privacy policy, icon and screenshots stay as set in the Developer Hub: a
changed `privacy-policy.md` is pasted there by hand (step 4 above).

`sign-addon.cmd` does the release workflow's signing by hand, for a release whose job cannot be
re-run: from the release's tag (it refuses a tree that differs from it), it builds `dist/firefox`,
uploads it to the unlisted channel and waits for the signed `.xpi`, which then goes onto the
release with `gh release upload v<version> dist/shisu_ko-<version>.xpi`.

## Installs from the GitHub releases

Every GitHub release's `.xpi` carries the same id as the listing and no `update_url`, so Firefox
asks AMO for its updates and moves it to the first listed version newer than its own; the
releases in between reach it through the popup's update banner, which links to the release page.
The unlisted `.xpi` files of 0.7.1 and 0.8.0 to 0.13.0 are the same kind of build. 0.14.0 has
none (AMO refused its listing texts). 0.14.1's only possible file is its listed one: once AMO
approves 0.14.1, `amo-xpi.yml` run by hand with `tag` `v0.14.1` attaches it (the schedule looks at
the newest release only), and a listed submission of a newer release before that approval
disables 0.14.1, which then keeps the zips alone. 0.14.2 has a tag and no release: its unlisted
upload waits for a human review on AMO, and its release was withdrawn. 0.14.3 is the first
release of the workflow that always attaches an `.xpi`.

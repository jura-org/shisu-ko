import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { decide } from "../cws.mjs";

// Which build goes where on addons.mozilla.org and the Chrome Web Store, a must-test
// (docs/dev/updates-and-release.md, "Release"). A tag makes a release and its .xpi, signed for
// self-distribution (the unlisted channel), and nothing more; the public listing is a workflow of
// its own, run by hand for the newest release, under <version>.1, since AMO takes a number once in
// either channel. A listed upload from the tag workflow would take the number the GitHub .xpi needs
// and put every release back into the listing's review queue. The Chrome Web Store gets every
// release by itself from a workflow of its own that the finished release starts, as the release's
// own Chrome zip, never a new build, and the tag workflow never holds the store's key; a schedule
// in a workflow of its own calls that one for the releases an older review held back.
// The checks read commands, not comments: a step commented out is a step gone.
const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8").replace(/\r\n/g, "\n");
const release = read(".github/workflows/release.yml");
const listing = read(".github/workflows/amo-listing.yml");
const attach = read(".github/workflows/amo-xpi.yml");
const store = read(".github/workflows/cws-listing.yml");
const catchUp = read(".github/workflows/cws-schedule.yml");
const tests = read(".github/workflows/tests.yml");
const publish = read("publish-addon.cmd");
const sign = read("sign-addon.cmd");

// The keys of a workflow's top-level `on:` block.
function triggers(yaml) {
  const lines = yaml.split("\n");
  const start = lines.indexOf("on:");
  assert.ok(start >= 0, "the workflow has an on: block");
  const keys = [];
  for (const line of lines.slice(start + 1)) {
    if (/^\S/.test(line)) break;
    const key = /^ {2}([\w-]+):/.exec(line);
    if (key) keys.push(key[1]);
  }
  return keys;
}

// One key's block under a workflow's top-level `on:` block, the key's own line first, up to the
// next line indented no deeper than the key; comment lines left out.
function trigger(yaml, name) {
  const lines = yaml.split("\n");
  const start = lines.indexOf(`  ${name}:`, lines.indexOf("on:"));
  assert.ok(start >= 0, `the workflow has no ${name} trigger`);
  const block = [lines[start]];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() && line.search(/\S/) <= 2) break;
    if (!line.trim().startsWith("#")) block.push(line);
  }
  return block.join("\n").trimEnd();
}

// The shell text of every run: step, one line or a block.
function runScripts(yaml) {
  const lines = yaml.split("\n");
  const scripts = [];
  lines.forEach((line, i) => {
    const step = /^(\s*)(?:- )?run: ?(.*)$/.exec(line);
    if (!step) return;
    if (!/^[|>]/.test(step[2])) {
      scripts.push(step[2]);
      return;
    }
    const indent = line.indexOf("run:");
    const body = [];
    for (const next of lines.slice(i + 1)) {
      if (next.trim() && next.search(/\S/) <= indent) break;
      body.push(next);
    }
    scripts.push(body.join("\n"));
  });
  return scripts;
}

// The command lines of every run: step, comments and blank lines left out, a line continued with
// a backslash joined to the next.
const shellLines = (yaml) => runScripts(yaml).join("\n").replace(/\\\n\s*/g, "").split("\n")
  .map((line) => line.trim()).filter((line) => line && !line.startsWith("#"));
// The command lines of a batch file, REM lines and blank lines left out.
const cmdLines = (text) => text.split("\n").map((line) => line.trim()).filter((line) => line && !/^@?rem\b/i.test(line));
const at = (lines, re) => lines.findIndex((line) => re.test(line));
const count = (lines, re) => lines.filter((line) => re.test(line)).length;

test("a tag makes the release and its self-distributed .xpi, and nothing for the listing", () => {
  assert.deepEqual(triggers(release), ["push"]);
  assert.match(release, /tags: \["v\[0-9\]\+\.\[0-9\]\+\.\[0-9\]\+"\]/);
  const lines = shellLines(release);
  // web-ext signs the package and waits for AMO's signature itself, as it did up to 0.13.0.
  assert.equal(count(lines, /^if npx --yes web-ext@\S+ sign --source-dir dist\/firefox --artifacts-dir dist --channel unlisted --no-input; then$/), 1);
  assert.equal(count(lines, /--approval-timeout/), 0);
  assert.equal(count(lines, /--channel listed/), 0);
  // Listing texts sent with a self-distributed upload would rewrite the public listing, and the
  // listing texts are no business of a release: a text AMO would refuse must not stop the .xpi.
  assert.equal(count(lines, /--amo-metadata|make_metadata/), 0);
  // Nor through the build tests: they run without the listing texts' own test.
  assert.equal(count(lines, /^npm (run )?test(:build)?$/), 0);
  assert.equal(count(lines, /^node --test \$\(ls scripts\/tests\/\*\.test\.mjs \| grep -v '\/amo-metadata\\\.test\\\.mjs\$'\)$/), 1);
  // A number AMO has not got is uploaded and signed by web-ext; an upload AMO never took fails the
  // job. A number AMO has (a re-run, or a signature that did not come within web-ext's wait) takes
  // AMO's signed file only when it holds this tag's build; none yet (a human review) is a warning,
  // and the release goes out with the unsigned .xpi.
  const missing = at(lines, /^if \[ "\$\(node scripts\/amo-xpi\.mjs status "\$version"\)" = missing \]; then$/);
  const sign = at(lines, /web-ext@\S+ sign /);
  const signed = at(lines, /^exit 0$/);
  const notTaken = lines.findIndex((line, i) => i > sign && /^if \[ "\$\(node scripts\/amo-xpi\.mjs status "\$version"\)" = missing \]; then$/.test(line));
  const fetch = at(lines, /^node scripts\/amo-xpi\.mjs fetch "\$version" --out dist --wait "\$wait" \|\| code=\$\?$/);
  const same = at(lines, /^0\) node scripts\/amo-xpi\.mjs same-build dist\/\*\.xpi dist\/firefox ;;$/);
  const unsignedWarning = at(lines, /^3\|4\) echo "::warning::/);
  const order = { missing, sign, signed, notTaken, fetch, same, unsignedWarning };
  for (const [name, index] of Object.entries(order)) assert.ok(index >= 0, `release.yml has no ${name} line`);
  const indices = Object.values(order);
  assert.deepEqual(indices, [...indices].sort((a, b) => a - b), "sign, or take AMO's file, in that order");
  assert.match(lines[notTaken + 1], /^echo "::error::/);
  assert.equal(lines[notTaken + 2], "exit 1");
  // Every release carries an .xpi: the signed one, or else the unsigned build under a name that
  // says so, with or without the AMO key (the step has no condition).
  const unsignedStep = release.indexOf("- name: Add the unsigned .xpi when there is no signed one");
  const signStep = release.indexOf("- name: Sign the Firefox package");
  const releaseStep = release.indexOf("- name: Create GitHub release");
  assert.ok(signStep >= 0 && unsignedStep > signStep && releaseStep > unsignedStep, "signed, then the unsigned stand-in, then the release");
  assert.match(release.slice(unsignedStep, releaseStep), /^- name: Add the unsigned \.xpi when there is no signed one\n {8}run: \|\n/);
  assert.equal(count(lines, /^if ! compgen -G "dist\/\*\.xpi" > \/dev\/null; then$/), 1);
  assert.equal(count(lines, /^cp "dist\/shisu-ko-\$\{GITHUB_REF_NAME#v\}-firefox\.zip" "dist\/shisu-ko-\$\{GITHUB_REF_NAME#v\}-firefox-unsigned\.xpi"$/), 1);
  assert.match(release.slice(releaseStep), /files: \|\n {12}dist\/\*\.zip\n {12}dist\/\*\.xpi\n/);
  assert.equal(count(lines, /gh release upload/), 0);
  // The Chrome Web Store is cws-listing.yml's business, started by the finished release: the store
  // holds a release back while an older one waits for its review, which the tag's one run cannot
  // wait out, and the tag workflow never holds the store's key.
  const commands = release.split("\n").filter((line) => !line.trim().startsWith("#"));
  assert.equal(count(commands, /cws\.mjs|chromewebstore\.googleapis\.com|CWS_/), 0);
});

test("the listing is its own workflow, run by hand for the newest release, as <version>.1", () => {
  assert.deepEqual(triggers(listing), ["workflow_dispatch"]);
  assert.match(listing, /\n {6}tag:\n {8}description: .+\n {8}required: true\n/);
  // The add-on comes from the tag, the scripts from the workflow's own commit.
  assert.match(listing, /- uses: actions\/checkout@v4\n {6}- uses: actions\/checkout@v4\n {8}with:\n {10}ref: refs\/tags\/\$\{\{ inputs\.tag \}\}\n {10}path: release\n/);
  const lines = shellLines(listing);
  const format = at(lines, /^if \[\[ ! "\$TAG" =~ \^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$ \]\]; then$/);
  // 0.14.1 is a listed version of its own: tags before 0.14.2 have no listed build to publish.
  const floor = at(lines, /^if \[ "\$\(printf '%s\\n' v0\.14\.2 "\$TAG" \| sort -V \| head -n 1\)" != v0\.14\.2 \]; then$/);
  const newest = at(lines, /^newest="\$\(gh release view -R "\$GITHUB_REPOSITORY" --json tagName --jq \.tagName\)"$/);
  const refuse = at(lines, /^if \[ "\$newest" != "\$TAG" \]; then$/);
  const build = at(lines, /^node release\/scripts\/build\.mjs --browser firefox$/);
  const stamp = at(lines, /^version="\$\(node scripts\/amo-xpi\.mjs listing release\/dist\/firefox\)"$/);
  const lint = at(lines, /^npx --yes web-ext@\S+ lint --source-dir release\/dist\/firefox$/);
  const state = at(lines, /^state="\$\(node scripts\/amo-xpi\.mjs status "\$LISTING_VERSION"\)"$/);
  const submit = at(lines, /web-ext@\S+ sign --source-dir release\/dist\/firefox --artifacts-dir release\/dist --channel listed --amo-metadata release\/docs\/amo\/amo-metadata\.json --approval-timeout 0 --no-input$/);
  const order = { format, floor, newest, refuse, build, stamp, lint, state, submit };
  for (const [name, index] of Object.entries(order)) assert.ok(index >= 0, `amo-listing.yml has no ${name} line`);
  const indices = Object.values(order);
  assert.deepEqual(indices, [...indices].sort((a, b) => a - b), "checked, built, stamped, linted, then submitted");
  // A number AMO has disabled is never taken again: that run fails rather than ending green.
  const disabled = at(lines, /^disabled\)$/);
  assert.ok(disabled > submit && /^echo "::error::/.test(lines[disabled + 1]) && lines[disabled + 2] === "exit 1");
  assert.equal(count(lines, /--channel unlisted/), 0);
});

test("the attach workflow only downloads what AMO signed", () => {
  assert.deepEqual(triggers(attach), ["schedule", "workflow_dispatch"]);
  const lines = shellLines(attach);
  assert.equal(count(lines, /web-ext/), 0);
  assert.equal(count(lines, /^if \[\[ ! "\$tag" =~ \^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$ \]\]; then$/), 1);
  // A release that is not there fails the step; it must not read as "no .xpi yet".
  assert.equal(count(lines, /^assets="\$\(gh release view "\$tag" --json assets --jq '\.assets\[\]\.name'\)"$/), 1);
  assert.equal(count(lines, /gh release view "\$tag".*\|/), 0);
  // The signed file goes on only when it is the release's own Firefox build.
  const download = at(lines, /^gh release download "\$tag" -p "shisu-ko-\$\{tag#v\}-firefox\.zip" -D dist\/release$/);
  const same = at(lines, /^if ! node scripts\/amo-xpi\.mjs same-build dist\/signed\/\*\.xpi "dist\/release\/shisu-ko-\$\{tag#v\}-firefox\.zip"; then$/);
  const upload = at(lines, /^gh release upload "\$tag" dist\/signed\/\*\.xpi --clobber$/);
  assert.ok(download >= 0 && same > download && upload > same && lines[same + 2] === "exit 1");
  // The unsigned stand-in is no signed .xpi: it is looked past, and removed once the signed one
  // is on the release.
  assert.equal(count(lines, /^unsigned="shisu-ko-\$\{tag#v\}-firefox-unsigned\.xpi"$/), 1);
  assert.equal(count(lines, /^if grep -v -x -F "\$unsigned" <<<"\$assets" \| grep -q '\\\.xpi\$'; then$/), 1);
  const removeUnsigned = at(lines, /^gh release delete-asset "\$tag" "\$unsigned" --yes$/);
  assert.ok(removeUnsigned > upload, "the unsigned stand-in goes once the signed file is on");
  // No file on AMO yet is a warning (the release job may still be uploading, or said so already);
  // a rejection fails the run, since the release job has usually ended green before it came.
  const four = at(lines, /^4\)$/);
  assert.equal(lines[four + 1], 'state="$(node scripts/amo-xpi.mjs status "${tag#v}")"');
  assert.equal(lines[four + 2], 'if [ "$state" = disabled ]; then');
  assert.match(lines[four + 3], /^echo "::error::/);
  assert.equal(lines[four + 4], "exit 1");
  assert.match(lines[four + 6], /^echo "::warning::/);
});

test("every release goes to the Chrome Web Store by itself, the newest one, as the release's own zip", () => {
  // The finished release starts it, cws-schedule.yml calls it to submit a release that an older
  // review held back, and a run by hand can put the newest release in that review's place. It has
  // no schedule of its own: GitHub switches off a workflow with a schedule after 60 days without
  // activity, and a workflow switched off no longer starts for a release either.
  assert.deepEqual(triggers(store), ["workflow_run", "workflow_call", "workflow_dispatch"]);
  // Both ways in declare cancel_review as the same boolean, off unless asked for, so that
  // inputs.cancel_review means the same whether the schedule calls or a person runs it.
  for (const name of ["workflow_call", "workflow_dispatch"]) {
    assert.match(trigger(store, name), /^ {2}[\w-]+:\n {4}inputs:\n {6}cancel_review:\n {8}description: .+\n {8}type: boolean\n {8}default: false$/, name);
  }
  // workflow_run names the release workflow by its name:, so a rename there would leave every
  // release to the next scheduled run, up to three hours late, without a word. Nor may it filter by
  // branch: a release run starts from a tag, and its head_branch is the tag, never main.
  const releaseName = /^name: (.+)$/m.exec(release);
  assert.ok(releaseName, "release.yml has a name");
  const started = /\n {2}workflow_run:\n(?: {4}#.*\n)* {4}workflows: (.+)\n {4}types: (.+)\n(?! {4})/.exec(store);
  assert.ok(started, "cws-listing.yml names the workflow and the event that start it, and nothing more (a branches filter misses every release)");
  assert.deepEqual(JSON.parse(started[1]), [releaseName[1]]);
  assert.equal(started[2], "[completed]");
  // A release workflow that failed made no release; the schedule still looks after the newest.
  // workflow_run matches by name alone, and a pull request from a fork can name a workflow of its
  // own "Release extensions": only a run the push of a tag in this repository started is a release.
  assert.match(store, /\n {4}if: github\.event_name != 'workflow_run' \|\| \(github\.event\.workflow_run\.conclusion == 'success' && github\.event\.workflow_run\.event == 'push' && github\.event\.workflow_run\.head_repository\.full_name == github\.repository\)\n/);
  // A waiting review is withdrawn only when a run by hand asks for it. One run at a time, and the
  // others wait their turn in order: none is cancelled, running or waiting. GitHub's default queue
  // keeps one run waiting and cancels it for the next, which would drop a run by hand that asked
  // for cancel_review. The group is the job's, so that a run whose job is skipped never takes a
  // place in the queue, which holds 100 and cancels whatever comes after. The schedule's call
  // waits in the same group, which holds across the repository's workflows.
  assert.match(store, /\n {4}concurrency:\n {6}group: cws-listing\n {6}cancel-in-progress: false\n {6}queue: max\n/);
  assert.doesNotMatch(store, /^concurrency:/m);
  // Every step runs in GitHub's default `bash -e`, and a failed step fails the job: a status or
  // submit that fails must not fall through to the `*)` branch and end green.
  assert.doesNotMatch(store, /^\s*(?:- )?(?:continue-on-error|shell|defaults):/m);
  // Without the key and the publisher id the run does nothing: every step but the check waits
  // for both.
  assert.match(store, /\n {6}CWS_KEY: \$\{\{ secrets\.CWS_SERVICE_ACCOUNT_JSON != '' && vars\.CWS_PUBLISHER_ID != '' \}\}\n/);
  const steps = store.slice(store.indexOf("\n    steps:\n")).split(/\n {6}- /).slice(1);
  assert.match(steps[0], /^name: Check the key\n {8}if: env\.CWS_KEY != 'true'\n/);
  for (const step of steps.slice(1)) assert.match(step, /\n {8}if: env\.CWS_KEY == 'true'$/m, step);
  // The release with the highest version, whatever started the run, taken only with a release
  // tag, and its own Chrome zip; the store is asked what it holds before anything goes up. GitHub's
  // latest release (gh release view with no tag) is the one published last, which a re-run of an
  // older tag's release makes the older version: the store would then never get the newer one.
  const lines = shellLines(store);
  assert.equal(count(lines, /gh release view/), 0);
  const list = at(lines, /^tags="\$\(gh release list -R "\$GITHUB_REPOSITORY" --exclude-drafts --exclude-pre-releases --limit 100 --json tagName --jq '\.\[\]\.tagName'\)"$/);
  const newest = at(lines, /^tag="\$\(grep -E '\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$' <<<"\$tags" \| sort -V \| tail -n 1\)"$/);
  const format = at(lines, /^if \[\[ ! "\$tag" =~ \^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$ \]\]; then$/);
  const download = at(lines, /^gh release download "\$tag" -R "\$GITHUB_REPOSITORY" -p "shisu-ko-\$\{tag#v\}-chrome\.zip" -D dist\/release$/);
  const version = at(lines, /^echo "VERSION=\$\{tag#v\}" >> "\$GITHUB_ENV"$/);
  const state = at(lines, /^state="\$\(node scripts\/cws\.mjs status "\$VERSION"\)"$/);
  const submit = at(lines, /^if ! state="\$\(node scripts\/cws\.mjs submit "dist\/release\/shisu-ko-\$VERSION-chrome\.zip" "\$VERSION"\)"; then$/);
  const order = { list, newest, format, download, version, state, submit };
  for (const [name, index] of Object.entries(order)) assert.ok(index >= 0, `cws-listing.yml has no ${name} line`);
  const indices = Object.values(order);
  assert.deepEqual(indices, [...indices].sort((a, b) => a - b), "the releases listed, the highest tag, checked, downloaded, asked about, then submitted");
  assert.match(lines[format + 1], /^echo "::error::/);
  assert.equal(lines[format + 2], "exit 1");
  // `set +e` would let a failed status or submit end green as surely as continue-on-error.
  assert.equal(count(lines, /^set\s+(?:\+\w*e\w*|\+o\s+errexit)\b/), 0);
  // The submit step is the status and one case on it, nothing after it, and the case is held as a
  // whole, every branch in this order: bash takes the first pattern that matches, so a branch
  // added or a *) moved up changes what a state does, and one line added to a branch can fail
  // every scheduled run. A label is taken wherever a line starts with one, on a one-line branch too.
  const opened = lines.indexOf('case "$state" in', state);
  const closed = lines.indexOf("esac", opened);
  assert.deepEqual([opened, closed], [state + 1, lines.length - 1], "the submit step is the status and one case on it");
  const branches = [];
  for (const line of lines.slice(opened + 1, closed)) {
    const label = /^([\w*|-]+)\)/.exec(line);
    if (label) branches.push({ label: label[1], lines: [] });
    branches.at(-1)?.lines.push(line);
  }
  // Each label is the word scripts/cws.mjs prints for what it names: a word renamed there would
  // fall through to *) and end green, and a "waiting" renamed would leave cancel_review doing
  // nothing without a word. A "rejected-older" without its branch would fall through to *) too,
  // and the release would never reach the store.
  const revision = (kind, crxVersion) => ({ state: kind, distributionChannels: [{ crxVersion }] });
  const words = {
    submit: decide({}, "1.0.0"),
    waiting: decide({ submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.9.0") }, "1.0.0"),
    staged: decide({ submittedItemRevisionStatus: revision("STAGED", "1.0.0") }, "1.0.0"),
    rejected: decide({ submittedItemRevisionStatus: revision("REJECTED", "1.0.0") }, "1.0.0"),
    "rejected-older": decide({ submittedItemRevisionStatus: revision("REJECTED", "0.9.0") }, "1.0.0"),
    "taken-down": decide({ takenDown: true }, "1.0.0"),
  };
  for (const [label, word] of Object.entries(words)) assert.equal(word, label, `scripts/cws.mjs no longer prints ${label}, which cws-listing.yml acts on`);
  assert.deepEqual(branches.map((branch) => branch.label), [...Object.keys(words), "*"]);
  // A branch's lines with the text of its messages left out: what runs, fails, warns or tells is
  // held, and the words may change. A message ends at its own closing quote, so that a command
  // appended after it (`; exit "1"`) stays in the shape and fails the comparison.
  const shape = (branch) => branch.lines.map((line) => line
    .replace(/^(echo "::\w+::)(?!\$message"$)[^"]*"$/, '$1..."')
    .replace(/^message="[^"]*"$/, 'message="..."'));
  const upload = (flag) => `if ! state="$(node scripts/cws.mjs submit "dist/release/shisu-ko-$VERSION-chrome.zip" "$VERSION"${flag})"; then`;
  // A rejection or a take-down needs a person: it fails a release's run and a run by hand. The
  // schedule meets it eight times a day until the next release or the end of the take-down, and
  // the store emails the developer about both, so there it warns and ends green.
  const needsPerson = (label) => [`${label})`, 'message="..."', 'if [ "$EVENT" = schedule ]; then', 'echo "::warning::$message"', "else", 'echo "::error::$message"', "exit 1", "fi", ";;"];
  assert.deepEqual(branches.map(shape), [
    // "submit" uploads as it is. An upload or a publish the store refuses fails the run, the
    // schedule's too, with an error that says what to do: nothing else tells anyone.
    ["submit)", upload(""), 'echo "::error::..."', "exit 1", "fi", 'echo "::notice::..."', ";;"],
    // A review that waits for an older version is withdrawn only inside the branch that asks
    // CANCEL_REVIEW, which the dispatch form alone sets; otherwise the release waits for the
    // schedule, and that is no failure.
    ["waiting)", 'if [ "$CANCEL_REVIEW" = true ]; then', upload(" --cancel-review"), 'echo "::error::..."', "exit 1", "fi",
      'echo "::notice::..."', "else", 'echo "::notice::..."', "fi", ";;"],
    // This version approved and staged waits for a person to publish it in the Developer
    // Dashboard: a warning, not a failure, since nothing is wrong with it.
    ["staged)", 'echo "::warning::..."', ";;"],
    needsPerson("rejected"),
    // An older version rejected holds nothing back, but this one may have been released while it
    // was in review and repeat what was rejected, so it goes up only on a person's decision: a run
    // by hand, or the first run of this version's own release, which uploads as "submit" does,
    // failing the same way when the store refuses it, and then warns to read the review. The
    // schedule, which no person starts, a re-run of a release workflow (which the download step
    // meets with the highest release whatever the tag) and another tag's release only warn.
    ["rejected-older)", 'if [ "$EVENT" = workflow_dispatch ] || { [ "$EVENT" = workflow_run ] && [ "$RELEASE_TAG" = "v$VERSION" ] && [ "$RUN_ATTEMPT" = 1 ]; }; then',
      upload(""), 'echo "::error::..."', "exit 1", "fi", 'echo "::warning::..."', "else", 'echo "::warning::..."', "fi", ";;"],
    needsPerson("taken-down"),
    // Anything else is the store busy with this version or past it, which the schedule meets
    // eight times a day while a review runs, or a review of it cancelled in the Dashboard, which
    // is left alone until the next release: no failure.
    ["*)", 'echo "::notice::..."', ";;"],
  ]);
  const branch = (label) => branches.find((each) => each.label === label).lines;
  assert.equal(count(lines, /--cancel-review/), 1);
  assert.match(store, /\n {10}CANCEL_REVIEW: \$\{\{ inputs\.cancel_review \}\}\n/);
  // "waiting" is also an older version approved and staged, which no review ends: only a person
  // publishing it in the Developer Dashboard does, and the notice says so.
  assert.match(branch("waiting")[8], /staged.*Developer Dashboard/);
  // Nor does the schedule submit once the store rejects the older version: it only warns then, and
  // the notice a release reads while it waits says so rather than promising the schedule does it.
  assert.match(branch("waiting")[8], /rejects.*only warns/);
  // The schedule's runs are listed under cws-schedule.yml, which cannot be run by hand, so the
  // notice names the workflow and the command, as the rejected-older warning does.
  assert.match(branch("waiting")[8], /run cws-listing\.yml by hand with cancel_review \(gh workflow run cws-listing\.yml -f cancel_review=true\)/);
  assert.doesNotMatch(branch("waiting")[8], /run this workflow by hand/);
  assert.match(branch("staged")[1], /Developer Dashboard/);
  // A refused upload or publish is fixed in the Developer Dashboard, and the errors say so.
  const error = (label) => branch(label).find((line) => line.startsWith('echo "::error::'));
  for (const label of ["submit", "waiting", "rejected-older"]) assert.match(error(label), /Developer Dashboard/);
  // "rejected-older" uploads only in a run by hand or in the first run of this version's own
  // release: a re-run of any tag's release workflow, another tag's release and the schedule reach
  // the warning. That arm never uploads and never fails, and says to read the review and then run
  // cws-listing.yml by hand or release the next patch version. The upload arm cancels nothing, a
  // refusal fails the run, and a submission warns to read the review. Its error sends no one to
  // wait for the schedule, which never submits past the rejection, but to a run by hand.
  const older = branch("rejected-older");
  const personArm = older.slice(2, older.indexOf("else"));
  const warnArm = older.slice(older.indexOf("else") + 1);
  assert.equal(older[1], 'if [ "$EVENT" = workflow_dispatch ] || { [ "$EVENT" = workflow_run ] && [ "$RELEASE_TAG" = "v$VERSION" ] && [ "$RUN_ATTEMPT" = 1 ]; }; then');
  assert.equal(count(warnArm, /cws\.mjs submit|(?:^|[;&|]\s*)exit\b/), 0);
  assert.match(warnArm[0], /^echo "::warning::.*Developer Dashboard.*gh workflow run cws-listing\.yml.*next patch version/);
  assert.deepEqual(personArm.map((line) => line.replace(/^(echo "::(?:error|warning)::)[^"]*"$/, '$1..."')), [upload(""), 'echo "::error::..."', "exit 1", "fi", 'echo "::warning::..."']);
  assert.match(personArm[4], /^echo "::warning::.*Developer Dashboard.*next patch version/);
  assert.equal(count(personArm, /--cancel-review/), 0);
  assert.doesNotMatch(error("rejected-older"), /the schedule (?:uploads|submits)|within three hours/);
  assert.match(error("rejected-older"), /run this workflow by hand/);
  // The store lists an item taken down again only after an appeal or a fixed version submitted in
  // the Dashboard, which this workflow never makes: the message says to do that by hand, not to wait.
  assert.match(branch("taken-down")[1], /appeal.*by hand/);
  // The lines of a case branch, up to its ;;.
  const branchOf = (from) => lines.slice(from + 1, lines.indexOf(";;", from));
  // Without the key: the schedule passes quietly, a release warns, a run by hand fails.
  const schedule = at(lines, /^schedule\)$/);
  const dispatch = at(lines, /^workflow_dispatch\)$/);
  const unset = lines.indexOf("*)");
  assert.ok(schedule >= 0 && dispatch > schedule && unset > dispatch && unset < list, "Check the key has its three branches");
  assert.equal(count(branchOf(schedule), /^exit\b|::(error|warning)::/), 0);
  assert.match(branchOf(dispatch)[0], /^echo "::error::/);
  assert.deepEqual(branchOf(dispatch).slice(1), ["exit 1"]);
  assert.match(branchOf(unset)[0], /^echo "::warning::/);
  assert.equal(count(branchOf(unset), /^exit\b/), 0);
  // Nothing is built again, and no action but GitHub's own two runs in the job that has the key.
  assert.equal(count(lines, /build\.mjs|web-ext|\bnpm\b|\bnpx\b/), 0);
  const uses = [...store.matchAll(/^ *(?:- )?uses: (\S+)/gm)].map((match) => match[1]);
  assert.deepEqual(uses, ["actions/checkout@v4", "actions/setup-node@v4"]);
  // The key reaches one step, the one that talks to the store.
  const secret = "${{ secrets.CWS_SERVICE_ACCOUNT_JSON }}";
  assert.equal(store.split(secret).length - 1, 1);
  const submitStep = steps.find((step) => step.startsWith("name: Submit it to the Chrome Web Store\n"));
  assert.ok(submitStep?.includes(secret), "the key is in the submit step's env");
  // What started the run reaches that step's shell through env, as everywhere else, and so does
  // the release run behind a workflow_run: its tag (a tag push's head_branch is the tag) and its
  // attempt, both empty for the schedule and a run by hand, which "rejected-older" reads.
  assert.match(submitStep, /\n {10}EVENT: \$\{\{ github\.event_name \}\}\n/);
  assert.match(submitStep, /\n {10}RELEASE_TAG: \$\{\{ github\.event\.workflow_run\.head_branch \}\}\n/);
  assert.match(submitStep, /\n {10}RUN_ATTEMPT: \$\{\{ github\.event\.workflow_run\.run_attempt \}\}\n/);
});

test("the schedule for held-back releases is a workflow of its own that only calls the store's", () => {
  // GitHub switches off a public repository's workflow that has a schedule after 60 days without
  // activity, and a workflow switched off starts for nothing. Kept in a file of its own, the
  // schedule takes only the catch-up of held-back releases with it, never the release's own run.
  assert.deepEqual(triggers(catchUp), ["schedule"]);
  // Every three hours, one cron: a release an older review held back waits for it, and a rarer
  // schedule would keep that release from the store for as long. The block is held whole, so a
  // second cron counts after a comment or a blank line too.
  assert.equal(trigger(catchUp, "schedule"), '  schedule:\n    - cron: "47 */3 * * *"');
  // It does nothing of its own: no step, no shell, and one job that calls cws-listing.yml, which
  // checks the key, asks the store and uploads exactly as for a release. The key reaches the call
  // through secrets: inherit (the publisher id is a repository variable, which a called workflow
  // reads from its caller's repository).
  const commands = catchUp.split("\n").filter((line) => !line.trim().startsWith("#")).join("\n");
  assert.equal(runScripts(catchUp).length, 0);
  assert.doesNotMatch(commands, /^\s*(?:- )?(?:run|steps|runs-on):/m);
  const jobs = commands.slice(commands.indexOf("\njobs:\n") + 1).split("\n").slice(1).filter((line) => /^ {2}\S/.test(line));
  assert.equal(jobs.length, 1, "cws-schedule.yml has one job");
  const uses = [...commands.matchAll(/^ *(?:- )?uses: (\S+)/gm)].map((match) => match[1]);
  assert.deepEqual(uses, ["./.github/workflows/cws-listing.yml"]);
  assert.match(commands, /\n {4}uses: \.\/\.github\/workflows\/cws-listing\.yml\n/);
  assert.match(commands, /\n {4}secrets: inherit\n/);
  // A called workflow gets no more than its caller grants, and reading the releases is all
  // cws-listing.yml needs: read access, on the workflow and on the job that calls. Both blocks are
  // held whole with blank lines left out, the job's to the end of the file: GitHub also allows
  // if:, strategy:, needs: and more on a calling job, and an if: would silence the catch-up
  // without a word, a matrix would call cws-listing.yml twice, and a permission added after a
  // blank line would widen what the call may do.
  const block = (from, to) => commands.slice(from, to).split("\n").filter((line) => line.trim());
  assert.deepEqual(block(commands.indexOf("\npermissions:\n") + 1, commands.indexOf("\njobs:\n")), ["permissions:", "  contents: read"]);
  assert.deepEqual(block(commands.indexOf("\njobs:\n") + 1), ["jobs:", "  catch-up:", "    uses: ./.github/workflows/cws-listing.yml",
    "    secrets: inherit", "    permissions:", "      contents: read"]);
  // The schedule never withdraws a review: it passes no inputs, so cancel_review keeps its default,
  // false. The called job waits its turn in cws-listing.yml's own concurrency group, which holds
  // across the repository's workflows, so the call needs none.
  assert.doesNotMatch(commands, /^\s*with:|cancel_review|concurrency:/m);
  // When GitHub switches the schedule off, a person reads the headers for what to turn back on:
  // they name this workflow, never cws-listing.yml, which has no schedule and stays switched on.
  for (const yaml of [release, store, catchUp]) assert.doesNotMatch(yaml, /gh workflow enable cws-listing\.yml/);
  for (const yaml of [release, catchUp]) assert.match(yaml, /gh workflow enable cws-schedule\.yml/);
});

test("no workflow expands an expression inside a shell script", () => {
  // A tag typed into the dispatch form is text from outside: it reaches the shell through env.
  for (const [name, yaml] of [["release.yml", release], ["amo-listing.yml", listing], ["amo-xpi.yml", attach], ["cws-listing.yml", store], ["cws-schedule.yml", catchUp], ["tests.yml", tests]]) {
    for (const script of runScripts(yaml)) assert.doesNotMatch(script, /\$\{\{/, `${name}: ${script}`);
  }
});

test("web-ext runs at one pinned version wherever it gets the AMO key or lints", () => {
  const versions = new Set();
  const all = [release, listing, attach, store, catchUp, tests].flatMap(shellLines).concat(cmdLines(publish), cmdLines(sign));
  for (const line of all.filter((l) => /\bweb-ext\b/.test(l))) {
    const pinned = /npx --yes web-ext@(\d+\.\d+\.\d+) /.exec(line);
    assert.ok(pinned, `not pinned to an exact version: ${line}`);
    versions.add(pinned[1]);
  }
  assert.equal(versions.size, 1, [...versions].join(", "));
});

test("the manual scripts check the tag, then build, stamp and submit like the workflows", () => {
  const lines = cmdLines(publish);
  // Nothing but digits and dots is expanded: the version is taken only when it is major.minor.patch,
  // and the tags only when they are v, digits and dots (git allows & | < > in a tag name).
  const versionLine = /^for \/f "delims=" %%v in \('node -p "const v = require\('\.\/addon\/manifest\.json'\)\.version; \/\^\[0-9\]\+\[\.\]\[0-9\]\+\[\.\]\[0-9\]\+\$\/\.test\(v\) \? v : ''"'\) do set "VERSION=%%v"$/;
  const version = at(lines, versionLine);
  const fetch = at(lines, /^git fetch --quiet --tags origin \|\| \($/);
  const newest = at(lines, /^for \/f "delims=" %%t in \('git tag --list "v\[0-9\]\*" --sort=-v:refname \^\| findstr \/r \/v \/c:"\[\^v0-9\.\]"'\) do if not defined NEWEST set "NEWEST=%%t"$/);
  const newestCheck = at(lines, /^if not "%NEWEST%"=="v%VERSION%" \($/);
  const tree = at(lines, /^git diff --quiet "v%VERSION%" -- addon docs\\amo scripts\\build\.mjs \|\| \($/);
  const untracked = at(lines, /^for \/f "delims=" %%f in \('git ls-files --others --exclude-standard -- addon'\) do \($/);
  const build = at(lines, /^node scripts\\build\.mjs --browser firefox \|\| \(pause & exit \/b 1\)$/);
  const stamp = at(lines, /^for \/f "delims=" %%v in \('node scripts\\amo-xpi\.mjs listing dist\\firefox'\) do set "LISTED=%%v"$/);
  const stamped = at(lines, /^if not "%LISTED%"=="%VERSION%\.1" \($/);
  const submit = at(lines, /^npx --yes web-ext@\S+ sign --source-dir dist\\firefox --artifacts-dir dist --channel listed --amo-metadata docs\\amo\\amo-metadata\.json --approval-timeout 0 --no-input$/);
  const order = { version, fetch, newest, newestCheck, tree, untracked, build, stamp, stamped, submit };
  for (const [name, index] of Object.entries(order)) assert.ok(index >= 0, `publish-addon.cmd has no ${name} line`);
  const indices = Object.values(order);
  assert.deepEqual(indices, [...indices].sort((a, b) => a - b), "publish-addon.cmd checks, builds, stamps, then submits");
  assert.equal(count(lines, /--source-dir addon/), 0);

  const signing = cmdLines(sign);
  assert.equal(count(signing, versionLine), 1);
  const signTree = at(signing, /^git diff --quiet "v%VERSION%" -- addon scripts\\build\.mjs \|\| \($/);
  const signBuild = at(signing, /^node scripts\\build\.mjs --browser firefox \|\| \(pause & exit \/b 1\)$/);
  const signSubmit = at(signing, /^npx --yes web-ext@\S+ sign --source-dir dist\\firefox --artifacts-dir dist --channel unlisted --no-input$/);
  assert.ok(signTree >= 0 && signBuild > signTree && signSubmit > signBuild, "sign-addon.cmd checks the tag, builds, then signs");
  assert.equal(count(signing, /--channel listed|--source-dir addon/), 0);
});

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// The listing texts a release is published with (amo-listing.yml, by hand, from its tag). AMO
// refuses a version whose release notes or reviewer notes run past 3,000 characters, and says so
// only when the version is submitted (0.14.0 was refused that way: 8,920 and 18,844 characters),
// so the limits are held here, in `npm test` and in CI, and make_metadata.py refuses them too. AMO
// counts characters, not bytes, which is what String.length counts for these texts (no character
// outside the BMP).
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const AMO = join(ROOT, "docs", "amo");
const NOTES_LIMIT = 3000;
const PYTHON = process.platform === "win32" ? "python" : "python3";

const read = (name) => readFileSync(join(AMO, name), "utf8").trim();
const version = JSON.parse(readFileSync(join(ROOT, "addon", "manifest.json"), "utf8")).version;

test("the release notes fit AMO's 3,000 characters", () => {
  const notes = read("release-notes.md");
  assert.ok(notes.length <= NOTES_LIMIT, `release-notes.md is ${notes.length} characters; AMO allows ${NOTES_LIMIT}`);
});

test("the reviewer notes fit AMO's 3,000 characters with the version filled in", () => {
  // make_metadata.py fills <version> in; a longer version string must not tip it over.
  const notes = read("reviewer-notes.md").replaceAll("<version>", version);
  assert.ok(notes.length <= NOTES_LIMIT, `reviewer-notes.md is ${notes.length} characters; AMO allows ${NOTES_LIMIT} (the full guide is reviewer-guide.md)`);
});

test("the summary and the description fit AMO's limits", () => {
  const summary = read("summary.txt");
  assert.ok(summary.length <= 250, `summary.txt is ${summary.length} characters; AMO allows 250`);
  assert.ok(!/https?:\/\/|www\./.test(summary), "summary.txt must not hold a URL");
  assert.ok(read("description.md").length <= 15000, "description.md is longer than AMO's 15,000 characters");
});

test("the reviewer notes link the full guide at the version's tag", () => {
  assert.match(read("reviewer-notes.md"), /blob\/v<version>\/docs\/amo\/reviewer-guide\.md/);
  assert.ok(read("reviewer-guide.md").length > NOTES_LIMIT, "reviewer-guide.md holds what the notes leave out");
});

// make_metadata.py itself, on a copy of docs/amo: it must refuse what AMO would refuse, before a
// tag is pushed, with a message that names the file.
function runMakeMetadata(files) {
  const dir = mkdtempSync(join(tmpdir(), "amo-metadata-"));
  try {
    mkdirSync(join(dir, "docs", "amo"), { recursive: true });
    mkdirSync(join(dir, "addon"));
    copyFileSync(join(ROOT, "addon", "manifest.json"), join(dir, "addon", "manifest.json"));
    for (const name of ["make_metadata.py", "summary.txt", "description.md", "release-notes.md", "reviewer-notes.md"]) {
      copyFileSync(join(AMO, name), join(dir, "docs", "amo", name));
    }
    for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, "docs", "amo", name), text, "utf8");
    return spawnSync(PYTHON, [join(dir, "docs", "amo", "make_metadata.py")], { encoding: "utf8" });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("make_metadata.py accepts the texts as they are", (t) => {
  const run = runMakeMetadata({});
  if (run.error) return t.skip(`no ${PYTHON} on PATH`);
  assert.equal(run.status, 0, run.stderr);
});

test("make_metadata.py refuses release notes over 3,000 characters", (t) => {
  const run = runMakeMetadata({ "release-notes.md": `- ${version}: ${"x".repeat(NOTES_LIMIT)}\n` });
  if (run.error) return t.skip(`no ${PYTHON} on PATH`);
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, /release-notes\.md is \d+ characters, AMO allows 3000/);
});

test("make_metadata.py refuses reviewer notes over 3,000 characters", (t) => {
  const run = runMakeMetadata({ "reviewer-notes.md": "y".repeat(NOTES_LIMIT + 1) });
  if (run.error) return t.skip(`no ${PYTHON} on PATH`);
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, /reviewer-notes\.md is \d+ characters/);
});

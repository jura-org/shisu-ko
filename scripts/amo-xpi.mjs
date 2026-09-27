#!/usr/bin/env node
// Asks addons.mozilla.org about one version of the add-on and downloads its signed .xpi once AMO
// has signed it (approved it, in AMO's words, which for a self-distributed version is its automatic
// validation), and prepares a build for the public listing. The release workflows use it: the
// upload returns before AMO has signed the version, so the signed file for the GitHub release is
// fetched afterwards. Node only, no packages.
//
//   node scripts/amo-xpi.mjs status <version>
//     prints missing, pending, public or AMO's own file status (disabled: rejected)
//   node scripts/amo-xpi.mjs fetch <version> [--out <dir>] [--wait <seconds>]
//     downloads the signed file into <dir> (default dist/signed) and prints its path; exit 3 when
//     AMO has not signed the version by the end of the wait (default 0: ask once), exit 4 when AMO
//     has no file to sign (missing: never uploaded, or still validating; disabled: rejected)
//   node scripts/amo-xpi.mjs listing <dir>
//     rewrites the version in <dir>/manifest.json for the public listing (0.15.0 -> 0.15.0.1) and
//     prints it: every release is signed for self-distribution under its own number, and AMO takes
//     a number once, in either channel, so the release's listed version is its number plus ".1"
//   node scripts/amo-xpi.mjs same-build <signed .xpi> <build folder or zip>
//     exit 0 when the signed file holds exactly the build (AMO's META-INF/ aside), else lists the
//     differences and exits 1: AMO keeps the file of a number's first upload, so a re-run for a
//     tag that moved, or a number signed by hand from other code, would attach the wrong build
//
// status and fetch need the AMO API key that web-ext sign takes: WEB_EXT_API_KEY and
// WEB_EXT_API_SECRET.
import { createHmac, randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";
import { mkdir, readdir, readFile, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { inflateRawSync } from "node:zlib";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";

export const ADDON_ID = "shisu-ko@multysquid.github.io";
export const API = "https://addons.mozilla.org/api/v5/";
export const PENDING_EXIT = 3;
export const NO_FILE_EXIT = 4;
const POLL_MS = 30_000;
const VERSION_RE = /^\d+(\.\d+){1,3}$/;
const RELEASE_VERSION_RE = /^\d+\.\d+\.\d+$/;
export const LISTING_SUFFIX = ".1";
const FILE_NAME_RE = /^[A-Za-z0-9._-]+\.xpi$/;

// AMO's API key is a JWT issuer and secret: every request carries a fresh HS256 token that
// lives a minute (AMO refuses one that lives longer than five).
export function jwt(key, secret, now = Date.now()) {
  const part = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const iat = Math.floor(now / 1000);
  const unsigned = `${part({ alg: "HS256", typ: "JWT" })}.${part({ iss: key, jti: randomUUID(), iat, exp: iat + 60 })}`;
  return `${unsigned}.${createHmac("sha256", secret).update(unsigned).digest("base64url")}`;
}

export function versionUrl(version) {
  const plain = String(version).replace(/^v/, "");
  if (!VERSION_RE.test(plain)) throw new Error(`not a version number: ${version}`);
  return `${API}addons/addon/${encodeURIComponent(ADDON_ID)}/versions/${plain}/`;
}

// A release is tagged v<major>.<minor>.<patch>; its build for the listing is that number plus
// LISTING_SUFFIX, which Firefox orders right after it and before the next release.
export function listingVersion(version) {
  const plain = String(version);
  if (!RELEASE_VERSION_RE.test(plain)) throw new Error(`not a release version (major.minor.patch): ${version}`);
  return plain + LISTING_SUFFIX;
}

// Only the number changes: the rest of the file stays byte for byte what the release's tag holds,
// so a reviewer comparing the upload with the repository finds that one line.
export function listingManifest(text) {
  const manifest = JSON.parse(text);
  const version = listingVersion(manifest.version);
  const entry = /("version"\s*:\s*")(\d+\.\d+\.\d+)(")/g;
  const found = [...text.matchAll(entry)].filter((match) => match[2] === manifest.version);
  if (found.length !== 1) throw new Error(`expected one "version" entry for ${manifest.version}, found ${found.length}`);
  const at = found[0].index + found[0][1].length;
  const next = text.slice(0, at) + version + text.slice(at + manifest.version.length);
  if (!isDeepStrictEqual(JSON.parse(next), { ...manifest, version })) throw new Error("the manifest changed beyond its version");
  return { text: next, version };
}

// The files of a zip, name -> bytes: the stored and deflated entries of a small archive (no zip64,
// no encryption), which is what web-ext, AMO's signing and scripts/build.mjs write. Folders are
// left out.
export function zipEntries(buffer) {
  const end = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (end < 0 || end + 22 > buffer.length) throw new Error("not a zip file");
  const count = buffer.readUInt16LE(end + 10);
  let at = buffer.readUInt32LE(end + 16);
  const files = new Map();
  for (let i = 0; i < count; i++) {
    if (at + 46 > buffer.length || buffer.readUInt32LE(at) !== 0x02014b50) throw new Error("broken zip directory");
    const method = buffer.readUInt16LE(at + 10);
    const size = buffer.readUInt32LE(at + 20);
    const nameLength = buffer.readUInt16LE(at + 28);
    const local = buffer.readUInt32LE(at + 42);
    const name = buffer.subarray(at + 46, at + 46 + nameLength).toString("utf8");
    at += 46 + nameLength + buffer.readUInt16LE(at + 30) + buffer.readUInt16LE(at + 32);
    if (name.endsWith("/")) continue;
    if (local + 30 > buffer.length || buffer.readUInt32LE(local) !== 0x04034b50) throw new Error(`broken zip entry ${name}`);
    const start = local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28);
    const data = buffer.subarray(start, start + size);
    if (method === 0) files.set(name, data);
    else if (method === 8) files.set(name, inflateRawSync(data));
    else throw new Error(`unsupported compression ${method} for ${name}`);
  }
  return files;
}

// A build folder's files, name -> bytes, as web-ext packs it: dotfiles left out (web-ext sign
// writes its .amo-upload-uuid into the folder it signs).
export async function folderEntries(dir, prefix = "") {
  const files = new Map();
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".")) continue;
    const path = join(dir, entry.name);
    const name = prefix + entry.name;
    if (entry.isDirectory()) for (const [inner, bytes] of await folderEntries(path, name + "/")) files.set(inner, bytes);
    else files.set(name, await readFile(path));
  }
  return files;
}

// What differs between a signed file and a build: AMO adds META-INF/ (the signature) and changes
// nothing else, so every other file must be the build's byte for byte, manifest.json compared as
// JSON, and neither side may hold a file the other lacks. [] means the same build.
export function buildDifferences(signed, build) {
  const differences = [];
  const theirs = new Map([...signed].filter(([name]) => !name.startsWith("META-INF/")));
  for (const [name, bytes] of build) {
    const other = theirs.get(name);
    if (!other) differences.push(`${name}: not in the signed file`);
    else if (!sameFile(name, bytes, other)) differences.push(`${name}: differs`);
  }
  for (const name of theirs.keys()) if (!build.has(name)) differences.push(`${name}: not in the build`);
  return differences;
}

function sameFile(name, a, b) {
  if (name !== "manifest.json") return Buffer.compare(a, b) === 0;
  try {
    return isDeepStrictEqual(JSON.parse(a.toString("utf8")), JSON.parse(b.toString("utf8")));
  } catch {
    return false;
  }
}

export function stateOf(detail) {
  if (!detail) return "missing";
  const status = detail.file?.status;
  if (!status || status === "unreviewed") return "pending";
  return status;
}

// The token goes with the download, so the file is only ever fetched from AMO itself.
export function downloadTarget(url) {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" || parsed.hostname !== "addons.mozilla.org") {
    throw new Error(`refusing to download the signed file from ${parsed.origin}`);
  }
  const name = decodeURIComponent(parsed.pathname.split("/").pop() || "");
  if (!FILE_NAME_RE.test(name)) throw new Error(`unexpected file name in ${url}`);
  return { url: parsed.href, name };
}

const USAGE = "usage: amo-xpi.mjs status <version> | fetch <version> [--out <dir>] [--wait <seconds>] | listing <dir> | same-build <xpi> <dir|zip>";

function parseArgs(argv) {
  const [command, target, ...rest] = argv;
  if (command === "listing") {
    if (!target || rest.length) throw new Error(USAGE);
    return { command, dir: target };
  }
  if (command === "same-build") {
    if (!target || rest.length !== 1 || !rest[0]) throw new Error(USAGE);
    return { command, xpi: target, build: rest[0] };
  }
  const opts = { command, version: target, out: join("dist", "signed"), wait: 0 };
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === "--out") opts.out = rest[++i];
    else if (rest[i] === "--wait") opts.wait = Number(rest[++i]);
    else throw new Error(`unknown argument: ${rest[i]}`);
  }
  if (!["status", "fetch"].includes(command) || !target || !opts.out || !(opts.wait >= 0)) {
    throw new Error(USAGE);
  }
  versionUrl(opts.version);
  return opts;
}

export async function run(argv, {
  env = process.env,
  fetch = globalThis.fetch,
  sleep = (ms) => new Promise((done) => setTimeout(done, ms)),
  now = Date.now,
  log = console.log,
  warn = console.error,
} = {}) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    warn(err.message);
    return 2;
  }
  if (opts.command === "listing") {
    try {
      const path = join(opts.dir, "manifest.json");
      const { text, version } = listingManifest(await readFile(path, "utf8"));
      await writeFile(path, text, "utf8");
      log(version);
      return 0;
    } catch (err) {
      warn(err.message);
      return 1;
    }
  }
  if (opts.command === "same-build") {
    try {
      const signed = zipEntries(await readFile(opts.xpi));
      const build = (await stat(opts.build)).isDirectory() ? await folderEntries(opts.build) : zipEntries(await readFile(opts.build));
      const differences = buildDifferences(signed, build);
      if (differences.length === 0) {
        log(`${opts.xpi} holds the build in ${opts.build}`);
        return 0;
      }
      warn(`${opts.xpi} is not the build in ${opts.build}:`);
      for (const line of differences.slice(0, 20)) warn(`  ${line}`);
      if (differences.length > 20) warn(`  and ${differences.length - 20} more`);
      return 1;
    } catch (err) {
      warn(err.message);
      return 1;
    }
  }
  const { WEB_EXT_API_KEY: key, WEB_EXT_API_SECRET: secret } = env;
  if (!key || !secret) {
    warn("WEB_EXT_API_KEY and WEB_EXT_API_SECRET are not set");
    return 2;
  }
  const headers = () => ({ Authorization: `JWT ${jwt(key, secret, now())}`, Accept: "application/json" });

  const detail = async () => {
    const response = await fetch(versionUrl(opts.version), { headers: headers() });
    if (response.status === 404) return null;
    if (!response.ok) {
      const text = (await response.text()).slice(0, 300);
      throw new Error(`AMO answered ${response.status} for ${opts.version}: ${text}`);
    }
    return response.json();
  };

  try {
    if (opts.command === "status") {
      log(stateOf(await detail()));
      return 0;
    }
    const deadline = now() + opts.wait * 1000;
    for (;;) {
      const info = await detail();
      const state = stateOf(info);
      if (state === "public") {
        const { url, name } = downloadTarget(info.file.url);
        const response = await fetch(url, { headers: headers() });
        if (!response.ok) throw new Error(`downloading ${name} failed: ${response.status}`);
        await mkdir(opts.out, { recursive: true });
        const path = join(opts.out, name);
        await writeFile(path, Buffer.from(await response.arrayBuffer()));
        log(path);
        return 0;
      }
      if (state !== "pending") {
        warn(`AMO has no signed file for ${opts.version}: ${state}`);
        return NO_FILE_EXIT;
      }
      if (now() >= deadline) {
        warn(`AMO has not signed ${opts.version} yet`);
        return PENDING_EXIT;
      }
      await sleep(Math.min(POLL_MS, Math.max(0, deadline - now())));
    }
  } catch (err) {
    warn(err.message);
    return 1;
  }
}

// Run as a script, not imported. The module's URL has its symlinks and junctions resolved while
// argv[1] keeps the path it was started by, so the two are compared as real paths: otherwise a
// checkout reached through a junction would run nothing and exit 0.
function isMain(argv1) {
  if (!argv1) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(argv1)).href;
  } catch {
    return false;
  }
}

if (isMain(process.argv[1])) {
  process.exitCode = await run(process.argv.slice(2));
}

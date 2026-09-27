import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { watch as watchFs } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const sourceDir = join(root, "addon");
const distDir = join(root, "dist");
const ignored = new Set(["tests", ".DS_Store"]);
const CHROME_MAX_SUGGESTED_KEYS = 4;

function parseArgs() {
  const args = process.argv.slice(2);
  const browserAt = args.indexOf("--browser");
  const browser = browserAt >= 0 ? args[browserAt + 1] : "all";
  if (!["all", "chrome", "firefox"].includes(browser)) throw new Error(`Unknown browser: ${browser}`);
  return { browser, watch: args.includes("--watch") };
}

async function filesUnder(dir, prefix = "") {
  const entries = (await readdir(dir, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name));
  const files = [];
  for (const entry of entries) {
    if (ignored.has(entry.name) || entry.name.startsWith(".")) continue;
    const path = join(dir, entry.name);
    const name = join(prefix, entry.name);
    if (entry.isDirectory()) files.push(...await filesUnder(path, name));
    else files.push([name, path]);
  }
  return files;
}

async function loadManifest() {
  return JSON.parse(await readFile(join(sourceDir, "manifest.json"), "utf8"));
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function zipStore(files) {
  const chunks = [], central = [];
  let offset = 0;
  for (const [name, data] of Object.entries(files).sort(([a], [b]) => a.localeCompare(b))) {
    const nameBytes = Buffer.from(name);
    const header = Buffer.alloc(30 + nameBytes.length);
    header.writeUInt32LE(0x04034b50, 0); header.writeUInt16LE(20, 4); header.writeUInt16LE(0x800, 6);
    header.writeUInt16LE(0, 8); header.writeUInt16LE(0, 10); header.writeUInt16LE(0, 12);
    header.writeUInt16LE(33, 12);
    header.writeUInt32LE(crc32(data), 14); header.writeUInt32LE(data.length, 18); header.writeUInt32LE(data.length, 22);
    header.writeUInt16LE(nameBytes.length, 26); nameBytes.copy(header, 30);
    chunks.push(header, data);
    const entry = Buffer.alloc(46 + nameBytes.length);
    entry.writeUInt32LE(0x02014b50, 0); entry.writeUInt16LE(20, 4); entry.writeUInt16LE(20, 6); entry.writeUInt16LE(0x800, 8);
    entry.writeUInt16LE(0, 12); entry.writeUInt16LE(33, 14);
    entry.writeUInt32LE(crc32(data), 16); entry.writeUInt32LE(data.length, 20); entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28); entry.writeUInt32LE(offset, 42); nameBytes.copy(entry, 46);
    central.push(entry); offset += header.length + data.length;
  }
  const centralSize = central.reduce((n, chunk) => n + chunk.length, 0);
  const end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(files ? Object.keys(files).length : 0, 8);
  end.writeUInt16LE(files ? Object.keys(files).length : 0, 10); end.writeUInt32LE(centralSize, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...chunks, ...central, end]);
}

function chromeManifest(source) {
  const manifest = structuredClone(source);
  delete manifest.browser_specific_settings;
  manifest.minimum_chrome_version = "120";
  manifest.background = { service_worker: "service-worker.js" };
  for (const icons of [manifest.icons, manifest.action?.default_icon]) {
    if (!icons) continue;
    for (const size of Object.keys(icons)) icons[size] = `icons/icon-${size}.png`;
  }
  manifest.icons["128"] = "icons/icon-128.png";
  // Chrome refuses an extension whose commands suggest more than four shortcuts ("Too many
  // shortcuts specified for 'commands'"); Firefox has no such limit. The first four in the
  // manifest keep theirs, the rest are left for the viewer to bind at chrome://extensions/shortcuts.
  let suggested = 0;
  for (const command of Object.values(manifest.commands || {})) {
    if (!command.suggested_key) continue;
    if (++suggested > CHROME_MAX_SUGGESTED_KEYS) delete command.suggested_key;
  }
  return manifest;
}

async function buildBrowser(browser, version) {
  const out = join(distDir, browser);
  await rm(out, { recursive: true, force: true });
  await mkdir(out, { recursive: true });
  for (const [name, path] of await filesUnder(sourceDir)) {
    if (browser === "chrome" && name === "manifest.json") continue;
    const target = join(out, name);
    await mkdir(join(target, ".."), { recursive: true });
    await cp(path, target);
  }
  if (browser === "chrome") await writeFile(join(out, "manifest.json"), `${JSON.stringify(chromeManifest(await loadManifest()), null, 2)}\n`);
  const files = {};
  for (const [name, path] of await filesUnder(out)) files[name.replaceAll("\\", "/")] = new Uint8Array(await readFile(path));
  const zip = zipStore(files);
  await writeFile(join(distDir, `shisu-ko-${version}-${browser}.zip`), zip);
}

async function buildOnce(browser) {
  const manifest = await loadManifest();
  await mkdir(distDir, { recursive: true });
  const browsers = browser === "all" ? ["firefox", "chrome"] : [browser];
  await Promise.all(browsers.map((name) => buildBrowser(name, manifest.version)));
  console.log(`Built ${browsers.join(" and ")} ${manifest.version} in dist/`);
}

const { browser, watch } = parseArgs();
await buildOnce(browser);
if (watch) {
  console.log("Watching addon/; reload the unpacked extension and YouTube after changes.");
  let pending;
  let building = false;
  let queued = false;
  const rebuild = async () => {
    if (building) return;
    building = true;
    try {
      do {
        queued = false;
        await buildOnce(browser);
      } while (queued);
    } catch (error) {
      console.error(error);
    } finally {
      building = false;
    }
  };
  watchFs(sourceDir, { recursive: true }, () => {
    queued = true;
    clearTimeout(pending);
    pending = setTimeout(rebuild, 100);
  });
}

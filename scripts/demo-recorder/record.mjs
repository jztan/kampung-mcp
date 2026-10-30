// Records the README hero GIF (docs/images/demo.gif) from the scripted replay
// on the landing page (pages/index.html).
//
// Pipeline: static-serve pages/ -> Playwright/chromium plays the replay while a
// CDP screencast saves lossless PNG frames -> ffmpeg crops to the two windows
// and builds a palette GIF -> gifsicle (lossy LZW). Playwright's own video goes
// through a low-bitrate webm, which blurs terminal text. The screencast ignores
// deviceScaleFactor, so the page is zoomed 2x in a 2x viewport instead: Retina
// screens need the extra pixels for small text to stay sharp.
//
// Usage:
//   cd scripts/demo-recorder
//   npm install
//   npx playwright install chromium
//   npm run record            # writes ../../docs/images/demo.gif
//
// Tunables via env: FPS, MAXCOLORS, LOSSY, SPEED, OUT_GIF, PORT.
// Requires `ffmpeg` on PATH (brew install ffmpeg).

import { chromium } from "playwright";
import gifsicle from "gifsicle";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, extname, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGES_DIR = resolve(HERE, "../../pages");
const OUT = process.env.OUT_GIF || resolve(HERE, "../../docs/images/demo.gif");
const PORT = Number(process.env.PORT || 8012);
const FPS = Number(process.env.FPS || 14);
const MAXCOLORS = Number(process.env.MAXCOLORS || 256);
const LOSSY = Number(process.env.LOSSY || 30);
const SPEED = Number(process.env.SPEED || 1.5);    // replay speed in the GIF
const HOLD_MS = 6000;                              // pause on the finished screen
const CW = 1280;
const CH = 860;
const ZOOM = 2;

const t0 = Date.now();
const mark = (s) => console.log(`  +${((Date.now() - t0) / 1000).toFixed(2)}s  ${s}`);

const TYPES = { ".html": "text/html", ".png": "image/png", ".svg": "image/svg+xml",
  ".ico": "image/x-icon" };
const server = createServer((req, res) => {
  let path = decodeURIComponent(req.url.split("?")[0]);
  if (path === "/") path = "/index.html";
  const file = join(PAGES_DIR, path);
  try {
    if (!file.startsWith(PAGES_DIR) || !statSync(file).isFile()) throw 0;
    res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream" });
    res.end(readFileSync(file));
  } catch {
    res.writeHead(404).end("not found");
  }
});
await new Promise((r) => server.listen(PORT, r));

const frameDir = mkdtempSync(join(tmpdir(), "demo-frames-"));
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: CW * ZOOM, height: CH * ZOOM },
  colorScheme: "dark",
  reducedMotion: "no-preference",
});
const page = await context.newPage();

// Skip analytics; the page only needs its fonts.
await page.route(/googletagmanager/, (route) => route.abort());
await page.goto(`http://localhost:${PORT}/?ga_optout=true`, { waitUntil: "load" });
await page.evaluate(() => document.fonts.ready);
// The sticky site nav would sit on top of the windows' title bars.
await page.addStyleTag({ content: `nav.top { display: none !important; } html { zoom: ${ZOOM}; }` });

// Put the windows at the top of the viewport, then start the replay from a
// clean frame: scrolling in starts it, so Replay restarts it after the jump.
await page.evaluate(() => {
  document.documentElement.style.scrollBehavior = "auto";
  document.querySelector("#desk").scrollIntoView({ block: "start" });
  window.scrollBy(0, -16);
});
await page.waitForTimeout(400);
const box = await page.locator("#desk").boundingBox();

// The screencast only emits a frame when the page changes, so each frame is
// held until the next one's timestamp.
const frames = [];
const cdp = await context.newCDPSession(page);
cdp.on("Page.screencastFrame", ({ data, metadata, sessionId }) => {
  const file = join(frameDir, `${String(frames.length).padStart(5, "0")}.png`);
  writeFileSync(file, Buffer.from(data, "base64"));
  frames.push({ file, at: metadata.timestamp });
  cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
});
await page.click("#replay");
await page.waitForTimeout(150);
await cdp.send("Page.startScreencast", {
  format: "png", maxWidth: CW * ZOOM, maxHeight: CH * ZOOM, everyNthFrame: 1,
});
mark(`replay started; windows at ${JSON.stringify(box)}`);

await page.waitForSelector("#receipt.on", { timeout: 240000 });
mark("replay finished");
await page.waitForTimeout(500);
await cdp.send("Page.stopScreencast");
mark(`${frames.length} frames captured`);

await context.close();
await browser.close();
server.close();

const list = frames.map(({ file, at }, i) => {
  const next = frames[i + 1];
  const held = next ? (next.at - at) / SPEED : 0.1;
  return `file '${file}'\nduration ${held.toFixed(4)}\n`;
});
const listFile = join(frameDir, "frames.txt");
writeFileSync(listFile, list.join(""));

const rawGif = join(frameDir, "raw.gif");
const crop = [box.width, Math.min(box.height, CH * ZOOM - box.y), box.x, box.y]
  .map(Math.round).join(":");
const vf = `crop=${crop},fps=${FPS},tpad=stop_mode=clone:stop_duration=${HOLD_MS / 1000},` +
  `split[s0][s1];[s0]palettegen=stats_mode=full:max_colors=${MAXCOLORS}[p];` +
  `[s1][p]paletteuse=dither=none:diff_mode=rectangle`;
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
  "-i", listFile, "-vf", vf, "-loop", "0", rawGif], { stdio: "inherit" });
execFileSync(gifsicle, ["-O3", `--lossy=${LOSSY}`, rawGif, "-o", OUT], { stdio: "inherit" });
mark(`gif: ${OUT} (${(statSync(OUT).size / 1e6).toFixed(2)} MB)`);

// Render the film: headless Edge draws each frame, ffmpeg stitches them.
//
//   node film/render.mjs                    the whole film -> film/out/tallyagent.mp4
//   node film/render.mjs --stills 12,40,90  preview frames -> film/build/still-<t>.png
//
// Edge ships with Windows, so playwright-core drives it and nothing is
// downloaded. The page has no clock of its own: every frame is
// window.render(t), so frame 4321 is the same picture every time.

import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright-core";

const here = dirname(fileURLToPath(import.meta.url));
const build = join(here, "build");
const out = join(here, "out");
mkdirSync(build, { recursive: true });
mkdirSync(out, { recursive: true });

// 60 frames a second: the motion is drawn, so every one of them is a real
// in-between rather than a repeated frame.
const FPS = Number(process.env.FPS || 60);
const args = process.argv.slice(2);
const stillsArg = args.includes("--stills") ? args[args.indexOf("--stills") + 1] : "";

// The captured product output and the narration timing, as a script the page
// can load from file:// - fetch() is refused there.
const capture = readFileSync(join(here, "data", "capture.json"), "utf8");
const timing = readFileSync(join(here, "data", "timing.json"), "utf8");
writeFileSync(join(build, "data.js"), `window.CAPTURE=${capture};\nwindow.TIMING=${timing};\n`);

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
await page.goto(pathToFileURL(join(here, "index.html")).href);
await page.waitForFunction(() => typeof window.render === "function");
const total = await page.evaluate(() => window.TOTAL);

async function frame(t) {
  await page.evaluate((x) => window.render(x), t);
  return page.screenshot({ type: "jpeg", quality: 92 });
}

if (stillsArg) {
  for (const t of stillsArg.split(",").map(Number)) {
    writeFileSync(join(build, `still-${t}.jpg`), await frame(t));
    console.log(`still at ${t}s`);
  }
  await browser.close();
  process.exit(process.exitCode || 0);
}

const video = join(build, "picture.mp4");
const ffmpeg = spawn("ffmpeg", [
  "-hide_banner", "-loglevel", "error", "-y",
  "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "mjpeg", "-i", "-",
  "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", video,
], { stdio: ["pipe", "inherit", "inherit"] });

const frames = Math.ceil(total * FPS);
const started = Date.now();
for (let i = 0; i < frames; i++) {
  const jpg = await frame(i / FPS);
  if (!ffmpeg.stdin.write(jpg)) await new Promise((r) => ffmpeg.stdin.once("drain", r));
  if (i % (FPS * 10) === 0) {
    const pct = ((i / frames) * 100).toFixed(0);
    console.log(`  ${pct}%  frame ${i}/${frames}  ${((Date.now() - started) / 1000).toFixed(0)}s`);
  }
}
ffmpeg.stdin.end();
await new Promise((r) => ffmpeg.on("close", r));
await browser.close();

// The real Tally footage goes into the framed windows the page left for it:
// each clip trimmed, sped up, given a slow push in, and overlaid exactly over
// its scene's clip rectangle for exactly its scene's time.
const T = JSON.parse(timing);
const RECT = { x: 96, y: 150, w: 1216, h: 760 };  // must match CLIP_RECT in film.js
const clipScenes = T.scenes.filter((s) => s.clip);
let composed = video;
if (clipScenes.length) {
  const inputs = ["-i", video];
  const filters = [];
  let last = "[0:v]";
  clipScenes.forEach((s, n) => {
    const file = join(here, "clips", `${s.clip}.mkv`);
    inputs.push("-i", file);
    const len = s.clip_seconds / s.speed;
    const push = 0.05;
    filters.push(
      `[${n + 1}:v]setpts=(PTS-STARTPTS)/${s.speed},fps=${FPS},` +
      `crop=w='iw/(1+${push}*t/${len.toFixed(3)})':h='ih/(1+${push}*t/${len.toFixed(3)})':x='(iw-ow)/2':y='(ih-oh)/2',` +
      `scale=${RECT.w}:${RECT.h}:flags=lanczos,setsar=1,setpts=PTS+${s.clip_at}/TB[c${n}]`,
    );
    const out = n === clipScenes.length - 1 ? "[v]" : `[o${n}]`;
    filters.push(`${last}[c${n}]overlay=${RECT.x}:${RECT.y}:eof_action=pass:enable='between(t,${s.clip_at},${(s.clip_at + len).toFixed(3)})'${out}`);
    last = out;
  });
  composed = join(build, "composed.mp4");
  await new Promise((res, rej) => {
    const p = spawn("ffmpeg", [
      "-hide_banner", "-loglevel", "error", "-y", ...inputs,
      "-filter_complex", filters.join(";"), "-map", "[v]",
      "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", composed,
    ], { stdio: "inherit" });
    p.on("close", (code) => (code === 0 ? res() : rej(new Error(`compose failed ${code}`))));
  });
  console.log(`  composited ${clipScenes.length} Tally clip(s)`);
}

// Lay the narration under the picture.
const final = join(out, "tallyagent.mp4");
await new Promise((res, rej) => {
  const mux = spawn("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-y", "-i", composed,
    "-i", join(build, "narration.wav"), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
    "-shortest", "-movflags", "+faststart", final,
  ], { stdio: "inherit" });
  mux.on("close", (code) => (code === 0 ? res() : rej(new Error(`mux failed ${code}`))));
});
console.log(`done: ${resolve(final)}  (${total.toFixed(1)}s, ${frames} frames at ${FPS}fps)`);

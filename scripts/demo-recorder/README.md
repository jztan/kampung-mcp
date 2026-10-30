# Demo GIF recorder

Regenerates `docs/images/demo.gif`, the hero of the README, by playing the scripted replay in `pages/index.html` and encoding the two windows as a GIF.

Rerun it after changing the replay. It needs Node.js 18+ and `ffmpeg` on your PATH (`brew install ffmpeg`).

```bash
cd scripts/demo-recorder
npm install
npx playwright install chromium   # one-time browser download
npm run record                    # writes ../../docs/images/demo.gif
```

The script serves `pages/` itself, restarts the replay on a clean frame, and saves lossless PNG frames through a browser screencast. The page is zoomed 2x in a 2x viewport, so the GIF has true 2x pixels (2400×1200) and small text stays sharp on Retina screens. Playwright's video recording blurs terminal text, so it is not used.

| Variable | Default | Effect |
| --- | --- | --- |
| `SPEED` | `1.5` | Playback speed in the GIF |
| `FPS` | `14` | Frame rate (higher is smoother and larger) |
| `MAXCOLORS` | `256` | GIF palette size |
| `LOSSY` | `30` | gifsicle lossy level (higher is smaller, with more artifacts) |
| `OUT_GIF` | `../../docs/images/demo.gif` | Output path |
| `PORT` | `8012` | Local server port |

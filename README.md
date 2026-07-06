# google-font-metrics

**Metrics-only** builds of the entire [Google Fonts](https://github.com/google/fonts)
catalog, published as a single downloadable archive attached to a
[GitHub Release](../../releases).

No application code lives here — this repo is a build pipeline. It produces one
tiny font file per family in which **every glyph outline has been emptied but
every metric table is preserved**, so a headless runtime can compute text
advance widths and vertical metrics that match the original font exactly.

## Why this exists

Headless runtimes (a Node MCP server, a Cloudflare Worker, vitest) measure text
with [opentype.js](https://github.com/opentypejs/opentype.js) so that
server-side layout matches what the browser draws. That needs real font files.
Bundling full Google Fonts is impossible (~GBs), but text measurement only reads
a font's **metric tables**, not its glyph outlines — and outlines are ~95%+ of a
font file.

A metrics-only font is ~10–15 KB (Latin) instead of hundreds of KB, so the whole
catalog fits in roughly 15–30 MB. Consumers download the release archive at
build time and expand it into their `dist/`, keeping runtime network egress at
zero. The browser itself never uses these files — it measures via the Canvas
API; only headless runtimes do.

## What a metrics-only font keeps and drops

`opentype.js` `font.getAdvanceWidth(text, size)` (kerning on, the default) and
`font.ascender` / `font.descender` / `font.unitsPerEm` return **identical**
results to the original font, because each stripped file keeps:

| Kept | Why |
| --- | --- |
| `hmtx` | advance widths |
| `cmap` | character → glyph mapping |
| `head`, `hhea`, `maxp`, `OS/2`, `post` | vertical metrics (`post` set to format 3.0 to drop glyph names) |
| `GPOS` + `GDEF`, legacy `kern` | kerning — opentype.js applies it inside `getAdvanceWidth` |
| `glyf` + `loca` | opentype.js refuses to parse a TrueType font without them. Every glyph is replaced with an **empty** glyph (`numberOfContours = 0`); `hmtx` is a separate table, so advance widths survive. |

and drops:

- glyph outlines/points and hinting (`fpgm` / `prep` / `cvt` / `gasp` / `hdmx` / `LTSH` / `VDMX`)
- `GSUB` — `getAdvanceWidth` does no shaping/ligature substitution, so it is
  unused for width. (Dropping it is also *safer*: some families ship `GSUB`
  lookups opentype.js cannot parse and which crash it mid-measurement. Without
  `GSUB` the stripped font is more robust than the original.)
- `DSIG`, and variable-font tables (`fvar` / `gvar` / `avar` / `HVAR` / `MVAR` /
  `STAT` / `cvar`) after instancing to a static weight
- colour / bitmap / SVG glyph tables (`CBDT` / `CBLC` / `sbix` / `EBDT` /
  `EBLC` / `EBSC` / `COLR` / `CPAL` / `SVG`) — embedded PNG bitmaps, colour
  layers and SVG artwork are pure rendering data (`hmtx` still holds the advance
  widths). A single colour-emoji font's bitmaps can be tens of MB; e.g. Noto
  Color Emoji strips from ~20 MB to ~320 KB.

### Scope for v1

Regular / weight 400 only, one file per family. Variable families are instanced
to their `wght=400` static instance before stripping. Other weights and italics
are a cheap later increment (each file is tiny). CFF/OTF-only families (rare in
google/fonts) are logged and skipped rather than shipped full-size — see the
`skipped` list in `manifest.json`.

## The archive

Each release attaches two assets:

- **`google-font-metrics.tar.gz`** — the distributable, laid out as:

  ```
  manifest.json
  NOTICE.md
  fonts/<Family Name>.ttf        e.g. fonts/Open Sans.ttf
  licenses/<family-dir>/OFL.txt  each family's upstream license
  ```

- **`manifest.json`** — also attached standalone for cheap lookups without
  downloading the archive:

  ```json
  {
    "version": "v1.0.0",
    "sourceRepo": "https://github.com/google/fonts",
    "sourceCommit": "<google/fonts sha>",
    "generatedWith": "fontTools 4.63.0",
    "instance": "wght=400",
    "count": 1500,
    "fonts": [
      { "family": "Open Sans", "file": "fonts/Open Sans.ttf",
        "weight": 400, "unitsPerEm": 2048, "ascender": 2189,
        "descender": -600, "license": "OFL" }
    ],
    "skipped": [ { "family": "...", "reason": "CFF/OTF outlines (unsupported in v1)" } ]
  }
  ```

  `unitsPerEm`, `ascender` and `descender` are exactly the values opentype.js
  exposes as `font.unitsPerEm` / `font.ascender` / `font.descender`.

## Consuming the release

Look up files by **canonical Google Fonts family name** (the key in
`manifest.json`).

```bash
# Fetch and expand the latest release into ./fonts-metrics
TAG=$(gh release view --json tagName -q .tagName)   # or hardcode a version
gh release download "$TAG" -p google-font-metrics.tar.gz
mkdir -p fonts-metrics && tar -xzf google-font-metrics.tar.gz -C fonts-metrics
```

```js
// Node / opentype.js
import opentype from "opentype.js";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const manifest = JSON.parse(readFileSync("fonts-metrics/manifest.json", "utf8"));
const byFamily = new Map(manifest.fonts.map((f) => [f.family, f]));

function loadFamily(family, root = "fonts-metrics") {
  const entry = byFamily.get(family);
  if (!entry) throw new Error(`no metrics for family: ${family}`);
  const buf = readFileSync(join(root, entry.file));
  return opentype.parse(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
}

const font = loadFamily("Open Sans");
font.getAdvanceWidth("Hello world", 16); // matches the real Open Sans
```

## Building locally

Requires Python 3.11+ and Node 20+.

```bash
pip install -r requirements.txt
(cd verify && npm ci)

# Full catalog (shallow-clones google/fonts into work/fonts; slow first run):
python scripts/build.py --clone --version dev

# Or a quick subset while iterating:
python scripts/build.py --clone --families "Inter,Open Sans,Roboto" --version dev
python scripts/build.py --clone --limit 50 --version dev   # first 50 families

# Verify (sanity + parity gate):
node verify/verify.mjs \
  --parity build/refs/parity.json \
  --fonts  build/out/fonts \
  --refs   build/refs
```

Outputs land in `build/out/` (staging) and `dist/` (the packaged
`google-font-metrics.tar.gz` + `manifest.json`). Both directories are
git-ignored — the Release is the distributable, never the git tree.

### Scripts

| Path | Role |
| --- | --- |
| `scripts/strip.py` | Core strip logic: instance → empty outlines → drop tables. Importable, and runnable on a single font. |
| `scripts/build.py` | Orchestrator: iterate google/fonts, pick Regular/400, strip, collect licenses, write `manifest.json`, package, enforce the size budget. |
| `verify/verify.mjs` | opentype.js gate: every output parses, and sampled families' advance widths + vertical metrics match a reference. |

## Verification (gates every release)

- **Parity** — for a representative sample (Latin, CJK, and variable families),
  the stripped font's `getAdvanceWidth` for a battery of strings, plus
  `ascender` / `descender` / `unitsPerEm`, must be **identical** to a *reference*
  font (same instance, outlines kept, `GSUB` dropped). The only difference
  between the two is the emptied outlines, so any mismatch means the strip
  corrupted a metric table and the build fails.
- **Sanity** — every output font parses cleanly in opentype.js and can measure
  text.
- **Size budget** — the total archive size is logged with the largest files
  (CJK dominates), and the build fails if it exceeds 60 MB (expected ~15–30 MB).

## Releasing

Either push a version tag, or run the workflow manually — CI runs the full
build + verification and, only if everything passes, publishes a Release with
the two assets attached.

```bash
git tag v1.0.0
git push origin v1.0.0
```

Or trigger it from the **Actions** tab / API (`workflow_dispatch`) with
`version` set to the release tag: the job creates the tag at the built commit
and publishes the Release itself via the built-in `GITHUB_TOKEN`, so no local
tag push is needed. Set `publish: false` (optionally with `limit` / `families`)
for a build-and-verify dry run that only uploads workflow artifacts.

## Licensing

These are **derivative font files**. Each family's upstream license (`OFL.txt` /
`LICENSE` / `UFL.txt` from its google/fonts directory) is bundled in the archive
under `licenses/`, and the `name` table's license entries are preserved
unchanged. See [`NOTICE.md`](NOTICE.md). The build scripts, workflow and docs in
this repo are MIT-licensed (see [`LICENSE`](LICENSE)); that license covers the
tooling, not the font derivatives, which remain under their respective upstream
licenses.

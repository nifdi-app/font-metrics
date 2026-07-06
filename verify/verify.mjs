// Verification gate for the metrics-only build. Run after scripts/build.py.
//
//   node verify/verify.mjs --parity <build/refs/parity.json> \
//                          --fonts <build/out/fonts> --refs <build/refs>
//
// Two checks, both of which must pass or the process exits non-zero:
//
//   1. Sanity  - every output font parses in opentype.js and exposes a cmap,
//                a positive unitsPerEm and non-trivial vertical metrics.
//
//   2. Parity  - for the sampled families, opentype.js getAdvanceWidth() of the
//                metrics-only font equals that of its reference font (outlines
//                kept, GSUB dropped) for a battery of sample strings, and
//                ascender / descender / unitsPerEm are identical. The only
//                difference between the two fonts is emptied outlines, so any
//                mismatch means the strip corrupted a metric table.
//
// opentype.js reads font.ascender / font.descender from hhea and
// font.unitsPerEm from head, exactly the values a consumer relies on.

import opentype from "opentype.js";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const SAMPLE_STRINGS = [
  "Hello World",
  "The quick brown fox jumps over the lazy dog",
  "AVATAR WAV To. Yo Va We",           // heavy kerning pairs
  "iiiii mmmmm lllll",                  // narrow vs wide advances
  "café naïve résumé Straße",           // accented / latin-ext
  "1234567890 !@#$%^&*()",
  "fi fl ffi ff",                       // ligature-prone sequences
  "— punctuation, “quotes” … —",
  "日本語のサンプル文字列です",              // CJK (ignored by fonts lacking it)
  "한국어 샘플 텍스트",                     // Hangul
];

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, "");
    out[key] = argv[i + 1];
  }
  return out;
}

function load(path) {
  const b = readFileSync(path);
  return opentype.parse(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
}

function sanity(fontsDir) {
  const files = readdirSync(fontsDir).filter((f) => f.endsWith(".ttf"));
  let failures = 0;
  for (const f of files) {
    const path = join(fontsDir, f);
    try {
      const font = load(path);
      if (!font.tables.cmap) throw new Error("no cmap table");
      if (!(font.unitsPerEm > 0)) throw new Error(`bad unitsPerEm ${font.unitsPerEm}`);
      if (font.ascender === font.descender) throw new Error("degenerate vertical metrics");
      // Force a measurement so any lazy parse blows up here, not in the consumer.
      font.getAdvanceWidth("Ag", 1000);
    } catch (err) {
      console.error(`  SANITY FAIL  ${f}: ${err.message}`);
      failures++;
    }
  }
  console.log(`Sanity: ${files.length - failures}/${files.length} fonts parse and measure cleanly.`);
  return failures === 0;
}

function parity(parityPath, fontsDir, refsDir) {
  const spec = JSON.parse(readFileSync(parityPath, "utf8"));
  const samples = spec.samples || [];
  if (samples.length === 0) {
    console.error("  PARITY FAIL: no parity samples were produced by the build.");
    return false;
  }
  let failures = 0;
  for (const s of samples) {
    const ref = load(join(refsDir, s.file));
    const strip = load(join(fontsDir, s.file));
    const problems = [];
    for (const m of ["unitsPerEm", "ascender", "descender"]) {
      if (ref[m] !== strip[m]) problems.push(`${m} ${ref[m]}!=${strip[m]}`);
    }
    for (const str of SAMPLE_STRINGS) {
      const wa = ref.getAdvanceWidth(str, 1000);
      const wb = strip.getAdvanceWidth(str, 1000);
      if (wa !== wb) problems.push(`width "${str}" ${wa}!=${wb}`);
    }
    if (problems.length) {
      console.error(`  PARITY FAIL  ${s.family} [${s.kind}]: ${problems.join("; ")}`);
      failures++;
    } else {
      console.log(`  parity OK    ${s.family} [${s.kind}]`);
    }
  }
  console.log(`Parity: ${samples.length - failures}/${samples.length} sampled families match.`);
  return failures === 0;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.parity || !args.fonts || !args.refs) {
    console.error("usage: node verify/verify.mjs --parity <parity.json> --fonts <fonts-dir> --refs <refs-dir>");
    process.exit(2);
  }
  const okSanity = sanity(args.fonts);
  const okParity = parity(args.parity, args.fonts, args.refs);
  if (okSanity && okParity) {
    console.log("\nVerification PASSED.");
    process.exit(0);
  }
  console.error("\nVerification FAILED.");
  process.exit(1);
}

main();

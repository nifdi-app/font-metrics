#!/usr/bin/env python3
"""Build the metrics-only Google Fonts catalog.

Pipeline
--------
1. Point at a checkout of https://github.com/google/fonts (``--fonts-repo``) or
   let this script shallow-clone one (``--clone``).
2. Walk the ``ofl/``, ``apache/`` and ``ufl/`` trees. Each family directory has a
   ``METADATA.pb`` giving the canonical family name and its font files.
3. For each family, pick the Regular / weight-400 / normal-style instance,
   instance it to a static wght=400 if it is variable, and strip it to a
   metrics-only font (see ``scripts/strip.py``).
4. Copy each family's license file(s) into ``licenses/``.
5. Emit ``manifest.json`` keyed by canonical family name.
6. For a representative sample (Latin, CJK, variable), also emit a parity
   *reference* font so ``verify/verify.mjs`` can prove advance widths are
   unchanged.
7. Package everything into ``google-font-metrics.tar.gz`` and enforce a size
   budget.

The generated fonts and archive are NOT committed to git; the release workflow
uploads them as GitHub Release assets.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field

import fontTools
from fontTools.ttLib import TTFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strip  # noqa: E402  (local module)

SOURCE_TREES = ["ofl", "apache", "ufl"]
LICENSE_FILENAMES = ["OFL.txt", "UFL.txt", "LICENSE.txt", "LICENSE"]
ARCHIVE_NAME = "google-font-metrics.tar.gz"
DEFAULT_MAX_BYTES = 60 * 1024 * 1024  # 60 MB ceiling; expected ~15-30 MB

# Families always included in the parity sample when present in the build.
CURATED_SAMPLES = {"Inter", "Open Sans", "Roboto", "Noto Sans JP", "Noto Sans SC"}
# CJK subset tags used to flag a family for CJK parity coverage.
CJK_SUBSETS = {
    "japanese", "korean", "chinese-simplified",
    "chinese-traditional", "chinese-hongkong",
}
# Cap on auto-selected parity samples per category (variable / cjk / latin).
AUTO_SAMPLES_PER_CATEGORY = 3


@dataclass
class FontBlock:
    style: str = "normal"
    weight: int = 400
    filename: str = ""


@dataclass
class Metadata:
    name: str = ""
    license: str = ""
    fonts: list[FontBlock] = field(default_factory=list)
    subsets: list[str] = field(default_factory=list)


def parse_metadata(path: str) -> Metadata:
    """Parse the text-format ``METADATA.pb`` well enough for our needs.

    We only need the family ``name``, the ``fonts { ... }`` blocks (style,
    weight, filename), ``license`` and ``subsets``. Full protobuf parsing is
    unnecessary and would pull in the fonts-repo schema.
    """
    meta = Metadata()
    depth = 0
    current: FontBlock | None = None
    in_fonts_block = False

    def unquote(v: str) -> str:
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            return v[1:-1]
        return v

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith("{"):
                key = line[:-1].strip()
                depth += 1
                if depth == 1 and key == "fonts":
                    in_fonts_block = True
                    current = FontBlock()
                continue
            if line == "}":
                depth -= 1
                if depth == 0 and in_fonts_block and current is not None:
                    meta.fonts.append(current)
                    current = None
                    in_fonts_block = False
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = unquote(value)
            if depth == 0:
                if key == "name" and not meta.name:
                    meta.name = value
                elif key == "license" and not meta.license:
                    meta.license = value
                elif key == "subsets":
                    meta.subsets.append(value)
            elif in_fonts_block and current is not None and depth == 1:
                if key == "style":
                    current.style = value
                elif key == "weight":
                    try:
                        current.weight = int(value)
                    except ValueError:
                        pass
                elif key == "filename":
                    current.filename = value
    return meta


def pick_regular(meta: Metadata) -> FontBlock | None:
    """Choose the Regular / weight-400 / normal-style font file.

    Prefers a normal-style block; among those, the one closest to weight 400.
    Returns ``None`` if the metadata lists no usable font file.
    """
    candidates = [f for f in meta.fonts if f.filename]
    if not candidates:
        return None
    normal = [f for f in candidates if f.style == "normal"] or candidates
    normal.sort(key=lambda f: abs(f.weight - strip.REGULAR_WEIGHT))
    return normal[0]


def find_license(family_dir: str) -> str | None:
    for name in LICENSE_FILENAMES:
        p = os.path.join(family_dir, name)
        if os.path.isfile(p):
            return p
    return None


def is_cjk(meta: Metadata) -> bool:
    return any(s in CJK_SUBSETS for s in meta.subsets)


@dataclass
class BuildResult:
    family: str
    file: str
    weight: int
    unitsPerEm: int
    ascender: int
    descender: int
    license: str


def source_commit(repo: str) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD"], text=True
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def clone_google_fonts(dest: str, ref: str) -> None:
    if os.path.isdir(os.path.join(dest, ".git")):
        print(f"  reusing existing clone at {dest}")
        return
    print(f"  shallow-cloning google/fonts@{ref} -> {dest}")
    # --filter=blob:none fetches trees now and file contents lazily on read, so
    # a limited/curated build (e.g. CI) only downloads the fonts it touches
    # instead of the whole ~2 GB repository. A full build ends up fetching every
    # blob anyway, at no extra cost over a plain shallow clone.
    subprocess.check_call([
        "git", "clone", "--depth", "1", "--filter=blob:none", "--branch", ref,
        "https://github.com/google/fonts.git", dest,
    ])


def iter_family_dirs(repo: str):
    for tree in SOURCE_TREES:
        tree_path = os.path.join(repo, tree)
        if not os.path.isdir(tree_path):
            continue
        for entry in sorted(os.listdir(tree_path)):
            fam_dir = os.path.join(tree_path, entry)
            if os.path.isdir(fam_dir) and os.path.isfile(
                os.path.join(fam_dir, "METADATA.pb")
            ):
                yield tree, entry, fam_dir


def sample_kind(meta: Metadata, is_var: bool, is_color: bool) -> str:
    if is_color:
        return "color"
    if is_cjk(meta):
        return "cjk"
    if is_var:
        return "variable"
    return "latin"


def want_sample(meta: Metadata, kind: str, counts: dict) -> bool:
    """Decide whether to emit a parity reference for this family."""
    if meta.name in CURATED_SAMPLES:
        return True
    if counts[kind] < AUTO_SAMPLES_PER_CATEGORY:
        counts[kind] += 1
        return True
    return False


def build(args: argparse.Namespace) -> int:
    repo = args.fonts_repo
    if args.clone:
        os.makedirs(repo, exist_ok=True)
        clone_google_fonts(repo, args.google_fonts_ref)
    if not os.path.isdir(repo):
        print(f"error: fonts repo not found: {repo}", file=sys.stderr)
        return 2

    out_dir = args.out
    fonts_dir = os.path.join(out_dir, "fonts")
    licenses_dir = os.path.join(out_dir, "licenses")
    refs_dir = args.refs
    for d in (fonts_dir, licenses_dir, refs_dir):
        os.makedirs(d, exist_ok=True)

    only = set(args.families.split(",")) if args.families else None

    results: list[BuildResult] = []
    skipped: list[dict] = []
    refs: list[dict] = []
    seen_families: set[str] = set()
    sample_counts = {"color": 0, "cjk": 0, "variable": 0, "latin": 0}
    processed = 0

    for tree, entry, fam_dir in iter_family_dirs(repo):
        if args.limit and processed >= args.limit:
            break
        meta = parse_metadata(os.path.join(fam_dir, "METADATA.pb"))
        if not meta.name:
            continue
        if only is not None and meta.name not in only:
            continue
        if meta.name in seen_families:
            continue

        chosen = pick_regular(meta)
        if chosen is None:
            skipped.append({"family": meta.name, "reason": "no font file in METADATA"})
            continue
        src_path = os.path.join(fam_dir, chosen.filename)
        if not os.path.isfile(src_path):
            skipped.append({"family": meta.name, "reason": f"missing file {chosen.filename}"})
            continue

        processed += 1
        try:
            font = TTFont(src_path)
        except Exception as exc:  # noqa: BLE001 - report and continue
            skipped.append({"family": meta.name, "reason": f"parse error: {exc}"})
            continue

        is_var = "fvar" in font
        is_color = any(t in font for t in strip._COLOR_BITMAP_TABLES)
        if strip.is_cff(font) and not strip.is_truetype(font):
            # CFF/OTF-only. v1 does not empty CFF charstrings, and we refuse to
            # silently ship a full-size file. Log and skip.
            skipped.append({"family": meta.name, "reason": "CFF/OTF outlines (unsupported in v1)"})
            print(f"  SKIP  {meta.name}: CFF/OTF outlines")
            continue

        out_name = f"{meta.name}.ttf"
        out_path = os.path.join(fonts_dir, out_name)
        try:
            strip.instance_to_regular(font)
            strip.strip_to_metrics_only(font)
            font.save(out_path)
        except strip.UnsupportedFontError as exc:
            skipped.append({"family": meta.name, "reason": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001
            skipped.append({"family": meta.name, "reason": f"strip error: {exc}"})
            print(f"  SKIP  {meta.name}: strip error: {exc}")
            continue

        m = strip.metrics(TTFont(out_path))

        # License file(s)
        lic_src = find_license(fam_dir)
        lic_rel = ""
        if lic_src:
            fam_lic_dir = os.path.join(licenses_dir, entry)
            os.makedirs(fam_lic_dir, exist_ok=True)
            shutil.copy2(lic_src, os.path.join(fam_lic_dir, os.path.basename(lic_src)))
            lic_rel = f"licenses/{entry}/{os.path.basename(lic_src)}"

        results.append(BuildResult(
            family=meta.name,
            file=f"fonts/{out_name}",
            weight=strip.REGULAR_WEIGHT,
            unitsPerEm=m["unitsPerEm"],
            ascender=m["ascender"],
            descender=m["descender"],
            license=meta.license or "",
        ))
        seen_families.add(meta.name)

        # Parity reference for a representative sample.
        kind = sample_kind(meta, is_var, is_color)
        if want_sample(meta, kind, sample_counts):
            ref_font = TTFont(src_path)
            try:
                strip.instance_to_regular(ref_font)
                strip.build_reference(ref_font)
                ref_path = os.path.join(refs_dir, out_name)
                ref_font.save(ref_path)
                refs.append({
                    "family": meta.name,
                    # Basename only; the verifier joins it against its --fonts
                    # (stripped) and --refs (reference) directories.
                    "file": out_name,
                    "kind": kind,
                })
            except Exception as exc:  # noqa: BLE001
                print(f"  note: could not build parity ref for {meta.name}: {exc}")

        if processed % 100 == 0:
            print(f"  ...{processed} families processed ({len(results)} built)")

    # Manifest
    results.sort(key=lambda r: r.family)
    manifest = {
        "version": args.version,
        "sourceRepo": "https://github.com/google/fonts",
        "sourceCommit": source_commit(repo),
        "generatedWith": f"fontTools {fontTools.version}",
        "instance": "wght=400",
        "count": len(results),
        "fonts": [vars(r) for r in results],
        "skipped": skipped,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")

    # Parity index for the verifier.
    with open(os.path.join(refs_dir, "parity.json"), "w", encoding="utf-8") as fh:
        json.dump({"manifest": manifest_path, "samples": refs}, fh, indent=2)

    print(f"\nBuilt {len(results)} families, skipped {len(skipped)}, "
          f"{len(refs)} parity samples.")

    # Bundle a NOTICE into the archive tree so it travels with the fonts.
    notice_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NOTICE.md")
    if os.path.isfile(notice_src):
        shutil.copy2(notice_src, os.path.join(out_dir, "NOTICE.md"))

    # Package
    archive_path = os.path.join(args.dist, ARCHIVE_NAME)
    os.makedirs(args.dist, exist_ok=True)
    package(out_dir, archive_path)

    # Also drop a standalone manifest.json next to the archive for release upload.
    shutil.copy2(manifest_path, os.path.join(args.dist, "manifest.json"))

    return report_size(out_dir, fonts_dir, archive_path, args.max_bytes)


def package(out_dir: str, archive_path: str) -> None:
    """tar+gzip the build tree deterministically."""
    members = []
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            full = os.path.join(root, name)
            arc = os.path.relpath(full, out_dir)
            members.append((full, arc))
    members.sort(key=lambda t: t[1])
    with tarfile.open(archive_path, "w:gz") as tar:
        for full, arc in members:
            ti = tar.gettarinfo(full, arcname=arc)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            with open(full, "rb") as fh:
                tar.addfile(ti, fh)
    print(f"Wrote {archive_path}")


def report_size(out_dir: str, fonts_dir: str, archive_path: str, max_bytes: int) -> int:
    archive_size = os.path.getsize(archive_path)
    total_fonts = sum(
        os.path.getsize(os.path.join(fonts_dir, f))
        for f in os.listdir(fonts_dir)
    ) if os.path.isdir(fonts_dir) else 0

    largest = sorted(
        ((os.path.getsize(os.path.join(fonts_dir, f)), f)
         for f in os.listdir(fonts_dir)),
        reverse=True,
    )[:10] if os.path.isdir(fonts_dir) else []

    mb = 1024 * 1024
    print(f"\nArchive:      {archive_size / mb:6.2f} MB  ({archive_path})")
    print(f"Fonts (raw):  {total_fonts / mb:6.2f} MB")
    print("Largest fonts:")
    for size, name in largest:
        print(f"  {size / 1024:8.1f} KB  {name}")

    if archive_size > max_bytes:
        print(f"\nSIZE BUDGET EXCEEDED: {archive_size / mb:.2f} MB > "
              f"{max_bytes / mb:.2f} MB", file=sys.stderr)
        return 1
    print(f"\nSize budget OK ({archive_size / mb:.2f} MB <= {max_bytes / mb:.2f} MB).")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--fonts-repo", default="work/fonts",
                   help="path to a google/fonts checkout")
    p.add_argument("--clone", action="store_true",
                   help="shallow-clone google/fonts into --fonts-repo if absent")
    p.add_argument("--google-fonts-ref", default="main",
                   help="branch/tag to clone (default: main)")
    p.add_argument("--out", default="build/out",
                   help="staging dir for fonts/licenses/manifest")
    p.add_argument("--refs", default="build/refs",
                   help="dir for parity reference fonts")
    p.add_argument("--dist", default="dist",
                   help="dir for the packaged archive + manifest")
    p.add_argument("--version", default=os.environ.get("BUILD_VERSION", "dev"),
                   help="version string recorded in the manifest")
    p.add_argument("--limit", type=int, default=0,
                   help="process at most N families (0 = all)")
    p.add_argument("--families", default="",
                   help="comma-separated family names to build (exact match)")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                   help="fail if the archive exceeds this many bytes")
    args = p.parse_args(argv)

    started = time.time()
    rc = build(args)
    print(f"\nDone in {time.time() - started:.1f}s (exit {rc}).")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

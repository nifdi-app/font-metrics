"""Core metrics-only stripping logic, built on fontTools.

A *metrics-only* font keeps every table opentype.js needs to compute advance
widths and vertical metrics, but replaces every glyph outline with an empty
glyph (``numberOfContours == 0``). Outlines are ~95% of a font file, so the
result is ~10-15 KB instead of hundreds of KB, while
``font.getAdvanceWidth(text, size)`` (kerning on) and
``font.ascender`` / ``font.descender`` / ``font.unitsPerEm`` return byte-for-byte
identical results to the original.

What is preserved:
  * ``hmtx``  – advance widths
  * ``cmap``  – character -> glyph mapping
  * ``head`` / ``hhea`` / ``maxp`` / ``OS/2`` / ``post``  – vertical metrics
  * ``GPOS`` + ``GDEF`` and legacy ``kern`` – kerning (opentype.js applies it
    inside getAdvanceWidth)
  * ``glyf`` + ``loca`` – kept but every glyph emptied. opentype.js refuses to
    parse a TrueType font without them; ``hmtx`` is a separate table, so advance
    widths survive the emptying.

What is dropped:
  * glyph outlines/points and hinting
    (``fpgm`` / ``prep`` / ``cvt`` / ``gasp`` / ``hdmx`` / ``LTSH`` / ``VDMX``)
  * ``GSUB`` – getAdvanceWidth does no shaping/ligature substitution for width,
    so it is unused. (It is also actively harmful: some families ship GSUB
    lookups that opentype.js cannot parse and which crash it during
    measurement. Dropping GSUB makes the stripped font *more* robust than the
    original in opentype.js.)
  * ``DSIG`` – digital signature, meaningless after editing
  * variable-font tables (``fvar`` / ``gvar`` / ``avar`` / ``HVAR`` / ``MVAR`` /
    ``STAT`` / ``cvar``) – removed after instancing to a static weight
  * colour / bitmap / SVG glyph tables (``CBDT`` / ``CBLC`` / ``sbix`` /
    ``EBDT`` / ``EBLC`` / ``EBSC`` / ``COLR`` / ``CPAL`` / ``SVG``) – these hold
    embedded PNG bitmaps, colour layers and SVG artwork, i.e. glyph *rendering*
    data. They do not affect advance widths (``hmtx`` is authoritative) but a
    single colour-emoji font's bitmaps can be tens of MB, which would blow the
    whole catalog's size budget on its own.
"""

from __future__ import annotations

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph
from fontTools.varLib.instancer import instantiateVariableFont

# Variable-font tables removed once the font has been instanced to a static
# weight. GSUB is dropped alongside them so the reference font can be measured in
# opentype.js without hitting unsupported GSUB lookups (see module docstring).
_VF_TABLES = ["fvar", "gvar", "avar", "HVAR", "MVAR", "STAT", "cvar"]

# Colour / bitmap / SVG glyph tables: rendering data only, never advance widths.
_COLOR_BITMAP_TABLES = [
    "CBDT", "CBLC", "sbix", "EBDT", "EBLC", "EBSC", "COLR", "CPAL", "SVG ",
]

# Tables removed from the metrics-only output.
DROP_TABLES = [
    "GSUB",
    "fpgm", "prep", "cvt ", "gasp", "hdmx", "LTSH", "VDMX",
    "DSIG",
] + _VF_TABLES + _COLOR_BITMAP_TABLES

REGULAR_WEIGHT = 400


class UnsupportedFontError(Exception):
    """Raised for fonts this pipeline cannot handle (e.g. CFF/OTF outlines)."""


def is_truetype(font: TTFont) -> bool:
    """True if the font carries TrueType (``glyf``) outlines."""
    return "glyf" in font


def is_cff(font: TTFont) -> bool:
    """True if the font carries CFF/CFF2 (PostScript) outlines."""
    return "CFF " in font or "CFF2" in font


def instance_to_regular(font: TTFont) -> None:
    """Instance a variable font to its Regular (wght=400) static instance.

    The ``wght`` axis is pinned to 400 (clamped to the axis range); every other
    axis is pinned to its default. Static fonts are left untouched.
    """
    if "fvar" not in font:
        return
    axes = {}
    for axis in font["fvar"].axes:
        if axis.axisTag == "wght":
            axes[axis.axisTag] = max(axis.minValue, min(REGULAR_WEIGHT, axis.maxValue))
        else:
            axes[axis.axisTag] = axis.defaultValue
    instantiateVariableFont(font, axes, inplace=True)


def _empty_outlines(font: TTFont) -> None:
    """Replace every glyph outline with an empty glyph (contours = 0)."""
    glyf = font["glyf"]
    for name in glyf.keys():
        glyf[name] = Glyph()  # numberOfContours defaults to 0


def strip_to_metrics_only(font: TTFont) -> None:
    """Turn ``font`` (already instanced) into a metrics-only font, in place.

    Raises ``UnsupportedFontError`` for non-TrueType fonts.
    """
    if not is_truetype(font):
        raise UnsupportedFontError(
            "CFF/OTF outlines are not supported by the v1 strip pipeline"
        )
    _empty_outlines(font)
    if "post" in font:
        font["post"].formatType = 3.0  # drop glyph names
    for tag in DROP_TABLES:
        if tag in font:
            del font[tag]


def build_reference(font: TTFont) -> None:
    """Turn ``font`` (already instanced) into a *reference* font, in place.

    A reference keeps glyph outlines and hinting but drops GSUB and the
    variable-font tables. It is the parity baseline: the only difference between
    a reference and the corresponding metrics-only font is the emptied outlines
    (plus hinting and glyph names), none of which affect advance width. So their
    opentype.js measurements must match exactly, which is what the verifier
    asserts. Both drop GSUB, so neither can hit an unsupported-GSUB crash.
    """
    for tag in ["GSUB"] + _VF_TABLES + _COLOR_BITMAP_TABLES:
        if tag in font:
            del font[tag]


def metrics(font: TTFont) -> dict:
    """Return the vertical metrics opentype.js exposes for this font.

    opentype.js reads ``font.ascender`` / ``font.descender`` straight from
    ``hhea`` and ``font.unitsPerEm`` from ``head``, so these values match what a
    consumer sees.
    """
    return {
        "unitsPerEm": font["head"].unitsPerEm,
        "ascender": font["hhea"].ascent,
        "descender": font["hhea"].descent,
    }


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Produce a metrics-only version of a single font."
    )
    parser.add_argument("src", help="source .ttf/.otf")
    parser.add_argument("out", help="destination metrics-only .ttf")
    parser.add_argument(
        "--reference",
        metavar="PATH",
        help="also emit the parity reference font (outlines kept, GSUB dropped)",
    )
    args = parser.parse_args(argv)

    if args.reference:
        ref = TTFont(args.src)
        instance_to_regular(ref)
        build_reference(ref)
        ref.save(args.reference)

    font = TTFont(args.src)
    instance_to_regular(font)
    strip_to_metrics_only(font)
    font.save(args.out)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

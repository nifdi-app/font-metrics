# NOTICE

The font files distributed in the `google-font-metrics.tar.gz` release archive
are **metrics-only derivatives** of fonts from the
[Google Fonts](https://github.com/google/fonts) project.

Each derivative is produced by:

1. instancing the family's Regular / weight-400 instance to a static font, then
2. replacing every glyph outline with an empty glyph (`numberOfContours = 0`)
   and removing hinting, `GSUB`, signature and variable-font tables.

The metric tables (`hmtx`, `cmap`, `head`, `hhea`, `maxp`, `OS/2`, `post`,
`GPOS`, `GDEF`, `kern`) are preserved so the files can be used to compute text
advance widths and vertical metrics. **These files contain no glyph outlines and
cannot be used to render or display text.**

## Licensing

These derivatives are redistributed under the same license as each upstream
family. Google Fonts families are licensed under one of:

- the [SIL Open Font License 1.1](https://scripts.sil.org/OFL) (`OFL.txt`),
- the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
  (`LICENSE`), or
- the [Ubuntu Font License 1.0](https://ubuntu.com/legal/font-licence)
  (`UFL.txt`).

Every family's original license file is bundled in the release archive under
`licenses/<family-directory>/`, and each font's `name` table license entries are
carried through unchanged. Consult the bundled license for the terms that apply
to a given family. The `license` field in `manifest.json` records which license
each family uses.

The build scripts, CI workflow and documentation in this repository are licensed
separately under the MIT License (see `LICENSE`). That license applies to the
tooling only and does **not** relicense the font derivatives, which remain under
their respective upstream licenses.

This project is not affiliated with or endorsed by Google.

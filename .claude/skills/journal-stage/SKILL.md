---
name: journal-stage
description: Add a timeline step (a new night or pipeline change, as produced on that date) or a one-change lesson to the M31 journal site - star-align the two images, export metadata-free web images, measure the numbers (frames, minutes, FWHM, SNR at fixed M31 positions, night conditions) and write the "what changed / what it did" copy into site/src/data/journal.json. Use when a new night, a pipeline change, or a new combined stack should appear on the site as a comparison slider, or when a stage's images or numbers need regenerating.
---

# One journal step or lesson

The site has two kinds of slider, both driven by `site/src/data/metrics/journal_spec_v2.json`:

- **Timeline steps** (`timeline`, the main story): the best image actually produced on a date, with cumulative
  frames. `before` is the previous step's `after`. Never reprocess an earlier step with newer code. To add a
  night: run it, re-combine all nights, then append one step whose `after` is the new combine's `final/history/`
  output, with a `provenance` line (date + commit). Single nights that aren't the cumulative best go in that
  step's `side_notes`. The hero (`hero`) is first light against the latest step. Steps that come out worse stay
  in, with a line on what went wrong.
- **Lessons** (`stages`, "What each change did"): one change as a before/after, reprocessed today, and the copy
  says so.

A slider shows the same sky at the same scale and rotation on both sides. Both images must show the same sky at the same scale
and rotation, and every number in the copy must come from a metrics file, never from memory or estimation.

The driver is `scripts/build_journal.py`; it calls `scripts/align_pair.py` and `scripts/export_web.py`.
Check `--help` on each before use, since the flags below may have moved on.

## 1. Describe the renders and the stage

Edit `site/src/data/metrics/journal_spec_v2.json`:

- `renders`: one entry per image the stage needs, if not already there: `label`, `image` (stretched
  PNG/TIFF/JPEG, normally `data/<session>/final/final.png`), `linear` (last linear image before denoise, e.g.
  `final/03_cc.fit`), optional `stack`, `wcs`, `nights`, `frames`/`minutes` overrides, `extra`.
- `stages`: `{id, title, before, after, dates, what_changed, what_it_did}`. Stage ids are `NN-slug`
  (e.g. `08-fourth-night`). The two sentences are templates filled from the measurements
  (`{before.snr_headline}`, `{after.fwhm_arcsec}`, `{d.snr_pct}`, ...), so they never hold a typed-in number.

Copy rules: short, first person, plain, one sentence each. Run the wording through the `humanizer` skill.
The location is "Belfast", nothing finer.

## 2. Conditions for any new night

```bash
.venv/bin/python scripts/night_conditions.py data/M31_2026-09-07 data/M31_2026-09-25 data/M31_2026-10-16 [new night] \
    --out site/src/data/metrics/conditions.json
```

Pass every night in one call (it rewrites the file). Weather comes from Open-Meteo at Belfast city centre
and the moon from astropy; never the FITS coordinates.

## 3. Measure, align, export, build

```bash
.venv/bin/python scripts/build_journal.py measure --only <render ids>     # SNR at 8 WCS-fixed M31 points, FWHM, frames, minutes
.venv/bin/python scripts/build_journal.py images  --only <stage id>      # align_pair.py + export_web.py
.venv/bin/python scripts/build_journal.py build                          # writes site/src/data/journal.json
```

- Alignment is star matching (astroalign similarity) onto the spec's common `ref` geometry, cropped to the
  overlap. `align_pair.py` exits non-zero if the median star residual is >= 1 px: fix the input, don't
  raise `--max-residual`.
- Framing is a site standard: no rotation, 4:3 landscape crop around M31's nucleus (moved up so M110 stays in),
  from the spec's `frame` block. Check that M31 is horizontal and M110 is in frame before deploying.
- `export_web.py` writes `site/public/img/<stage>/{before,after}{,-1200}.{avif,webp}` from bare pixels
  (no EXIF/XMP/ICC) and re-checks every output.
- Intermediate PNGs stay out of the repo.

To do a single pair by hand instead:

```bash
.venv/bin/python scripts/align_pair.py BEFORE.png AFTER.png --out-before B.png --out-after A.png \
    [--ref REF.png --pad 0.0] --metrics site/src/data/metrics/align_<stage>.json
.venv/bin/python scripts/export_web.py B.png A.png --stage <stage> --manifest site/src/data/metrics/images_<stage>.json
```

## 4. Check

Read the new entry in `journal.json` and confirm each number matches its metrics file. Then
`make privacy-check`, `make site`, look at the stage locally (`npm --prefix site run preview`), and deploy
with the `deploy-site` skill.

#!/usr/bin/env python
"""
Measure-first, CFA-aware row/column banding correction for Seestar sub-frames.

Sony IMX585-class sensors can show horizontal banding: small offsets per
physical sensor row. In an alt-az stack the pattern rotates against the sky
and smears into a rotating hash, so if present it must be removed on each
raw frame *before* registration.  But subtracting a per-row estimate also
injects the estimate's own noise, so this tool only corrects what it can
actually measure:

  1. Split the CFA image into its 4 colour planes; build a smooth background
     model per plane (large-mesh SEP background) and a star/galaxy mask.
  2. For each physical row take the median of the masked residual over the
     two OUTER 25 % strips of the frame separately -> two independent
     estimates of the row offset that exclude the target in the middle.  High-pass both (running median, default
     31 rows) so only thin stripes count, never real sky gradients.
  3. Real banding is common to both strips; noise (and galaxy structure)
     is not.  The covariance gives the true banding RMS, var - cov the noise.
  4. Fixed-pattern pass: average the per-frame profiles over all frames. If
     the averaged profile is significant it is subtracted from every frame
     (nearly noise-free, since it is estimated from hundreds of frames).
  5. Per-frame pass: only frames whose own banding RMS exceeds both
     --min-band-rms (default 1.0 ADU) and the estimator's own noise RMS get
     their profile subtracted (otherwise the fix would add more than it removes).
  6. Frames needing no correction are hard-linked unchanged into --out.

Header and Bayer pattern are preserved; HISTORY cards are added.

Usage:
  deband.py --in DIR --out DIR [--axis rows|cols|both] [--window 31] [--min-band-rms 1.0]
            [--no-fixed-pattern] [--force] [--workers N] [--dry-run]
"""
import argparse, json, os, shutil, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import sep
from astropy.io import fits
from scipy.ndimage import binary_dilation, median_filter


def _fill(prof):
    bad = ~np.isfinite(prof)
    if bad.any():
        prof[bad] = np.interp(np.flatnonzero(bad), np.flatnonzero(~bad), prof[~bad]) if (~bad).any() else 0
    return prof


def hp(prof, window):
    return prof - median_filter(prof, size=window, mode="reflect")


def residual_and_mask(img, sigma):
    """Background-subtracted residual and star/galaxy mask for a CFA image."""
    resid = np.empty_like(img)
    mask = np.zeros_like(img, dtype=bool)
    for oy in (0, 1):
        for ox in (0, 1):
            p = np.ascontiguousarray(img[oy::2, ox::2])
            bk = sep.Background(p, bw=128, bh=128, fw=3, fh=3)
            r = p - bk.back()
            resid[oy::2, ox::2] = r
            mask[oy::2, ox::2] = binary_dilation(r > sigma * bk.globalrms, iterations=2)
    return resid, mask


EDGE_FRAC = 0.25  # estimate offsets from the outer 25 % strips on each side, away from the target


def profiles(img, window, sigma, edge_frac=None):
    """Returns (A, B) high-passed row-offset profiles for a 2-D CFA image (rows axis), estimated
    independently from the two OUTER strips of the frame (first/last edge_frac of the columns).
    Using the edges rather than halves keeps the target (which sits in the middle) out of the
    estimate, so structure in a galaxy cannot masquerade as banding."""
    ef = EDGE_FRAC if edge_frac is None else edge_frac
    resid, mask = residual_and_mask(img, sigma)
    W = img.shape[1]
    w = max(int(W * ef), 64)
    m = np.where(mask, np.nan, resid)
    A = hp(_fill(np.nanmedian(m[:, :w], axis=1)), window)
    B = hp(_fill(np.nanmedian(m[:, W - w :], axis=1)), window)
    return A.astype(np.float32), B.astype(np.float32)


def band_stats(L, R):
    cov = float(np.cov(L, R)[0, 1])
    var = 0.5 * (float(L.var()) + float(R.var()))
    return dict(band_rms=float(np.sqrt(max(cov, 0))), noise_rms=float(np.sqrt(max(var - cov, 0))))


def measure(args):
    src, axis, window, sigma = args
    d = fits.getdata(src, memmap=False).astype(np.float32)
    out = {}
    for ax in (["rows", "cols"] if axis == "both" else [axis]):
        img = d if ax == "rows" else np.ascontiguousarray(d.T)
        L, R = profiles(img, window, sigma)
        out[ax] = (L, R)
    return os.path.basename(src), out


def apply(args):
    src, dst, axis, window, sigma, fixed, min_band, force, dry = args
    with fits.open(src, memmap=False) as h:
        hdr = h[0].header.copy()
        d = h[0].data.astype(np.float32)
    changed = False
    report = {}
    for ax in (["rows", "cols"] if axis == "both" else [axis]):
        img = d if ax == "rows" else np.ascontiguousarray(d.T)
        note = []
        if fixed.get(ax) is not None:
            img -= fixed[ax][:, None]
            note.append("fixed-pattern")
            changed = True
        L, R = profiles(img, window, sigma)
        st = band_stats(L, R)
        # Subtracting the estimate replaces banding power b^2 with estimator-noise power n^2,
        # so a per-frame correction only helps when b > n (and is above the absolute floor).
        if force or (st["band_rms"] >= min_band and st["band_rms"] > st["noise_rms"]):
            img -= (0.5 * (L + R))[:, None]
            note.append(f"per-frame (band rms {st['band_rms']:.2f})")
            changed = True
        report[ax] = dict(**st, applied=note)
        if ax == "cols":
            d = np.ascontiguousarray(img.T)
        else:
            d = img
    if dry:
        return os.path.basename(src), report, changed
    if changed:
        fixed_u16 = np.clip(np.rint(d), 0, 65535).astype(np.uint16)
        for ax, r in report.items():
            hdr["HISTORY"] = f"deband {ax}: {', '.join(r['applied'])}; band rms {r['band_rms']:.2f} noise rms {r['noise_rms']:.2f} ADU"
        fits.PrimaryHDU(fixed_u16, header=hdr).writeto(dst, overwrite=True)
    else:
        if os.path.exists(dst):
            os.unlink(dst)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return os.path.basename(src), report, changed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--axis", choices=["rows", "cols", "both"], default="rows")
    ap.add_argument("--window", type=int, default=31, help="running-median window (rows) for the high-pass")
    ap.add_argument("--sigma", type=float, default=2.5, help="star/galaxy mask threshold in background-RMS units")
    ap.add_argument("--min-band-rms", type=float, default=1.0, help="per-frame correction only if measured banding RMS >= this (ADU)")
    ap.add_argument("--fixed-snr", type=float, default=3.0, help="apply fixed pattern only if its RMS exceeds this many x its noise")
    ap.add_argument("--no-fixed-pattern", action="store_true")
    ap.add_argument("--force", action="store_true", help="apply per-frame correction to every frame regardless of measurement")
    ap.add_argument("--workers", type=int, default=max(2, os.cpu_count() - 2))
    ap.add_argument("--dry-run", action="store_true", help="measure only, write nothing")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    files = sorted(Path(a.inp).glob("*.fit")) + sorted(Path(a.inp).glob("*.fits"))
    if a.limit:
        files = files[: a.limit]
    if not files:
        sys.exit(f"no FITS in {a.inp}")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    axes = ["rows", "cols"] if a.axis == "both" else [a.axis]
    t0 = time.time()

    # ---- pass 1: measure every frame
    print(f"pass 1: measuring banding in {len(files)} frames ({a.workers} workers) ...")
    with ProcessPoolExecutor(a.workers) as ex:
        meas = list(ex.map(measure, [(str(f), a.axis, a.window, a.sigma) for f in files], chunksize=4))
    per = {ax: [band_stats(m[1][ax][0], m[1][ax][1]) for m in meas] for ax in axes}
    fixed = {}
    summary = {}
    for ax in axes:
        band = np.array([p["band_rms"] for p in per[ax]]); noise = np.array([p["noise_rms"] for p in per[ax]])
        L = np.mean([m[1][ax][0] for m in meas], axis=0); R = np.mean([m[1][ax][1] for m in meas], axis=0)
        fst = band_stats(L, R)
        fixed_noise = float(np.sqrt(np.mean(noise ** 2) / len(files)))
        use_fixed = (not a.no_fixed_pattern) and fst["band_rms"] > a.fixed_snr * fixed_noise
        fixed[ax] = (0.5 * (L + R)).astype(np.float32) if use_fixed else None
        n_frame = int(((band >= a.min_band_rms) & (band > noise)).sum()) if not a.force else len(files)
        summary[ax] = dict(
            per_frame_band_rms_median=float(np.median(band)), per_frame_band_rms_max=float(band.max()),
            per_frame_noise_rms_median=float(np.median(noise)),
            fixed_pattern_rms=fst["band_rms"], fixed_pattern_noise_floor=fixed_noise, fixed_pattern_applied=bool(use_fixed),
            frames_needing_per_frame_fix=n_frame, min_band_rms=a.min_band_rms,
        )
        print(f"  [{ax}] per-frame banding rms: median {np.median(band):.2f} ADU (max {band.max():.2f}); row-noise rms {np.median(noise):.2f}")
        print(f"  [{ax}] fixed pattern across frames: {fst['band_rms']:.2f} ADU vs noise floor {fixed_noise:.2f} -> {'APPLY' if use_fixed else 'negligible, skip'}")
        print(f"  [{ax}] frames above per-frame threshold {a.min_band_rms} ADU: {n_frame}/{len(files)}")

    # ---- pass 2: apply where warranted, link the rest
    if a.dry_run:
        print("dry run - nothing written"); (out / "deband_summary.json").write_text(json.dumps(summary, indent=2)); return
    print("pass 2: applying corrections / linking unchanged frames ...")
    jobs = [(str(f), str(out / f.name), a.axis, a.window, a.sigma, fixed, a.min_band_rms, a.force, False) for f in files]
    n_changed = 0
    with ProcessPoolExecutor(a.workers) as ex:
        for name, rep, changed in ex.map(apply, jobs, chunksize=4):
            n_changed += int(changed)
    summary["frames_modified"] = n_changed
    summary["frames_linked_unchanged"] = len(files) - n_changed
    (out / "deband_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"done in {time.time()-t0:.0f}s: {n_changed} frames corrected, {len(files)-n_changed} linked unchanged -> {out}")


if __name__ == "__main__":
    main()

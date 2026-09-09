#!/usr/bin/env python
"""
Measure a stacked (linear) FITS the way the pipeline review did, so changes can be verified:

  sky noise   1.4826 x MAD of (luminance - 9 px median filter) over sky pixels, in stack units
              and as % of the sky median.  Sky = pixels whose 65 px gaussian-smoothed luminance
              is below median + 1 MAD, inside the central 70 % of the frame.
  signal      median of the 8 px gaussian-smoothed luminance where it sits 3-200 sky-MAD above
              sky (faint galaxy / nebula; MAD = per-pixel MAD of the sky pixels), minus sky.
              SNR = signal / sky noise.
  FWHM        2 x median half-flux radius of unsaturated stars (sep, 5 sigma detection, peak
              > 30 sigma and < 0.5 of the frame maximum), in arcsec at 3.665"/px x --bin.
  corners     noise in 200x200 boxes at the four corners (TL/TR/BL/BR) divided by the central
              sky noise; > 1 means the corners have less coverage (registration / rotation).

Accepts 3xHxW, HxWx3 or 2-D images, float or uint16; luminance = mean of channels.
--bin N averages NxN blocks before measuring (compare against a binned stack).

Usage:
  measure_stack.py STACK.fit [STACK2.fit ...] [--bin N] [--pix-scale 3.665]
"""
import argparse, os, sys

import numpy as np
import sep
from astropy.io import fits
from scipy.ndimage import gaussian_filter, median_filter

PIX_SCALE_DEFAULT = 3.665  # arcsec/px, S30 Pro plate solve


def mad(x):
    x = x[np.isfinite(x)]
    m = np.median(x)
    return m, float(np.median(np.abs(x - m)))


def load_luminance(path, binning=1):
    with fits.open(path, memmap=False) as h:
        d = h[0].data
    d = np.asarray(d, dtype=np.float64)
    if d.ndim == 3:
        if d.shape[0] in (1, 3, 4):
            lum = d.mean(axis=0)
        elif d.shape[-1] in (1, 3, 4):
            lum = d.mean(axis=-1)
        else:
            sys.exit(f"{path}: cannot tell channel axis of shape {d.shape}")
    elif d.ndim == 2:
        lum = d
    else:
        sys.exit(f"{path}: unsupported ndim {d.ndim}")
    if binning > 1:
        H, W = (lum.shape[0] // binning) * binning, (lum.shape[1] // binning) * binning
        lum = lum[:H, :W].reshape(H // binning, binning, W // binning, binning).mean(axis=(1, 3))
    return np.ascontiguousarray(lum.astype(np.float32))


def central(shape, frac=0.70):
    H, W = shape
    dy, dx = int(H * (1 - frac) / 2), int(W * (1 - frac) / 2)
    return slice(dy, H - dy), slice(dx, W - dx)


def measure(path, binning, pix_scale, box=200):
    lum = load_luminance(path, binning)
    H, W = lum.shape
    cs = central(lum.shape)
    core = np.zeros(lum.shape, bool); core[cs] = True

    # ---- sky mask from the heavily smoothed image (galaxy / bright nebula excluded)
    smooth_sky = gaussian_filter(lum, 65)
    m, d = mad(smooth_sky[core])
    sky_mask = core & (smooth_sky < m + d)
    sky = float(np.median(lum[sky_mask]))

    # ---- pixel-to-pixel noise: high-pass with a 9 px median filter
    hp = lum - median_filter(lum, size=9, mode="reflect")
    _, nmad = mad(hp[sky_mask])
    noise = 1.4826 * nmad
    noise_pct = 100 * noise / sky if sky else np.nan

    # ---- faint extended signal: 3-200 MAD above sky in the lightly smoothed image
    smooth8 = gaussian_filter(lum, 8)
    _, smad = mad(lum[sky_mask])  # per-pixel MAD of the sky itself sets the band
    lo, hi = sky + 3 * smad, sky + 200 * smad
    gal = core & (smooth8 > lo) & (smooth8 < hi)
    signal = float(np.median(smooth8[gal]) - sky) if gal.sum() > 100 else np.nan
    snr = signal / noise if noise else np.nan

    # ---- stars
    bkg = sep.Background(lum, bw=64, bh=64)
    sub = lum - bkg.back()
    rms = float(bkg.globalrms)
    objs = sep.extract(sub, thresh=5.0, err=rms, minarea=5, deblend_cont=0.005, clean=True)
    good = (objs["flag"] == 0) & (objs["peak"] > 30 * rms) & (objs["peak"] < 0.5 * float(lum.max()))
    o = objs[good]
    if len(o) >= 5:
        r50, _ = sep.flux_radius(sub, o["x"], o["y"], 10.0 * np.ones(len(o)), 0.5,
                                 normflux=o["flux"], subpix=5)
        r50 = r50[np.isfinite(r50) & (r50 > 0.3)]
        fwhm_px = 2.0 * float(np.median(r50))
        fwhm_arcsec = fwhm_px * pix_scale * binning
    else:
        fwhm_px, fwhm_arcsec = np.nan, np.nan

    # ---- corner coverage: noise in 200x200 boxes vs the central sky noise
    b = max(8, box // binning)
    boxes = {"TL": (slice(0, b), slice(0, b)), "TR": (slice(0, b), slice(W - b, W)),
             "BL": (slice(H - b, H), slice(0, b)), "BR": (slice(H - b, H), slice(W - b, W))}
    corner = {}
    for k, (sy, sx) in boxes.items():
        patch = hp[sy, sx]
        valid = np.isfinite(lum[sy, sx]) & (lum[sy, sx] != 0)
        if valid.mean() < 0.5:
            corner[k] = np.nan  # no data in this corner
        else:
            _, cmad = mad(patch[valid])
            corner[k] = 1.4826 * cmad / noise if noise else np.nan

    return dict(file=path, shape=f"{W}x{H}", sky=sky, noise=noise, noise_pct=noise_pct,
                signal=signal, snr=snr, n_gal_px=int(gal.sum()), fwhm_px=fwhm_px,
                fwhm_arcsec=fwhm_arcsec, nstars=int(len(o)), corner=corner)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stacks", nargs="+", metavar="STACK.fit")
    ap.add_argument("--bin", type=int, default=1, help="average NxN blocks before measuring")
    ap.add_argument("--pix-scale", type=float, default=PIX_SCALE_DEFAULT, help="arcsec/px of the unbinned stack")
    a = ap.parse_args()
    if a.bin < 1:
        sys.exit("--bin must be >= 1")

    hdr = f"{'file':<28} {'size':>9} {'sky':>9} {'noise':>9} {'noise%':>7} {'signal':>9} {'SNR':>6} {'FWHM\"':>6} {'FWHMpx':>6} {'stars':>5}  corner/centre TL TR BL BR"
    print(hdr); print("-" * len(hdr))
    for p in a.stacks:
        r = measure(p, a.bin, a.pix_scale)
        name = os.path.basename(p)
        if len(name) > 28:
            name = "..." + name[-25:]
        c = " ".join("  n/a" if not np.isfinite(v) else f"{v:5.2f}" for v in r["corner"].values())
        print(f"{name:<28} {r['shape']:>9} {r['sky']:9.4g} {r['noise']:9.4g} {r['noise_pct']:7.2f} {r['signal']:9.4g} "
              f"{r['snr']:6.2f} {r['fwhm_arcsec']:6.1f} {r['fwhm_px']:6.2f} {r['nstars']:5d}  {c}")


if __name__ == "__main__":
    main()

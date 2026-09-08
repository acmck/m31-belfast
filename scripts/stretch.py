#!/usr/bin/env python
"""
Finish a linear, colour-calibrated, background-extracted RGB stack:
  crop rotation-starved edges -> neutral sky background -> SCNR green removal -> linked auto-stretch
  (midtone transfer function, Siril-style) -> arcsinh colour boost ->
  saturation -> grey sky (background chroma-noise desaturation) ->
  16-bit TIFF, PNG and JPEG, plus a preview strip of 3 strengths.

Usage:
  stretch.py IN.fit(s) --out PREFIX [--crop 0.04] [--target-bg 0.15] [--shadow-sigma 2.8]
             [--asinh 3] [--saturation 1.25] [--scnr 0.8] [--no-scnr]
"""
import argparse
from pathlib import Path
import numpy as np
from astropy.io import fits
from PIL import Image


def load_rgb(path):
    d = fits.getdata(path).astype(np.float32)
    if d.ndim == 2:
        d = np.stack([d, d, d])
    if d.shape[0] != 3 and d.shape[-1] == 3:
        d = np.moveaxis(d, -1, 0)
    if d.max() > 1.5:                      # 16-bit integer data
        d = d / 65535.0
    return d  # (3, H, W) in 0..1


def mtf(x, m):
    """Midtone transfer function (PixInsight/Siril)."""
    return (m - 1) * x / ((2 * m - 1) * x - m)


def neutralize_background(rgb):
    """Shift each channel so its sky background sits at the same level (neutral grey sky)."""
    meds = []
    for c in rgb:
        med = np.median(c); mad = 1.4826 * np.median(np.abs(c - med))
        sky = c[np.abs(c - med) < 2.5 * mad]
        meds.append(np.median(sky) if sky.size else med)
    meds = np.array(meds, dtype=np.float32)
    return np.clip(rgb - (meds - meds.mean())[:, None, None], 0, None)


def autostretch(rgb, target_bg=0.15, shadow_sigma=2.8):
    """Linked-channel autostretch: same shadows clip and midtone for all channels."""
    lum = rgb.mean(axis=0)
    med = np.median(lum)
    mad = 1.4826 * np.median(np.abs(lum - med))
    c0 = max(0.0, med - shadow_sigma * mad)
    m = mtf(med - c0, target_bg)  # midtone so the background lands at target_bg
    out = np.clip((rgb - c0) / (1 - c0), 0, 1)
    return mtf(out, m)


def scnr(rgb, amount=0.8, protect_sigma=3.0):
    """Average-neutral SCNR restricted to real signal.  On pure background noise, plain SCNR
    clips green wherever noise makes it the top channel and leaves a magenta cast; here the
    correction is weighted by how far the pixel sits above the sky (in noise units)."""
    r, g, b = rgb
    lum = rgb.mean(axis=0)
    med = np.median(lum); mad = 1.4826 * np.median(np.abs(lum - med))
    w = np.clip((lum - med) / (protect_sigma * mad + 1e-9), 0, 1)
    m = 0.5 * (r + b)
    g2 = np.where(g > m, g - amount * w * (g - m), g)
    return np.stack([r, g2, b])


def asinh_color(rgb, beta=3.0):
    """Arcsinh stretch applied on luminance with colour ratios preserved (colour boost)."""
    if beta <= 0:
        return rgb
    lum = rgb.mean(axis=0) + 1e-6
    f = np.arcsinh(beta * lum) / np.arcsinh(beta) / lum
    return np.clip(rgb * f, 0, 1)


def desaturate_background(rgb, amount=0.7, protect_sigma=3.0):
    """Pull sky-background pixels toward grey.  After photometric colour calibration the blue channel
    carries ~2x the noise of red/green (the camera's weak blue response gets scaled up), which reads
    as a coloured cast in a stretched sky.  Signal (stars, galaxy) is protected by the same
    noise-unit weight used for SCNR, so object colour is untouched."""
    if amount <= 0:
        return rgb
    lum = rgb.mean(axis=0)
    med = np.median(lum); mad = 1.4826 * np.median(np.abs(lum - med))
    w = np.clip((lum - med) / (protect_sigma * mad + 1e-9), 0, 1)      # 0 = sky, 1 = signal
    k = amount * (1 - w)
    return np.clip(lum + (rgb - lum) * (1 - k), 0, 1)


def smooth_background(rgb, sigma=1.0, protect_sigma=3.0):
    """Masked luminance noise reduction: blur only where the pixel is sky (weight 0), leave stars and
    galaxy (weight 1) untouched.  Equivalent to Siril's masked noise reduction / a starless-background blur."""
    if sigma <= 0:
        return rgb
    from scipy.ndimage import gaussian_filter
    lum = rgb.mean(axis=0)
    med = np.median(lum); mad = 1.4826 * np.median(np.abs(lum - med))
    w = np.clip((lum - med) / (protect_sigma * mad + 1e-9), 0, 1)
    out = np.empty_like(rgb)
    for i in range(3):
        out[i] = w * rgb[i] + (1 - w) * gaussian_filter(rgb[i], sigma)
    return out


def saturate(rgb, s=1.25):
    lum = rgb.mean(axis=0)
    return np.clip(lum + s * (rgb - lum), 0, 1)


def to_img(rgb):
    return np.moveaxis(np.clip(rgb, 0, 1), 0, -1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp")
    ap.add_argument("--out", required=True, help="output prefix (no extension)")
    ap.add_argument("--crop", type=float, default=0.04, help="fraction to trim from each edge")
    ap.add_argument("--target-bg", type=float, default=0.10)
    ap.add_argument("--shadow-sigma", type=float, default=2.8)
    ap.add_argument("--asinh", type=float, default=3.0)
    ap.add_argument("--saturation", type=float, default=1.15)
    ap.add_argument("--scnr", type=float, default=0.8)
    ap.add_argument("--no-scnr", action="store_true")
    ap.add_argument("--no-neutralize", action="store_true", help="skip per-channel background neutralisation")
    ap.add_argument("--bg-smooth", type=float, default=1.0, help="sigma (px) of a sky-only luminance blur; 0 = off, 0.8-1.5 is gentle")
    ap.add_argument("--bg-desat", type=float, default=0.7, help="desaturate sky-background colour noise (0 = off, 1 = fully grey sky)")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270], help="rotate output (Seestar frames are portrait)")
    a = ap.parse_args()

    rgb = load_rgb(a.inp)
    _, H, W = rgb.shape
    cy, cx = int(H * a.crop), int(W * a.crop)
    rgb = rgb[:, cy:H - cy, cx:W - cx]
    if a.rotate:
        rgb = np.rot90(rgb, k=a.rotate // 90, axes=(1, 2))
    lin = neutralize_background(rgb) if not a.no_neutralize else rgb.copy()

    def finish(target_bg, asinh, sat):
        x = autostretch(lin, target_bg, a.shadow_sigma)
        if not a.no_neutralize:
            x = neutralize_background(x)
        if not a.no_scnr:
            x = scnr(x, a.scnr)
        x = asinh_color(x, asinh)
        x = saturate(x, sat)
        x = desaturate_background(x, a.bg_desat)
        x = smooth_background(x, a.bg_smooth)
        return x

    final = finish(a.target_bg, a.asinh, a.saturation)
    img8 = (to_img(final) * 255).round().astype(np.uint8)
    img16 = (to_img(final) * 65535).round().astype(np.uint16)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img8).save(f"{out}.png")
    Image.fromarray(img8).save(f"{out}.jpg", quality=93, subsampling=0)
    try:
        import tifffile
        tifffile.imwrite(f"{out}.tif", img16, photometric="rgb", compression="zlib")
    except ImportError:
        Image.fromarray(img8).save(f"{out}.tif")
    # preview strip: gentle / default / strong
    variants = [(a.target_bg * 0.7, max(a.asinh - 1.5, 0), 1.1), (a.target_bg, a.asinh, a.saturation), (a.target_bg * 1.35, a.asinh + 2, a.saturation + 0.15)]
    thumbs = []
    for tb, ah, s in variants:
        t = (to_img(finish(tb, ah, s)) * 255).astype(np.uint8)
        im = Image.fromarray(t); im.thumbnail((700, 700)); thumbs.append(im)
    wsum = sum(t.width for t in thumbs) + 10 * (len(thumbs) - 1)
    strip = Image.new("RGB", (wsum, max(t.height for t in thumbs)), (20, 20, 20))
    x = 0
    for t in thumbs:
        strip.paste(t, (x, 0)); x += t.width + 10
    strip.save(f"{out}_preview_strip.jpg", quality=88)
    print(f"wrote {out}.tif/.png/.jpg and {out}_preview_strip.jpg  (gentle | default | strong)")


if __name__ == "__main__":
    main()

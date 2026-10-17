#!/usr/bin/env python
"""
Register a before/after pair of stretched images onto one geometry so a comparison slider shows the same sky at
the same scale and rotation, then crop both to their common overlap.  With --ref, every pair of a series is put on
one reference geometry (optionally padded so images larger than the reference keep their edges), so all the
comparisons share the same orientation.

  align_pair.py BEFORE AFTER --out-before B.png --out-after A.png [--ref REF [--pad 0.1]] [--metrics M.json] [--max-residual 1.0]

Nights are framed at different field rotations (alt-az), and crops differ between runs, so pixel (x, y) is not the
same sky in two outputs.  For each image that has to move (BEFORE, plus AFTER when --ref is given):

  1. stars are detected with sep on the luminance (background-subtracted, 5 sigma, brightest 400 kept);
  2. astroalign matches star triangles and fits a similarity transform (rotation + uniform scale + shift) onto
     the target's star list (target = REF if given, else AFTER);
  3. the fit is checked on every star, not only the control points: each transformed star is matched to its nearest
     target star within 3 px; the median distance of those matches is the residual reported (target pixels);
  4. the image is warped with Lanczos-4 resampling, and a validity mask is warped with it.

Both outputs are then cropped to the largest axis-aligned rectangle where both are valid (or, with --aspect, the
largest window of that aspect placed as close as possible to M31's nucleus, --center-up px above it), so they have
identical pixel dimensions.  The script exits non-zero if any residual is >= --max-residual (default 1 px).

Inputs: 8- or 16-bit PNG/TIFF/JPEG (3-channel).  Outputs: 16-bit PNG (intermediate files; never published as is).
"""
import argparse, json, sys
from pathlib import Path

import astroalign as aa
import cv2
import numpy as np
import sep
from scipy.spatial import cKDTree


def load(path):
    im = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if im is None:
        sys.exit(f"cannot read {path}")
    if im.ndim == 2:
        im = np.dstack([im] * 3)
    im = im[..., :3][..., ::-1]  # BGR -> RGB
    scale = 65535.0 if im.dtype == np.uint16 else 255.0
    return im.astype(np.float32) / scale


def stars(img, n=400):
    lum = np.ascontiguousarray(img.mean(axis=2).astype(np.float32))
    bkg = sep.Background(lum, bw=64, bh=64)
    sub = lum - bkg.back()
    sep.set_extract_pixstack(2_000_000)
    o = sep.extract(sub, 5.0, err=bkg.globalrms, minarea=5, deblend_cont=0.005)
    # drop huge or elongated blobs (galaxy core, M32, blends) and keep the brightest point sources
    # (saturated stars are kept: their centroids are still good, and JPEG renders clip most bright stars)
    o = o[(o["npix"] < 400) & (o["b"] / np.maximum(o["a"], 1e-6) > 0.6)]
    o = o[np.argsort(-o["flux"])][:n]
    return np.column_stack([o["x"], o["y"]])


def residual(T, src, dst, tol=3.0):
    p = T(src)
    d, _ = cKDTree(dst).query(p, distance_upper_bound=tol)
    d = d[np.isfinite(d)]
    return (float(np.median(d)) if len(d) else float("inf")), int(len(d))


def register(img, target_shape, target_xy):
    # Different renders reach different depths, so the brightest-N lists can overlap poorly; retry with other N
    # and keep the fit whose residual over all stars is smallest.
    best = None
    tries = [(400, 400, 60), (200, 200, 50), (800, 800, 80), (120, 200, 40), (300, 150, 50), (150, 300, 50),
             (600, 300, 100), (300, 600, 100)]
    for n_src, n_tgt, n_ctrl in tries:
        src_xy = stars(img, n_src)
        try:
            T, (s_pts, d_pts) = aa.find_transform(src_xy, target_xy[:n_tgt], max_control_points=n_ctrl)
        except Exception:
            continue
        med, n = residual(T, src_xy, target_xy)
        if n >= 30 and (best is None or med < best[1]):
            best = (T, med, n, s_pts)
        if best and best[1] < 0.5:
            break
    if best is None:
        raise SystemExit("no acceptable star match found")
    T, med, n, s_pts = best
    H, W = target_shape
    M = T.params[:2, :]
    warped = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    valid = cv2.warpAffine(np.ones(img.shape[:2], np.uint8), M, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)
    valid = cv2.erode(valid, np.ones((7, 7), np.uint8))  # drop the resampling fringe at the edges
    info = dict(method="astroalign similarity", n_stars=n, n_control=int(len(s_pts)),
                median_residual_px=round(med, 3), rotation_deg=round(float(np.degrees(T.rotation)), 3),
                scale=round(float(T.scale), 5), shift_px=[round(float(v), 2) for v in T.translation])
    return np.clip(warped, 0, 1), valid.astype(bool), info


def largest_rectangle(mask, step=4):
    """Largest axis-aligned all-True rectangle (histogram method on a step-x downsampled mask)."""
    # a downsampled cell counts only if its whole step x step block is valid
    h, w = mask.shape[0] // step, mask.shape[1] // step
    blk = mask[: h * step, : w * step].reshape(h, step, w, step).all(axis=(1, 3))
    heights = np.zeros(w, int); best = (0, 0, 0, 0, 0)
    for r in range(h):
        heights = np.where(blk[r], heights + 1, 0)
        st = []
        for c in range(w + 1):
            hc = heights[c] if c < w else 0
            start = c
            while st and st[-1][1] >= hc:
                s, sh = st.pop()
                area = sh * (c - s)
                if area > best[0]:
                    best = (area, r - sh + 1, s, sh, c - s)
                start = s
            st.append((start, hc))
    _, r0, c0, hh, ww = best
    return int(r0 * step), int(c0 * step), int(hh * step), int(ww * step)


def aspect_rectangle(mask, aspect, target_xy, step=4, slack=0.97):
    """Largest all-valid rectangle of the given width/height aspect, placed as close as possible to TARGET_XY
    (its centre).  Uses a summed-area table on a step-x downsampled mask; the size is backed off by SLACK from
    the maximum so the window can move towards the target."""
    h, w = mask.shape[0] // step, mask.shape[1] // step
    blk = mask[: h * step, : w * step].reshape(h, step, w, step).all(axis=(1, 3)).astype(np.int32)
    sat = np.pad(blk.cumsum(0).cumsum(1), ((1, 0), (1, 0)))

    def fits(ww):
        hh = max(1, int(round(ww / aspect)))
        if ww > w or hh > h:
            return None
        tot = sat[hh:, ww:] - sat[:-hh, ww:] - sat[hh:, :-ww] + sat[:-hh, :-ww]
        return hh, tot == ww * hh

    lo, hi = 1, w
    while lo < hi:  # largest width that fits anywhere
        mid = (lo + hi + 1) // 2
        f = fits(mid)
        lo, hi = (mid, hi) if f is not None and f[1].any() else (lo, mid - 1)
    ww = max(1, int(lo * slack))
    hh, ok = fits(ww)
    ys, xs = np.nonzero(ok)
    tx, ty = target_xy[0] / step - ww / 2, target_xy[1] / step - hh / 2
    i = int(np.argmin((xs - tx) ** 2 + (ys - ty) ** 2))
    return int(ys[i] * step), int(xs[i] * step), int(hh * step), int(ww * step)


def nucleus(img):
    lum = img.mean(axis=2).astype(np.float32)
    y, x = np.unravel_index(np.argmax(cv2.GaussianBlur(lum, (0, 0), 25)), lum.shape)
    return float(x), float(y)


def save16(path, img):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), (np.clip(img, 0, 1) * 65535 + 0.5).astype(np.uint16)[..., ::-1])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before"); ap.add_argument("after")
    ap.add_argument("--ref", help="common reference geometry for both (default: AFTER's own geometry)")
    ap.add_argument("--out-before", required=True); ap.add_argument("--out-after", required=True)
    ap.add_argument("--pad", type=float, default=0.0, help="with --ref: enlarge the reference canvas by this fraction per side")
    ap.add_argument("--metrics", help="write alignment metrics JSON here")
    ap.add_argument("--max-residual", type=float, default=1.0)
    ap.add_argument("--aspect", help="crop to the largest window of this aspect (e.g. 4:3) centred on M31's nucleus")
    ap.add_argument("--center-up", type=float, default=0.0, help="with --aspect: aim the window this many px above the nucleus")
    a = ap.parse_args()

    before, after = load(a.before), load(a.after)
    if a.ref:
        ref = load(a.ref); H, W = ref.shape[:2]
        py, px = int(round(a.pad * H)), int(round(a.pad * W))
        ref_xy = stars(ref, 800) + np.array([px, py]); shape = (H + 2 * py, W + 2 * px)
        b_w, b_valid, b_info = register(before, shape, ref_xy)
        a_w, a_valid, a_info = register(after, shape, ref_xy)
    else:
        shape = after.shape[:2]
        b_w, b_valid, b_info = register(before, shape, stars(after, 800))
        a_w, a_valid = after, np.ones(shape, bool)
        a_info = dict(method="identity (target geometry)", n_stars=None, median_residual_px=0.0)
    if a.aspect:
        aw, ah = (float(v) for v in a.aspect.split(":"))
        nx, ny = nucleus(a_w)
        r0, c0, hh, ww = aspect_rectangle(b_valid & a_valid, aw / ah, (nx, ny - a.center_up))
    else:
        r0, c0, hh, ww = largest_rectangle(b_valid & a_valid)
    b_c, a_c = b_w[r0:r0 + hh, c0:c0 + ww], a_w[r0:r0 + hh, c0:c0 + ww]
    save16(a.out_before, b_c); save16(a.out_after, a_c)

    worst = max(b_info["median_residual_px"], a_info["median_residual_px"])
    res = dict(before=dict(src=a.before, **b_info), after=dict(src=a.after, **a_info), ref=a.ref,
               crop=dict(x=c0, y=r0, width=ww, height=hh), output_size=[ww, hh], worst_median_residual_px=worst,
               method="astroalign similarity", n_stars=min(x for x in (b_info["n_stars"], a_info["n_stars"]) if x),
               median_residual_px=worst)
    if a.metrics:
        Path(a.metrics).parent.mkdir(parents=True, exist_ok=True)
        Path(a.metrics).write_text(json.dumps(res, indent=1))
    print(f"before: {b_info['n_stars']} stars matched, median residual {b_info['median_residual_px']:.3f} px, "
          f"rot {b_info.get('rotation_deg')} deg, scale {b_info.get('scale')}")
    print(f"after:  {a_info['method']}, residual {a_info['median_residual_px']:.3f} px")
    print(f"common crop {ww}x{hh} at ({c0},{r0})")
    if worst >= a.max_residual:
        sys.exit(f"alignment residual {worst:.3f} px >= {a.max_residual} px")


if __name__ == "__main__":
    main()

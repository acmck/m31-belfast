#!/usr/bin/env python
"""
Measure every render the journal site shows, align and export each stage's before/after pair, and write
site/src/data/journal.json.  Everything on the page comes from here, and every number carries its provenance.

  build_journal.py measure  [--spec SPEC] [--only RENDER ...] [--force]
  build_journal.py images   [--spec SPEC] [--only STAGE ...]
  build_journal.py build    [--spec SPEC] [--conditions site/src/data/metrics/conditions.json] [--out site/src/data/journal.json]
  build_journal.py all      (measure + images + build)

SPEC (default site/src/data/metrics/stages_spec.json) lists
  renders: id -> {label, image (stretched PNG/TIFF/JPEG), linear (last linear image before denoise), stack (linear
           stack for FWHM, defaults to linear), wcs (file whose header has the plate solution, default linear),
           nights [ids], frames / minutes (optional overrides; default STACKCNT and LIVETIME of the stack header),
           extra: ["ring", "color"] for stage-specific measurements}
  stages:  [{id, title, before, after, dates, what_changed, what_it_did}]  -- the two sentences are templates
           filled from the measurements ({before.snr_headline}, {after.fwhm_arcsec}, {d.snr_pct}, ...)
  ref, pad: the render whose geometry every pair is registered onto (so all stages share one orientation)

What is measured (per render, written to site/src/data/metrics/renders/<id>.json):
  SNR at fixed M31 positions.  The image's own plate solution (WCS) places eight points at fixed offsets from
  M31's nucleus (RA 00h42m44.3s, Dec +41d16m07s) along the major axis (PA 38 deg: 20' and 40' each way) and the
  minor axis (10' and 20' each way), clear of M32 and NGC 205.  Binned 4x4 (14.7"; removes the different
  pixel-to-pixel correlation of drizzle and of the scope's own debayering), per channel: a plane is fitted to sky
  pixels and subtracted; signal = median within 1.2' of each point; noise = 1.4826 x MAD of the sky after a 9-px
  median high-pass.  The three channel SNRs are added in quadrature (the SNR of the optimally weighted channel
  sum), which makes the figure independent of colour-calibration factors.  Headline = median of the eight points.
  FWHM, sky noise, frame count and minutes: scripts/measure_stack.py on the linear stack, and its FITS header.
  ring (stage 03): radial profile of the starless linear image around the nucleus; ring depth = the largest dip
  of the profile below the brightest point further out, in % of that point.
  color (stage 05): per-channel signal at the eight points; disk colour as R/G and B/G ratios.
Nothing from the FITS headers other than frame count and live time is written out, and no coordinates.
"""
import argparse, json, subprocess, sys, warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
from astropy.wcs.utils import proj_plane_pixel_scales
import astropy.units as u
from scipy.ndimage import gaussian_filter, median_filter

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from measure_stack import measure as measure_stack_fn  # noqa: E402

METRICS = ROOT / "site/src/data/metrics"
M31 = SkyCoord("00h42m44.3s", "+41d16m07s")
PA_MAJOR = 38.0
POSITIONS = [  # name, position angle (deg E of N), offset (arcmin)
    ("major 20′ NE", PA_MAJOR, 20), ("major 40′ NE", PA_MAJOR, 40),
    ("major 20′ SW", PA_MAJOR + 180, 20), ("major 40′ SW", PA_MAJOR + 180, 40),
    ("minor 10′ SE", PA_MAJOR + 90, 10), ("minor 20′ SE", PA_MAJOR + 90, 20),
    ("minor 10′ NW", PA_MAJOR + 270, 10), ("minor 20′ NW", PA_MAJOR + 270, 20),
]
BIN, R_ARCMIN = 4, 1.2


def rel(p):
    p = Path(p)
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return "scratch:" + p.name


def load_rgb(path):
    d = np.asarray(fits.getdata(path), dtype=np.float64)
    if d.ndim == 3 and d.shape[-1] in (3, 4) and d.shape[0] not in (3, 4):
        d = np.moveaxis(d, -1, 0)
    if d.ndim == 2:
        d = d[None]
    return d[:3]


def binned(a, b=BIN):
    H, W = (a.shape[-2] // b) * b, (a.shape[-1] // b) * b
    a = a[..., :H, :W]
    return a.reshape(*a.shape[:-2], H // b, b, W // b, b).mean(axis=(-3, -1))


def mad(x):
    x = x[np.isfinite(x)]
    m = np.median(x)
    return m, float(np.median(np.abs(x - m)))


def sky_model(lum):
    """sky mask (heavily smoothed image below median + 1 MAD, central 70 %) and a plane fitted to it"""
    H, W = lum.shape
    core = np.zeros(lum.shape, bool); core[int(H * .15):H - int(H * .15), int(W * .15):W - int(W * .15)] = True
    valid = np.isfinite(lum) & (lum != 0)
    sm = gaussian_filter(np.where(valid, lum, np.median(lum[valid])), 65 / BIN)
    m, d = mad(sm[core & valid])
    sky = core & valid & (sm < m + d)
    yy, xx = np.mgrid[0:H, 0:W]
    A = np.column_stack([np.ones(sky.sum()), xx[sky], yy[sky]])
    coef, *_ = np.linalg.lstsq(A, lum[sky], rcond=None)
    plane = coef[0] + coef[1] * xx + coef[2] * yy
    return sky, plane, valid


def position_pixels(wcs_path, shape_unbinned):
    w = WCS(fits.getheader(wcs_path), naxis=2)
    scale = float(np.mean(proj_plane_pixel_scales(w)) * 3600)
    out = []
    for name, pa, off in POSITIONS:
        c = M31.directional_offset_by(pa * u.deg, off * u.arcmin)
        x, y = w.world_to_pixel(c)
        out.append((name, float(x), float(y)))
    nx, ny = w.world_to_pixel(M31)
    return out, (float(nx), float(ny)), scale


def measure_render(r):
    lin = ROOT / r["linear"] if not str(r["linear"]).startswith("/") else Path(r["linear"])
    wcs_p = Path(r.get("wcs") or lin); wcs_p = wcs_p if wcs_p.is_absolute() else ROOT / wcs_p
    stack_p = Path(r.get("stack") or lin); stack_p = stack_p if stack_p.is_absolute() else ROOT / stack_p
    rgb = load_rgb(lin)
    pos, nuc, scale = position_pixels(wcs_p, rgb.shape[1:])
    rgb_b = binned(rgb)
    lum = rgb_b.mean(axis=0)
    sky, _, valid = sky_model(lum)
    # per channel: plane fitted to the sky pixels, per-pixel noise from a median high-pass of the sky
    flat, noise = [], []
    for c in rgb_b:
        H, W = c.shape
        yy, xx = np.mgrid[0:H, 0:W]
        A = np.column_stack([np.ones(sky.sum()), xx[sky], yy[sky]])
        coef, *_ = np.linalg.lstsq(A, c[sky], rcond=None)
        f = c - (coef[0] + coef[1] * xx + coef[2] * yy)
        hp = f - median_filter(f, size=9, mode="reflect")
        flat.append(f); noise.append(1.4826 * mad(hp[sky])[1])
    rad = R_ARCMIN * 60 / (scale * BIN)
    snr, chan = {}, {}
    for name, x, y in pos:
        xb, yb = (x + 0.5) / BIN - 0.5, (y + 0.5) / BIN - 0.5
        disk = ((xx - xb) ** 2 + (yy - yb) ** 2 <= rad ** 2) & valid
        if disk.sum() < 0.8 * np.pi * rad ** 2:
            snr[name] = None  # outside the frame
            continue
        per = [float(np.median(flat[i][disk]) / noise[i]) for i in range(3)]
        # SNR of the optimally weighted sum of the three channels: invariant to colour-calibration scale factors
        snr[name] = round(float(np.sqrt(np.sum(np.square(np.clip(per, 0, None))))), 2)
        chan[name] = [float(np.median(flat[i][disk])) for i in range(3)]
    vals = [v for v in snr.values() if v is not None]
    # nucleus sanity check: WCS position vs the peak of a 40 px-smoothed image (unbinned px)
    sm = gaussian_filter(lum, 40 / BIN)
    nb = ((nuc[0] + 0.5) / BIN - 0.5, (nuc[1] + 0.5) / BIN - 0.5)
    win = sm[max(0, int(nb[1]) - 25):int(nb[1]) + 25, max(0, int(nb[0]) - 25):int(nb[0]) + 25]
    py, px = np.unravel_index(np.argmax(win), win.shape)
    nuc_off = float(np.hypot(px + max(0, int(nb[0]) - 25) - nb[0], py + max(0, int(nb[1]) - 25) - nb[1]) * BIN)

    ms = measure_stack_fn(str(stack_p), 1, scale)
    hdr = fits.getheader(stack_p)
    frames = r.get("frames") or int(hdr.get("STACKCNT", 0)) or None
    minutes = r.get("minutes") or (round(float(hdr.get("LIVETIME", 0)) / 60, 1) if hdr.get("LIVETIME") else None)
    if minutes is None and frames and r.get("exposure_s"):
        minutes = round(frames * r["exposure_s"] / 60, 1)
        r["minutes_source"] = f"STACKCNT x {r['exposure_s']} s (no LIVETIME in header)"
    res = dict(
        id=r["id"], label=r["label"], nights=r.get("nights", []),
        frames=frames, minutes=minutes,
        fwhm_arcsec=round(float(ms["fwhm_arcsec"]), 1) if np.isfinite(ms["fwhm_arcsec"]) else None,
        stars=int(ms["nstars"]), sky_noise_pct=round(float(ms["noise_pct"]), 3),
        snr=snr, snr_headline=round(float(np.median(vals)), 2) if vals else None,
        nucleus_wcs_vs_peak_px=round(nuc_off, 1),
        provenance=dict(
            snr=f"{rel(lin)} (WCS from {rel(wcs_p)}): binned {BIN}x{BIN}, per channel sky plane removed, median within "
                f"{R_ARCMIN}' of each position / sky noise (1.4826 MAD of 9-px median high-pass); channels combined "
                "in quadrature (SNR of the optimal channel sum)",
            fwhm_arcsec=f"scripts/measure_stack.py on {rel(stack_p)} at {scale:.3f}\"/px (from the plate solution)",
            frames="STACKCNT of " + rel(stack_p) if not r.get("frames") else "spec override: " + str(r.get("frames_source", "")),
            minutes=r.get("minutes_source") or ("LIVETIME of " + rel(stack_p)),
            image=rel(ROOT / r["image"]) if not str(r["image"]).startswith("/") else rel(r["image"]),
        ),
    )
    if "color" in r.get("extra", []) and chan:
        sig = np.median(np.array(list(chan.values())), axis=0)
        res["color"] = dict(r_over_g=round(float(sig[0] / sig[1]), 3), b_over_g=round(float(sig[2] / sig[1]), 3),
                            provenance=f"{rel(lin)}: per-channel median signal above sky at the eight positions")
    if "ring" in r.get("extra", []):
        res["ring"] = ring_depth(r, wcs_p, nuc)
    return res


def ring_depth(r, wcs_p, nuc):
    sl = Path(r["starless"]); sl = sl if sl.is_absolute() else ROOT / sl
    lum = load_rgb(sl).mean(axis=0)
    H, W = lum.shape
    yy, xx = np.mgrid[0:H, 0:W]
    rr = np.hypot(xx - nuc[0], yy - nuc[1])
    sel = rr < 400
    sky_v = float(np.median(lum[::8, ::8]))
    edges = np.arange(0, 402, 3)
    idx = np.digitize(rr[sel], edges)
    v = lum[sel]
    prof = np.array([np.median(v[idx == i]) if np.any(idx == i) else np.nan for i in range(1, len(edges))]) - sky_v
    best, at = 0.0, None
    for i in range(len(prof) - 1):
        outer = np.nanmax(prof[i + 1:])
        if outer > 0 and prof[i] < outer:
            d = (outer - prof[i]) / outer
            if d > best:
                best, at = d, float(edges[i])
    scale = float(np.mean(proj_plane_pixel_scales(WCS(fits.getheader(wcs_p), naxis=2))) * 3600)
    return dict(depth_pct=round(float(100 * best), 1), radius_arcmin=round(at * scale / 60, 1) if at is not None else None,
                provenance=f"{rel(sl)}: median radial profile (3 px annuli to 400 px) around the WCS nucleus, minus the "
                           "image median; depth = largest drop below the brightest point further out")


class A(dict):
    """dict with attribute access, for {before.snr_headline}-style copy templates"""
    __getattr__ = dict.__getitem__


def load_spec(p):
    return json.loads(Path(p).read_text())


def cmd_measure(spec, only, force):
    out = METRICS / "renders"; out.mkdir(parents=True, exist_ok=True)
    for rid, r in spec["renders"].items():
        if only and rid not in only:
            continue
        f = out / f"{rid}.json"
        if f.exists() and not force:
            continue
        r = dict(r, id=rid)
        m = measure_render(r)
        f.write_text(json.dumps(m, indent=1, ensure_ascii=False))
        print(f"{rid:<22} frames {m['frames']}  {m['minutes']} min  FWHM {m['fwhm_arcsec']}\"  SNR {m['snr_headline']}  "
              f"nucleus WCS-peak {m['nucleus_wcs_vs_peak_px']} px" + (f"  ring {m['ring']}" if 'ring' in m else "")
              + (f"  color {m['color']['r_over_g']}/{m['color']['b_over_g']}" if 'color' in m else ""))


def cmd_images(spec, only):
    scratch = ROOT / spec.get("scratch", "data/journal_pairs")
    for st in spec["stages"]:
        if only and st["id"] not in only:
            continue
        b, a = spec["renders"][st["before"]], spec["renders"][st["after"]]
        ip = lambda r: str(ROOT / r["image"]) if not str(r["image"]).startswith("/") else r["image"]
        ref = spec["renders"][spec["ref"]] if spec.get("ref") else None
        cmd = [sys.executable, str(ROOT / "scripts/align_pair.py"), ip(b), ip(a),
               "--out-before", str(scratch / st["id"] / "before.png"), "--out-after", str(scratch / st["id"] / "after.png"),
               "--metrics", str(METRICS / "alignment" / f"{st['id']}.json")]
        if ref:
            cmd += ["--ref", ip(ref), "--pad", str(spec.get("pad", 0.0))]
        subprocess.run(cmd, check=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/export_web.py"), str(scratch / st["id"] / "before.png"),
                        str(scratch / st["id"] / "after.png"), "--stage", st["id"],
                        "--out-root", str(ROOT / "site/public/img"), "--rotate", str(spec.get("rotate", 90)),
                        "--manifest", str(METRICS / "images" / f"{st['id']}.json")], check=True)


def side(m, cond_by_night):
    nights = m.get("nights", [])
    skies = []
    for n in nights:
        c = cond_by_night.get(n)
        if c is None:
            continue
        skies.append("dark" if c["moon"]["alt_deg_max"] <= 0 else f"moon {c['moon']['illum_pct']:.0f} %")
    sky = " + ".join(dict.fromkeys(skies)) if skies else None
    return dict(label=m["label"], frames=m["frames"], minutes=round(m["minutes"]) if m["minutes"] else None,
                fwhm_arcsec=m["fwhm_arcsec"], sky=sky, snr=m["snr"], snr_headline=m["snr_headline"])


def pct(a, b):
    return round(100 * (b - a) / a) if a and b else None


def fmt_utc(s):
    return s[11:16]


def night_entry(c, nid):
    w, mo = c["weather"], c["moon"]
    d0 = date.fromisoformat(nid)  # sessions are named by the evening the night started
    label = f"{d0.day}/{(d0 + timedelta(days=1)).day} {d0:%b %Y}"
    minutes = round(c["frames_kept"] * c["exposure_s"] / 60)
    moon_txt = (f"moon {mo['illum_pct']:.0f} % below horizon" if mo["alt_deg_max"] <= 0
                else f"moon {mo['illum_pct']:.0f} % up to {mo['alt_deg_max']:.0f}°")
    log = (f"{fmt_utc(c['utc_start'])}–{fmt_utc(c['utc_end'])} UTC · {c['frames_total']} × {c['exposure_s']:.0f} s · "
           f"{moon_txt} · cloud {w['cloud_pct_mean']:.0f} % · gusts {w['gust_mph_max']:.0f} mph · "
           f"{round(100 * c['scope_drop_rate'])} % dropped")
    return {
        "id": nid, "label": label, "utc_start": c["utc_start"], "utc_end": c["utc_end"],
        "exposure_s": c["exposure_s"], "frames_total": c["frames_total"], "frames_kept": c["frames_kept"],
        "qa_rejected": c["qa_rejected"], "scope_drop_rate": c["scope_drop_rate"], "minutes": minutes,
        "fwhm_arcsec": c["fwhm_arcsec_frames_median"], "sky_adu": c["sky_adu"],
        "moon": {k: mo[k] for k in ("illum_pct", "alt_deg_max", "alt_deg_mean")},
        "weather": {k: w[k] for k in ("cloud_pct_mean", "gust_mph_max", "humidity_pct_mean", "temp_minus_dew_c_min")},
        "weather_source": w["source"], "log_line": log,
    }


def cmd_build(spec, cond_path, out):
    cond = json.loads(Path(cond_path).read_text())
    by_night = {}
    for c in cond["nights"]:
        nid = c["session"].replace("M31_", "")
        by_night[nid] = c
    nights = [night_entry(c, nid) for nid, c in by_night.items()]
    rm = {rid: json.loads((METRICS / "renders" / f"{rid}.json").read_text()) for rid in spec["renders"]}
    stages, chart = [], []
    for i, st in enumerate(spec["stages"]):
        b, a = rm[st["before"]], rm[st["after"]]
        al = json.loads((METRICS / "alignment" / f"{st['id']}.json").read_text())
        im = json.loads((METRICS / "images" / f"{st['id']}.json").read_text())["images"]
        ctx = dict(before=A(b), after=A(a), n=by_night, d=A(
            snr_pct=pct(b["snr_headline"], a["snr_headline"]), fwhm_pct=pct(b["fwhm_arcsec"], a["fwhm_arcsec"]),
            minutes_ratio=round(a["minutes"] / b["minutes"], 2) if a["minutes"] and b["minutes"] else None,
            sqrt_t_pct=round(100 * ((a["minutes"] / b["minutes"]) ** 0.5 - 1)) if a["minutes"] and b["minutes"] else None))
        fill = lambda s: s.format(**ctx) if s else s
        img = {}
        for k in ("before", "after"):
            e = im[k]
            img[k] = {"avif": e["avif"], "webp": e["webp"], "avif_1200": e["avif_1200"], "webp_1200": e["webp_1200"],
                      "width": e["width"], "height": e["height"]}
        img["placeholder"] = im["placeholder"]
        worst = max(al["before"]["median_residual_px"], al["after"]["median_residual_px"])
        stages.append({
            "id": st["id"], "index": i, "title": st["title"], "dates": st["dates"], "night_ids": st.get("night_ids", st["dates"]),
            "before": side(b, by_night), "after": side(a, by_night),
            "what_changed": fill(st["what_changed"]), "what_it_did": fill(st["what_it_did"]),
            "images": img,
            "alignment": {"method": "astroalign similarity",
                          "n_stars": min(x for x in (al["before"].get("n_stars"), al["after"].get("n_stars")) if x),
                          "median_residual_px": round(worst, 3)},
        })
        chart.append({"stage_id": st["id"], "label": a["label"], "minutes": round(a["minutes"]) if a["minutes"] else None,
                      "snr": a["snr_headline"]})
    js = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "nights": nights, "stages": stages,
          "chart": chart, "hero_stage_id": spec.get("hero_stage_id", stages[-1]["id"]),
          "method": {"snr": __doc__.split("What is measured")[1].split("Nothing from")[0].strip()}}
    Path(out).write_text(json.dumps(js, indent=1, ensure_ascii=False))
    print(f"wrote {out}: {len(nights)} nights, {len(stages)} stages")
    for s in stages:
        print(f"  {s['id']:<11} SNR {s['before']['snr_headline']} -> {s['after']['snr_headline']}   "
              f"FWHM {s['before']['fwhm_arcsec']} -> {s['after']['fwhm_arcsec']}   align {s['alignment']['median_residual_px']} px")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["measure", "images", "build", "all"])
    ap.add_argument("--spec", default=str(METRICS / "stages_spec.json"))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--conditions", default=str(METRICS / "conditions.json"))
    ap.add_argument("--out", default=str(ROOT / "site/src/data/journal.json"))
    a = ap.parse_args()
    spec = load_spec(a.spec)
    if a.cmd in ("measure", "all"):
        cmd_measure(spec, a.only, a.force)
    if a.cmd in ("images", "all"):
        cmd_images(spec, a.only)
    if a.cmd in ("build", "all"):
        cmd_build(spec, a.conditions, a.out)


if __name__ == "__main__":
    main()

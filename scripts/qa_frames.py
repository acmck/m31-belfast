#!/usr/bin/env python
"""
Per-frame quality assessment for Seestar (OSC, CFA) sub-frames.

For every light frame this measures, on a 2x2-binned green "superpixel" image:
  * star count, FWHM (native pixels), roundness (b/a), background level & RMS,
  * background gradient across the frame, saturated pixel count,
  * horizontal / vertical banding amplitude (high-passed row/column medians),
then flags outliers with robust (median / MAD) statistics and writes:
  <out>/frames.csv, <out>/summary.json, <out>/report.html, <out>/*.png
  <out>/selected.txt, <out>/rejected.txt
  <select_dir>/  -> hard links to the frames that passed (feed this to Siril)

Usage:
  qa_frames.py --lights DIR --out DIR --select-dir DIR [thresholds...]
"""
import argparse, base64, io, json, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import sep
from astropy.io import fits
from scipy.ndimage import median_filter

PIX_SCALE_DEFAULT = 3.74  # arcsec/px for S30 Pro (2.9 um @ 160 mm)


def robust(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    med = np.median(x)
    mad = 1.4826 * np.median(np.abs(x - med)) if len(x) else np.nan
    return med, mad


def band_metric(resid, mask, axis, window=31):
    """True stripe amplitude along `axis` (1 = rows, 0 = cols), separating banding from noise.
    Row offsets are estimated independently from the two halves of the frame; real banding is
    common to both (covariance), noise is not.  Returns (banding_rms, noise_rms) in ADU."""
    m = np.where(mask, np.nan, resid)
    if axis == 0:
        m = m.T
    W = m.shape[1]
    def prof(x):
        p = np.nanmedian(x, axis=1)
        bad = ~np.isfinite(p)
        if bad.any() and (~bad).any():
            p[bad] = np.interp(np.flatnonzero(bad), np.flatnonzero(~bad), p[~bad])
        return p - median_filter(np.nan_to_num(p), size=window, mode="reflect")
    w = max(int(W * 0.25), 32)          # outer strips only, keeps the target out of the estimate
    L, R = prof(m[:, :w]), prof(m[:, W - w :])
    cov = float(np.cov(L, R)[0, 1]); var = 0.5 * (float(L.var()) + float(R.var()))
    return float(np.sqrt(max(cov, 0))), float(np.sqrt(max(var - cov, 0)))


def measure(path, pix_scale):
    try:
        return _measure(path, pix_scale)
    except Exception as e:  # truncated / unreadable file: keep the row, it will be rejected
        return dict(file=os.path.basename(path), error=f"{type(e).__name__}: {e}", nstars=0)


def _measure(path, pix_scale):
    t0 = time.time()
    with fits.open(path, memmap=False) as h:
        hdr = h[0].header
        d = h[0].data.astype(np.float32)
    pat = hdr.get("BAYERPAT", "GRBG").strip()
    # green pixel offsets for the 4 common patterns
    goff = {"GRBG": [(0, 0), (1, 1)], "GBRG": [(0, 0), (1, 1)],
            "RGGB": [(0, 1), (1, 0)], "BGGR": [(0, 1), (1, 0)]}[pat]
    g = 0.5 * (d[goff[0][0]::2, goff[0][1]::2] + d[goff[1][0]::2, goff[1][1]::2])
    g = np.ascontiguousarray(g)

    bkg = sep.Background(g, bw=64, bh=64)
    back = bkg.back()
    sub = g - back
    rms = float(bkg.globalrms)
    objs = sep.extract(sub, thresh=5.0, err=rms, minarea=5, deblend_cont=0.005, clean=True)
    sat_level = 65535 - float(bkg.globalback)
    good = (objs["flag"] == 0) & (objs["peak"] < 0.8 * sat_level) & (objs["npix"] >= 5)
    o = objs[good]
    if len(o) >= 10:
        r50, _ = sep.flux_radius(sub, o["x"], o["y"], 6.0 * np.ones(len(o)), 0.5,
                                 normflux=o["flux"], subpix=5)
        r50 = r50[np.isfinite(r50) & (r50 > 0.3)]
        fwhm_px = 2.0 * float(np.median(r50)) * 2.0  # superpixel -> native
        roundness = float(np.median(o["b"] / o["a"]))
        ellip = float(np.median(1 - o["b"] / o["a"]))
        # brightest-star flux proxy for transparency (top 50 unsaturated stars)
        top = np.sort(o["flux"])[-50:]
        flux50 = float(np.median(top))
    else:
        fwhm_px, roundness, ellip, flux50 = np.nan, np.nan, np.nan, np.nan

    # star/galaxy mask for banding metric
    mask = sub > 3.0 * rms
    from scipy.ndimage import binary_dilation
    mask = binary_dilation(mask, iterations=1)
    band_row, band_row_noise = band_metric(sub, mask, axis=1)
    band_col, band_col_noise = band_metric(sub, mask, axis=0)

    # gradient: range of background model relative to level
    grad = float(np.percentile(back, 99) - np.percentile(back, 1))

    return dict(
        file=os.path.basename(path),
        date_obs=hdr.get("DATE-OBS"),
        exptime=hdr.get("EXPTIME"),
        ccd_temp=hdr.get("CCD-TEMP"),
        focuspos=hdr.get("FOCUSPOS"),
        gain=hdr.get("GAIN"),
        nstars=int(len(o)),
        fwhm_px=fwhm_px,
        fwhm_arcsec=fwhm_px * pix_scale if np.isfinite(fwhm_px) else np.nan,
        roundness=roundness,
        ellipticity=ellip,
        bg=float(bkg.globalback),
        bg_rms=rms,
        gradient=grad,
        flux_top50=flux50,
        sat_px=int((d >= 65000).sum()),
        band_row_rms=band_row, band_row_noise=band_row_noise,
        band_col_rms=band_col, band_col_noise=band_col_noise,
        secs=round(time.time() - t0, 2),
    )


def thumb_png(path, size=480):
    """Autostretched PNG thumbnail (base64) of the green superpixel image."""
    import matplotlib
    matplotlib.use("Agg")
    from PIL import Image
    d = fits.getdata(path).astype(np.float32)
    g = 0.5 * (d[0::2, 0::2] + d[1::2, 1::2])
    lo, hi = np.percentile(g, [1, 99.7])
    im = np.clip((g - lo) / max(hi - lo, 1), 0, 1) ** 0.5
    img = Image.fromarray((im * 255).astype(np.uint8))
    img.thumbnail((size, size))
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def cadence_stats(df):
    """Estimate how many exposures the Seestar itself discarded (it only saves frames its live stacker
    accepted).  Gaps between saved frames that are multiples of the normal cadence = dropped frames."""
    t = pd.to_datetime(df["date_obs"], errors="coerce").dropna().sort_values()
    if len(t) < 5:
        return {}
    dt = np.diff(t.to_numpy().astype("datetime64[s]").astype(float))
    exp = float(df["exptime"].median())
    cad = float(np.median(dt))
    slots = (t.iloc[-1] - t.iloc[0]).total_seconds() / cad + 1
    gaps = dt[dt > 1.5 * cad]
    dropped = int(round(sum(g / cad - 1 for g in gaps[gaps < 600])))  # gaps >10 min counted as pauses, not drops
    pauses = [(str(t.iloc[i].time())[:8], int(d)) for i, d in enumerate(dt) if d >= 600]
    return dict(saved_frames=int(len(t)), cadence_s=cad, per_frame_overhead_s=cad - exp,
                session_span_min=float((t.iloc[-1] - t.iloc[0]).total_seconds() / 60 + exp / 60),
                estimated_captured=int(round(slots)), scope_dropped_frames=dropped,
                scope_drop_rate=float(dropped / max(slots, 1)), long_pauses=pauses)


def make_plots(df, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = pd.to_datetime(df["date_obs"])
    cols = [("fwhm_arcsec", "FWHM (arcsec)"), ("roundness", "Roundness b/a"),
            ("nstars", "Stars detected"), ("bg", "Background (ADU)"),
            ("band_row_rms", "Row banding rms (ADU, noise-separated)"), ("flux_top50", "Top-50 star flux (transparency)"),
            ("gradient", "Background gradient (ADU)"), ("ccd_temp", "Sensor temp (C)")]
    fig, axes = plt.subplots(4, 2, figsize=(14, 13), sharex=True)
    for ax, (c, label) in zip(axes.ravel(), cols):
        ok = df["selected"]
        ax.plot(t[ok], df.loc[ok, c], ".", ms=3, color="#2a6f97", label="kept")
        ax.plot(t[~ok], df.loc[~ok, c], "x", ms=5, color="#d1495b", label="rejected")
        ax.set_title(label, fontsize=10); ax.grid(alpha=.3)
    axes[0, 0].legend(loc="best", fontsize=8)
    for ax in axes[-1]:
        ax.tick_params(axis="x", rotation=30)
    fig.suptitle(f"Frame QA - {len(df)} frames, {int(df['selected'].sum())} kept")
    fig.tight_layout(); fig.savefig(out / "qa_timeseries.png", dpi=110); plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(14, 3.6))
    for ax, c in zip(axes, ["fwhm_arcsec", "roundness", "bg"]):
        ax.hist(df[c].dropna(), bins=40, color="#2a6f97"); ax.set_title(c); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(out / "qa_hist.png", dpi=110); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lights", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--select-dir", help="directory to fill with hard links to kept frames")
    ap.add_argument("--workers", type=int, default=max(2, os.cpu_count() - 2))
    ap.add_argument("--pix-scale", type=float, default=PIX_SCALE_DEFAULT)
    ap.add_argument("--trend-window", type=int, default=41, help="frames in the rolling median used as the local trend")
    ap.add_argument("--fwhm-mad", type=float, default=3.0, help="reject if FWHM exceeds local trend by k*MAD of the deviations")
    ap.add_argument("--fwhm-min-delta", type=float, default=1.5, help="...with at least this many arcsec of headroom")
    ap.add_argument("--fwhm-ratio", type=float, default=1.30, help="also reject if FWHM > ratio * session median")
    ap.add_argument("--round-min", type=float, default=0.75, help="absolute roundness floor")
    ap.add_argument("--round-mad", type=float, default=3.0)
    ap.add_argument("--round-min-delta", type=float, default=0.06, help="minimum roundness drop below trend to reject")
    ap.add_argument("--nstars-frac", type=float, default=0.6, help="reject if stars < frac * local trend")
    ap.add_argument("--bg-mad", type=float, default=4.0, help="reject if background jumps above local trend by k*MAD")
    ap.add_argument("--bg-min-delta", type=float, default=15.0, help="...and by at least this many ADU")
    ap.add_argument("--flux-frac", type=float, default=0.6, help="reject if top-50 star flux < frac*median (haze)")
    ap.add_argument("--band-mad", type=float, default=5.0, help="reject if row banding > med + k*MAD ...")
    ap.add_argument("--band-min", type=float, default=2.0, help="...and > this many ADU")
    ap.add_argument("--no-thumbs", action="store_true")
    a = ap.parse_args()

    lights = sorted(Path(a.lights).glob("*.fit")) + sorted(Path(a.lights).glob("*.fits"))
    if not lights:
        sys.exit(f"no FITS in {a.lights}")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    print(f"measuring {len(lights)} frames with {a.workers} workers ...")
    t0 = time.time()
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(measure, [str(p) for p in lights], [a.pix_scale] * len(lights), chunksize=4))
    df = pd.DataFrame(rows).sort_values("date_obs").reset_index(drop=True)
    print(f"  measured in {time.time()-t0:.0f}s")

    # ---- robust rejection
    # Seeing, focus and altitude drift through a session, so FWHM / roundness / star counts are
    # judged against a rolling median (local trend) as well as against absolute caps.
    def detrended(x, win=a.trend_window):
        base = x.rolling(win, center=True, min_periods=max(5, win // 4)).median()
        return x - base, base
    if "error" not in df.columns:
        df["error"] = np.nan
    reasons = [[] for _ in range(len(df))]
    def flag(mask, why):
        for i in np.where(mask.fillna(False).to_numpy())[0]:
            reasons[i].append(why)

    flag(df["error"].notna(), "unreadable file")
    flag(df.fwhm_arcsec.isna() & df["error"].isna(), "no stars measured")

    med, mad = robust(df.fwhm_arcsec)
    dev, base = detrended(df.fwhm_arcsec)
    _, dmad = robust(dev)
    lim = max(a.fwhm_mad * dmad, a.fwhm_min_delta)
    flag(dev > lim, f"fwhm > local trend +{lim:.1f}\"")
    flag(df.fwhm_arcsec > a.fwhm_ratio * med, f"fwhm > {a.fwhm_ratio:.2f}x session median ({a.fwhm_ratio*med:.1f}\")")

    rmed, rmad = robust(df.roundness)
    rdev, _ = detrended(df.roundness)
    _, rdmad = robust(rdev)
    rlim = max(a.round_mad * rdmad, a.round_min_delta)
    flag((rdev < -rlim) | (df.roundness < a.round_min), f"roundness < trend -{rlim:.2f} or < {a.round_min}")

    nmed, _ = robust(df.nstars)
    ndev, nbase = detrended(df.nstars)
    flag((df.nstars < a.nstars_frac * nbase) | (df.nstars < a.nstars_frac * nmed), f"stars < {a.nstars_frac:.0%} of trend")

    bmed, bmad = robust(df.bg)
    bdev, _ = detrended(df.bg)
    _, bdmad = robust(bdev)
    flag(bdev > max(a.bg_mad * bdmad, a.bg_min_delta), "background jump (cloud/light)")

    fmed, _ = robust(df.flux_top50)
    fdev, fbase = detrended(df.flux_top50)
    flag(df.flux_top50 < a.flux_frac * fbase, "low transparency")

    kmed, kmad = robust(df.band_row_rms)
    flag((df.band_row_rms > kmed + a.band_mad * kmad) & (df.band_row_rms > a.band_min), "severe banding")

    df["reject_reason"] = ["; ".join(r) for r in reasons]
    df["selected"] = df.reject_reason == ""
    df.to_csv(out / "frames.csv", index=False)
    (out / "selected.txt").write_text("\n".join(df.loc[df.selected, "file"]) + "\n")
    (out / "rejected.txt").write_text("\n".join(f"{f}\t{r}" for f, r in zip(df.loc[~df.selected, "file"], df.loc[~df.selected, "reject_reason"])) + "\n")

    cad = cadence_stats(df)
    summary = dict(
        n_frames=int(len(df)), n_selected=int(df.selected.sum()), n_rejected=int((~df.selected).sum()),
        scope_estimated_captured=cad.get("estimated_captured"), scope_dropped_frames=cad.get("scope_dropped_frames"),
        scope_drop_rate=cad.get("scope_drop_rate"), cadence_s=cad.get("cadence_s"), session_span_min=cad.get("session_span_min"),
        long_pauses=cad.get("long_pauses"),
        exposure_kept_min=float(df.loc[df.selected, "exptime"].sum() / 60),
        fwhm_arcsec_median=float(med), fwhm_arcsec_mad=float(mad),
        roundness_median=float(rmed), nstars_median=float(nmed),
        bg_median=float(bmed), bg_mad=float(bmad),
        band_row_rms_median=float(kmed), band_col_rms_median=float(robust(df.band_col_rms)[0]),
        sat_px_median=float(df.sat_px.median()),
        thresholds=vars(a),
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    make_plots(df, out)

    # ---- hard-link selected frames
    if a.select_dir:
        sd = Path(a.select_dir); sd.mkdir(parents=True, exist_ok=True)
        for old in sd.glob("*.fit*"):
            old.unlink()
        for f in df.loc[df.selected, "file"]:
            src = Path(a.lights) / f
            try:
                os.link(src, sd / f)
            except OSError:
                import shutil; shutil.copy2(src, sd / f)
        print(f"  {int(df.selected.sum())} frames linked into {sd}")

    # ---- HTML report
    rej = df[~df.selected]
    thumbs = ""
    if not a.no_thumbs and len(rej):
        cells = []
        for _, r in rej.iterrows():
            b64 = thumb_png(str(Path(a.lights) / r.file))
            cells.append(f'<figure><img src="data:image/png;base64,{b64}"><figcaption><b>{r.file}</b><br>{r.reject_reason}<br>FWHM {r.fwhm_arcsec:.1f}" round {r.roundness:.2f} stars {r.nstars} bg {r.bg:.0f}</figcaption></figure>')
        thumbs = "<h2>Rejected frames</h2><div class=grid>" + "".join(cells) + "</div>"
    tbl = df[["file", "date_obs", "nstars", "fwhm_arcsec", "roundness", "bg", "gradient", "band_row_rms", "sat_px", "flux_top50", "selected", "reject_reason"]].round(2)
    html = f"""<!doctype html><meta charset=utf-8><title>Frame QA</title>
<style>body{{font:14px system-ui;margin:24px;color:#222}} table{{border-collapse:collapse;font-size:12px}} td,th{{border:1px solid #ddd;padding:2px 6px}}
tr.rej{{background:#fde8ea}} .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}} figure{{margin:0}} img{{width:100%}} figcaption{{font-size:11px}}
pre{{background:#f4f4f4;padding:8px}}</style>
<h1>Frame QA: {Path(a.lights).parent.name}</h1>
<p><b>Frame budget:</b> the scope captured about <b>{cad.get("estimated_captured","?")}</b> exposures over {cad.get("session_span_min",0):.0f} min
(cadence {cad.get("cadence_s",0):.0f} s), saved <b>{len(df)}</b> (its live stacker dropped ~{cad.get("scope_dropped_frames","?")},
{100*cad.get("scope_drop_rate",0):.0f} %), and this QA kept <b>{int(df.selected.sum())}</b> = {summary["exposure_kept_min"]:.1f} min of integration.</p>
<pre>{json.dumps({k:v for k,v in summary.items() if k!='thresholds'}, indent=1)}</pre>
<img src="qa_timeseries.png" style="max-width:1400px"><br><img src="qa_hist.png" style="max-width:1400px">
{thumbs}
<h2>All frames</h2>
{tbl.to_html(index=False, classes='t')}
"""
    (out / "report.html").write_text(html)
    print(json.dumps({k: v for k, v in summary.items() if k != "thresholds"}, indent=1))
    print(f"report: {out/'report.html'}")


if __name__ == "__main__":
    main()

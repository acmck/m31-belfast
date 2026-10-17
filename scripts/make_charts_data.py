#!/usr/bin/env python
"""
Data for three journal charts -> site/src/data/charts.json (+ site/src/data/metrics/charts_provenance.json)

  focus         per night, per-frame star FWHM (arcsec, qa/frames.csv fwhm_arcsec = fwhm_px x 3.665"/px) against
                UTC time, in time bins (<= 200 per night): median/min/max of the QA-kept frames, plus the count and
                median FWHM of QA-rejected frames in the bin.  start/end FWHM = median of the kept frames in the
                first and last 15 minutes of kept frames (25 Sep's last 23 minutes were all rejected: cloud).
  funnel        per night: exposure slots -> saved by the scope -> dropped by the scope -> rejected by QA ->
                excluded by the stacker -> stacked, from the slot model of make_story_frames.py and the
                "three-nights" step of story_frames.json (the current combined stack); checked to reconcile.
  wind          per night and UTC hour: Open-Meteo hourly gust (max of the preceding hour, so the hour
                [H, H+1) uses the value stamped H+1) at Belfast city centre, slots and scope drops in that hour
                (dropped slots are placed in their cadence gap).  Pearson r of drop rate vs gust over the
                night-hours with >= MIN_SLOTS slots.
  clear_nights  one row per night (the evening's date) 2026-09-01 .. 2026-10-16: mean cloud cover over the hourly
                samples 21:00 .. 03:00 UTC (7 samples), usable if <= 30 %, moon illumination at 00:00 UTC.
  first_light   snr / minutes / fwhm of the Seestar live stack (journal.json timeline step 1, "before").

Weather comes from Open-Meteo at the public Belfast city-centre point only (night_conditions.BELFAST).  The
outputs carry no coordinates, request URLs, file names or FITS keys.

  .venv/bin/python scripts/make_charts_data.py
"""
import json, math, sys, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from night_conditions import APIS, BELFAST, moon_info  # noqa: E402
from make_story_frames import NIGHTS, slots  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "data"
OUT = ROOT / "site/src/data/charts.json"
PROV = ROOT / "site/src/data/metrics/charts_provenance.json"
STORY = ROOT / "site/src/data/metrics/story_frames.json"
JOURNAL = ROOT / "site/src/data/journal.json"

MAX_POINTS = 200
EDGE_MIN = 15          # start/end FWHM window
MIN_SLOTS = 30         # night-hours with fewer slots (< ~5.5 min of imaging) are left out of the correlation
CLEAR_FROM, CLEAR_TO = date(2026, 9, 1), date(2026, 10, 16)
WIN_START_H, WIN_END_H = 21, 3   # cloud window, UTC, hourly samples inclusive
USABLE_CLOUD = 30.0
FORECAST = ("open-meteo forecast (past_days)", "https://api.open-meteo.com/v1/forecast")


def r1(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 1)


def iso(t):
    return pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- focus
def focus(session):
    df = pd.read_csv(D / session / "qa/frames.csv")
    df["t"] = pd.to_datetime(df["date_obs"])
    df = df.sort_values("t").reset_index(drop=True)
    df["sel"] = df["selected"].astype(bool)
    t0, t1 = df.t.iloc[0], df.t.iloc[-1]
    span_s = (t1 - t0).total_seconds()
    bin_s = max(60, math.ceil(span_s / MAX_POINTS / 60) * 60)
    df["bin"] = ((df.t - t0).dt.total_seconds() // bin_s).astype(int)
    pts = []
    for b, g in df.groupby("bin"):
        k = g.loc[g.sel, "fwhm_arcsec"].dropna()
        rj = g.loc[~g.sel]
        pts.append(dict(t=iso(t0 + pd.Timedelta(seconds=(b + 0.5) * bin_s)),
                        med=r1(k.median()) if len(k) else None, lo=r1(k.min()) if len(k) else None,
                        hi=r1(k.max()) if len(k) else None, n=int(len(k)),
                        rej=int(len(rj)), rej_med=r1(rj.fwhm_arcsec.median()) if rj.fwhm_arcsec.notna().any() else None))
    assert len(pts) <= MAX_POINTS
    edge = pd.Timedelta(minutes=EDGE_MIN)
    kept = df[df.sel & df.fwhm_arcsec.notna()]
    allf = df[df.fwhm_arcsec.notna()]
    k0, k1 = kept.t.iloc[0], kept.t.iloc[-1]  # windows anchored on the first/last kept frame (25 Sep ends in cloud)

    def edges(x):
        return r1(x.loc[x.t <= k0 + edge, "fwhm_arcsec"].median()), r1(x.loc[x.t >= k1 - edge, "fwhm_arcsec"].median())
    s, e = edges(kept)
    sa, ea = edges(allf)
    return dict(utc_start=iso(t0), utc_end=iso(t1), bin_s=bin_s, frames=int(len(df)), kept=int(df.sel.sum()),
                rejected=int((~df.sel).sum()), median_kept=r1(kept.fwhm_arcsec.median()),
                kept_utc_start=iso(k0), kept_utc_end=iso(k1), start_fwhm=s, end_fwhm=e, drift=r1(e - s), start_fwhm_all_saved=sa, end_fwhm_all_saved=ea,
                points=pts)


# ---------------------------------------------------------------- weather
def fetch_hourly(var, start, end):
    """Hourly `var` at Belfast city centre, start..end dates inclusive -> ({utc datetime: value}, source per hour)."""
    params = dict(latitude=BELFAST[0], longitude=BELFAST[1], hourly=var, wind_speed_unit="mph", timezone="UTC")
    vals, src = {}, {}

    def take(js, name):
        h = js.get("hourly", {})
        for tt, v in zip(h.get("time", []), h.get(var, [])):
            ts = datetime.fromisoformat(tt).replace(tzinfo=timezone.utc)
            if v is not None and ts not in vals and start <= ts.date() <= end:
                vals[ts] = float(v); src[ts] = name

    name, base = APIS[0]  # ERA5 archive
    try:
        q = dict(params, start_date=start.isoformat(), end_date=end.isoformat())
        with urllib.request.urlopen(base + "?" + urllib.parse.urlencode(q), timeout=90) as r:
            take(json.load(r), name)
    except Exception as e:
        print(f"{name}: {e}", file=sys.stderr)
    want = [datetime.combine(start, datetime.min.time(), timezone.utc) + timedelta(hours=i)
            for i in range(((end - start).days + 1) * 24)]
    if any(w not in vals for w in want):
        name, base = FORECAST
        past = (datetime.now(timezone.utc).date() - start).days + 1
        q = dict(params, past_days=min(92, past), forecast_days=1)
        with urllib.request.urlopen(base + "?" + urllib.parse.urlencode(q), timeout=90) as r:
            take(json.load(r), name)
    return vals, src


# ---------------------------------------------------------------- funnel + wind
def slot_times(session):
    seq, df, cad = slots(session)
    times, files = [], []
    last = None
    k = 0
    tt = pd.to_datetime(df["date_obs"]).sort_values().tolist()
    it = iter(tt)
    for f in seq:
        if f is None:
            k += 1
            times.append(last + pd.Timedelta(seconds=k * cad))
        else:
            last = next(it); k = 0
            times.append(last)
        files.append(f)
    return pd.DataFrame(dict(t=times, dropped=[f is None for f in files])), cad


def main():
    story = json.load(open(STORY))
    journal = json.load(open(JOURNAL))
    stack = story["steps"]["three-nights"]["counts"]
    out = dict(generated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), location="Belfast",
               focus={}, funnel={}, wind={}, clear_nights={}, first_light={})

    # focus
    for nid, sess in NIGHTS.items():
        out["focus"][nid] = focus(sess)

    # funnel
    for nid, sess in NIGHTS.items():
        n = story["nights"][nid]; c = stack[nid]
        row = dict(slots=n["slots"], saved=n["saved"], dropped_by_scope=n["scope_dropped"],
                   rejected_by_qa=c.get("q", 0), excluded_by_stacker=c.get("z", 0), stacked=c.get("k", 0))
        assert row["slots"] == row["saved"] + row["dropped_by_scope"]
        assert row["saved"] == row["rejected_by_qa"] + row["excluded_by_stacker"] + row["stacked"]
        jn = next(x for x in journal["nights"] if x["id"] == nid)
        assert row["saved"] == jn["frames_total"] and row["rejected_by_qa"] == jn["qa_rejected"]
        out["funnel"][nid] = row
    total_stacked = sum(r["stacked"] for r in out["funnel"].values())
    assert total_stacked == journal["hero"]["after"]["frames"], (total_stacked, journal["hero"]["after"]["frames"])

    # wind
    gust_src = {}
    pairs = []
    for nid, sess in NIGHTS.items():
        st, cad = slot_times(sess)
        assert int(st.dropped.sum()) == out["funnel"][nid]["dropped_by_scope"] and len(st) == out["funnel"][nid]["slots"]
        st["hour"] = st.t.dt.floor("h")
        d0, d1 = st.t.min().date(), (st.t.max() + pd.Timedelta(hours=1)).date()
        g, src = fetch_hourly("wind_gusts_10m", d0, d1)
        rows = []
        for h, grp in st.groupby("hour"):
            key = (h + pd.Timedelta(hours=1)).to_pydatetime().replace(tzinfo=timezone.utc)  # gust = max of preceding hour
            gust = g.get(key)
            gust_src.setdefault(src.get(key), 0); gust_src[src.get(key)] += 1
            sl, dr = int(len(grp)), int(grp.dropped.sum())
            rows.append(dict(hour_utc=h.strftime("%Y-%m-%dT%H:00Z"), gust_mph=r1(gust), slots=sl, dropped=dr,
                             drop_rate=round(dr / sl, 3)))
            if sl >= MIN_SLOTS and gust is not None:
                pairs.append((gust, dr / sl, nid))
        assert sum(r["dropped"] for r in rows) == out["funnel"][nid]["dropped_by_scope"]
        out["wind"][nid] = rows
    x = np.array([p[0] for p in pairs]); y = np.array([p[1] for p in pairs])
    r = float(np.corrcoef(x, y)[0, 1])
    # within-night (night means removed): does wind explain hour-to-hour changes, not just night-to-night ones?
    xd, yd = x.copy(), y.copy()
    for nid in NIGHTS:
        m = np.array([p[2] == nid for p in pairs])
        xd[m] -= x[m].mean(); yd[m] -= y[m].mean()
    r_within = float(np.corrcoef(xd, yd)[0, 1])
    # two-sided p for Pearson r via the t distribution
    from scipy import stats
    p = float(stats.pearsonr(x, y).pvalue)
    out["wind_correlation"] = dict(
        pearson_r=round(r, 2), n_hours=int(len(pairs)), p_value=round(p, 3), r_squared=round(r * r, 2),
        pearson_r_within_night=round(r_within, 2),
        min_slots_per_hour=MIN_SLOTS,
        note=f"Drop rate vs city-centre gust over night-hours with >= {MIN_SLOTS} exposure slots. "
             "Gusts are a city-centre model value, not a measurement at the scope; a sheltered garden can differ.")

    # clear nights
    cloud, csrc = fetch_hourly("cloud_cover", CLEAR_FROM, CLEAR_TO + timedelta(days=1))
    imaged = set(NIGHTS)
    rows, srcs = [], {}
    d = CLEAR_FROM
    while d <= CLEAR_TO:
        base = datetime.combine(d, datetime.min.time(), timezone.utc)
        hrs = [base + timedelta(hours=h) for h in range(WIN_START_H, 24 + WIN_END_H + 1)]
        v = [cloud.get(h) for h in hrs]
        ok = [a for a in v if a is not None]
        for h in hrs:
            srcs[csrc.get(h)] = srcs.get(csrc.get(h), 0) + 1
        mean = float(np.mean(ok)) if len(ok) == len(hrs) else None
        mid = base + timedelta(days=1)
        moon = moon_info(mid - timedelta(hours=3), mid + timedelta(hours=3))
        rows.append(dict(night=d.isoformat(), cloud_pct_mean=r1(mean), usable=None if mean is None else bool(mean <= USABLE_CLOUD),
                         imaged=d.isoformat() in imaged, moon_illum_pct=round(moon["illum_pct"]),
                         moon_alt_max_deg=round(moon["alt_deg_max"])))
        d += timedelta(days=1)
    usable = sum(1 for x_ in rows if x_["usable"])
    have = sum(1 for x_ in rows if x_["usable"] is not None)
    out["clear_nights"] = dict(
        window_utc=f"{WIN_START_H:02d}:00-{WIN_END_H:02d}:00 (hourly samples {WIN_START_H:02d}:00 to {WIN_END_H:02d}:00 inclusive, 7 per night)",
        usable_rule=f"mean cloud cover <= {USABLE_CLOUD:.0f} %", usable=usable, total=have,
        imaged_usable=sum(1 for x_ in rows if x_["imaged"] and x_["usable"]),
        source=", ".join(sorted(k for k in srcs if k)), rows=rows)

    # first light
    fl = journal["timeline"][0]["before"]
    out["first_light"] = dict(label=fl["label"], snr=fl["snr_headline"], minutes=fl["minutes"], fwhm=fl["fwhm_arcsec"])

    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")

    prov = dict(
        generated=out["generated"], script="scripts/make_charts_data.py",
        focus="Per-frame star FWHM from the pipeline's frame QA (already in arcsec: px x 3.665\"/px plate scale). "
              f"Time bins of 60 s (7/8 Sep, 16/17 Oct) or 120 s (25/26 Sep), at most {MAX_POINTS} per night; "
              "med/lo/hi over QA-kept frames, rej/rej_med for QA-rejected frames. start/end = median of kept frames "
              f"in the {EDGE_MIN} min after the first / before the last kept frame; *_all_saved use the same windows "
              "but include rejected frames.",
        funnel="Exposure slots from the cadence model used by the story tile wall (11 s cadence; a gap of n cadences "
               "= n-1 dropped exposures; gaps of 10 min or more are pauses). Rejected/excluded/stacked from the "
               "story's three-nights step (the 17 Oct combined stack). Reconciled: slots = saved + dropped, "
               "saved = rejected + excluded + stacked, stacked total = the hero's frame count.",
        wind="Open-Meteo hourly wind_gusts_10m (mph, max of the preceding hour), Belfast city centre; "
             f"sources: {', '.join(f'{k} ({v} h)' for k, v in gust_src.items())}. Drops per UTC hour from the slot "
             "model, dropped slots placed in their gap at the cadence.",
        clear_nights=f"Open-Meteo hourly cloud_cover, Belfast city centre; sources: "
                     f"{', '.join(f'{k} ({v} samples)' for k, v in srcs.items())}. Moon: astropy, illumination "
                     "at 00:00 UTC and max altitude over 21:00-03:00 UTC, Belfast city centre.",
        first_light="journal.json timeline step 1, before side (Seestar live stack).")
    PROV.write_text(json.dumps(prov, indent=1, ensure_ascii=False) + "\n")

    for nid, f in out["focus"].items():
        print(f"{nid} focus: {f['start_fwhm']} -> {f['end_fwhm']} (drift {f['drift']}), all saved {f['start_fwhm_all_saved']} -> "
              f"{f['end_fwhm_all_saved']}, median {f['median_kept']}, {len(f['points'])} bins of {f['bin_s']} s")
    for nid, fu in out["funnel"].items():
        print(nid, "funnel", fu)
    for nid, w in out["wind"].items():
        print(nid, "wind", [(x_["hour_utc"][11:16], x_["gust_mph"], x_["slots"], x_["drop_rate"]) for x_ in w])
    print("wind correlation", out["wind_correlation"])
    cn = out["clear_nights"]
    print(f"clear nights: {cn['usable']}/{cn['total']} usable, source {cn['source']}")
    for x_ in cn["rows"]:
        print(" ", x_)
    print("first light", out["first_light"])


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""
Observing conditions for one or more imaging nights, for the journal site.

For each night session (data/M31_<date>) this reads what the frames say about themselves and adds the weather
and the moon over the frames' actual UTC span:

  from the session   utc_start / utc_end   first frame start .. last frame start + exposure (qa/frames.csv date_obs)
                     frames_total          frames the scope saved (qa/summary.json n_frames)
                     frames_kept           frames that passed QA (n_selected); qa_rejected = n_rejected
                     scope_drop_rate       share of exposures the scope's live stacker dropped (mostly wind)
                     sky_adu               median frame background (qa bg_median, raw ADU incl. the ~1107 pedestal)
                     fwhm_arcsec           median per-frame star FWHM (qa fwhm_arcsec_median)
  weather            Open-Meteo hourly history at Belfast city centre (54.60, -5.93) -- never the scope's own
                     coordinates -- for every hour that overlaps the span: cloud cover (mean, %), wind gusts
                     (max, mph), relative humidity (mean, %), temperature minus dew point (min, C).
                     The ERA5 archive API is tried first; nights it has not caught up with yet come from
                     Open-Meteo's historical-forecast API.  The source used is recorded per night.
  moon               astropy: illuminated fraction at mid-span, altitude max/mean (5-minute samples) at the same
                     Belfast city-centre location.

  night_conditions.py SESSION[@NAME] [SESSION ...] --out site/src/data/metrics/conditions.json [--raw-dir DIR]

The output carries no FITS header values and no site coordinates other than the public city-centre point.
"""
import argparse, csv, json, math, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, get_body
from astropy.time import Time

BELFAST = (54.60, -5.93)  # city centre; deliberately coarse
HOURLY = "cloud_cover,wind_gusts_10m,relative_humidity_2m,temperature_2m,dew_point_2m"
APIS = [("open-meteo archive (ERA5)", "https://archive-api.open-meteo.com/v1/archive"),
        ("open-meteo historical forecast", "https://historical-forecast-api.open-meteo.com/v1/forecast")]


def frame_span(session):
    rows = list(csv.DictReader(open(session / "qa" / "frames.csv")))
    t = []
    for r in rows:
        if not r.get("date_obs"):
            continue
        d = datetime.fromisoformat(r["date_obs"]).replace(tzinfo=timezone.utc)
        t.append((d, float(r.get("exptime") or 10.0)))
    t.sort()
    return t[0][0], t[-1][0] + timedelta(seconds=t[-1][1]), t[0][1]


def fetch_weather(start, end, raw_dir=None, tag=""):
    params = dict(latitude=BELFAST[0], longitude=BELFAST[1], hourly=HOURLY, wind_speed_unit="mph",
                  temperature_unit="celsius", timezone="UTC",
                  start_date=start.date().isoformat(), end_date=end.date().isoformat())
    lo = start.replace(minute=0, second=0, microsecond=0)
    last_err = None
    for name, base in APIS:
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                js = json.load(r)
        except Exception as e:  # network / API error -> next source
            last_err = f"{name}: {e}"
            continue
        h = js.get("hourly", {})
        times = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc) for x in h.get("time", [])]
        idx = [i for i, tt in enumerate(times) if lo <= tt <= end]
        cols = {k: [h[k][i] for i in idx] for k in HOURLY.split(",")}
        if not idx or any(v is None for vals in cols.values() for v in vals):
            last_err = f"{name}: missing hours for {start:%Y-%m-%d}"
            continue
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            # drop the snapped grid-cell coordinates: the site publishes "Belfast" only
            raw = {k: v for k, v in js.items() if k not in ("latitude", "longitude", "elevation")}
            (raw_dir / f"openmeteo_{tag}.json").write_text(json.dumps({"source": name, "url": url, "response": raw}, indent=1))
        tmd = [t - d for t, d in zip(cols["temperature_2m"], cols["dew_point_2m"])]
        return dict(
            source=name, url=url, hours_utc=[times[i].strftime("%Y-%m-%dT%H:%MZ") for i in idx],
            cloud_pct_mean=round(float(np.mean(cols["cloud_cover"])), 1),
            gust_mph_max=round(float(np.max(cols["wind_gusts_10m"])), 1),
            humidity_pct_mean=round(float(np.mean(cols["relative_humidity_2m"])), 1),
            temp_minus_dew_c_min=round(float(np.min(tmd)), 1),
            hourly={k: cols[k] for k in cols},
        )
    sys.exit(f"weather unavailable: {last_err}")


def moon_info(start, end):
    loc = EarthLocation(lat=BELFAST[0] * u.deg, lon=BELFAST[1] * u.deg, height=0 * u.m)
    n = max(2, int((end - start).total_seconds() // 300) + 1)
    times = Time(start) + np.linspace(0, (end - start).total_seconds(), n) * u.s
    moon = get_body("moon", times, loc)
    alt = moon.transform_to(AltAz(obstime=times, location=loc)).alt.deg
    # illuminated fraction at mid-span from the geocentric sun-moon geometry (astroplan's formula)
    tm = Time(start + (end - start) / 2)
    sun_g, moon_g = get_body("sun", tm), get_body("moon", tm)
    elong = sun_g.separation(moon_g)
    phase = np.arctan2(sun_g.distance * np.sin(elong), moon_g.distance - sun_g.distance * np.cos(elong))
    illum = (1 + np.cos(phase)) / 2.0
    return dict(illum_pct=round(float(illum) * 100, 1), alt_deg_max=round(float(alt.max()), 1),
                alt_deg_mean=round(float(alt.mean()), 1), samples=n, at_utc=tm.isot[:16] + "Z",
                method="astropy get_body (moon, sun) at Belfast city centre; illumination = (1+cos phase angle)/2")


def night(arg, raw_dir):
    path, _, name = str(arg).partition("@")  # SESSION or SESSION@NAME (e.g. a backed-up QA folder)
    session = Path(path)
    start, end, exptime = frame_span(session)
    qa = json.load(open(session / "qa" / "summary.json"))
    name = name or session.name
    w = fetch_weather(start, end, raw_dir, name)
    m = moon_info(start, end)
    return dict(
        session=name,
        utc_start=start.strftime("%Y-%m-%dT%H:%M:%SZ"), utc_end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        exposure_s=exptime, frames_total=int(qa["n_frames"]), frames_kept=int(qa["n_selected"]),
        qa_rejected=int(qa["n_rejected"]), scope_drop_rate=round(float(qa["scope_drop_rate"]), 3),
        scope_estimated_captured=int(qa.get("scope_estimated_captured", 0)),
        sky_adu=round(float(qa["bg_median"]), 1), fwhm_arcsec_frames_median=round(float(qa["fwhm_arcsec_median"]), 1),
        weather=w, moon=m,
        provenance={
            "utc_start/utc_end": f"{path}/qa/frames.csv date_obs (first, last + exptime)",
            "frames_total/kept/qa_rejected/scope_drop_rate/sky_adu/fwhm": f"{path}/qa/summary.json",
            "weather": f"{w['source']} hourly at {BELFAST[0]},{BELFAST[1]}, hours overlapping the span",
            "moon": m["method"],
        },
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", help="keep the raw Open-Meteo responses here")
    a = ap.parse_args()
    res = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "location": "Belfast city centre",
           "nights": [night(s, Path(a.raw_dir) if a.raw_dir else None) for s in a.sessions]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    for n in res["nights"]:
        w, m = n["weather"], n["moon"]
        print(f"{n['session']}: {n['utc_start']} .. {n['utc_end']}  {n['frames_total']} saved, {n['frames_kept']} kept, "
              f"drop {n['scope_drop_rate']:.0%}  sky {n['sky_adu']} ADU  cloud {w['cloud_pct_mean']} %  gust {w['gust_mph_max']} mph  "
              f"RH {w['humidity_pct_mean']} %  T-Td {w['temp_minus_dew_c_min']} C  moon {m['illum_pct']} % alt max {m['alt_deg_max']}  [{w['source']}]")


if __name__ == "__main__":
    main()

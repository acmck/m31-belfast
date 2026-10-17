"""Per-frame strips for the story's tile wall -> site/src/data/metrics/story_frames.json

For each night, the exposure slots in time order (cadence from qa/frames.csv date_obs, as qa_frames.cadence_stats
does): a saved frame, or a slot the Seestar dropped (gap of n cadences = n-1 dropped; gaps >= 600 s are pauses).
For each story step, one status character per slot of each night that exists at that step:
  k  in that step's stack
  q  rejected by that step's frame QA
  z  excluded by the stacker (Siril drizzle wrote an all-zero registered frame; check_registered.py unselect)
  d  dropped by the scope (never saved; position from the cadence gap)
  u  saved on the scope but not yet copied at that step
Nights absent from a step are omitted. Strings are also given run-length encoded ("k12q1k40...").
No timestamps, no coordinates: only order and status.
"""
import json, re
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]; D = ROOT / "data"
NIGHTS = {"2026-09-07": "M31_2026-09-07", "2026-09-25": "M31_2026-09-25", "2026-10-16": "M31_2026-10-16"}


def slots(session):
    """Same cadence model as qa_frames.cadence_stats (whole-second times, median cadence, gaps < 600 s); the total
    dropped count matches its scope_dropped_frames exactly, spread over the gaps by largest remainder."""
    df = pd.read_csv(D / session / "qa/frames.csv")
    df["t"] = pd.to_datetime(df["date_obs"]); df = df.sort_values("t").reset_index(drop=True)
    t = df["t"].to_numpy().astype("datetime64[s]").astype(float)
    dt = np.diff(t); cad = float(np.median(dt))
    gi = [i for i, g in enumerate(dt) if 1.5 * cad < g < 600]
    frac = np.array([dt[i] / cad - 1 for i in gi])
    total = int(round(frac.sum()))
    n = np.floor(frac).astype(int)
    for j in np.argsort(-(frac - n))[: total - n.sum()]:
        n[j] += 1
    gap = dict(zip(gi, n))
    seq = []  # list of file name or None (dropped)
    for i, f in enumerate(df["file"]):
        seq.append(f)
        seq += [None] * int(gap.get(i, 0))
    return seq, df, cad


def selected(csv):
    df = pd.read_csv(csv)
    return set(df.loc[df.selected.astype(bool), "file"]), set(df["file"])


def stack_files(session_dir, nights_in_order):
    """Files in Siril sequence order (sorted lights_db) and the 1-based indices unselected in stack2.ssf."""
    files = sorted(p.name for p in (session_dir / "lights_db").iterdir() if p.suffix == ".fit")
    ss = session_dir / "process/stack2.ssf"
    bad = [int(m.group(1)) for m in re.finditer(r"unselect r_light_ (\d+) \d+", ss.read_text())] if ss.exists() else []
    return files, {files[i - 1] for i in bad}


def rle(s):
    out, i = [], 0
    while i < len(s):
        j = i
        while j < len(s) and s[j] == s[i]:
            j += 1
        out.append(f"{s[i]}{j - i}"); i = j
    return "".join(out)


def strip(seq, in_stack, qa_sel, copied, zero):
    out = []
    for f in seq:
        if f is None:
            out.append("d")
        elif f not in copied:
            out.append("u")
        elif f in zero:
            out.append("z")
        elif f in in_stack:
            out.append("k")
        elif f not in qa_sel:
            out.append("q")
        else:
            raise SystemExit(f"{f}: selected but not stacked and not excluded")
    return "".join(out)


def main():
    S = {nid: slots(sess) for nid, sess in NIGHTS.items()}
    sel_today = {nid: selected(D / sess / "qa/frames.csv")[0] for nid, sess in NIGHTS.items()}
    all_files = {nid: {f for f in S[nid][0] if f} for nid in NIGHTS}
    first_sel = selected(D / "M31_2026-09-07/first_pipeline/qa/frames.csv")[0]
    r529_sel, r529_copied = selected(D / "M31_2026-09-25/run_529/qa/frames.csv")

    c16_files, c16_zero = stack_files(D / "M31_combined_2026-10-16", None)
    c17_files, c17_zero = stack_files(D / "M31_combined_2026-10-17", None)
    n3_files, n3_zero = stack_files(D / "M31_2026-10-16", None)
    n2_files, n2_zero = stack_files(D / "M31_2026-09-25", None)

    steps = {
        # Seestar's live stack: every saved frame (it saves only frames it stacked)
        "first-light": {"2026-09-07": strip(S["2026-09-07"][0], all_files["2026-09-07"], all_files["2026-09-07"], all_files["2026-09-07"], set())},
        "first-pipeline": {"2026-09-07": strip(S["2026-09-07"][0], first_sel, first_sel, all_files["2026-09-07"], set())},
        "review": {"2026-09-07": strip(S["2026-09-07"][0], sel_today["2026-09-07"], sel_today["2026-09-07"], all_files["2026-09-07"], set())},
        # the moonlit night as a whole (journal nights[] numbers, today's QA), stacked alone 17 Oct (cur_0925)
        "moon": {"2026-09-07": strip(S["2026-09-07"][0], sel_today["2026-09-07"], sel_today["2026-09-07"], all_files["2026-09-07"], set()),
                 "2026-09-25": strip(S["2026-09-25"][0], sel_today["2026-09-25"] - n2_zero, sel_today["2026-09-25"], all_files["2026-09-25"], n2_zero)},
        "two-nights": {
            "2026-09-07": strip(S["2026-09-07"][0], sel_today["2026-09-07"] - c16_zero, sel_today["2026-09-07"], all_files["2026-09-07"], c16_zero),
            "2026-09-25": strip(S["2026-09-25"][0], r529_sel - c16_zero, r529_sel, r529_copied, c16_zero)},
        "sharp": {"2026-09-07": strip(S["2026-09-07"][0], sel_today["2026-09-07"] - c16_zero, sel_today["2026-09-07"], all_files["2026-09-07"], c16_zero),
                  "2026-09-25": strip(S["2026-09-25"][0], r529_sel - c16_zero, r529_sel, r529_copied, c16_zero),
                  "2026-10-16": strip(S["2026-10-16"][0], sel_today["2026-10-16"] - n3_zero, sel_today["2026-10-16"], all_files["2026-10-16"], n3_zero)},
        "three-nights": {nid: strip(S[nid][0], sel_today[nid] - c17_zero, sel_today[nid], all_files[nid], c17_zero) for nid in NIGHTS},
    }
    # consistency with the published numbers
    out = {"_about": __doc__.strip(), "nights": {}, "steps": {}}
    for nid, (seq, df, cad) in S.items():
        out["nights"][nid] = {"slots": len(seq), "saved": int(sum(f is not None for f in seq)),
                              "scope_dropped": int(sum(f is None for f in seq)), "cadence_s": round(cad, 1)}
    for step, per in steps.items():
        counts = {nid: {c: s.count(c) for c in "kqzdu" if s.count(c)} for nid, s in per.items()}
        out["steps"][step] = {"k_tiles_all_nights": sum(c.get("k", 0) for c in counts.values()), "counts": counts,
                              "rle": {nid: rle(s) for nid, s in per.items()}, "strip": per}
    # Sep 25 rejection reasons, grouped (today's QA)
    df = pd.read_csv(D / "M31_2026-09-25/qa/frames.csv"); r = df.loc[~df.selected.astype(bool), "reject_reason"].fillna("")
    out["qa_reasons_2026-09-25"] = {
        "rejected": int(len(r)),
        "sky_or_cloud": int(r.str.contains("background jump|low transparency|stars < 60%|no stars").sum()),
        "shape_only": int((~r.str.contains("background jump|low transparency|stars < 60%|no stars")).sum()),
        "_def": "sky_or_cloud = reason mentions a background jump, low transparency, too few stars or no stars; shape_only = roundness/FWHM only"}
    (ROOT / "site/src/data/metrics/story_frames.json").write_text(json.dumps(out, indent=1))
    for step, v in out["steps"].items():
        print(step, v["k_tiles_all_nights"], v["counts"])
    print(out["nights"]); print(out["qa_reasons_2026-09-25"])


if __name__ == "__main__":
    main()

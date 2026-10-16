#!/usr/bin/env bash
# Register + stack a session with Siril 1.4 (headless).
#
#   scripts/stack.sh SESSION_DIR [INPUT_SUBDIR] [--no-drizzle] [--pixfrac 1.0] [--scale 1.0]
#                                [--framing current|max|min|cog] [--rej w|l|g|a] [--limit N]
#                                [--weight none|noise|nbstars|wfwhm] [--ref auto|mid|N]
#                                [--flat PATH] [--maximize] [--feather N] [--fastnorm]
#
# INPUT_SUBDIR defaults to lights_db (debanded, QA-selected frames), falling back
# to lights_good, then lights.
#
# Options
#   --no-drizzle        debayer at convert time and register with lanczos4 interpolation instead of
#                       Bayer drizzle (faster, slightly softer; output is still 3-layer RGB)
#   --pixfrac F         drizzle pixel fraction (default 1.0)
#   --scale F           drizzle output scale (default 1.0)
#   --framing MODE      seqapplyreg framing: current (reference footprint, default) | max (union of all
#                       frames - stack with --maximize) | min (common area) | cog
#   --rej TYPE          stack rejection: w (winsorized, default) | l (linear fit) | g (GESDT) | a (k-MAD)
#   --limit N           only stack the first N frames (builds SESSION/lights_subset of hard links)
#   --weight W          stack frame weighting (default nbstars):
#                         none    - no -weight flag
#                         noise   - lower-background-noise frames weigh more
#                         nbstars - star count from registration (measured on M31 2026-09-07: +11 % SNR
#                                   for a 4 % FWHM cost vs unweighted - best compromise)
#                         wfwhm   - weighted FWHM (sharpest frames dominate: -17..-23 % SNR for a 9 %
#                                   star-size gain on the same data, so no longer the default)
#   --ref R             reference frame for seqapplyreg (default mid):
#                         auto    - keep the reference chosen by register -2pass (the sharpest frame,
#                                   which on an alt-az session tends to sit at one end of the field-
#                                   rotation range, leaving three corners with ~2x noise)
#                         mid     - setref to frame round(N/2) of the sequence (middle of the rotation
#                                   range, so all corners get roughly even coverage)
#                         N       - setref to 1-based frame index N of the converted sequence
#                       Verified with Siril 1.4.4: `setref` after `register -2pass` is honoured -
#                       the 2-pass only stores per-frame homographies + the reference index in the .seq
#                       (6th field of the S line, 0-based), and seqapplyreg recomposes every transform
#                       relative to whatever reference is set when it runs. setref itself logs nothing
#                       beyond rewriting light_.seq; the proof is the .seq field and the moved stack
#                       footprint (blank corners shift with the reference). No re-registration needed.
#   --flat PATH         absolute path to a CFA master flat FITS (same layout as the lights; Siril
#                       normalises it itself). Inserts `calibrate light -flat=PATH -cfa -equalize_cfa`
#                       after convert and works on the pp_light sequence (output r_pp_light). With
#                       drizzle the flat is also passed to `seqapplyreg -drizzle -flat=PATH` to weight
#                       the drizzled input pixels, as Siril's stock OSC_Preprocessing_BayerDrizzle.ssf
#                       does. With --no-drizzle the debayer moves from convert to `calibrate -debayer`
#                       so the CFA flat is applied before demosaicing.
#   --maximize          stack -maximize: output canvas encompasses every frame (use with --framing max;
#                       multi-night / mosaic-ish sessions)
#   --feather N         stack -feather=N: feather each frame's borders over N px (hides seams between
#                       frames with different framing; multi-night sessions)
#   --fastnorm          stack -fastnorm: faster location/scale estimators than IKSS for normalisation
#   --seqsubsky DEG     remove a degree-DEG polynomial background from every frame before registration
#                       (Siril seqsubsky; works on CFA frames, so it is compatible with drizzle). Aimed at
#                       moonlit / light-polluted nights where the gradient turns with the field. Off by default.
#
# Pipeline (OSC; the Seestar has no calibration masters unless --flat is given):
#   convert  -> CFA FITS sequence                      (process/light_*.fit)  [-debayer if --no-drizzle]
#   calibrate -flat (optional)                          (process/pp_light_*.fit)
#   register -2pass                                     (analyses every frame, computes transforms)
#   setref <mid>                                        (unless --ref auto)
#   seqapplyreg -drizzle scale 1.0                      (Bayer drizzle: debayer-free, no interpolation colour artefacts)
#   stack    winsorized sigma clip, additive+scaling norm, nbstars weights, RGB-equalised
#   -> SESSION/stack/result.fit (32-bit linear RGB, same orientation as the Seestar app shows)
# The converted/calibrated/registered frames and drizztmp are removed once the stack exists.
# macOS ships bash 3.2; re-exec under Homebrew bash 5 if available
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
SESSION="$(cd "${1:?session dir}" && pwd)"; shift
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SEQSUBSKY=0; SUB=""; DRIZZLE=1; PIXFRAC=1.0; SCALE=1.0; FRAMING=current; REJ=w; LIMIT=0
WEIGHT=nbstars; REF=mid; FLAT=""; MAXIMIZE=0; FEATHER=0; FASTNORM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-drizzle) DRIZZLE=0;;
    --pixfrac) PIXFRAC="$2"; shift;;
    --scale) SCALE="$2"; shift;;
    --framing) FRAMING="$2"; shift;;
    --rej) REJ="$2"; shift;;
    --limit) LIMIT="$2"; shift;;
    --weight) WEIGHT="$2"; shift;;
    --ref) REF="$2"; shift;;
    --flat) FLAT="$2"; shift;;
    --maximize) MAXIMIZE=1;;
    --feather) FEATHER="$2"; shift;;
    --fastnorm) FASTNORM=1;;
    --seqsubsky) SEQSUBSKY="$2"; shift;;
    -*) echo "unknown option $1"; exit 2;;
    *) SUB="$1";;
  esac; shift
done
case "$WEIGHT" in none|noise|nbstars|wfwhm) ;; *) echo "--weight must be none|noise|nbstars|wfwhm (got '$WEIGHT')"; exit 2;; esac
case "$REF" in auto|mid) ;; ''|*[!0-9]*) echo "--ref must be auto|mid|N (got '$REF')"; exit 2;; esac
case "$FEATHER" in ''|*[!0-9]*) echo "--feather needs an integer pixel count (got '$FEATHER')"; exit 2;; esac
if [ -n "$FLAT" ]; then
  FLAT="$(cd "$(dirname "$FLAT")" && pwd)/$(basename "$FLAT")"
  [ -f "$FLAT" ] || { echo "no flat $FLAT"; exit 1; }
fi
if [ -z "$SUB" ]; then
  for c in lights_db lights_good lights; do [ -d "$SESSION/$c" ] && SUB=$c && break; done
fi
IN="$SESSION/$SUB"
[ -d "$IN" ] || { echo "no input dir $IN"; exit 1; }
SIRIL="${SIRIL:-/Applications/Siril.app/Contents/MacOS/siril-cli}"
N=$(ls "$IN"/*.fit* | wc -l | tr -d ' ')
echo "stacking $N frames from $SUB  (drizzle=$DRIZZLE scale=$SCALE pixfrac=$PIXFRAC framing=$FRAMING rej=$REJ weight=$WEIGHT ref=$REF flat=${FLAT:-none} maximize=$MAXIMIZE feather=$FEATHER fastnorm=$FASTNORM)"

# Siril reads a whole directory; for --limit build a temp dir of links.
if [ "$LIMIT" -gt 0 ]; then
  T="$SESSION/lights_subset"; rm -rf "$T"; mkdir -p "$T"
  ls "$IN"/*.fit* | head -n "$LIMIT" | while read -r f; do ln "$f" "$T/"; done
  IN="$T"; SUB=lights_subset
  N=$(ls "$IN"/*.fit* | wc -l | tr -d ' ')
fi

# Reference frame index (1-based, in the converted sequence, which keeps the directory's sorted order).
case "$REF" in
  auto) REFIDX=0;;
  mid)  REFIDX=$(( (N + 1) / 2 ));;
  *)    REFIDX=$REF; [ "$REFIDX" -ge 1 ] && [ "$REFIDX" -le "$N" ] || { echo "--ref $REF out of range 1..$N"; exit 2; };;
esac

rm -rf "$SESSION/process"; mkdir -p "$SESSION/process" "$SESSION/stack"
SCRIPT="$SESSION/process/stack.ssf"

# Sequence names: convert -> light; calibrate -> pp_light; seqapplyreg -> r_<seq>.
SEQ=light
CONVERT="convert light -out=../process"
CALIB=""
if [ -n "$FLAT" ]; then
  SEQ=pp_light
  CALIB="calibrate light -flat=$FLAT -cfa -equalize_cfa"
  # no-drizzle needs RGB frames: demosaic after flat-fielding rather than at convert time
  [ "$DRIZZLE" = 1 ] || CALIB="$CALIB -debayer"
elif [ "$DRIZZLE" = 0 ]; then
  # Without -debayer the CFA mosaic would be registered as mono and stacked into a 1-layer image.
  CONVERT="convert light -debayer -out=../process"
fi
if [ "$SEQSUBSKY" -gt 0 ]; then
  CALIB="${CALIB:+$CALIB
}seqsubsky $SEQ $SEQSUBSKY"
  SEQ="bkg_$SEQ"
fi
REG="register $SEQ -2pass"
[ "$REFIDX" -gt 0 ] && REG="$REG
setref $SEQ $REFIDX"
if [ "$DRIZZLE" = 1 ]; then
  APPLY="seqapplyreg $SEQ -drizzle -scale=$SCALE -pixfrac=$PIXFRAC -kernel=square -framing=$FRAMING"
  [ -n "$FLAT" ] && APPLY="$APPLY -flat=$FLAT"
else
  APPLY="seqapplyreg $SEQ -interp=lanczos4 -framing=$FRAMING"
fi
STACKOPTS="-norm=addscale -output_norm -rgb_equal"
[ "$FASTNORM" = 1 ] && STACKOPTS="$STACKOPTS -fastnorm"
[ "$WEIGHT" = none ] || STACKOPTS="$STACKOPTS -weight=$WEIGHT"
[ "$FEATHER" -gt 0 ] && STACKOPTS="$STACKOPTS -feather=$FEATHER"
[ "$MAXIMIZE" = 1 ] && STACKOPTS="$STACKOPTS -maximize"

HEADER='requires 1.4.0
setext fit
set core.mem_mode=1
set core.mem_ratio=0.75'
# Two Siril runs with a check in between: Siril 1.4.4's drizzle occasionally writes an all-zero frame
# (even the reference), which makes the stack abort with "Normalization failed".  check_registered.py
# finds those and the second run unselects them (and moves the reference if needed) before stacking.
{
  echo "$HEADER"
  echo "cd $SUB"
  echo "$CONVERT"
  echo "cd ../process"
  [ -n "$CALIB" ] && echo "$CALIB"
  echo "$REG"
  echo "$APPLY"
  echo "close"
} > "$SCRIPT"
SCRIPT2="$SESSION/process/stack2.ssf"
LOG="$SESSION/stack/siril_stack.log"
t0=$(date +%s)
echo "--- siril script (register):"; sed 's/^/    /' "$SCRIPT"
"$SIRIL" -d "$SESSION" -s "$SCRIPT" 2>&1 | tee "$LOG" | grep -E -i 'error|warning|reference|frames|Total|Registration|drizzle|calibrat' | grep -v -i 'Not an image' || true
echo "--- checking registered frames"
FIX="$("$ROOT/.venv/bin/python" "$ROOT/scripts/check_registered.py" "$SESSION/process" "r_$SEQ" 2> >(sed 's/^/    /' >&2))" || FIX=""
FILTER=""; [ -n "$FIX" ] && FILTER="-filter-included"
{
  echo "$HEADER"
  echo "cd process"
  [ -n "$FIX" ] && echo "$FIX"
  echo "stack r_$SEQ rej $REJ 3 3 $STACKOPTS $FILTER -32b -out=../stack/result"
  echo "close"
} > "$SCRIPT2"
echo "--- siril script (stack):"; sed 's/^/    /' "$SCRIPT2"
"$SIRIL" -d "$SESSION" -s "$SCRIPT2" 2>&1 | tee -a "$LOG" | grep -E -i 'error|warning|stack|saved|rejected|selected' | grep -v -i 'Not an image' || true
rc=${PIPESTATUS[0]}
echo "siril rc=$rc, $(( $(date +%s) - t0 ))s, log: $LOG"
ls -la "$SESSION/stack/result.fit" 2>/dev/null || { echo "no result produced - see $LOG"; exit 1; }
# The converted, calibrated and registered frames (process/light_*, pp_light_*, r_*, process/drizztmp)
# are ~10-50 GB; drop them once the stack exists.
rm -f "$SESSION"/process/r_light_*.fit "$SESSION"/process/light_*.fit \
      "$SESSION"/process/pp_light_*.fit "$SESSION"/process/r_pp_light_*.fit \
      "$SESSION"/process/bkg_*.fit "$SESSION"/process/r_bkg_*.fit
rm -rf "$SESSION"/process/drizztmp

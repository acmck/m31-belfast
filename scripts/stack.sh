#!/usr/bin/env bash
# Register + stack a session with Siril 1.4 (headless).
#
#   scripts/stack.sh SESSION_DIR [INPUT_SUBDIR] [--no-drizzle] [--pixfrac 1.0] [--scale 1.0]
#                                [--framing current|max|min|cog] [--rej w|l|g|a] [--limit N]
#
# INPUT_SUBDIR defaults to lights_db (debanded, QA-selected frames), falling back
# to lights_good, then lights.
#
# Pipeline (OSC, no calibration masters - the Seestar has none):
#   convert  -> CFA FITS sequence                     (process/light_*.fit)
#   register -2pass                                    (picks the best reference, computes transforms)
#   seqapplyreg -drizzle scale 1.0                     (Bayer drizzle: debayer-free, no interpolation colour artefacts)
#   stack    winsorized sigma clip, additive+scaling norm, wFWHM weights, RGB-equalised
#   -> SESSION/stack/result.fit (32-bit linear RGB, same orientation as the Seestar app shows)
# macOS ships bash 3.2; re-exec under Homebrew bash 5 if available
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
SESSION="$(cd "${1:?session dir}" && pwd)"; shift
SUB=""; DRIZZLE=1; PIXFRAC=1.0; SCALE=1.0; FRAMING=current; REJ=w; LIMIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-drizzle) DRIZZLE=0;;
    --pixfrac) PIXFRAC="$2"; shift;;
    --scale) SCALE="$2"; shift;;
    --framing) FRAMING="$2"; shift;;
    --rej) REJ="$2"; shift;;
    --limit) LIMIT="$2"; shift;;
    -*) echo "unknown option $1"; exit 2;;
    *) SUB="$1";;
  esac; shift
done
if [ -z "$SUB" ]; then
  for c in lights_db lights_good lights; do [ -d "$SESSION/$c" ] && SUB=$c && break; done
fi
IN="$SESSION/$SUB"
[ -d "$IN" ] || { echo "no input dir $IN"; exit 1; }
SIRIL="${SIRIL:-/Applications/Siril.app/Contents/MacOS/siril-cli}"
N=$(ls "$IN"/*.fit* | wc -l | tr -d ' ')
echo "stacking $N frames from $SUB  (drizzle=$DRIZZLE scale=$SCALE pixfrac=$PIXFRAC framing=$FRAMING rej=$REJ)"

# Siril reads a whole directory; for --limit build a temp dir of links.
if [ "$LIMIT" -gt 0 ]; then
  T="$SESSION/lights_subset"; rm -rf "$T"; mkdir -p "$T"
  ls "$IN"/*.fit* | head -n "$LIMIT" | while read -r f; do ln "$f" "$T/"; done
  IN="$T"; SUB=lights_subset
fi

rm -rf "$SESSION/process"; mkdir -p "$SESSION/process" "$SESSION/stack"
SCRIPT="$SESSION/process/stack.ssf"
if [ "$DRIZZLE" = 1 ]; then
  REG="register light -2pass
seqapplyreg light -drizzle -scale=$SCALE -pixfrac=$PIXFRAC -kernel=square -framing=$FRAMING"
else
  REG="register light -2pass
seqapplyreg light -interp=lanczos4 -framing=$FRAMING"
fi
cat > "$SCRIPT" <<SSF
requires 1.4.0
setext fit
set core.mem_mode=1
set core.mem_ratio=0.75
cd $SUB
convert light -out=../process
cd ../process
$REG
stack r_light rej $REJ 3 3 -norm=addscale -output_norm -rgb_equal -weight=wfwhm -32b -out=../stack/result
close
SSF
echo "--- siril script:"; sed 's/^/    /' "$SCRIPT"
LOG="$SESSION/stack/siril_stack.log"
t0=$(date +%s)
"$SIRIL" -d "$SESSION" -s "$SCRIPT" 2>&1 | tee "$LOG" | grep -E -i 'error|warning|reference|frames|stack|Total|saved|exposure|rejected|Registration|drizzle' | grep -v -i 'Not an image' || true
rc=${PIPESTATUS[0]}
echo "siril rc=$rc, $(( $(date +%s) - t0 ))s, log: $LOG"
ls -la "$SESSION/stack/result.fit" 2>/dev/null || { echo "no result produced - see $LOG"; exit 1; }
# The converted and drizzled frames (process/light_*, process/drizztmp) are ~10-50 GB; drop them once the stack exists.
rm -f "$SESSION"/process/r_light_*.fit "$SESSION"/process/light_*.fit
rm -rf "$SESSION"/process/drizztmp

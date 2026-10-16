#!/usr/bin/env bash
# Build a multi-night session from nights that have already been through QA + deband, then stack and
# post-process it with multi-night settings.
#
#   scripts/combine_sessions.sh OUT_SESSION NIGHT_SESSION [NIGHT_SESSION ...] [-- extra run_pipeline stages]
#
#   e.g. scripts/combine_sessions.sh data/M31_combined_2026-10-16 data/M31_2026-09-07 data/M31_2026-09-25
#
# Each night keeps its own folder (raw lights, QA, deband); QA is judged per night because sky brightness
# differs a lot between nights (a moonlit night would otherwise be rejected as "cloud").  OUT_SESSION gets:
#   lights_db/     hard links to every night's QA-selected, debanded frames (lights_db, else lights_good)
#   sessions.txt   which nights went in, and how many frames from each
# then run_pipeline.sh runs `stack post` on it with:
#   --weight noise           frames from a moonlit night are noisier; inverse-noise weighting keeps them from
#                            diluting the dark-sky nights (docs/REVIEW.md: noise weighting matters across nights)
#   framing current          (the default) output is the reference frame's rectangle; areas fewer frames cover
#                            are just noisier and stretch.py's auto-crop trims them.  --framing max --maximize
#                            was tried first and is a bad fit: nights framed at different rotations give a ragged
#                            union canvas with large empty regions that break star removal and the crop.
#   --fastnorm               faster normalisation estimators for long sequences
# Extra flags can still be passed through STACK_ARGS / POST_ARGS in the environment.
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:?output session dir}"; shift
[ $# -ge 1 ] || { echo "need at least one night session"; exit 2; }
mkdir -p "$OUT/lights_db"
OUT="$(cd "$OUT" && pwd)"
find "$OUT/lights_db" -name '*.fit*' -delete
: > "$OUT/sessions.txt"
total=0
for N in "$@"; do
  N="$(cd "$N" && pwd)"
  SRC="$N/lights_db"; [ -n "$(ls "$SRC"/*.fit 2>/dev/null | head -1)" ] || SRC="$N/lights_good"
  [ -d "$SRC" ] || { echo "no lights_db or lights_good in $N - run qa/deband on it first"; exit 1; }
  n=0
  for f in "$SRC"/*.fit; do
    b="$(basename "$f")"
    [ -e "$OUT/lights_db/$b" ] && { echo "duplicate frame name $b (in two nights?)"; exit 1; }
    ln "$f" "$OUT/lights_db/$b" 2>/dev/null || cp "$f" "$OUT/lights_db/$b"
    n=$((n+1))
  done
  total=$((total+n))
  echo "$(basename "$N")  $n frames  (from $(basename "$SRC"))" | tee -a "$OUT/sessions.txt"
done
echo "total  $total frames" | tee -a "$OUT/sessions.txt"

STACK_ARGS="--weight noise --fastnorm ${STACK_ARGS:-}" \
  "$ROOT/run_pipeline.sh" "$OUT" stack post

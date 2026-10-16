#!/usr/bin/env bash
# End-to-end Seestar S30 Pro pipeline, tuned for M31.
#
#   ./run_pipeline.sh SESSION_DIR [stages...]      stages: qa deband flat stack post   (default: all)
#
# SESSION_DIR layout (created by scripts/pull_frames.sh):
#   lights/          raw Seestar sub-frames (.fit, CFA)
#   seestar_stack/   the scope's own live stack, for comparison
# Produced:
#   qa/              frames.csv, report.html, plots, selected/rejected lists
#   lights_good/     hard links to frames that passed QA
#   lights_db/       debanded copies of lights_good      (input to Siril)
#   masters/synthetic_flat.fit   vignetting model fitted to the frames themselves (flat stage)
#   stack/result.fit 32-bit linear RGB stack
#   final/           solved, colour-calibrated, background-extracted, denoised, stretched outputs
#
# M31 defaults live in config/m31.env (M31_QA_ARGS, M31_DEBAND_ARGS, M31_FLAT_ARGS, M31_STACK_ARGS, M31_POST_ARGS),
# each with the measured reason for it. They restate the scripts' own defaults. PIPELINE_CONFIG=path picks another
# file; PIPELINE_CONFIG=/dev/null skips it.
# Environment overrides: QA_ARGS, DEBAND_ARGS, FLAT_ARGS, STACK_ARGS, POST_ARGS  (extra flags for each stage).
# They are appended after the config's flags, and every stage lets the last flag win, so they override it.
# Defaults follow docs/REVIEW.md: no trend-relative frame culling, column deband checked, synthetic flat,
# nbstars weighting with a mid-session reference, Siril subsky before PCC, auto-crop, highlight knee.
# macOS ships bash 3.2; re-exec under Homebrew bash 5 if available
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SESSION="$(cd "${1:?session dir}" && pwd)"; shift
STAGES=("$@"); [ ${#STAGES[@]} -eq 0 ] && STAGES=(qa deband flat stack post)
PY="$ROOT/.venv/bin/python"
# Target defaults first, caller's overrides after them (last flag wins in every stage).
CONFIG="${PIPELINE_CONFIG:-$ROOT/config/m31.env}"
M31_QA_ARGS=""; M31_DEBAND_ARGS=""; M31_FLAT_ARGS=""; M31_STACK_ARGS=""; M31_POST_ARGS=""
# shellcheck disable=SC1090
if [ -f "$CONFIG" ] || [ "$CONFIG" = /dev/null ]; then . "$CONFIG"; else echo "no config $CONFIG"; exit 2; fi
QA_ARGS="$M31_QA_ARGS ${QA_ARGS:-}"
DEBAND_ARGS="$M31_DEBAND_ARGS ${DEBAND_ARGS:-}"
FLAT_ARGS="$M31_FLAT_ARGS ${FLAT_ARGS:-}"
STACK_ARGS="$M31_STACK_ARGS ${STACK_ARGS:-}"
POST_ARGS="$M31_POST_ARGS ${POST_ARGS:-}"
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/pipeline_$(basename "$SESSION")_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "session: $SESSION   stages: ${STAGES[*]}   log: $LOG   config: $CONFIG"

has() { for s in "${STAGES[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }
t() { date +%H:%M:%S; }

if has qa; then
  echo; echo "########## [$(t)] QA: measure + select frames"
  # shellcheck disable=SC2086
  "$PY" "$ROOT/scripts/qa_frames.py" --lights "$SESSION/lights" --out "$SESSION/qa" --select-dir "$SESSION/lights_good" ${QA_ARGS:-}
fi
if has deband; then
  echo; echo "########## [$(t)] DEBAND: CFA-aware row banding removal"
  IN="$SESSION/lights_good"; [ -d "$IN" ] || IN="$SESSION/lights"
  # shellcheck disable=SC2086
  "$PY" "$ROOT/scripts/deband.py" --in "$IN" --out "$SESSION/lights_db" --axis both ${DEBAND_ARGS:-}
fi
FLAT_OPT=""
if has flat; then
  echo; echo "########## [$(t)] FLAT: measure vignetting from the frames' own sky (galaxy masked out) and fit a flat if it is real"
  mkdir -p "$SESSION/masters"
  FLAT_LOG="$SESSION/masters/synth_flat.log"
  # shellcheck disable=SC2086
  if "$PY" "$ROOT/scripts/synth_flat.py" --lights "$SESSION/lights" --out "$SESSION/masters/synthetic_flat.fit" --plot "$SESSION/masters/synthetic_flat.png" ${FLAT_ARGS:-} | tee "$FLAT_LOG"; then
    if grep -q "negligible once the target's own light is excluded" "$FLAT_LOG"; then
      echo "  vignetting is negligible once the target is excluded from the measurement (see $FLAT_LOG) - not applying a flat"
    else
      FLAT_OPT="--flat $SESSION/masters/synthetic_flat.fit"
    fi
  else
    echo "synthetic flat failed; stacking without a flat"
  fi
elif [ -f "$SESSION/masters/synthetic_flat.fit" ] && ! grep -q "negligible once the target's own light is excluded" "$SESSION/masters/synth_flat.log" 2>/dev/null; then
  FLAT_OPT="--flat $SESSION/masters/synthetic_flat.fit"
fi
if has stack; then
  echo; echo "########## [$(t)] STACK: Siril 2-pass registration (mid-session reference), Bayer drizzle, winsorized rejection, nbstars weighting"
  # shellcheck disable=SC2086
  "$ROOT/scripts/stack.sh" "$SESSION" $FLAT_OPT ${STACK_ARGS:-}
fi
if has post; then
  echo; echo "########## [$(t)] POST: solve, Siril gradient removal, colour-calibrate, GraXpert denoise, star removal, stretch"
  # shellcheck disable=SC2086
  "$ROOT/scripts/post_process.sh" "$SESSION" ${POST_ARGS:-}
  # keep a dated copy + log line of this result, and point data/<TARGET>_latest at this session
  "$ROOT/scripts/record_result.sh" "$SESSION" || echo "record_result failed (result itself is fine)"
fi
echo; echo "########## [$(t)] done"

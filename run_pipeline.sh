#!/usr/bin/env bash
# End-to-end Seestar post-processing pipeline.
#
#   ./run_pipeline.sh SESSION_DIR [stages...]      stages: qa deband stack post   (default: all)
#
# SESSION_DIR layout (created by scripts/pull_frames.sh):
#   lights/          raw Seestar sub-frames (.fit, CFA)
#   seestar_stack/   the scope's own live stack, for comparison
# Produced:
#   qa/              frames.csv, report.html, plots, selected/rejected lists
#   lights_good/     hard links to frames that passed QA
#   lights_db/       debanded copies of lights_good      (input to Siril)
#   stack/result.fit 32-bit linear RGB stack
#   final/           solved, colour-calibrated, background-extracted, denoised, stretched outputs
#
# Environment overrides: QA_ARGS, DEBAND_ARGS, STACK_ARGS, POST_ARGS  (extra flags for each stage)
# macOS ships bash 3.2; re-exec under Homebrew bash 5 if available
if [ "${BASH_VERSINFO[0]}" -lt 4 ] && [ -x /opt/homebrew/bin/bash ]; then exec /opt/homebrew/bin/bash "$0" "$@"; fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SESSION="$(cd "${1:?session dir}" && pwd)"; shift
STAGES=("$@"); [ ${#STAGES[@]} -eq 0 ] && STAGES=(qa deband stack post)
PY="$ROOT/.venv/bin/python"
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/pipeline_$(basename "$SESSION")_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "session: $SESSION   stages: ${STAGES[*]}   log: $LOG"

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
  "$PY" "$ROOT/scripts/deband.py" --in "$IN" --out "$SESSION/lights_db" ${DEBAND_ARGS:-}
fi
if has stack; then
  echo; echo "########## [$(t)] STACK: Siril 2-pass registration, Bayer drizzle, winsorized rejection"
  # shellcheck disable=SC2086
  "$ROOT/scripts/stack.sh" "$SESSION" ${STACK_ARGS:-}
fi
if has post; then
  echo; echo "########## [$(t)] POST: solve, colour-calibrate, GraXpert BGE + denoise, stretch"
  # shellcheck disable=SC2086
  "$ROOT/scripts/post_process.sh" "$SESSION" ${POST_ARGS:-}
fi
echo; echo "########## [$(t)] done"

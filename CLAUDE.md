# M31 from Belfast: notes for Claude Code

An M31-only processing pipeline for a ZWO Seestar S30 Pro, plus the Astro journal site in `site/` that shows
the progress stage by stage. Start with `README.md`; the measured history is in `docs/`.

## How to run

- Python: `.venv/bin/python` (uv, Python 3.12). Add dependencies with `uv add`, never pip.
- One night: `make night SESSION=data/M31_YYYY-MM-DD` (or `./run_pipeline.sh SESSION [qa deband flat stack post]`).
- All nights together: `make combine` (wraps `scripts/combine_sessions.sh data/M31_combined_<today> <every night>`).
- Measure a stack: `.venv/bin/python scripts/measure_stack.py data/<session>/stack/result.fit`.
- Site: `make site` (= `npm --prefix site run build`), `make deploy` (build, dist checks, Cloudflare Pages
  project `m31-belfast` via the project-local wrangler: `npm --prefix site exec wrangler -- ...`, never a global install).
- Privacy: `make privacy-check` before any push and before any deploy. It must pass.
- Skills: `.claude/skills/new-night` (pull a night end to end), `journal-stage` (one before/after stage
  for the site), `deploy-site`.

## Conventions

- **One folder per night** under `data/`, named by the evening the night started: `data/M31_2026-09-25`
  holds the night of 25/26, including frames dated 09-26. Combined stacks are `data/M31_combined_<date>`.
- **Defaults have evidence.** M31 defaults live in `config/m31.env`, each with its measured reason. Don't
  change a default without new measurements on this data; read `docs/REVIEW.md` and `docs/lab-notes.md`
  first. Overrides go through `QA_ARGS` / `DEBAND_ARGS` / `FLAT_ARGS` / `STACK_ARGS` / `POST_ARGS`.
- **Run Siril, GraXpert and Dark Star jobs one at a time.** Each one uses the whole machine. Never run two
  pipeline stages in parallel, or from parallel agents.
- **Git:** commit straight to `main`. Commits are GPG-signed by the repo-local config; never change the
  repo-local identity or signing settings, and never pass `--no-gpg-sign`. A passphrase dialog may appear.
- **Pushing:** only as the GitHub account `acmck`. Run `gh auth switch -h github.com -u acmck` first, push,
  then switch back to the previously active account (`gh auth switch -h github.com -u <previous>`).
- **Never commit** `data/`, `logs/`, `tools/astap/` or `tools/downloads/` (all git-ignored), or any FITS file.
- **Never publish** FITS files, FITS headers, logs, or the observing site's coordinates (frame headers carry
  them to about 10 m). The location is "Belfast", nothing finer. Weather lookups use Belfast city centre
  (54.60, -5.93), never header values. Published images carry no metadata.
- Don't write work, employer or private-network details into this repo; `make privacy-check` enforces it.
- Use American English in prose and comments; don't rename existing identifiers for spelling.

## Where things live

- `run_pipeline.sh`, `scripts/`: the pipeline (stage order and flags in the README).
- `config/m31.env`: M31-specific defaults and why.
- `docs/REVIEW.md`: the independent review behind most defaults; `docs/REVIEW_PROMPT.md`: the prompt for it.
- `docs/lab-notes.md`: measurements and dead ends, night by night.
- `data/<session>/final/history.log` and `final/history/*.jpg`: every post-processing run, with SNR and FWHM.
  `data/M31_latest` points at the most recent result.
- `site/src/data/journal.json` and `site/src/data/metrics/`: the numbers the site shows. Every figure on the
  site must come from there; never invent one.
- `site/public/img/<stage>/`: exported before/after images (`scripts/export_web.py`), metadata-free.

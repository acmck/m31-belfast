# M31 from Belfast: common tasks.
#
#   make night SESSION=data/M31_2026-10-16 [STAGES="stack post"]   run the pipeline on one night
#   make combine [OUT=data/M31_combined_YYYY-MM-DD] [NIGHTS="..."]   stack every night's frames together
#   make site                                                        build the journal site into site/dist
#   make site-check                                                  privacy checks on site/dist
#   make deploy                                                      site + site-check, then Cloudflare Pages
#   make privacy-check                                               private terms, emails, coordinates, image metadata
SHELL := /bin/bash
PY := .venv/bin/python
SESSION ?=
STAGES ?=
NIGHTS ?= $(sort $(wildcard data/M31_20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]))
OUT ?= data/M31_combined_$(shell date +%Y-%m-%d)
PROJECT ?= m31-belfast

.PHONY: night combine site site-check deploy privacy-check

night:
	@test -n "$(SESSION)" || { echo "usage: make night SESSION=data/M31_YYYY-MM-DD [STAGES=\"qa deband flat stack post\"]"; exit 2; }
	./run_pipeline.sh "$(SESSION)" $(STAGES)

combine:
	@test -n "$(strip $(NIGHTS))" || { echo "no nights under data/"; exit 2; }
	scripts/combine_sessions.sh "$(OUT)" $(NIGHTS)

site:
	npm --prefix site run build

site-check:
	@test -d site/dist || { echo "site/dist missing: run make site"; exit 2; }
	$(PY) scripts/privacy_check.py --dist site/dist

deploy: site site-check privacy-check
	npm --prefix site exec wrangler -- pages deploy site/dist --project-name $(PROJECT)

# Scans tracked files plus untracked, non-ignored ones; skips data/ logs/ node_modules/ .git/ .venv/.
# Exits non-zero on any hit.
privacy-check:
	$(PY) scripts/privacy_check.py

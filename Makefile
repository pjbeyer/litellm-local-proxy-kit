# Owner entry point; run from the kit root (or use make -C).
.DEFAULT_GOAL := help

export POLICY ?= $(HOME)/.config/litellm-restart-controller/policy.json
export REQUEST_ID
export ASSET_DIGEST

# Keep preview, apply, and activation separately invoked, even with make -j.
ifneq ($(word 2,$(MAKECMDGOALS)),)
$(error Run one maintenance target at a time; see make help)
endif

.PHONY: help status preview apply verify prepare restart health
help:
	@printf '%s\n' \
	  'make status    Read controller status' \
	  'make preview   Preview installer changes without applying them' \
	  'make apply     Stage reviewed files and verify them; no restart' \
	  'make verify    Check installed files and deployment drift' \
	  'make prepare   Ask the controller to rehearse and prepare a restart' \
	  'make restart REQUEST_ID=... ASSET_DIGEST=...   Submit an owner-approved plan' \
	  'make health    Check liveness, readiness, and unauthenticated access'

status preview apply verify prepare restart health:
	@python3 -I -B owner_maintenance.py "$@"

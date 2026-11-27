# Owner entry point; run from the kit root (or use make -C).
.DEFAULT_GOAL := help

export POLICY ?= $(HOME)/.config/litellm-restart-controller/policy.json
export REQUEST_ID
export ASSET_DIGEST

# Keep preview, apply, and activation separately invoked, even with make -j.
ifneq ($(word 2,$(MAKECMDGOALS)),)
$(error Run one maintenance target at a time; see make help)
endif

.PHONY: help status health check-binding preview apply verify prepare restart
help:
	@printf '%s\n' \
	  'Inspect (choose the relevant check):' \
	  'make status    Read controller status' \
	  'make health    Check liveness, readiness, and unauthenticated access' \
	  'make check-binding   Compare on-disk controller binding only' \
	  '' \
	  'Stage (one command at a time; review before apply):' \
	  'make preview   Preview installer changes without applying them' \
	  'make apply     Stage reviewed files and verify them; no restart' \
	  'make verify    Check installed files and deployment drift' \
	  '' \
	  'Restart (separate, owner-gated workflow):' \
	  'make prepare   Ask the controller to rehearse and prepare a restart' \
	  'make restart REQUEST_ID=... ASSET_DIGEST=...   Submit an owner-approved plan'

status health check-binding preview apply verify prepare restart:
	@python3 -I -B owner_maintenance.py "$@"

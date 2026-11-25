# Owner maintenance targets for the local LiteLLM proxy deployment.
#
# Thin wrappers around the existing installer and restart-controller clients;
# no new control path is introduced here. Defaults derive from the installed
# controller policy and can be overridden per invocation, e.g.
#   make stage DEPLOYMENT_ROOT=/path/to/deployment-repo
#
# launchd / service control is intentionally absent: the installed restart
# controller (and the owner's own terminal) remain the only sanctioned ways
# to restart the proxy. See docs/restart-controller-operations.md for the
# approval flow between `prepare` and `restart`.

KIT_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
POLICY ?= $(HOME)/.config/litellm-restart-controller/policy.json

DEPLOYMENT_ROOT ?= $(shell python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deployment_root"])' $(POLICY) 2>/dev/null)
CONTROLLER_ROOT ?= $(shell python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["controller_root"])' $(POLICY) 2>/dev/null)
PORT ?= 4000

CONTROLLER := python3 $(CONTROLLER_ROOT)/restart_controller.py --policy $(POLICY)
BASE_URL := http://127.0.0.1:$(PORT)

.PHONY: help status verify stage prepare restart health
.DEFAULT_GOAL := help

help: ## Show available targets
	@echo "Owner maintenance targets (paths derive from the installed policy: $(POLICY))"
	@awk '/^[a-zA-Z_-]+:.* ## / {sub(/:.*/,""); sub(/ ## /,"|"); print "  make " $$0}' $(firstword $(MAKEFILE_LIST)) \
		| awk -F'|' '{printf "%-10s %s\n", $$1, $$2}'

status: ## Read-only controller status (no service contact, no restart)
	$(CONTROLLER) status

verify: ## Read-only drift check + installer verification of the deployment
	python3 $(KIT_ROOT)/core/drift_check.py --deployment-root $(DEPLOYMENT_ROOT)
	bash $(KIT_ROOT)/core/installer.sh $(DEPLOYMENT_ROOT) --verify

stage: ## Stage reviewed sources: installer dry-run, apply, verify
	bash $(KIT_ROOT)/core/installer.sh $(DEPLOYMENT_ROOT) --dry-run
	bash $(KIT_ROOT)/core/installer.sh $(DEPLOYMENT_ROOT) apply
	bash $(KIT_ROOT)/core/installer.sh $(DEPLOYMENT_ROOT) --verify

prepare: ## Ask the controller to prepare a restart plan (owner approves next)
	$(CONTROLLER) prepare

restart: ## Submit an owner-approved plan: REQUEST_ID=... ASSET_DIGEST=...
	@test -n "$(REQUEST_ID)" && test -n "$(ASSET_DIGEST)" || { \
		echo "usage: make restart REQUEST_ID=<request id> ASSET_DIGEST=<asset digest>" >&2; exit 64; }
	$(CONTROLLER) restart "$(REQUEST_ID)" "$(ASSET_DIGEST)"

health: ## Check proxy liveness, readiness, and unauthenticated-models 401
	@curl -fsS $(BASE_URL)/health/liveliness >/dev/null && echo "liveliness: 200"
	@curl -fsS $(BASE_URL)/health/readiness >/dev/null && echo "readiness: 200"
	@code=$$(curl -s -o /dev/null -w '%{http_code}' $(BASE_URL)/v1/models); \
	if [ "$$code" = 401 ]; then echo "models: 401 (auth enforced)"; \
	else echo "models: $$code (expected 401)" >&2; exit 1; fi

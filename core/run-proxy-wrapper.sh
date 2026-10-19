#!/bin/bash
# Generalized fail-closed launchd wrapper for a local LiteLLM proxy deployment.
# (spec 015, Contract 1 / research R5 — shared core, consumed by BOTH deployments)
#
# Design rules (FR-005, hardened per red-team findings):
#   * Every resolution is verified by VALUE EVIDENCE (non-empty, from this
#     wrapper's own export set) — never by matching a variable NAME (F-CI-1).
#   * Every resolution is classified: FAIL_CLOSED (abort, exit 78) or
#     JUSTIFIED_WARN (documented degrade path — currently only the salt key,
#     which falls back to master_key as the at-rest cipher).
#   * litellm does NOT refuse to start when master_key resolves to nothing —
#     it logs CRITICAL and then treats every request as an internal user.
#     Refusing here is the only safe behavior.
#
# Inputs (environment, all optional-with-defaults):
#   LITELLM_DEPLOYMENT      deployment name (work|personal) — selects manifest
#   LITELLM_CONFIG_DIR      apply-target config dir   (default per deployment)
#   LITELLM_PROXY_CONFIG    config file path          (default: <config_dir>/config.rendered.yaml)
#   LITELLM_LOCAL_PORT      port                      (default 4000)
#   OP_KEYCHAIN_SERVICE     Keychain item holding the 1Password service-account token
#   OP_KEYCHAIN_ACCOUNT     Keychain account (default pjbeyer)
#   CREDENTIALS_MANIFEST    path to credentials.toml (Contract 1)
#   LITELLM_PATCH_SENTINEL  patch sentinel symbol to assert in the installed build (FR-006)
#   LITELLM_PROXY_PYTHON    python interpreter of the pinned build (must be set)
#
# Exit codes: 78 = EX_CONFIG (launchd backs off; no hot-loop on bad config).
set -euo pipefail

DEPLOYMENT="${LITELLM_DEPLOYMENT:?LITELLM_DEPLOYMENT must be set (work|personal)}"
PORT="${LITELLM_LOCAL_PORT:-4000}"
export OP_CACHE=false
export OP_LOAD_DESKTOP_APP_SETTINGS=false

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$DEPLOYMENT" "$*"; }
die() { log "ERROR: $*"; exit 78; }
resolved_vars=""   # value-evidence ledger: every var this wrapper itself exported

note_resolved() { # note_resolved VAR — record that THIS wrapper set VAR
  resolved_vars="$resolved_vars $1"
}
is_resolved_by_wrapper() { # is_resolved_by_wrapper VAR -> 0/1
  case " $resolved_vars " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# ---- 1. Service-account token: Keychain primary, manifest file fallback -----
OP_KEYCHAIN_SERVICE="${OP_KEYCHAIN_SERVICE:-litellm-gateways-op-token}"
OP_KEYCHAIN_ACCOUNT="${OP_KEYCHAIN_ACCOUNT:-pjbeyer}"
if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  tok="$(/usr/bin/security find-generic-password -s "$OP_KEYCHAIN_SERVICE" -a "$OP_KEYCHAIN_ACCOUNT" -w 2>/dev/null || true)"
  if [ -n "$tok" ]; then
    export OP_SERVICE_ACCOUNT_TOKEN="$tok"
    note_resolved OP_SERVICE_ACCOUNT_TOKEN
    log "resolved OP_SERVICE_ACCOUNT_TOKEN from Keychain ($OP_KEYCHAIN_SERVICE)"
  else
    die "no Keychain item '$OP_KEYCHAIN_SERVICE' and no OP_SERVICE_ACCOUNT_TOKEN in env (required=true)"
  fi
else
  note_resolved OP_SERVICE_ACCOUNT_TOKEN
fi

OP_BIN="${OP_BIN:-$(command -v op || echo /opt/homebrew/bin/op)}"
command -v "$OP_BIN" >/dev/null 2>&1 || die "op CLI not found on PATH (or /opt/homebrew/bin/op)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ---- 2. Credential resolution from the manifest (Contract 1) ----------------
# The manifest is authoritative: env_var | op_ref | required. This loop is
# generic; the manifest decides what exists and whether it is required.
CREDENTIALS_MANIFEST="${CREDENTIALS_MANIFEST:?CREDENTIALS_MANIFEST must point at credentials.toml}"
[ -f "$CREDENTIALS_MANIFEST" ] || die "manifest missing: $CREDENTIALS_MANIFEST"

# Parse with python (stdlib tomllib) via the helper file — no shell parsing of
# untrusted shapes, no heredocs inside command substitution.
manifest_json="$(/usr/bin/python3 "$(dirname "$0")/parse_credentials.py" "$CREDENTIALS_MANIFEST")" \
  || die "manifest unreadable: $CREDENTIALS_MANIFEST"

while IFS=$'\t' read -r var ref required; do
  [ -n "$var" ] || continue
  if val="$("$OP_BIN" read "$ref" 2>/dev/null)" && [ -n "$val" ]; then
    export "$var=$val"
    note_resolved "$var"
    log "resolved $var from 1Password (value-evidence ok)"
  else
    if [ "$required" = "1" ]; then
      die "required credential $var ($ref) unresolvable — refusing to start (would come up unauthenticated/unconfigured)"
    else
      log "WARN (JUSTIFIED): optional credential $var ($ref) unresolvable — documented degrade path applies (see manifest purpose)"
    fi
  fi
done <<< "$manifest_json"

# ---- 3. Master-key guard: value evidence, never name matching (F-CI-1) -----
# The config's master_key references some env var (e.g. LITELLM_LOCAL_KEY).
# Old code grepped for the LITERAL name — a renamed reference took the else
# branch and the proxy STARTED WIDE OPEN. Instead: find the master_key env
# reference in the rendered config and require that var to be resolved here.
CONFIG="${LITELLM_PROXY_CONFIG:?LITELLM_PROXY_CONFIG must be set}"
[ -f "$CONFIG" ] || die "config missing: $CONFIG"
MASTER_KEY_VAR="$(sed -n '/^general_settings:/,/^[a-z_]*:/p' "$CONFIG" | grep -oE 'os\.environ/[A-Z_][A-Z_0-9]*' | head -1 | cut -d/ -f2 || true)"
[ -n "$MASTER_KEY_VAR" ] || die "config does not reference a master-key env var — refusing (litellm would start unauthenticated)"
is_resolved_by_wrapper "$MASTER_KEY_VAR" && [ -n "$(printenv "$MASTER_KEY_VAR")" ] \
  || die "master-key variable $MASTER_KEY_VAR referenced by config was NOT resolved by this wrapper (value-evidence gate — FR-005)"
log "master-key gate OK: $MASTER_KEY_VAR resolved by value evidence"

# ---- 4. Patch sentinel + pin assertion (FR-006, F-CI-2) ---------------------
PROXY_PYTHON="${LITELLM_PROXY_PYTHON:?LITELLM_PROXY_PYTHON must point at the pinned build interpreter}"
SENTINEL="${LITELLM_PATCH_SENTINEL:-args_already_complete}"
MODULE="litellm.completion_extras.litellm_responses_transformation.transformation"
# Patch sentinel check lives in its own file: core/check_patch_sentinel.py
# (avoids embedding python inside bash command substitution — shellcheck-safe).
if ! env -u PYTHONPATH -u PYTHONHOME "$PROXY_PYTHON" \
     "$(dirname "$0")/check_patch_sentinel.py" "$SENTINEL" "$MODULE"; then
  die "pinned-build patch assertion FAILED (sentinel '$SENTINEL' absent or module unloadable)"
fi
log "patch assertion OK: sentinel '$SENTINEL' present in $MODULE"

# ---- 5. Launch ----------------------------------------------------------------
LITELLM_BIN="$(dirname "$PROXY_PYTHON")/litellm"
[ -x "$LITELLM_BIN" ] || die "litellm launcher not found next to the pinned interpreter: $LITELLM_BIN"
log "starting $DEPLOYMENT proxy on 127.0.0.1:$PORT (config: $CONFIG)"
exec env -u PYTHONPATH -u PYTHONHOME "$LITELLM_BIN" \
  --config "$CONFIG" --host 127.0.0.1 --port "$PORT"

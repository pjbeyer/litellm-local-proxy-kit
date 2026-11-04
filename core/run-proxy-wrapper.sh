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
#   OP_KEYCHAIN_ACCOUNT     Keychain account (default: current uid's name)
#   CREDENTIALS_MANIFEST    path to credentials.toml (Contract 1)
#   LITELLM_PATCH_SENTINEL  patch sentinel symbol to assert in the installed build (FR-006)
#   LITELLM_PROXY_PYTHON    python interpreter of the pinned build (must be set)
#
# Exit codes: 78 = EX_CONFIG (launchd backs off; no hot-loop on bad config).
set -euo pipefail

PREFLIGHT_ONLY=0
ARG_PORT=""
for arg in "$@"; do
  case "$arg" in
    --preflight) [ "$PREFLIGHT_ONLY" = "0" ] || exit 64; PREFLIGHT_ONLY=1 ;;
    *[!0-9]*|"") echo "usage: run-proxy-wrapper.sh [PORT] [--preflight]" >&2; exit 64 ;;
    *)
      [ -z "$ARG_PORT" ] && [ "${#arg}" -le 5 ] || exit 64
      [ "$((10#$arg))" -ge 1 ] && [ "$((10#$arg))" -le 65535 ] || exit 64
      ARG_PORT="$((10#$arg))"
      ;;
  esac
done

DEPLOYMENT="${LITELLM_DEPLOYMENT:?LITELLM_DEPLOYMENT must be set (work|personal)}"
PORT="${ARG_PORT:-${LITELLM_LOCAL_PORT:-4000}}"
export OP_CACHE=false
export OP_LOAD_DESKTOP_APP_SETTINGS=false

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$DEPLOYMENT" "$*"; }
die() { log "ERROR: $*"; exit 78; }
# A registered managed job may retry while an owner-authorized bridge serves.
# Refuse before vault/MCP/DB initialization; do not start a competing proxy.
# --preflight intentionally checks credentials without disturbing that listener.
if [ "$PREFLIGHT_ONLY" = "0" ] && [ -n "${LITELLM_PROXY_LABEL:-}" ]; then
  port_check=0
  /usr/bin/python3 -c '
import errno, socket, sys
try:
    port = int(sys.argv[1])
    if not 1 <= port <= 65535:
        sys.exit(2)
    with socket.socket() as s:
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", port))
    sys.exit(1 if result == 0 else (0 if result == errno.ECONNREFUSED else 2))
except (OSError, ValueError):
    sys.exit(2)
' "$PORT" || port_check=$?
  case "$port_check" in
    0) ;;
    1) die "loopback port $PORT already occupied — preserving existing listener" ;;
    *) die "cannot establish loopback port availability — refusing to start" ;;
  esac
fi
resolved_vars=""   # value-evidence ledger: every var this wrapper itself exported

note_resolved() { # note_resolved VAR — record that THIS wrapper set VAR
  resolved_vars="$resolved_vars $1"
}
is_resolved_by_wrapper() { # is_resolved_by_wrapper VAR -> 0/1
  case " $resolved_vars " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# Validate the complete inventory before Keychain or any credential resolver.
[ -n "${CREDENTIALS_MANIFEST:-}" ] || die "CREDENTIALS_MANIFEST must point at credentials.toml"
[ -f "$CREDENTIALS_MANIFEST" ] || die "manifest missing: $CREDENTIALS_MANIFEST"
manifest_json="$(/usr/bin/python3 "$(dirname "$0")/parse_credentials.py" "$CREDENTIALS_MANIFEST")" \
  || die "manifest unreadable: $CREDENTIALS_MANIFEST"

# ---- 1. Service-account token: Keychain primary, manifest file fallback -----
OP_KEYCHAIN_SERVICE="${OP_KEYCHAIN_SERVICE:-litellm-gateways-op-token}"
OP_KEYCHAIN_ACCOUNT="${OP_KEYCHAIN_ACCOUNT:-$(id -un 2>/dev/null || true)}"
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
# Inventory parsing and uniqueness validation already completed before step 1.

while IFS=$'\t' read -r var ref required source; do
  [ -n "$var" ] || continue
  source="${source:-op}"
  if [ "$source" = "aws-sso-shim" ]; then
    # Deferred: all aws-sso-shim rows are satisfied by ONE shim run below.
    continue
  fi
  if [ "$source" = "cli-token" ]; then
    # Closed argv, bounded resolution; stdout is consumed only by this shell.
    # The resolver is a generic CLI token tool: `cli-token` executes
    # `<bin> auth print-token` with fixed argv and a 30s bound; set
    # CLI_TOKEN_BIN to the absolute path of the resolver binary.
    cli_bin="${CLI_TOKEN_BIN:-$(command -v cli-token || true)}"
    if val="$(/usr/bin/python3 -c '
import subprocess, sys
try:
    r = subprocess.run([sys.argv[1], "auth", "print-token"], capture_output=True, text=True, timeout=30)
    v = r.stdout.strip()
    if r.returncode or not v or v.startswith("op://") or any(c.isspace() for c in v):
        sys.exit(1)
    print(v)
except (OSError, subprocess.SubprocessError):
    sys.exit(1)
' "$cli_bin" 2>/dev/null)" && [ -n "$val" ]; then
      export "$var=$val"
      note_resolved "$var"
      log "resolved $var from cli-token (value-evidence ok)"
    elif [ "$required" = "1" ]; then
      die "required credential $var from cli-token unresolvable — refusing to start"
    else
      unset "$var"
      log "WARN (JUSTIFIED): optional credential $var from cli-token unresolvable"
    fi
    continue
  fi
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

# ---- 2a. AWS SSO shim credentials (Phase 2: Bedrock embeddings) --------------
# One shim run satisfies every source=aws-sso-shim row. The shim exits 78 when
# no usable SSO token exists; that is a JUSTIFIED degrade here (the semantic
# tool filter fails open without embeddings — chat traffic is unaffected), so
# required shim rows WARN instead of aborting. Value evidence still applies:
# every shim var must be non-empty after sourcing, else the same WARN path.
shim_bin="$(dirname "$0")/aws-sso-cred-shim.sh"
shim_needed=0
while IFS=$'\t' read -r var ref required source; do
  [ "$source" = "aws-sso-shim" ] && shim_needed=1
done <<< "$manifest_json"

if [ "$shim_needed" = "1" ]; then
  [ -x "$shim_bin" ] || die "manifest declares aws-sso-shim credentials but shim is missing: $shim_bin"
  if shim_out="$("$shim_bin" 2>/dev/null)" && [ -n "$shim_out" ]; then
    # shellcheck disable=SC1090
    eval "$shim_out"
    while IFS=$'\t' read -r var ref required source; do
      [ "$source" = "aws-sso-shim" ] || continue
      if [ -n "$(printenv "$var" 2>/dev/null || true)" ]; then
        note_resolved "$var"
        log "resolved $var from aws-sso-shim (value-evidence ok)"
      elif [ "$required" = "1" ]; then
        log "WARN (JUSTIFIED): shim did not set $var — semantic-filter degrade path applies (filter fails open; chat unaffected)"
      else
        log "WARN (JUSTIFIED): shim did not set optional $var"
      fi
    done <<< "$manifest_json"
  else
    while IFS=$'\t' read -r var ref required source; do
      [ "$source" = "aws-sso-shim" ] || continue
      if [ "$required" = "1" ]; then
        log "WARN (JUSTIFIED): aws-sso-shim unavailable for $var — semantic-filter degrade path applies (filter fails open; chat unaffected)"
      else
        log "WARN (JUSTIFIED): aws-sso-shim unavailable for optional $var"
      fi
    done <<< "$manifest_json"
  fi
fi

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

# ---- 3b. Coverage gate: every os.environ/VAR the config references must be
# accounted for (FR-005). The upstream proxy FAILS OPEN here: a deployment
# whose credential is missing is silently dropped at router construction
# ("ignoring and continuing with other deployments") while the server comes
# up and serves everything else. A manifest row that was DELETED (not merely
# unresolvable) is invisible to the resolve loop above, so coverage must be
# checked against the config's own references. For each referenced var:
#   - resolved by this wrapper (value evidence)          -> covered
#   - non-empty in the environment (shim / operator env) -> covered
#   - declared in the manifest as optional               -> covered (degrade path)
#   - otherwise                                          -> exit 78
for CONFIG_VAR in $(sed -n 's/.*os\.environ\/\([A-Z_][A-Z_0-9]*\).*/\1/p' "$CONFIG" | sort -u); do
  if is_resolved_by_wrapper "$CONFIG_VAR" && [ -n "$(printenv "$CONFIG_VAR" 2>/dev/null || true)" ]; then
    continue
  fi
  if [ -n "$(printenv "$CONFIG_VAR" 2>/dev/null || true)" ]; then
    log "coverage: $CONFIG_VAR resolved from environment (not by this wrapper)"
    continue
  fi
  if grep -q "env_var=\"$CONFIG_VAR\"" "$CREDENTIALS_MANIFEST" || grep -q "env_var = \"$CONFIG_VAR\"" "$CREDENTIALS_MANIFEST"; then
    # Declared in the manifest: the resolve loop above already classified the
    # row (required+unresolvable dies there; optional unresolved WARNs there).
    # Nothing further to require at coverage time.
    log "coverage: $CONFIG_VAR declared in the manifest (row classification already applied)"
    continue
  fi
  die "config references $CONFIG_VAR via os.environ but no manifest row covers it — refusing (upstream proxy would silently drop the dependent deployment)"
done
log "coverage gate OK: all $(sed -n 's/.*os\.environ\/\([A-Z_][A-Z_0-9]*\).*/\1/p' "$CONFIG" | sort -u | wc -l | tr -d ' ') referenced vars accounted for"

# ---- 3c. Classifier-key preflight -------------------------------------------
# The Jev classifier does not reference its key via os.environ in the config;
# it reads TYPESAFE_API_KEY at request time via get_secret_str. If that row is
# omitted from the manifest nothing above detects it, and the failure is
# fail-open at runtime. If the config declares a Jev classifier, require the
# row to exist.
if grep -Eq 'classifier_type[[:space:]]*[:=][[:space:]]*["]?jev' "$CONFIG"; then
  grep -Eq "env_var[[:space:]]*=[[:space:]]*\"TYPESAFE_API_KEY\"" "$CREDENTIALS_MANIFEST" \
    || die "config declares a Jev classifier but the manifest has no TYPESAFE_API_KEY row — refusing (upstream proxy would start and silently drop the smart-router deployment)"
fi
log "classifier preflight OK: Jev classifier manifest row present"

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
if [ "$PREFLIGHT_ONLY" = "1" ]; then
  PIN_LOCK="$(dirname "$0")/pin.lock"
  [ -f "$PIN_LOCK" ] || die "preflight requires the installed pin.lock"
  env -u PYTHONPATH -u PYTHONHOME "$PROXY_PYTHON" \
    "$(dirname "$0")/check_pin.py" "$PIN_LOCK" \
    || die "pinned-build provenance preflight FAILED"
  log "preflight complete — no listener started and no process stopped"
  exit 0
fi
log "starting $DEPLOYMENT proxy on 127.0.0.1:$PORT (config: $CONFIG)"
exec env -u PYTHONPATH -u PYTHONHOME "$LITELLM_BIN" \
  --config "$CONFIG" --host 127.0.0.1 --port "$PORT"

#!/bin/bash
# Idempotent installer: repo -> XDG apply targets (spec 015, Contract 5).
#
# Modes:
#   --dry-run   (default) show what would change; touch nothing
#   apply       write apply targets + launchd plists
#   --verify    byte-check apply targets against repo-rendered sources
#
# Rules (FR-001/FR-002/FR-005):
#   * writes ONLY the declared apply targets
#   * removes/replaces stale entry points it owns (legacy launchers)
#   * never touches unowned files (those become drift-check exemptions)
#   * fails closed (exit 78) if any required credential reference is
#     unresolvable at check time (manifest parse must succeed)
#   * never starts/stops launchd jobs — operator runs launchctl (EDR Mac)
#
# Usage: installer.sh <deployment-root> [--dry-run|apply|--verify]
#   deployment-root contains: deployments/<name>/{overlay.yaml,credentials.toml,pin.lock}
set -euo pipefail

CORE_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="dry-run"
TARGET_ROOT=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    apply) MODE="apply" ;;
    --verify) MODE="verify" ;;
    *) TARGET_ROOT="$arg" ;;
  esac
done
[ -n "$TARGET_ROOT" ] || { echo "usage: installer.sh <deployment-root> [--dry-run|apply|--verify]" >&2; exit 64; }

# shellcheck disable=SC1091
. "$TARGET_ROOT/deploy.conf.sh" 2>/dev/null || true

DEPLOY_NAME="${LITELLM_DEPLOYMENT:?deploy.conf.sh must set LITELLM_DEPLOYMENT}"
DEPLOY_DIR="$TARGET_ROOT/deployments/$DEPLOY_NAME"
CONFIG_DIR="${LITELLM_CONFIG_DIR:?deploy.conf.sh must set LITELLM_CONFIG_DIR (apply target)}"
LOG_DIR="${LITELLM_LOG_DIR:?deploy.conf.sh must set LITELLM_LOG_DIR (apply target)}"
LABEL="${LITELLM_PROXY_LABEL:?deploy.conf.sh must set LITELLM_PROXY_LABEL}"
PORT="${LITELLM_LOCAL_PORT:?deploy.conf.sh must set LITELLM_LOCAL_PORT}"
PROXY_PYTHON="${LITELLM_PROXY_PYTHON:?deploy.conf.sh must set LITELLM_PROXY_PYTHON (pinned build)}"
SENTINEL="${LITELLM_PATCH_SENTINEL:-args_already_complete}"
MANIFEST="$DEPLOY_DIR/credentials.toml"
OVERLAY="$DEPLOY_DIR/overlay.yaml"
PIN_LOCK="$DEPLOY_DIR/pin.lock"

log() { printf '%s [installer] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
die() { log "ERROR: $*"; exit 78; }

[ -f "$OVERLAY" ] || die "overlay missing: $OVERLAY"
[ -f "$MANIFEST" ] || die "credentials manifest missing: $MANIFEST"
[ -f "$PIN_LOCK" ] || die "pin.lock missing: $PIN_LOCK"

# ---- Gate 1: manifest parses (fail closed on unparseable/empty) -------------
/usr/bin/python3 "$CORE_DIR/parse_credentials.py" "$MANIFEST" >/dev/null || die "credentials manifest invalid"

# ---- Gate 2: patch sentinel present in the pinned build (FR-006) ------------
if ! env -u PYTHONPATH -u PYTHONHOME "$PROXY_PYTHON" "$CORE_DIR/check_patch_sentinel.py" "$SENTINEL" \
      "litellm.completion_extras.litellm_responses_transformation.transformation" >/dev/null 2>&1; then
  die "pinned build fails the patch sentinel check — refusing to install around it"
fi
log "pinned build patch assertion OK ($SENTINEL)"

# ---- Gate 2a: dist-info version + worktree HEAD match pin.lock (FR-006) ------
if ! env -u PYTHONPATH -u PYTHONHOME "$PROXY_PYTHON" "$CORE_DIR/check_pin.py" \
      "$PIN_LOCK" >/dev/null 2>&1; then
  die "pinned build fails the pin-provenance check — refusing to install around it"
fi
log "pinned build provenance OK (dist-info + HEAD match pin.lock)"

# ---- Render config + plists into a staging dir ------------------------------
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/litellm-install.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
TEMPLATE="$CORE_DIR/config.template.header.txt"
[ -f "$TEMPLATE" ] || TEMPLATE="$CORE_DIR/../deployments/$DEPLOY_NAME/config.header.txt"
"$PROXY_PYTHON" "$CORE_DIR/render_config.py" --overlay "$OVERLAY" --template "$TEMPLATE" \
  --out "$STAGE/config.rendered.yaml" || die "config render failed"
"$PROXY_PYTHON" "$CORE_DIR/check_credential_coverage.py" \
  --config "$STAGE/config.rendered.yaml" --manifest "$MANIFEST" --overlay "$OVERLAY" \
  || die "rendered config credential coverage failed — no apply targets changed"
# Pinned venv python for rendering tools: venvs ignore user-site entirely, so
# operator PYTHONUSERBASE/PYTHONPATH pollution (e.g. a stale shared user-site)
# cannot break the render. Stdlib-only helpers below stay on /usr/bin/python3.
"$PROXY_PYTHON" "$CORE_DIR/install_launchagent.py" --label "$LABEL" --port "$PORT" \
  --log-dir "$LOG_DIR" --wrapper-dir "$CONFIG_DIR" --work-dir "$CONFIG_DIR" \
  --deployment "$DEPLOY_NAME" --proxy-python "$PROXY_PYTHON" --runtime-overlay "$OVERLAY" \
  --op-keychain-service "${OP_KEYCHAIN_SERVICE:?deploy.conf.sh must set OP_KEYCHAIN_SERVICE}" \
  --op-keychain-account "${OP_KEYCHAIN_ACCOUNT:?deploy.conf.sh must set OP_KEYCHAIN_ACCOUNT}" \
  --out "$STAGE" || die "plist render failed"
cp "$CORE_DIR/run-proxy-wrapper.sh" "$CORE_DIR/check_patch_sentinel.py" "$CORE_DIR/check_pin.py" \
   "$CORE_DIR/parse_credentials.py" "$CORE_DIR/rotate-logs.sh" "$CORE_DIR/aws-sso-cred-shim.sh" \
   "$CORE_DIR/harvest-filter-metrics.sh" "$CORE_DIR/check_credential_coverage.py" \
   "$STAGE/"
# The plist points at this deployed manifest; shipping only the parser leaves
# every installed wrapper failing closed before credential resolution.
cp "$MANIFEST" "$STAGE/credentials.toml"
cp "$PIN_LOCK" "$STAGE/pin.lock"

# Managed file set: everything the installer owns at the apply target.
MANAGED=(config.rendered.yaml "$LABEL.plist" "$LABEL.rotate.plist" run-proxy-wrapper.sh check_patch_sentinel.py check_pin.py parse_credentials.py rotate-logs.sh aws-sso-cred-shim.sh harvest-filter-metrics.sh credentials.toml pin.lock check_credential_coverage.py)
# Stale entry points the installer owns and must remove/rename (FR-001/F-CI-4).
STALE=(run-local-proxy.sh launchd-wrapper.sh)

apply() {
  if [ "$MODE" = "apply" ]; then
    mkdir -p "$CONFIG_DIR" "$LOG_DIR"
  fi
  local f
  for f in "${MANAGED[@]}"; do
    if ! cmp -s "$STAGE/$f" "$CONFIG_DIR/$f" 2>/dev/null; then
      if [ "$MODE" = "apply" ]; then
        # Atomic per-file replacement: a retrying launchd wrapper keeps its
        # old inode instead of reading a partially overwritten shell script.
        local pending="$CONFIG_DIR/.$f.install.$$"
        cp -p "$STAGE/$f" "$pending"
        mv -f "$pending" "$CONFIG_DIR/$f"
        log "wrote $CONFIG_DIR/$f"
      else
        log "DRY-RUN would write $CONFIG_DIR/$f"
      fi
    fi
  done
  if [ "$MODE" = "apply" ]; then
    local s
    for s in "${STALE[@]}"; do
      if [ -e "$CONFIG_DIR/$s" ]; then
        mv "$CONFIG_DIR/$s" "$LOG_DIR/retired-$s.$(date +%Y%m%d%H%M%S)"
        log "retired stale entry point $CONFIG_DIR/$s (moved to $LOG_DIR)"
      fi
    done
    log "apply complete — operator: launchctl bootstrap/bootout $LABEL as needed"
  elif [ "$MODE" = "dry-run" ]; then
    local s
    for s in "${STALE[@]}"; do
      [ -e "$CONFIG_DIR/$s" ] && log "DRY-RUN would retire stale entry point $CONFIG_DIR/$s"
    done
    log "dry-run complete — no changes written"
  fi
}

verify() {
  local f rc=0
  for f in "${MANAGED[@]}"; do
    if ! cmp -s "$STAGE/$f" "$CONFIG_DIR/$f" 2>/dev/null; then
      log "VERIFY FAIL: $CONFIG_DIR/$f differs from repo-rendered source"
      rc=1
    fi
  done
  [ "$rc" = "0" ] && log "verify OK: apply targets byte-match the repo render"
  return $rc
}

case "$MODE" in
  verify) verify ;;
  *) apply ;;
esac

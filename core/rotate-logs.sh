#!/usr/bin/env bash
# rotate-logs.sh — size-based log rotation for a launchd-managed proxy.
# (spec 015 Contract 4: kit core copy; the launchd rotate job runs this from
# the apply-target config dir, installed by core/installer.sh.)
#
# Why this exists: launchd's StandardOutPath/StandardErrorPath append forever
# and launchd has no rotation feature. A long-lived proxy therefore grows its
# error log without bound (observed: 2.0M of mostly repeated stack traces).
#
# The subtlety that makes the obvious approach wrong: launchd holds an open
# file descriptor on the log. Renaming the file does NOT redirect launchd,
# which keeps writing to the renamed inode and leaves the new path empty. So
# this rotates by COPY then TRUNCATE IN PLACE, which preserves launchd's fd.
#
# Truncate-in-place only works because launchd opens these files O_APPEND, so
# the next write lands at the truncated EOF rather than a stale offset (which
# would produce a sparse file full of NUL bytes). Asserted in tests, not trusted.
#
# Safe to run by hand or on a timer. Never fails the timer on a missing file.
set -euo pipefail

# Inputs from the rotate plist EnvironmentVariables (identity-neutral core:
# no deploy-env.sh sourcing, no operator defaults beyond generic fallbacks).
LABEL="${LITELLM_PROXY_LABEL:-litellm}"
LOG_DIR="${LITELLM_PROXY_LOG_DIR:?rotate plist must set LITELLM_PROXY_LOG_DIR}"
MAX_BYTES="${LITELLM_LOG_MAX_BYTES:-5242880}"   # 5 MiB
KEEP="${LITELLM_LOG_KEEP:-3}"                   # generations to retain
LOGS=("proxy.log" "proxy.err.log")

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

# ---- 0. Metrics harvest (Phase 2): persist semantic-filter events BEFORE the
# rotate step can truncate them. Runs only if the harvester ships beside this
# script; wrapped in a timeout and best-effort — a harvesting failure must
# never prevent rotation (same fail-open posture as the filter itself).
harvest_bin="$(dirname "$0")/harvest-filter-metrics.sh"
if [[ -x "$harvest_bin" ]]; then
  if command -v timeout >/dev/null 2>&1; then
    if ! timeout 60 "$harvest_bin" 2>&1 | sed 's/^/[harvest] /'; then
      log "WARN: filter-metrics harvest failed (non-fatal; rotation proceeds)"
    fi
  else
    "$harvest_bin" 2>&1 | sed 's/^/[harvest] /' || log "WARN: filter-metrics harvest failed (non-fatal; rotation proceeds)"
  fi
else
  log "no harvester shipped; skipping filter-metrics harvest"
fi

[[ -d "$LOG_DIR" ]] || { log "no log dir at $LOG_DIR (nothing to do)"; exit 0; }

rotated=0
for name in "${LOGS[@]}"; do
  f="$LOG_DIR/$name"
  [[ -f "$f" ]] || continue

  size="$(stat -f%z "$f" 2>/dev/null || echo 0)"
  [[ "$size" =~ ^[0-9]+$ ]] || size=0
  if (( size <= MAX_BYTES )); then
    continue
  fi

  # Shift generations oldest-first so nothing is overwritten.
  i="$KEEP"
  while (( i > 1 )); do
    prev="$f.$((i - 1))"
    [[ -f "$prev" ]] && mv -f "$prev" "$f.$i"
    i=$((i - 1))
  done

  cp "$f" "$f.1"
  : > "$f"          # truncate in place: keeps launchd's descriptor valid
  rotated=$((rotated + 1))
  log "rotated $name (${size} bytes > ${MAX_BYTES}), kept $KEEP generations"
done

if (( rotated == 0 )); then
  log "no log exceeded ${MAX_BYTES} bytes"
fi

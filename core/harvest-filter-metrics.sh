#!/usr/bin/env bash
# harvest-filter-metrics.sh — persist the MCP semantic tool filter's per-request
# metrics out of the proxy logs before rotation can eat them.
#
# Why this exists: the filter's per-request evidence lives only in proxy.log
# lines ("Semantic tool filter: N->K MCP tools ...") and the response header;
# neither lands in the Postgres spend logs (verified 2026-10-24: 0 of 633
# post-flip rows carry filter stats). The rotate job truncates logs at 5 MiB
# and keeps 3 generations, so unharvested lines age out within days. This
# script appends every observed filter event to a permanent JSONL.
#
# Design constraints:
# - Provider-agnostic: it parses the generic filter log lines only. It never
#   references Bedrock, AWS, or any specific embedding provider. A future
#   swap of embedding model changes nothing here; the active embedding model
#   is read from the rendered config as an informational echo when present.
# - Feature-inactive tolerant: if the filter is disabled (no config block, or
#   no filter lines in the harvested window), the JSONL still gets a small
#   heartbeat record per run so gap analysis can distinguish "feature off"
#   from "proxy down" from "no MCP traffic". Zero filter events is a fact
#   worth recording once per run, not an error.
# - Never blocks rotation: this runs inside rotate-logs.sh before the rotate
#   step, wrapped with a timeout, and every failure path exits 0. Losing an
#   hour of metrics is acceptable; breaking rotation is not.
# - Crash-safe cursor: byte offset per log file in a state file keyed by
#   filename. On shrink (copy+truncate rotation signature) the cursor resets
#   to 0 so nothing is skipped and nothing is double-read.
#
# State/data locations (XDG):
#   data:  ${XDG_DATA_HOME:-~/.local/share}/litellm/filter-metrics.jsonl
#   state: ${XDG_STATE_HOME:-~/.local/state}/litellm/filter-harvest-cursor
#
# Env (set by rotate-logs.sh / rotate plist):
#   LITELLM_PROXY_LOG_DIR  - directory holding proxy.log / proxy.err.log
#   LITELLM_FILTER_CONFIG  - optional path to rendered config (defaults to
#                            the apply-target config dir sibling of LOG_DIR)
#   LITELLM_FILTER_HARVEST - set to 0 to disable harvesting entirely
set -uo pipefail

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "harvest: $*"; }

[[ "${LITELLM_FILTER_HARVEST:-1}" = "1" ]] || { log "disabled by LITELLM_FILTER_HARVEST=0"; exit 0; }

LOG_DIR="${LITELLM_PROXY_LOG_DIR:?harvest requires LITELLM_PROXY_LOG_DIR}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/litellm"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/litellm"
OUT="$DATA_DIR/filter-metrics.jsonl"
CURSOR="$STATE_DIR/filter-harvest-cursor"

mkdir -p "$DATA_DIR" "$STATE_DIR"

# Optional: echo the active embedding model + operating point from the
# rendered config, provider-agnostically. Missing config or keys are fine —
# the echo fields are informational and simply stay absent.
CONFIG="${LITELLM_FILTER_CONFIG:-$(dirname "$0")/config.rendered.yaml}"
emb_model=""
sim_threshold=""
top_k=""
filter_enabled=""
if [[ -n "$CONFIG" && -f "$CONFIG" ]]; then
  # Indented block scan under mcp_semantic_tool_filter; no yaml dependency.
  in_block=0
  while IFS= read -r line; do
    case "$line" in
      '  mcp_semantic_tool_filter:'*) in_block=1; continue ;;
      *) [[ $in_block = 1 ]] && ! [[ "$line" == '    '* ]] && break ;;
    esac
    if [[ $in_block = 1 ]]; then
      case "$line" in
        *'enabled:'*)   filter_enabled="${line##*enabled: }" ;;
        *'embedding_model:'*) emb_model="${line##*embedding_model: }" ;;
        *'similarity_threshold:'*) sim_threshold="${line##*similarity_threshold: }" ;;
        *'top_k:'*)     top_k="${line##*top_k: }" ;;
      esac
    fi
  done < "$CONFIG"
fi

now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
events=0
touch "$OUT" "$CURSOR"

# Parse one log file's new bytes. Filter lines look like:
#   ...INFO: hook.py:254 - Semantic tool filter: 894->7 MCP tools (0 native preserved, 1 total)
#   ...INFO: hook.py:261 - Semantic tool filter: all 11 tools are native, no MCP filtering applied
# plus failure-mode warnings from the filter itself. Only these stable shapes
# are parsed; anything else passes through untouched.
harvest_file() {
  local f="$1" name="$2"
  [[ -f "$f" ]] || return 0
  local size off
  size="$(stat -f%z "$f" 2>/dev/null || echo 0)"
  off="$(awk -v n="$name" '$1 == n { print $2; exit }' "$CURSOR" 2>/dev/null)"
  [[ "$off" =~ ^[0-9]+$ ]] || off=0

  if (( size < off )); then
    # Copy+truncate rotation signature: start over from the top.
    off=0
  fi

  if (( size > off )); then
    local slice
    slice="$(tail -c +$((off + 1)) "$f" 2>/dev/null || true)"
    while IFS= read -r line; do
      case "$line" in
        *"Semantic tool filter: "*"->"*" MCP tools"*)
          local stats
          stats="${line##*Semantic tool filter: }"
          stats="${stats%% MCP tools*}"
          local before after
          before="${stats%%->*}"
          after="${stats##*->}"
          if [[ "$before" =~ ^[0-9]+$ && "$after" =~ ^[0-9]+$ ]]; then
            emb_json="null"
            [[ -n "$emb_model" ]] && emb_json="\"$emb_model\""
            printf '{"ts":"%s","event":"filter","before":%s,"after":%s,"embedding_model":%s,"similarity_threshold":%s,"top_k":%s}\n' \
              "$now" "$before" "$after" "$emb_json" "${sim_threshold:-null}" "${top_k:-null}" >> "$OUT"
            events=$((events + 1))
          fi
          ;;
        *"tools are native, no MCP filtering applied"*)
          local n
          n="${line##*all }"
          n="${n%% tools are native*}"
          [[ "$n" =~ ^[0-9]+$ ]] || n=-1
          printf '{"ts":"%s","event":"native_only","native_tools":%s}\n' "$now" "$n" >> "$OUT"
          events=$((events + 1))
          ;;
        *"Failed to emit semantic filter metadata"*|*"semantic tool filter hook"*"failed"*|*"semantic-router"*"error"*)
          printf '{"ts":"%s","event":"filter_error","detail":%s}\n' \
            "$now" "$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[:300]))')" >> "$OUT"
          events=$((events + 1))
          ;;
      esac
    done <<< "$slice"
  fi

  # Persist the new offset for this file (atomic-ish: rewrite whole cursor).
  if [[ -s "$CURSOR" ]] && grep -qv "^$name " "$CURSOR" 2>/dev/null; then
    grep -v "^$name " "$CURSOR" > "$CURSOR.tmp" && mv -f "$CURSOR.tmp" "$CURSOR"
  elif [[ -s "$CURSOR" ]]; then
    grep -v "^$name " "$CURSOR" > "$CURSOR.tmp" && mv -f "$CURSOR.tmp" "$CURSOR"
  fi
  printf '%s %s\n' "$name" "$size" >> "$CURSOR"
}

harvest_file "$LOG_DIR/proxy.log" "proxy.log"
harvest_file "$LOG_DIR/proxy.err.log" "proxy.err.log"

# Heartbeat: one row per run regardless of activity, so the rollup can tell
# feature-off from proxy-down from no-traffic. Heartbeats carry the config
# echo (absent = filter block missing from config = feature disabled).
hb_enabled="${filter_enabled:-absent}"
emb_json="null"
[[ -n "$emb_model" ]] && emb_json="\"$emb_model\""
printf '{"ts":"%s","event":"heartbeat","filter_enabled":"%s","embedding_model":%s,"similarity_threshold":%s,"top_k":%s,"filter_events":%s}\n' \
  "$now" "$hb_enabled" "$emb_json" "${sim_threshold:-null}" "${top_k:-null}" "$events" >> "$OUT"

log "harvested $events filter events -> $OUT (config echo: enabled=$hb_enabled model=${emb_model:-none})"
exit 0

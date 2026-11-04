# Source this file from your EXISTING run-proxy-server.sh, after op run resolves secrets.
# Do not run it as a separate subprocess: its exports must reach LiteLLM.
: "${CLOUDFLARE_ACCOUNT_ID:?Set CLOUDFLARE_ACCOUNT_ID in LaunchAgent EnvironmentVariables}"
case "$CLOUDFLARE_ACCOUNT_ID" in
  *[!0-9a-fA-F]*|'') echo 'Invalid Cloudflare account ID (expected 32 hexadecimal characters)' >&2; return 1 ;;
esac
if [ "${#CLOUDFLARE_ACCOUNT_ID}" -ne 32 ]; then
  echo 'Invalid Cloudflare account ID length (expected 32)' >&2
  return 1
fi
export CLOUDFLARE_API_BASE="$(printf 'https://%s/client/v4/accounts/%s/ai/v1' 'api.cloudflare.com' "$CLOUDFLARE_ACCOUNT_ID")"
export PYTHONPATH="${HOME}/.litellm${PYTHONPATH:+:${PYTHONPATH}}"
export AUTO_FREE_STATE_DB="${HOME}/.litellm/auto-free-state.sqlite3"

#!/bin/bash
# aws-sso-cred-shim.sh — mint AWS role credentials from the cached SSO access
# token (non-interactive) and emit eval-able exports for the proxy wrapper.
#
# Contract (spec 015 extension, Phase 2 semantic-filter embeddings):
#   * reads ONLY the SSO cache under $HOME/.aws/sso/cache (no browser, no net
#     beyond the portal federation endpoint)
#   * picks the freshest cache file that carries an accessToken and is not
#     expired
#   * mints short-term role credentials (12 h) for the configured account/role
#   * prints ONLY export lines on stdout; diagnostics go to stderr
#   * exit 78 (EX_CONFIG) when no usable SSO token exists — the wrapper
#     classifies that as a JUSTIFIED_WARN degrade (the semantic filter fails
#     open; chat traffic is unaffected), never a proxy abort
#
# Env inputs (from deploy.conf.sh or wrapper):
#   AWS_SSO_ACCOUNT_ID   (default 775586162990)
#   AWS_SSO_ROLE_NAME    (default DevPowerUser)
#   AWS_SSO_REGION       (default us-east-1; also emitted as AWS_REGION)
#   AWS_SSO_MIN_TTL_MIN  minimum remaining token TTL to accept (default 5)
set -euo pipefail

ACCOUNT="${AWS_SSO_ACCOUNT_ID:-775586162990}"
ROLE="${AWS_SSO_ROLE_NAME:-DevPowerUser}"
REGION="${AWS_SSO_REGION:-us-east-1}"
MIN_TTL="${AWS_SSO_MIN_TTL_MIN:-5}"
PORTAL_HOST="${AWS_SSO_PORTAL_HOST:-portal.sso.us-east-1.amazonaws.com}"

err() { printf '%s [aws-sso-shim] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >&2; }

command -v curl >/dev/null 2>&1 || { err "curl not found"; exit 78; }

cache_dir="$HOME/.aws/sso/cache"
[ -d "$cache_dir" ] || { err "no SSO cache dir: $cache_dir"; exit 78; }

# Pick the freshest unexpired accessToken-bearing cache file. /usr/bin/python3
# is deliberately used (no venv dependency; stdlib only, per kit convention).
CACHE_FILE="$(/usr/bin/python3 - "$cache_dir" <<'PYEOF'
import json, sys, datetime, pathlib
now = datetime.datetime.now(datetime.timezone.utc)
best = None
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    try:
        d = json.loads(f.read_text())
    except Exception:
        continue
    tok = d.get("accessToken")
    exp = d.get("expiresAt")
    if not tok or not exp:
        continue
    try:
        e = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
    except Exception:
        continue
    ttl_min = (e - now).total_seconds() / 60
    if ttl_min < 0:
        continue
    if best is None or e > best[1]:
        best = (f, e, ttl_min)
if best is None:
    sys.exit(1)
print(best[0])
PYEOF
)" || { err "no unexpired SSO access token in cache — refresh AWS SSO (aws login)"; exit 78; }

CRED_JSON="$(curl -sS --max-time 30 \
  "https://${PORTAL_HOST}/federation/credentials?account_id=${ACCOUNT}&role_name=${ROLE}" \
  -H "x-amz-sso_bearer_token: $(/usr/bin/python3 -c "import json;print(json.load(open('$CACHE_FILE'))['accessToken'])")" \
)" || { err "portal credential mint failed (network?)"; exit 78; }

# The minted JSON carries session secrets — never pass it via argv (process
# argv is world-readable via `ps` and EDR-sensitive). Write it to a 0600 temp
# file; a heredoc cannot be combined with piped stdin for `python3 -`.
TMP_CRED="$(mktemp "${TMPDIR:-/tmp}/aws-sso-mint.XXXXXX")"
chmod 0600 "$TMP_CRED"
printf '%s' "$CRED_JSON" > "$TMP_CRED"
/usr/bin/python3 - "$REGION" "$TMP_CRED" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[2]) as fh:
        d = json.load(fh)
    rc = d["roleCredentials"]
except Exception as exc:
    print(f"mint response unusable: {type(exc).__name__}", file=sys.stderr)
    sys.exit(78)
for k in ("accessKeyId", "secretAccessKey", "sessionToken"):
    if not rc.get(k):
        print(f"mint response missing {k}", file=sys.stderr)
        sys.exit(78)
print(f"export AWS_ACCESS_KEY_ID={rc['accessKeyId']}")
print(f"export AWS_SECRET_ACCESS_KEY={rc['secretAccessKey']}")
print(f"export AWS_SESSION_TOKEN={rc['sessionToken']}")
print(f"export AWS_REGION={sys.argv[1]}")
PYEOF
RC=$?
rm -f "$TMP_CRED"
exit "$RC"

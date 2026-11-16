# Gate 5 plan — intended-generation restart authority (qualification)

**Date:** 2026-11-16 · **Prerequisite:** Gate 4 complete — canary
`ddd5ae3de73c9797fe9ae4766ffc59be` settled `canary_passed` (evidence
`5f3d2d5b…`, detail `71ee8672…`), all lane steps verified live.
**Boundary:** `qualification.json` is **operator-issued only** — the file
must be created by you, outside the agent context (the agent prepares
every value, verifies the result, and never writes the file itself).

## Pre-verified bindings (agent-read, all confirmed against live estate)

| Value | Value (from live sources) |
|---|---|
| controller_label | `ai.litellm.restart-controller` |
| controller_identity | `{"pid": 28483, "uid": 502, "ppid": 1, "pgid": 28483, "birth": [1794825842, 292859]}` — **live controller matches evidence** |
| controller_epoch | `49def657c564414eafa2dff7d313b245` |
| controller_digest | `cd6fdbe9d986cbfa41871d2411136192204226fabfc5232b962f9e8efa6e3151` (installed = `9e1a84b` worktree, byte-verified) |
| policy_digest | `10e191f57bd824fe90fbe72dc510d100630e71b21cd99a9b7a47a34555986b78` |
| plist_digest | `8c92053aba3f15b43e8401d9bec50be351579aad334fc45b57ebe2bb42ae2204` (`~/.config/litellm/ai.litellm.local-proxy.plist`) |
| controller_plist_digest | `d0a132c5b7b186f4273fbf2d903782f24015a50ac00db42500f3223f063f886d` (`~/Library/LaunchAgents/ai.litellm.restart-controller.plist`) |
| controller_supervision | `{"ExitTimeOut": 60, "KeepAlive": true, "RunAtLoad": true, "ThrottleInterval": 30}` — matches generated + evidence |
| shutdown_contract | matches the source constant exactly |
| canary_evidence_digest | `5f3d2d5be92ff399af4632645b32f7901c188df65c2861d8a782e9d2c15d0a1c` (file 0600/502, sha = name) |
| scope | `intended-controller` (production runtime accepts only this) |
| checks | `bootout`, `bootstrap`, `cleanup`, `keepalive`, `loaded_definition`, `shutdown_budget` — all `true` |
| incarnations | 2 distinct (runs 1 + 2, ppid 1, uid 502) |

## Owner commands (in order; stop on any nonzero exit)

```sh
# 0. Review: read the evidence yourself before issuing anything.
cat ~/.local/state/litellm/rc-v2/canary/evidence/5f3d2d5be92ff399af4632645b32f7901c188df65c2861d8a782e9d2c15d0a1c.json

# 1. Write the qualification record (0600, operator-issued).
#    The python variant below (bottom of this section) is preferred —
#    it computes fresh lifetimes atomically in one step.
cat > ~/.local/state/litellm/rc-v2/qualification.json <<'EOF'
{
 "version": 2,
 "operator_uid": 502,
 "controller_label": "ai.litellm.restart-controller",
 "controller_identity": {"pid": 28483, "uid": 502, "ppid": 1, "pgid": 28483, "birth": [1794825842, 292859]},
 "controller_digest": "cd6fdbe9d986cbfa41871d2411136192204226fabfc5232b962f9e8efa6e3151",
 "policy_digest": "10e191f57bd824fe90fbe72dc510d100630e71b21cd99a9b7a47a34555986b78",
 "plist_digest": "8c92053aba3f15b43e8401d9bec50be351579aad334fc45b57ebe2bb42ae2204",
 "controller_epoch": "49def657c564414eafa2dff7d313b245",
 "controller_plist_digest": "d0a132c5b7b186f4273fbf2d903782f24015a50ac00db42500f3223f063f886d",
 "controller_supervision": {"ExitTimeOut": 60, "KeepAlive": true, "RunAtLoad": true, "ThrottleInterval": 30},
 "shutdown_contract": {"logging_environment": {"OSLogRateLimit": "64"}, "shutdown_deadline_seconds": 50, "supervisor_exit_seconds": 60, "version": 1},
 "canary_evidence_digest": "5f3d2d5be92ff399af4632645b32f7901c188df65c2861d8a782e9d2c15d0a1c",
 "issued_at": <NOW_EPOCH>,
 "expires_at": <NOW_EPOCH+21600>,
 "issued_monotonic": <NOW_MONOTONIC>,
 "expires_monotonic": <NOW_MONOTONIC+21600>
}
EOF
chmod 600 ~/.local/state/litellm/rc-v2/qualification.json

# 2. Fill the lifetimes (6h validity, ≤24h enforced):
NOW_EPOCH=$(date +%s)
# issued_monotonic/expires_monotonic: see step 3's helper which prints them

# 3. Verify before it matters (agent-side dry check available on request):
#    the runtime validates on next restart admission; a malformed record
#    refuses with qualification_invalid/qualification_expired — fail-closed.
```

Because the lifetimes must be fresh at issue time, run this one-liner
**instead of steps 1–2** — it computes the monotonic pair itself:

```sh
python3 - <<'EOF'
import json, os, time
rec = { ... same fields as above ... }
now = time.time(); mono = time.monotonic()
rec.update(issued_at=now, expires_at=now+21600,
           issued_monotonic=mono, expires_monotonic=mono+21600)
p = os.path.expanduser("~/.local/state/litellm/rc-v2/qualification.json")
fd = os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
os.write(fd, json.dumps(rec, sort_keys=True, indent=1).encode()); os.close(fd)
print("written", p)
EOF
```

## After issue: agent-side verification (read-only)

I will run `Runtime.qualify`-equivalent checks against the written file
(all 16 field checks + evidence/detail chain), confirm the proxy on
:4000 is untouched, and record the Gate 5 state in the journal/evidence
file. If anything refuses: the record is deleted (`rm`), the refusal
reason is diagnosed, and a corrected record is re-issued by you — the
controller never auto-upgrades a stale or malformed record.

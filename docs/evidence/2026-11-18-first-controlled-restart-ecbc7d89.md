# First controlled restart — request ecbc7d893cf44d3e (2026-11-18)

## Outcome: succeeded

The first production controlled restart of `ai.litellm.local-proxy` completed
via the restart controller, generation `761fdf29` (kit HEAD `23efa05`).

## Chain of record

1. **Generation deploy** (owner, per `owner-runbook-761fdf29.md`): bootout of
   stale `54060da8` daemon, retire (rename) of bundle + plist, policy swap to
   config path BEFORE install (plist embeds the stable path), install +
   `--verify` (verified, activation not performed), one-paste arm-canary +
   bootstrap.
2. **Canary lane** (evidence `a66cd7bf…`): all six checks true; two
   incarnations (bootstrap → cooperative exit → KeepAlive re-birth); terminal
   journal row `canary_passed`; `controller_plist_digest` `db9f012b…` matched
   the pre-install dry-run prediction.
3. **Qualification reissue** (`2026-11-18-761fdf29-reissue.sh`): self-locating;
   bound daemon pid 58955 / epoch `418035e7` / evidence `a66cd7bf`; old record
   archived (`archived-qualification-v2-pre-761fdf29-20261118.json`); 6h TTL.
4. **Prepare** (socket RPC): first client attempt hit a 60s CLI timeout; the
   daemon-side operation completed cleanly (`busy` = mutex held by in-flight
   work, not a stuck row). Second attempt returned the full plan:
   - request `ecbc7d893cf44d3e93b92a6a90ac96ae`, asset `d9e36cd4…`
   - candidate checks: liveliness 200, readiness 200, models 238, completions
     2x200, invalid-key 401; mcp_parity `not_established` (documented gap)
   - primary identity pid 47263, birth 1794796523 (SC-006: unchanged)
   - 5-minute plan expiry
5. **Owner approval** (`2026-11-18-approve-restart-ecbc7d89.sh`): owner wrote
   `approvals/<rid>.json` (0600, exact approval_binding(plan) fields, expiry ≤
   plan deadline), then submitted `restart` — receipt `status: restarted`.
6. **Postcheck (this session)**:
   - NEW proxy pid **65498**, listening :4000, birth 2026-11-18 12:49:01 EST
   - controller epoch UNCHANGED `418035e7` (daemon untouched by the restart)
   - journal row terminal: phase=restarted, outcome=restarted
   - approval consumed (file gone, no `mutation.json` fence)
   - health liveliness 200, readiness 200, unauth /v1/models 401 (correct)

## Lessons folded into the ops skill

- A CLI-client timeout on `prepare` does NOT abort the daemon-side operation;
  `busy` afterwards means the mutex is still held by in-flight work — wait and
  re-check status instead of assuming a stuck row.
- Approval + restart belong in ONE owner paste alongside a live countdown, or
  the 5-minute plan window dies in transit.

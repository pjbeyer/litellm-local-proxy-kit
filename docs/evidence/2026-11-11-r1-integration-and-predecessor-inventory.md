# R1 integration + R9/T026 predecessor-state inventory — 2026-11-11

**Session:** implementation session (Hermes default profile), worktree
`litellm-local-proxy-shutdown`, branch `feat/controller-shutdown`.
**Authority:** `docs/handoffs/2026-11-09-implementation-handoff.md` +
`docs/restart-controller-reconciliation-spec.md` (sections 14–16), same repo
tree. Private working document — scrub local paths/IDs before any public
derivative.

## R1 — release-lane integration onto the canonical lineage (DONE)

Four draft-lane commits cherry-picked onto `d0691b2`, each its own atomic
commit, T009 dirty partials untouched throughout:

| Picked | Origin (draft lane) | Subject | Files |
|---|---|---|---|
| `a5b219d` | `8a0399c` | ci: add offline test suite gate (macOS+Linux) and publication hygiene scans | `.github/workflows/tests.yml` (+46) |
| `dff2ec3` | `27574b5` | chore: add MIT license | `LICENSE` (+21) |
| `892ea19` | `aeb21a5` | chore(deps): bump stable candidate to litellm 1.108.0 with validation note | `requirements-stable-candidate.txt`, `docs/publication-readiness.md` |
| `466fe7a` | `9a1c58a` | fix(preflight): read config from deployments/personal after shared-core restructure | `preflight.py` |

Preconditions verified before picking: `deployments/personal/proxy_config.yaml`
exists (8981a77 restructure is an ancestor of this lineage); preflight.py was
at the pre-fix root-level path. Content equivalence pick↔origin confirmed by
the independent reviewer (verdict below).

**Post-integration test run** (test venv
`flex-ai-litellm-local-proxy/.venv-tests`, Python 3.15.2; TMPDIR redirected to
the approved scratch root):

```
tests/test_restart_preflight.py tests/test_restart_controller.py
tests/test_controller_qualification.py tests/test_restart_runtime.py
→ 102 passed, 14 subtests passed (19.16s)
```

**Baseline discipline:** pre-change run of the plan's first-run trio was
85 passed / 14 subtests, with one transient failure
(`ControllerTest::test_socket_dispatch_and_bad_json`,
`Fault: controller_unavailable`) under full-suite load that passes in
isolation and passed on the immediate re-run — timing-sensitive socket test,
relevant input to T013 (wire/CLI drain) scope. No code change was made for it.

**Independent review (2026-11-11, read-only subagent):**
**PASS_WITH_MINOR_OBSERVATIONS** — all four commits FAITHFUL:

- a5b219d: byte-identical to 8a0399c; single-parent; all CI-referenced paths
  exist at HEAD; hygiene greps clean.
- dff2ec3: byte-identical to 27574b5; single file.
- 892ea19: byte-identical to aeb21a5; version claims internally consistent
  (1.108.0 pin matches the supersede note, pending tool-transport caveat
  stated in both files).
- 466fe7a: byte-identical to 9a1c58a; the only config path resolves to an
  existing file; no stale root-path usages remain.
- Dirty-file isolation confirmed: none of the five uncommitted files appear
  in any pick (`git log d0691b2..HEAD -- <files>` empty).

Observations (all non-blocking, faithful-to-original artifacts preserved):
1. tests.yml header comment names the kit worktree (cosmetic staleness if the
   branch ever renames; content equality to 8a0399c required, no action).
2. tests.yml push trigger is restricted to `main` — pushes of this feature
   branch do not trigger CI until PR/merge (intended gate behavior).
3. The validation note's "204 tests" is the kit-lane suite count; this lane
   runs 102 (accurate as written; reader beware).

**Not done in R1 (correctly):** no wholesale lane merge; no public-snapshot
copy over private source; `flexai-token` vs `cli-token` naming NOT normalized
(owner decision, packet decision 2).

## R1.2 — registry census (read-only)

- `core/restart_control/` actual modules: `__init__.py`, `engine.py`,
  `process_identity.py`, `recovery.py`, `runtime.py`,
  `runtime_identity.py`, `shutdown.py`, `wire.py`.
- `runtime_identity.py` source registry lists all of: engine.py, wire.py,
  runtime.py, runtime_identity.py, process_identity.py, shutdown.py,
  recovery.py — exact match with the actual module set.
- `docs/restart-controller.md` CT-006 kit-source registry lists the same
  restart_control set plus `core/restart_controller.py`,
  `core/install_restart_controller.py` and the proxy installer helper set.
- `proof_worker.py` (T010) and `canary.py` (T016) correctly absent from both
  registries — their registration belongs to their own source-introduction
  commits per the plan.
- Installer `MANAGED` set (proxy deployment closure) consistent with the
  installed-proxy paths list in CT-006.
- **Open (T024 scope):** installed-bundle byte-match vs registry is NOT
  asserted here — the installed bundle is a LEGACY generation (below) and its
  replacement is a separately gated SC-010 staged operation.

## R9/T026 — predecessor-state inventory (read-only, hash-pinned)

Observations 2026-11-11 17:23:51 EST, secret-free, no credentials resolved,
no service operation performed. The running controller job
(`ai.litellm.restart-controller`, PID 1199, runs=1, never exited) and the
running proxy (PID 1217, 127.0.0.1:4000) were observed read-only and are
PRESERVED — no bundle, plist, policy, or state file was modified.

### Predecessor state root `~/.local/state/litellm/rc/`

- `approvals/` — EMPTY (zero consumed approvals)
- `requests/` — EMPTY (zero pending/prepared requests)
- `snapshots/` — EMPTY
- `recovery/` — ABSENT (generation predates the recovery journal)
- `qualification.json` — ABSENT ⇒ restart authority correctly disabled
- `controller.lock` — 0 bytes, 0600, 2026-11-02 23:07
- `controller.sock` — socket, 0600, live (controller PID 1199)

**Inventory verdict: zero pending requests, zero consumed approvals, zero
snapshots, zero recovery records. No reconciliation debt exists in the
predecessor state.** Per-item disposition: none required.

### Legacy installed bundle `~/.local/share/litellm/restart-controller-aee69ab/`

Generation WITHOUT `shutdown.py`/`recovery.py` (pre-shutdown-contract;
confirmed absent). SHA-256 (first 16 hex) / size per file:

```
c6b8b836e22aeb22   1105  install.json
d09d8cdc291327fc   4735  install_restart_controller.py
136f2b100a705baa    173  restart_control/__init__.py
1553b6b938088c5a  19710  restart_control/engine.py
b0ccf3ef1c8bde1c  36071  restart_control/runtime.py
c271b47a25e47694  24640  restart_control/runtime_identity.py
8e948626f43238d6   2406  restart_control/process_identity.py
fd4fc795f9013577   5072  restart_control/wire.py
7328e3a2005b170b   2348  restart_controller.py
(+ cpython-311 pycache copies of the above)
```

### Policy and plist (non-secret fields only)

- Policy `~/.config/litellm-restart-controller/policy.json` SHA-256
  `e94204622209b504…a983e56` (full hash in session record). version 1;
  kit_root → READINESS worktree (STALE pointer, operator reconciliation
  required before any qualification v2); deployment_root → work repo;
  state_dir → the rc root above; controller_root → the legacy bundle;
  candidate_port 4401 (no listener — correct); models approved: 2 names.
  No credential-shaped values observed in the policy file.
- Controller plist `~/Library/LaunchAgents/ai.litellm.restart-controller.plist`
  SHA-256 `d4c9db703d8256a5…a98fee` (full hash in session record).
- Full-length hashes for policy/plist are recorded here only as truncated
  prefixes plus the session-scoped full values to keep this file compact;
  both full digests are pinned in the T026 owner packet.

### Live supervision snapshot (branch B confirmation)

- `ai.litellm.restart-controller`: state running, runs = 1, PID 1199,
  last exit code (never exited) — unchanged since the 2026-11-09 R0 record.
- Proxy `127.0.0.1:4000` LISTEN by PID 1217 (UID 502) — branch A unchanged.
- No listener on 4401 (candidate port unused — expected).

## Disposition and next gates

- R1 complete pending owner eyes; commits are local-only (no push authority —
  conservative profile; `bd dolt push`/`git push` require explicit approval).
- R2 (T009 completion) remains **BLOCKED** on owner disposition of
  `the yt5.1 refusal review` (preserved guard refusals + source-data signal-veto).
  Nothing downstream (R3–R7) was started.
- T026 owner packet can be assembled from this inventory: predecessor state is
  clean; the legacy bundle + stale kit_root pointer are the two reconciliation
  items for the operator before qualification v2 could ever be issued.
- G4–G7 remain unconsumed by this session.
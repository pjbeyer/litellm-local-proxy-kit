# Implementation Plan: Isolated LiteLLM rehearsal

> **For Hermes:** Use subagent-driven-development after the owner approves execution and the mandatory Spec Validate gate permits implementation.

**Branch**: `feat/isolated-proxy-rehearsal` | **Date**: 2026-11-26 | **Spec**: [004](spec.md)

## Summary

Rehearsal support has two tiers. The routine default is `make rehearse-check`: it verifies reviewed inputs and renders the proposed overlay with the kit's trusted renderer, reporting a fixed-vocabulary summary and an optional reviewed-baseline comparison — no guest, no candidate execution, no managed contact, and no extra hardware. The opt-in is `make rehearse`: a short-lived supervisor owns a disposable offline macOS guest whose worker uses the kit's existing renderer, exact runtime proof, and patch sentinel to test a proposed overlay and pinned patched build against local fixtures. Nothing enrolls or contacts the managed controller, modifies installed files, or promotes a result. The opt-in lane may run on an external drive or a separate macOS runner, but routine use never requires extra local hardware.

## Technical Context

- **Routine lane:** Use the trusted host Python and existing PyYAML installation offline. Admit only check manifests, reviewed overlay/pin bytes and an optional baseline overlay. The routine foundation owns shared regular-file admission; native integration reuses it without conflating manifest or receipt types. Parse and render in memory using the tracked template and fixed context. No candidate, live reads, downloads or VM ledger. Budget and typed outcome rules are in the [configuration-check contract](contracts/configuration-check.md).

- **Stack and platform:** Python 3.11+ host/worker, packaged PyYAML where renderer requires it; Apple Silicon macOS guest with compatible pinned candidate Python and already present dependencies. Apple Virtualization-backed offline boundary. An adapter, if needed, is confined to `rehearsal/vm/`.
- **Storage:** Durable private run ledger under the operator's XDG state root; one exclusive run scope; read-only staged inputs and disposable VM/cache volume; no host directory sharing. T002 records enforceable storage ceilings and T009 validates them on the selected backend. No raw logs, resolved production values or test key on disk.
- **Boundedness:** One operator-initiated run at a time; no automatic retries or downloads. Use a monotonic clock. The 300-second verification budget starts at invocation and ends when host result validation finishes. It includes admission, startup, probes, final identity revalidation and guest frame validation. The 180-second startup sub-budget covers launch through readiness. The 30-second probe sub-budget starts at readiness and covers the full remaining probe sequence; each probe, including readiness, has at most five seconds. Bound nested operations to the earliest applicable deadline. Teardown has its own 60 seconds from entry to `closing`, including durable settlement. If closure remains unproved or settlement is known not to have committed at the deadline, retain exclusivity and report `incomplete` when controlled output is possible. If a terminal settlement commit is itself indeterminate, retain exclusivity and return nonzero without claiming a terminal receipt. On verification expiry with proved absence of a reservation return `refused`; if reservation outcome is unknown, fence allocation and report no terminal result; after reservation enter `closing`, then return `failed` only on durably settled closure or use the unresolved-disposition rules above. Check budgets with a fixture clock and timed inert tests.
- **Testing:** Red-first pytest for command, manifest, receipts and state machine; fixture-backed isolated tests for ownership and deadline failures; an actual compatible macOS guest and different patched build for release acceptance. Do not use a generic process or simulated responses as live isolation evidence.

## Constitution Check

- **I — Public source, private deployments:** Publish only closed, sanitized results and reviewed artifacts. No host paths, raw responses, secrets or private tracker IDs in commits.
- **II — Preserve operator authority:** Use a dedicated worktree. Neither lane controls managed services or grants promotion authority. Prove native isolation before running the candidate; refuse unknown state. Samples of managed state are not continuous integrity proof.
- **III — Specify consequential changes and verify offline:** Require revision-bound validation and independent review. Red-first fixtures cover changed inputs, missing boundaries, identity loss and incomplete cleanup. Routine results do not prove runtime behavior; native acceptance requires evidence from the actual backend and candidate.
- **IV — Keep maintenance simple:** Reuse the renderer, runtime proof and sentinel without changing managed behavior. Keep runbooks aligned with their commands, and validate each mode's versioned manifest and result separately.

## Design and boundaries

See [research](research.md), [CLI and runner contract](contracts/rehearsal.md), [data model](data-model.md), and [quickstart](quickstart.md). The host reads a bounded closed manifest and stages reviewed immutable opaque bytes from explicit roots before guest admission, and does not interpret proposal content; only the admitted guest does. It grants no guest access to the host's managed state, and interprets only a bounded typed result from the guest. The host independently attests teardown. Identity- or cleanup-uncertain outcomes cannot pass, and a later invocation stops at any unfinished run record. The v1 fixture set is health/auth plus an approved local stub route; apply the closed proposal-to-fixture mapping before launch and give no coverage credit for substitutions or exclusions.

| Requirement | Existing owner / reuse | New work |
| --- | --- | --- |
| FR-001 | Root Makefile conventions | Explicit manifest and reviewed digests; separate command |
| FR-002 | `core/render_config.py`, `core/check_pin.py`, `core/check_patch_sentinel.py` and native proof | Guest adapter and exact launch binding |
| FR-003 | Existing pilot's separation rules | VM/device policy and trusted offline admission probes |
| FR-004 | Existing health/auth semantics | Per-probe guest listener and process continuity |
| FR-005 | Existing concepts of ownership, not controller state | Independent supervisor and durable run record |
| FR-006 | None reusable as a wire schema | Closed sanitized result and host attestation |
| FR-007 | Spec 003 owns the live controller | No live imports or controller dispatch; regression tests |
| FR-008 | Trusted renderer and tracked template | Data-only parsing, fixed render context, bounded advisory summary and optional baseline |

## Project Structure

- `Makefile` only routes the new action; existing `owner_maintenance.py` keeps existing behavior.
- `rehearsal/check.py` owns the routine entry point. `rehearsal/cli.py`, `rehearsal/supervisor.py`, `rehearsal/worker.py` own invocation, host lifetime, and guest checks, respectively. `rehearsal/vm/` holds the single backend adapter if the target-host research gate requires it.
- `rehearsal/schemas/` holds manifest and result schemas. `tests/test_rehearsal_*.py` and `tests/fixtures/rehearsal/` contain contract and disposable end-to-end fixtures.
- `docs/rehearsal-configuration-check.md` is the routine runbook; link it directly from the README. `docs/isolated-rehearsal.md` is the separate native runbook. Do not duplicate Spec 003 or update the shared feature pointer.

## Gates and evidence

- **Both lanes:** Current spec and tasks comprehension validation and applicable independent review must bind the current hashes. The closeout record must also bind the SHA-256 of the sorted normative packet manifest (spec, plan, tasks, data model, research, quickstart, and both contracts) to those records and to separate owner authorization; a material change to any normative artifact invalidates the corresponding approval until rebound. Before either lane starts, record separate owner comprehension of the current spec and tasks via Spec Validate, and obtain independent document review covering the shared contract and the intended lane with no unresolved blocking findings. Record the reviewer, scope, disposition and packet digest. Earlier approvals and waivers do not transfer. Use the standard extension from the separately maintained scaffold; do not commit local state or change shared integration hooks. Obtain explicit owner authorization before implementing either lane. Owner comprehension and execution authorization are open until recorded for the current revision.
- **Routine acceptance:** Run verification in T001, T012 and T013 with no guest/candidate bundle. A successful check only proves reviewed data was checked; it is never native acceptance. T003/T007 verify SC-006 separately. If a release claims both lanes, require routine and native acceptance plus SC-006.
- **Native feasibility (T002):** Blocked on a qualified environment. Prove device policy, guest denials, result transport and fail-stop lifetime with inert inputs before candidate-bearing work. No automatic download, relocation or fallback.
- **Native release:** T009 validates the actual boundary; T010 separately proves native behavior and same-host coexistence with a real different build, including the environment-only identity pair and independent observer comparisons. A separately qualified runner may prove native behavior, but its result does not satisfy SC-002 for another Mac. Neither a static review nor the routine check substitutes for these gates.

# Implementation Plan: Isolated LiteLLM rehearsal

> **For Hermes:** Use subagent-driven-development after the owner approves execution and the mandatory Spec Validate gate permits implementation.

**Branch**: `feat/isolated-proxy-rehearsal` | **Date**: 2026-11-26 | **Spec**: [004](spec.md)

## Summary

Add `make rehearse` as a separate host entry point. A short-lived supervisor owns a disposable offline macOS guest. A guest worker uses the kit's existing renderer, exact runtime proof, and patch sentinel to test a proposed overlay and pinned patched build against local fixtures. Nothing enrolls or contacts the managed controller, modifies installed files, or promotes a result.

## Technical Context

- **Stack and platform:** Python 3.11+ host/worker, packaged PyYAML where renderer requires it; Apple Silicon macOS guest with compatible pinned candidate Python and already present dependencies. Apple Virtualization-backed offline boundary. An adapter, if needed, is confined to `rehearsal/vm/`.
- **Storage:** Durable private run ledger under the operator's XDG state root; one exclusive run scope; read-only staged inputs and disposable VM/cache volume; no host directory sharing. No raw logs, resolved production values or test key on disk.
- **Boundedness:** One operator-initiated run at a time. Supervisor owns absolute verification, VM/worker, per-probe and teardown deadlines; no automatic retries, no daemon enablement, no download or provider lane. Start with a 300-second verification, 180-second readiness, five-second HTTP and 60-second teardown budget, reducing a nested operation to the remaining parent deadline. Validate bounds with a fixture clock and actual timed negative runs.
- **Testing:** Red-first pytest for command, manifest, receipts and state machine; fixture-backed isolated tests for ownership and deadline failures; an actual compatible macOS guest and different patched build for release acceptance. Do not use a generic process or simulated responses as live isolation evidence.

## Constitution Check

- **I–II (fail closed, honest claims):** Verify the exact VM device policy before boot, prove boundary against disposable sentinels before candidate execution, refuse unknown state. Isolation comes from the verified hypervisor boundary, not the kit's lifecycle guard. Samples of managed state are not continuous integrity proof.
- **III (test-first):** Red/green tests exercise missing boundaries, changed inputs, identity loss and incomplete cleanup. Mutation checks remove the containment admission and successful-teardown conditions in isolated fixtures and must cause the specified tests to fail.
- **IV (publication hygiene):** Closed sanitized result; no host paths, raw responses, secrets or tracker IDs in commits. Plan, tests and docs are under the dedicated worktree.
- **V–VII (reuse, schemas and parity):** Reuse renderer, runtime proof and sentinel; preserve `core/`, guard and controller behavior. Version and validate both public manifest and receipt on every load; require proof about the exact VM/image/build and the runtime that actually answers.

## Design and boundaries

See [research](research.md), [CLI and runner contract](contracts/rehearsal.md), [data model](data-model.md), and [quickstart](quickstart.md). The host stages a closed manifest and reviewed immutable bytes, grants no guest access to the host's managed state, and interprets only a bounded typed result from the guest. The host independently attests teardown. Identity- or cleanup-uncertain outcomes cannot pass, and a later invocation stops at any unfinished run record. The v1 fixture set is health/auth plus an approved local stub route; unrelated production features are unexercised, not automatically stripped and credited.

| Requirement | Existing owner / reuse | New work |
| --- | --- | --- |
| FR-001 | Root Makefile conventions | Explicit manifest and reviewed digests; separate command |
| FR-002 | `core/render_config.py`, `core/check_pin.py`, `core/check_patch_sentinel.py` and native proof | Guest adapter and exact launch binding |
| FR-003 | Existing pilot's separation rules | VM/device policy and trusted offline admission probes |
| FR-004 | Existing health/auth semantics | Per-probe guest listener and process continuity |
| FR-005 | Existing concepts of ownership, not controller state | Independent supervisor and durable run record |
| FR-006 | None reusable as a wire schema | Closed sanitized result and host attestation |
| FR-007 | Spec 003 owns the live controller | No live imports or controller dispatch; regression tests |

## Project Structure

- `Makefile` only routes the new action; existing `owner_maintenance.py` keeps existing behavior.
- `rehearsal/cli.py`, `rehearsal/supervisor.py`, `rehearsal/worker.py` own invocation, host lifetime, and guest checks, respectively. `rehearsal/vm/` holds the single backend adapter if the target-host research gate requires it.
- `rehearsal/schemas/` holds manifest and result schemas. `tests/test_rehearsal_*.py` and `tests/fixtures/rehearsal/` contain contract and disposable end-to-end fixtures.
- `docs/isolated-rehearsal.md` is the operator runbook; the README links it. Do not duplicate Spec 003 or update the shared feature pointer.

## Gates and evidence

- Red-team gate: findings resolved; independent amended-spec re-review approved. Current operator answers received. **Spec Validate execution gate: Blocked.** The task-local receipt write was authorized but refused by the lifecycle guard; the gate still reports that the spec has not been validated. Before implementation, record the spec result through the supported route, satisfy the required peer review, validate the current tasks separately, and verify the implementation gate. This plan does not substitute for the receipt or authorize execution.
- Plan/tasks security review: independent reviews found gaps in guest admission, input/receipt binding and ownership sequencing. See [dispositions](security-review.md); these are design corrections, not proof of the backend. Owner plan review and selection of the execution method remain separate from spec comprehension.
- VM feasibility: **Open**, owner/implementer. Prove no NIC/host channels, constrained result transport and fail-stop lifetime on the target Mac with inert inputs before any candidate import. If the selected backend cannot prove these, do not launch or advertise a working candidate lane.
- Distinct real patched build acceptance: **Open**, operator plus independent read-only observer. No live acceptance run while controller reconciliation makes attribution ambiguous. Existing maintenance regressions must pass independently.

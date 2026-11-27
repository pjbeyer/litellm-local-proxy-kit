# Implementation Plan: Restart controller operations

**Feature**: `003-restart-controller` · **Date**: 2026-11-25 · [Spec](spec.md)

## Summary

Reconcile the existing controller under `specs/`, consolidate recurring operations around the existing dispatcher, and plan a narrower read-only on-disk diagnostic. Do not modify the production controller or guard inventory.

## Technical context

Python 3, standard library and Make for the owner dispatcher; existing pinned Python/PyYAML for drift; macOS for deployed launchd; disposable policy and fixtures for testing. Scope excludes live state and approval writes. Dependencies and alternatives are in [research](research.md).

## Constitution check

Keep private identifiers and deployment data out of public artifacts (I). Preserve operator authority and fail-closed behavior without changing installed state (II). Bind validation and review to the current artifacts, and verify success and refusals offline (III). Reuse existing interfaces; do not change generation schemas or receipts (IV).

## Project structure and traceability

| Requirement | Owner | Disposition and evidence |
|---|---|---|
| FR-001 | `AGENTS.md`, `docs/restart-controller-operations.md`, `Makefile` | Consolidate navigation and document one-command authority boundaries; help and fixture test |
| FR-002 | `owner_maintenance.py`, `core/installer.sh` | Inherited; test explicit policy dispatch, stage verification, fixed client and no activation |
| FR-003 | `owner_maintenance.py` (proposed) | New convenience check; existing install verifier is distinct and remains supported. Await approval before changing code |
| FR-004 | Controller contract, `engine.py`, `wire.py`, `runtime.py` | Inherited; controller, qualification and interruption fixtures |
| FR-005 | Shutdown and reconciliation contracts, `runtime.py`, `recovery.py`, `startup.py` | Inherited; shutdown, canary and recovery fixtures |
| FR-006 | This feature, historical plans and `docs/evidence/` | Codify and link; Spec Kit artifact and coverage checks |

See [data model](data-model.md), [contracts](contracts/), [quickstart](quickstart.md) and [tasks](tasks.md).

## On-disk diagnostic design (T004)

Add one read-only Make target backed by `owner_maintenance.py`. Read the supplied
installed policy once with the existing policy reader. Use that parsed object
both for routing and the canonical policy digest. Use its explicit `kit_root`
and `controller_root`, never the caller’s checkout as a fallback. Do not invoke
the installer or import code from the policy-bound source or installed bundle.

Compare `controller_root/install.json` to the version 1 receipt emitted by
`core/install_restart_controller.py`: integer `version`, `policy_digest` and
`sources` covering exactly `CONTROLLER_INPUTS`. Reuse the byte/mode checks from
`runtime_identity.py`; compare the policy-bound kit entries and the corresponding
installed files (without the `core/` prefix). A receipt is comparison data, not
proof of provenance or permission. Refuse missing, unreadable, invalid, non-regular,
or changed inputs rather than treating a partial comparison as a match.

Report **on-disk controller binding only**. Exit zero for a complete match and
nonzero otherwise. Distinguish mismatch, input error and invalid receipt in the
result without printing raw contents, deployment paths or tracebacks. The target
accepts no approval input, adds no dependency and performs no writes (including
logs or temporary files), service calls or subprocesses. The existing install
verifier remains the supported bundle/plist parity check; neither command grants
live qualification or approval.

## Gates and complexity

No new service, dependency or runtime schema.

| Gate | Disposition |
|---|---|
| Pre-plan adversarial review | [Recorded for the draft](red-team-findings-2026-11-25.md); findings addressed. Not an approval of the current candidate. |
| Shared Spec Kit scaffold | Use the checked-in Hermes-first scaffold and [kit extension workflow](../../docs/project-tooling.md#install-hermes-commands-separately). Do not copy a bootstrap or another feature’s approval state. |
| Spec Validate comprehension and applicable peer review | **Open.** Validate the current spec and task bytes with the operator, complete applicable peer review, and run the checked-in gate before T004. A structure pass is not approval. |
| Plan/tasks security review | **Open.** Review the current design and acceptance criteria before starting T004. |
| Diagnostic verification | Pending T004. Run the offline acceptance fixtures before marking T004 complete. |
| Live activation, qualification and approval | Out of scope. Historical receipts and offline results confer no live authority. |

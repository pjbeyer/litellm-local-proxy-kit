# Implementation Plan: Restart controller operations

**Feature**: `003-restart-controller` · **Date**: 2026-11-25 · [Spec](spec.md)

## Summary

Reconcile the existing controller under `specs/`, consolidate recurring operations around the existing dispatcher, and plan a narrower read-only on-disk diagnostic. Do not modify the production controller or guard inventory.

## Technical context

Python 3, standard library and Make for the owner dispatcher; existing pinned Python/PyYAML for drift; macOS for deployed launchd; disposable policy and fixtures for testing. Scope excludes live state and approval writes. Dependencies and alternatives are in [research](research.md).

## Constitution check

Preserve fail-closed behavior and bounded same-user protection (I–II). Fixture tests and independent review are required for behavioral change (III); strip private identifiers and machine paths (IV). Reuse existing interfaces (V); named generation schemas and receipts do not change (VI–VII). No installation as a side effect, bypass, or live state change.

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

## Gates and complexity

No new service, dependency or runtime schema.

| Gate | Disposition |
|---|---|
| Pre-plan adversarial review | [Recorded for the draft](red-team-findings-2026-11-25.md); findings addressed. Not an approval of the current candidate. |
| Shared Spec Kit scaffold | **Open.** Use the repo-wide recovery when it lands. This feature does not publish its local bootstrap, the older validation implementation, or another feature’s approval state. |
| Spec Validate comprehension and applicable peer review | **Open.** The operator must validate the current spec bytes; rerun the installed gate before T004. A structure pass is not approval. |
| Plan/tasks security review and proposed implementation verification | **Open.** Run the applicable review and test the proposed diagnostic before marking T004 complete. |
| Live activation, qualification and approval | Out of scope. Historical receipts and offline results confer no live authority. |

# Tasks: isolated LiteLLM rehearsal

## Setup and foundations

- [ ] T001 Enumerate allowed virtual devices, mount roles, and reviewed image identity in `rehearsal/vm/`; prove a disposable guest on the target Apple Silicon Mac with no network, no host sharing or credential bridges, a guest-only endpoint allowlist inherited by helpers, and safe lifetime after worker or CLI loss. Use an unapproved guest-local sentinel throughout all candidate stages, prove report-channel isolation, and record supported versions and limits in `specs/004-isolated-proxy-rehearsal/research.md`. Stop this lane if the evidence is unavailable (FR-003, SC-003).
- [ ] T002 [P] Add red-first manifest and receipt schema tests in `tests/test_rehearsal_cli.py`; cover known-good, duplicate keys, unexpected or missing data, a manifest replaced together with its internal hashes, invalid input roots, wrong run and mismatched proposal digests in a schema-valid frame, repeated or oversize frame, and typed incomplete result. Then add closed schemas in `rehearsal/schemas/` (FR-001, FR-006).
- [ ] T003 [P] Add red-first lifetime tests in `tests/test_rehearsal_supervisor.py`; cover exclusive first record, unresolved-run refusal before allocation, an occupied endpoint, CLI, worker and supervisor loss, loss before handle return and during finalization, bounded failure and owned-only cleanup. Add comparison sentinels for managed paths and sockets to `tests/fixtures/rehearsal/` (FR-005, SC-003).
- [ ] T004 Implement the exact reviewed backend admission and supervisor ownership in `rehearsal/supervisor.py` and `rehearsal/vm/`; reserve the durable namespace before allocation and bind objects at creation, require supervised pre-candidate denial evidence, and retain an unresolved record when the hypervisor cannot prove closure. Require all expected admitted identities, fixture values and checks in the guest result. Run the backend/fixture tests red then green (depends on T001, T002, T003).

## US1 — Rehearse a proposal without touching the managed proxy

- [ ] T005 [US1] Write red-first startup and per-probe binding tests in `tests/test_rehearsal_worker.py`, covering provenance-before-sentinel, a valid bundle paired with different prepared files, sealed config/runtime identity, an env-only `fixture_trace` identity change, wrong listener and listener replacement, supported interrupts, keyless denial and test-key acceptance. Implement the isolated renderer, pinned proof and request adapter in `rehearsal/worker.py`; repeat runtime/effective identity proof before success (FR-002, FR-004, SC-001, SC-003) (depends on T004).
- [ ] T006 [US1] Implement `make rehearse` in `Makefile` and `rehearsal/cli.py`, use immutable reviewed input bytes and no `POLICY` fallback, and integrate supervisor/worker. Run `tests/test_rehearsal_cli.py` and `tests/test_owner_maintenance.py`; verify other targets and closed controller inventory unchanged (FR-007, SC-004) (depends on T002, T004, T005).

## US2 — Keep rehearsal evidence separate from approval

- [ ] T007 [US2] Complete `docs/isolated-rehearsal.md`, link from `README.md`, and assert no approval, qualification, managed controller calls, or promotion in `tests/test_rehearsal_cli.py`. Describe refused, failed, incomplete and passed outcomes; distinguish fixture route coverage from excluded provider or production feature coverage (depends on T006).

## Release acceptance and review

- [ ] T008 Run the disposable denial and teardown matrix, including isolation-owner loss after boot on the actual backend, next-run refusal before allocation, and guest-local sentinel denial, plus mutation controls for missing containment and premature success. Use `tests/test_rehearsal_supervisor.py`, `tests/test_rehearsal_worker.py` and the isolated fixture harness; preserve sanitized evidence and require the full offline suite and diff hygiene (depends on T007).
- [ ] T009 Run the different-real-build quickstart on the proven backend only with the exact operator-reviewed proposal. Capture independent before/during/after managed identities and state per `specs/004-isolated-proxy-rehearsal/quickstart.md`, no active reconciliation interference, and a sanitized teardown result. A stub or simulated proof cannot close this task (SC-002) (depends on T008).
- [ ] T010 Run independent Codex branch review, security branch review, task verification, cleanup, and reconciliation. Verify `Makefile` and runbook match the tested CLI and receipts; publish only reviewed and verified artifacts. Do not qualify or restart the managed proxy (depends on T009).

## Execution Wave DAG

- W1: T001, T002, T003
- W2: T004
- W3: T005
- W4: T006
- W5: T007
- W6: T008
- W7: T009
- W8: T010

The operator-approved plan, Spec Validate permission, and proven backend are mandatory gates; a passing fixture alone does not allow the different-build run.

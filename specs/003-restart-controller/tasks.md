# Tasks: Restart controller operations

This is the reconciliation plan, not a reenactment of the original delivery. Beads owns backlog/status.

- [x] T001 [US4] Rehome the controller contracts and historical plans, document precedence and ownership, and add Spec Kit navigation (FR-006, SC-004).
- [x] T002 [US1] Consolidate routine commands and their authority boundaries in the runbook; keep Make and the dispatcher as the only routine entry point (depends on T001) (FR-001, FR-002, SC-001).
- [x] T003 [US3] Trace preparation, approval, qualification and recovery to the controller and offline tests; preserve historical evidence without reasserting live readiness (depends on T001) (FR-004, FR-005, SC-003).
- [ ] T004 [US2] After the revision-bound Spec Validate gate and plan/tasks security review clear, add the [read-only disk check](plan.md#on-disk-diagnostic-design-t004) to `owner_maintenance.py` and expose it through one Make target. Reuse the policy reader, receipt schema, controller-input registry, and byte/mode checks. Keep the existing install verifier for bundle/plist parity (depends on T002) (FR-003, SC-002).

### T004 offline acceptance

- A complete match exits zero. Changed policy, changed kit or installed bytes or modes, missing receipt, malformed receipt, and unreadable inputs all exit nonzero. Include non-regular inputs and a receipt with incomplete coverage. Distinguish mismatch, input error, and invalid receipt in the result.
- Every result labels the scope as on-disk controller binding only; it never reports live qualification or grants approval. Fixtures assert no file writes, service contact, or subprocesses on success and refusal, and no approval argument is accepted. Use disposable fixtures, not the installed deployment.

## Execution Wave DAG

- Wave 1: T001
- Wave 2: T002, T003
- Wave 3: T004 (requires a passing Spec Validate gate for current spec/tasks and a completed plan/tasks security review before work starts)

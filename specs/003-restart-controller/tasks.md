# Tasks: Restart controller operations

This is the reconciliation plan, not a reenactment of the original delivery. Beads owns backlog/status.

- [x] T001 [US4] Rehome the controller contracts and historical plans, document precedence and ownership, and add Spec Kit navigation (FR-006, SC-004).
- [x] T002 [US1] Consolidate routine commands and their authority boundaries in the runbook; keep Make and the dispatcher as the only routine entry point (depends on T001) (FR-001, FR-002, SC-001).
- [x] T003 [US3] Trace preparation, approval, qualification and recovery to the controller and offline tests; preserve historical evidence without reasserting live readiness (depends on T001) (FR-004, FR-005, SC-003).
- [ ] T004 [US2] After owner Spec Validate and required review, implement and fixture-test the proposed no-subprocess disk diagnostic. Until then use the documented owner install verifier for bundle parity (depends on T002) (FR-003, SC-002).

## Execution Wave DAG

- Wave 1: T001
- Wave 2: T002, T003
- Wave 3: T004 (owner comprehension gate and review)

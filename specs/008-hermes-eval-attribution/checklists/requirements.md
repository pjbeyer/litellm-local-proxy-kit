# Specification Quality Checklist: Hermes Exact Quality-Eval Attribution

**Purpose**: Check requirements before planning. Checkmarks do not qualify a live transport or authorize scoring.

**Created**: 2026-11-24

**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] The spec leads with the evaluator's problem and defines independently testable user stories.
- [x] All mandatory sections are present; named interfaces are compatibility boundaries, not implementation prescriptions.

## Requirement Completeness

- [x] Exact call ID, plugin token, trusted provenance and durable user/assistant identities are required; no heuristic substitutes. (FR-005, FR-006)
- [x] Both endpoints, stream-mode evidence, local-only tagging and the unsupported-path action are defined. (FR-002, FR-003, FR-004, FR-011)
- [x] The complete cohort, positive exclusions, replay, collisions, expiry, lag, the coherent as-of verdict point and fail-closed result are defined. (FR-005, FR-007, FR-008, FR-009)
- [x] The plugin is opt-in; inference and the tier census remain independent; qualification cannot call judges or mutate ledgers. (FR-001, FR-010)
- [x] Success criteria cover concurrent attribution, negative cases, non-egress and rollback. (SC-001, SC-002, SC-003, SC-004)
- [x] Package validation, operator documentation and sanitized evidence are explicit. (FR-001, FR-012)

## Readiness Gates

- [ ] Maintainer has reviewed the draft and approved proceeding to planning.
- [ ] Public Hermes interfaces, independent cohort intake and trusted SpendLog completeness have been demonstrated before implementation.
- [ ] Each required endpoint has an authorized canary receipt; each advertised mode has its own evidence. No live support or scoring is implied by the draft.

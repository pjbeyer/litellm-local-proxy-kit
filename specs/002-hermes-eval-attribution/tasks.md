# Tasks: Hermes Exact Quality-Eval Attribution

This is the dependency-ordered execution plan, not the project backlog or authorization to start. Beads tracks status. No feature files named below exist as part of this plan. G1–G3 in the [plan](plan.md) must clear before writing feature code or fixtures; if supported interfaces cannot meet the bar, return to spec review. This plan does not authorize a live probe or scoring.

## Phase 1: Entry gates

- [ ] T001 [P] Obtain and record maintainer design review against the kit constitution, the applicable extension reviews, and operator comprehension against `specs/002-hermes-eval-attribution/spec.md` and `specs/002-hermes-eval-attribution/plan.md` (FR-011, FR-012).
- [ ] T002 [P] Investigate each mode on both required endpoints; demonstrate supported final dispatch, non-egress, accepted response capture, exact physical row identities and same-UID revision or immutability evidence. Record build/configuration and evidence or the blocking gap in `specs/002-hermes-eval-attribution/research.md` (FR-002, FR-004, FR-005, FR-006, FR-011, SC-003).
- [ ] T003 Validate token-independent roster/outcome identity, authenticated client/route and exchange/billing census, complete reads, coherent verdict point and bounded lag on the same candidate path; record evidence or stop in `specs/002-hermes-eval-attribution/research.md` (FR-008, FR-009, SC-002) (depends on T002)

## Phase 2: Foundational red tests and package

- [ ] T004 [P] Pin wire and binding expectations and establish red in `tests/test_quality_attribution_binding.py`: overlap, reversed arrival, retries, tool follow-ups, control client, alternate alias, interruption/supersession, malformed or prepopulated metadata, duplicate/replay IDs, two durable rows, and transformed or compacted output (FR-002, FR-003, FR-004, FR-005, FR-006, SC-001) (depends on T001, T002, T003)
- [ ] T005 [P] Pin verdict expectations and establish red in `tests/test_quality_attribution_qualification.py`: missing roster outcome or billed retry row, equal wrong identities, duplicate candidates, incomplete pages, trusted tier/exclusions, empty and REASONING-only cohorts, row revision changes and ABA, pre-cut conflict/revocation/expiry, and lag up to the absolute deadline. Assert zero judge/ledger calls and no input-store mutation (FR-007, FR-008, FR-009, FR-010, SC-002, SC-004) (depends on T001, T002, T003)
- [ ] T006 [P] Pin package, disabled and non-egress behavior and establish red in `tests/test_quality_attribution_lifecycle.py`: opt-in, mode/config drift, alternate routes, redirect/fallback/provider sinks, inference/control parity, failure and revocation across disable/re-enable (FR-001, FR-002, FR-004, FR-010, FR-011, SC-003, SC-004) (depends on T001, T002, T003)
- [ ] T007 Package a separate default-off integration, with only the supported interfaces and exact compatibility admission configured, in `integrations/hermes/quality-attribution/plugin.yaml` and `integrations/hermes/quality-attribution/__init__.py` (FR-001, FR-011) (depends on T004, T005, T006)

## Phase 3: US1 — Attribute the answer that was kept (P1)

**Independent test:** The binding fixture yields exactly two intended pairs and zero joins to controls or discarded attempts. An allowlisted alternate attributes without a router tier.

- [ ] T008 [US1] Implement scope and final-dispatch admission, one opaque metadata field with conflict refusal, and supported accepted response observation in `integrations/hermes/quality-attribution/transport.py` (FR-002, FR-003, FR-004, FR-005, SC-001) (depends on T007)
- [ ] T009 [US1] Verify exact wire and terminal lineage, both physical transcript rows and durable revision evidence, and persist only minimal private versioned binding state. Establish atomic conflict precedence, idempotency, read-side expiry and pruning without replay refresh in `integrations/hermes/quality-attribution/bindings.py`; turn the binding fixture green (FR-005, FR-006, FR-007, SC-001) (depends on T008)

## Phase 4: US2 — Qualify without scoring (P1)

**Independent test:** Run qualification against fixture evidence without registering the plugin or loading judge/ledger interfaces; expect complete tier totals or exit 2 with no successful verdict.

- [ ] T010 [US2] Implement independent roster/outcome and billing reconciliation, a complete scoped read, evidence-backed exclusions, and the exact bidirectional join and tier accounting in `integrations/hermes/quality-attribution/qualification.py` (FR-005, FR-008, SC-001, SC-002) (depends on T009)
- [ ] T011 [US2] Enforce the common validation point, once-only positive-lag retry, complete verdict/exit contract and sanitized reason totals in `integrations/hermes/quality-attribution/qualification.py` and turn the qualification fixture green (FR-006, FR-007, FR-009, FR-010, SC-002, SC-004) (depends on T010)

## Phase 5: US3 — Opt in without changing inference (P2)

**Independent test:** Compare disabled, active and faulted runs through local/control/provider/redirect sinks. Outside local ingress there is no added tag, and disablement revokes old evidence without interrupting inference or the tier census.

- [ ] T012 [US3] Wire separate tagging and read-only qualification controls, generation capture and revocation across dispatch, binding and verdict, and unchanged inference on failures in `integrations/hermes/quality-attribution/__init__.py`, `integrations/hermes/quality-attribution/transport.py`, `integrations/hermes/quality-attribution/bindings.py` and `integrations/hermes/quality-attribution/qualification.py`; turn the lifecycle fixture green and re-run binding and qualification fixtures (FR-001, FR-002, FR-004, FR-007, FR-010, FR-011, SC-003, SC-004) (depends on T011)

## Phase 6: Packaging, qualification and handoff

- [ ] T013 Document install, validation, compatibility, separate controls, diagnostics, state retention and rollback including residual SpendLog tags in `integrations/hermes/quality-attribution/README.md`; run package validation, offline tests and independent review (FR-001, FR-010, FR-012, SC-004) (depends on T012)
- [ ] T014 With separate operator authorization, run bounded installed-plugin canaries using `specs/002-hermes-eval-attribution/quickstart.md`; record sanitized receipts for one COMPLEX success on each endpoint, each advertised mode, and non-egress and rollback controls. Leave unsupported modes disabled and scoring unavailable (FR-004, FR-010, FR-011, FR-012, SC-003, SC-004) (depends on T013)

## Execution Wave DAG

| Wave | Ready tasks |
| --- | --- |
| 1 | T001, T002 |
| 2 | T003 |
| 3 | T004, T005, T006 |
| 4 | T007 |
| 5 | T008 |
| 6 | T009 |
| 7 | T010 |
| 8 | T011 |
| 9 | T012 |
| 10 | T013 |
| 11 | T014 |

## Delivery strategy

Review and feasibility investigation can proceed independently; use a single owner for writing the shared research record in sequence. After entry clearance, the three red suites can be prepared in parallel in separate files. US1 supplies the exact binding primitive, US2 supplies the read-only verdict, and US3 proves controls and non-interference across the full path. Offline US1 is the first incremental proof, not a supported release. Ship no V1 mode without the complete contract and the two-endpoint qualification gate.

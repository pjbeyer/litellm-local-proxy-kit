# Specification Analysis Report

**Disposition:** Planning artifacts are ready for maintainer review. Implementation entry is blocked. No transport is qualified.

The original draft review covered the [specification](spec.md), its source project's proposed constitution, the [plan](plan.md), [research](research.md), [data model](data-model.md), [contract](contracts/attribution.md), [validation guide](quickstart.md), and [task order](tasks.md). The specification SHA-256 recorded by that review was `495aa601e5ca43b9f3c638989ecc91e1b74bc6e018eb6ed2a0e2913defc7ec70`. The [kit constitution](../../.specify/memory/constitution.md) governs the import; it is not the source project's proposed text.

**Import gate:** `check-gate.sh 002-hermes-eval-attribution` exited 1: `Spec has not been validated`. The recovered tooling and this historical gate result do not establish that the required hooks run in a fresh checkout. Verify the reviewed extension setup before using it for future phases. The draft review below does not satisfy the registered Red Team, security review, or implementation gates.

## Findings and dispositions

| ID | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| A1 | High — feasibility | The inspected Hermes hooks do not demonstrate supported capture of the accepted wire response joined to both durable, current transcript rows. A stable UID alone is insufficient: the pinned Hermes source includes an in-place user-row update. | G2 / T002. Investigate all four endpoint/mode combinations and prove at least one mode on each required endpoint. If no supported seam meets the bar, return to spec review. Do not substitute private interception or heuristic matching. |
| A2 | High — feasibility | A complete, independent exchange-to-SpendLog census and a coherent validation point across the evidence stores are required but not demonstrated. The candidate design in research is not a supported read contract. | G3 / T003. Require evidence for provenance, census and read completeness, row revision currency, and a coherent validation point. Incomplete or unprovable evidence remains a stall, never a pass or `no samples`. |
| A3 | Gate — approval | The imported design has not been approved against the kit constitution. | G1 / T001. Leave approval and reviewer-owned checklist entries open. No operator comprehension result or live authorization is recorded. |
| A4 | Tooling (source draft) | The proxy lifecycle guard refused the prerequisite invocation in the source worktree, which did not have registered extension hooks. | That invocation was not retried or counted as a pass. The checks below refer to the draft review; the import gate above refers to the kit worktree. |

## Coverage and sequencing

| Scope | Tasks with evidence or implementation coverage |
| --- | --- |
| Package, admission and wire behavior: FR-001, FR-002, FR-003, FR-004 | T002, T004, T006, T007, T008, T012, T013, T014 |
| Exact exchange and transcript lineage: FR-005, FR-006, FR-007 | T002, T004, T005, T008, T009, T010, T011, T012 |
| Population, verdict and separation: FR-008, FR-009, FR-010 | T003, T005, T006, T010, T011, T012, T013, T014 |
| Compatibility and operator evidence: FR-011, FR-012 | T001, T002, T006, T007, T012, T013, T014 |
| Accepted-pair control: SC-001 | T004, T008, T009, T010 |
| Fail-closed read-only verdict: SC-002 | T003, T005, T010, T011 |
| Endpoint and non-egress evidence: SC-003 | T002, T006, T012, T014 |
| Disable, fault and read-only controls: SC-004 | T005, T006, T011, T012, T013, T014 |

The review found no unmapped task or uncovered requirement, no plan-versus-spec scope conflict, and no requested exception to the proposed constitution. Gate clearance precedes red-first fixtures; installed-plugin canaries follow implementation and require separate authorization. Scoring is not part of this feature.

**Executed checks:** `verify_spec_md.py` passed (12 functional requirements and 4 success criteria); `verify_tasks_md.py` passed (14 tasks, 16 of 16 IDs covered). Structural checks confirmed 11 dependency-consistent waves, local links and anchors, scaffold manifest parity, and no feature placeholders. The focused hygiene scan passed for the planning artifacts and bundled scaffold. These are document checks, not runtime validation.

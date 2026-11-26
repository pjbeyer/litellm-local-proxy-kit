# Consistency analysis: restart controller

Scope: retrospective documentation and existing dispatcher. This is not platform qualification or the operator’s comprehension record.

| Requirement | Task | Acceptance surface |
|---|---|---|
| FR-001, FR-002; SC-001 | T002 | Agent entry point, command table, `make help`, dispatcher fixtures |
| FR-003; SC-002 | T004 | Proposed disk-only check. No implementation claim; owner and review gates remain open. |
| FR-004, FR-005; SC-003 | T003 | Controller, shutdown and reconciliation contracts; offline controller fixtures |
| FR-006; SC-004 | T001 | Spec Kit feature paths, contract and history links, identifier and task-coverage checks |

- **A1 — Feature collision (resolved):** The sibling attribution feature already uses number `002` in the shared recovery candidate. Move this feature to `003` without changing the sibling’s files or approval state.
- **A2 — Diagnostic scope (resolved in documentation):** Proxy verification, controller install parity and live qualification answer different questions. The runbook names each separately and does not advertise a command for T004.
- **A3 — Historical authority (resolved in documentation):** The migrated contracts retain the historical design; their headers no longer read as current permission. The reconciliation contract governs conflicts with the older design. Incident evidence remains historical.
- **A4 — Implementation admission (open):** Local prerequisites and identifier coverage can be checked independently of the shared scaffold’s publication. They cannot satisfy Spec Validate, applicable review or the T004 fixtures. Do not use the local bootstrap as a substitute for the shared gate.

No controller, guard or installed artifacts are changed by this documentation slice. The [plan](plan.md#gates-and-complexity) records the remaining gates.

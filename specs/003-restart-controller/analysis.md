# Consistency analysis: restart controller

Scope: specification reconciliation, the owner dispatcher, and the offline disk check. This is not platform qualification or the operator’s comprehension record.

| Requirement | Task | Acceptance surface |
|---|---|---|
| FR-001, FR-002; SC-001 | T002 | Agent entry point, command table, `make help`, dispatcher fixtures |
| FR-003; SC-002 | T004 | `make check-binding` compares the policy digest and controller file facts to the install receipt. `tests/test_owner_maintenance.py` covers matches, drift, invalid inputs and the read-only boundary. |
| FR-004, FR-005; SC-003 | T003 | Controller, shutdown and reconciliation contracts; offline controller fixtures |
| FR-006; SC-004 | T001 | Spec Kit feature paths, contract and history links, identifier and task-coverage checks |

- **A1 — Feature collision (resolved):** The sibling attribution feature already uses number `002` in the shared recovery candidate. Move this feature to `003` without changing the sibling’s files or approval state.
- **A2 — Diagnostic scope:** `make check-binding`, proxy verification, controller install parity, and live qualification answer different questions. The runbook keeps them separate.
- **A3 — Historical authority (resolved in documentation):** The migrated contracts retain the historical design; their headers no longer read as current permission. The reconciliation contract governs conflicts with the older design. Incident evidence remains historical.
- **A4 — Implementation admission:** The implementation gate returned `allowed-with-warning` in the preparation worktree for the validated spec and task revisions. The warnings reflect separate maintainer waivers, not peer approval. Approval state is private and does not travel with this branch. Check the gate in the worktree where T004 will run; execute the offline acceptance fixtures as part of T004.

## Pre-implementation security review

Independent follow-up found no remaining blockers in the revised design and acceptance criteria. The reviewed work keeps the diagnostic read-only, treats an install receipt as comparison data rather than proof of authority, and requires offline fixtures for complete matches and refusals. The review covered these SHA-256 digests:

- `spec.md`: `b0afa49ec9345730075f62f25c1a56f77665592b8117e60cc0a090e55255635c`
- `plan.md`: `24f8fb3458b31b99377e8ddc28a4810f827b7a7f3689da9df51903719280991f`
- `tasks.md`: `a34e731983d9960e40a29f30fc0fffeb7d6fa8a969c1e318939eaeda7f5c15fd`

This technical review is not operator validation, spec peer approval, or a runtime test of T004.

T004 changes only the owner command and its fixtures, not the controller or guard inventory. The reviewed spec, plan and task files are preserved byte-for-byte; this analysis records implementation evidence separately. No offline result qualifies or authorizes a live controller.

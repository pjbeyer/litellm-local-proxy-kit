# Kit specifications

## Feature contracts on the default branch

- [001 — Proxy Lifecycle Guard fail-closed follow-up](001-proxy-lifecycle-guard-plugin/spec.md): draft for bounded batch decisions and assignment handling. It does not authorize implementation or activation.
- [002 — Hermes exact quality-eval attribution](002-hermes-eval-attribution/spec.md): design draft. No transport is qualified and implementation is not approved. See the [plan and entry gates](002-hermes-eval-attribution/plan.md).
- [003 — Restart controller operations](003-restart-controller/spec.md): controller contract, shutdown amendment, later fixes and the operations workflow. [Plan](003-restart-controller/plan.md) · [Tasks](003-restart-controller/tasks.md) · [Offline quickstart](003-restart-controller/quickstart.md).
- [005 — Project provisioning baseline](005-project-provisioning-baseline/spec.md): contributor setup, repository scaffolding, and opt-in release automation.

Feature numbers are stable identifiers, not a count of merged specifications. A gap can be reserved for branch-only work; do not renumber existing features to fill it. Keep each feature's spec and supporting artifacts in its own `specs/NNN-short-name/` directory. The [guard incidence-reduction contract](../docs/contracts/guard-incidence-reduction.md) is a separate operational contract.

Start in a dedicated kit worktree. Beads owns backlog and status. The checked-in [Spec Kit scaffold and constitution](../docs/project-tooling.md#work-in-a-fresh-checkout) provide the Hermes-first baseline. Do not commit feature pointers, private approval records, or machine-local extension configuration. See the feature [plan](003-restart-controller/plan.md#gates-and-complexity) for implementation prerequisites.

Run the Spec Kit `verify_spec_md.py` and `verify_tasks_md.py` checks against the feature directory and follow the [offline quickstart](003-restart-controller/quickstart.md). Where a feature requires Spec Validate, install a reviewed pinned extension before use. For feature 003 the gate command is `bash .specify/extensions/spec-validate/scripts/bash/check-gate.sh 003-restart-controller`. If the extension or approval evidence is missing, the gate is not cleared. Never create an approval record on the operator's behalf.

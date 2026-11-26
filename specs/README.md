# Kit specifications

- [003 — Restart controller operations](003-restart-controller/spec.md): controller contract, shutdown amendment, later fixes and the operations workflow. [Plan](003-restart-controller/plan.md) · [Tasks](003-restart-controller/tasks.md) · [Offline quickstart](003-restart-controller/quickstart.md).
- Guard interception is separate: start with the [guard incidence-reduction contract](../docs/contracts/guard-incidence-reduction.md). The guard and Hermes attribution features and their project-wide Spec Kit tooling are tracked separately.

Start in a dedicated kit worktree. Beads owns backlog and status. The checked-in [Spec Kit scaffold and constitution](../docs/project-tooling.md#work-in-a-fresh-checkout) provide the Hermes-first baseline. Do not commit feature pointers, private approval records, or machine-local extension configuration. See the feature [plan](003-restart-controller/plan.md#gates-and-complexity) for implementation prerequisites.

Run the Spec Kit `verify_spec_md.py` and `verify_tasks_md.py` checks against the feature directory and follow the [offline quickstart](003-restart-controller/quickstart.md). Where a feature requires Spec Validate, install a reviewed pinned extension before use. For feature 003 the gate command is `bash .specify/extensions/spec-validate/scripts/bash/check-gate.sh 003-restart-controller`. If the extension or approval evidence is missing, the gate is not cleared. Never create an approval record on the operator's behalf.

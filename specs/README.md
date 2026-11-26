# Kit specifications

- [003 — Restart controller operations](003-restart-controller/spec.md): controller contract, shutdown amendment, later fixes and the operations workflow. [Plan](003-restart-controller/plan.md) · [Tasks](003-restart-controller/tasks.md) · [Offline quickstart](003-restart-controller/quickstart.md).
- Guard interception is separate: start with the [guard incidence-reduction contract](../docs/contracts/guard-incidence-reduction.md). The guard and Hermes attribution features and their project-wide Spec Kit tooling are tracked separately.

Start in a dedicated kit worktree. Beads owns backlog and status. The shared Spec Kit scaffold, constitution and hooks come from the separate repo-wide recovery. If you have a temporary local bootstrap, move it out of the checkout before staging; never commit it, its approval records or `.specify/feature.json`. See the feature [plan](003-restart-controller/plan.md#gates-and-complexity) for implementation prerequisites.

Run the Spec Kit `verify_spec_md.py` and `verify_tasks_md.py` scripts from the `speckit` skill against the feature directory, and run the [offline quickstart](003-restart-controller/quickstart.md). The installed Spec Validate extension owns the pre-implementation gate; run `.specify/extensions/spec-validate/scripts/bash/check-gate.sh 003-restart-controller` with Bash. Never create an approval record on the operator's behalf.

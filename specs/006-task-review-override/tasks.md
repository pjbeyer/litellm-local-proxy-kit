# Tasks: Revision-bound task-review override

- [x] T001 Add a separate default-off opt-in and validate the feature, exact digests, attribution, and pending disposition in the shared policy parser (FR-001, FR-002).
- [x] T002 Wire the Bash and PowerShell gate checks and report each exception used without editing approval state (FR-003, FR-004; depends on T001).
- [x] T003 Document the maintainer procedure, revocation, and state preservation (SC-002; depends on T002).
- [x] T004 Run offline entry-point tests and obtain independent review of the change (SC-001; depends on T002 and T003).

## Execution Wave DAG

- Wave 1: T001
- Wave 2: T002
- Wave 3: T003
- Wave 4: T004

# Implementation Plan: Revision-bound task-review override

Reuse the existing approval-state reader and writer, SHA-256 routines, and shared Python policy parser. Keep the existing gate JSON and exit codes. Add a separate opt-in and a separate receipt so legacy spec-review authorizations never apply to tasks.

Both gate scripts verify current validation digests before checking the task waiver. The shared policy checks the named feature, exact receipt bindings, and the opt-in. The gate reports warnings only when every other implementation prerequisite passes.

The gate is an offline workflow control in a trusted worktree, not an identity or access-control system. `by` records the maintainer's decision; it does not authenticate the editor. Keep approval state Git-ignored and leave deployed services and security gates untouched.

Test both entry points against synthetic state and config. Run the branch-scope detector and obtain independent code review before committing the change. Do not enable the waiver for any real feature as part of delivering the mechanism.

# Feature Specification: Profile-scoped guard maintenance

**Status:** Proposed. This specification does not authorize implementation, installation, or activation. It does not change the guard decision contract in [Spec 001](../001-proxy-lifecycle-guard-plugin/spec.md) or the proxy operations in [Spec 003](../003-restart-controller/spec.md).

## User Scenarios & Testing

### US1 — Check the installed guard without loading it (P1)

An operator runs `make guard-verify PROFILE=… BASE_REV=…` from a kit worktree. Given an existing profile and an owner-selected full commit ID for the expected installed release, the command checks the installed guard files, private permissions, and integrity reference. It reports on-disk parity, not whether the gateway loaded the guard. Test with disposable profiles and both matching and changed files.

### US2 — Preview and stage an upgrade without activating it (P1)

An operator runs `make guard-preview PROFILE=… BASE_REV=… REV=…` to see which managed files would change. After reviewing the candidate, the operator can run `make guard-stage` with the same inputs to assemble candidate and rollback files in private staging. Staging refuses if the installation no longer matches the expected predecessor. It never edits the profile. Test with disposable repositories and profiles and no running services.

### Edge Cases

- An unknown profile, invalid or missing commit, missing file, unsafe path, or integrity mismatch stops the operation. Do not fall back to the current profile, `HEAD`, a mutable Git ref, or the controller policy.
- An interrupted stage must not leave a package that can be mistaken for complete. A stage package is not approval and can become stale before cutover.
- The scheduled watchdog is a separate consumer. A changed watchdog file is not silently copied, removed, or scheduled by these targets.

## Requirements

- **FR-001**: Reuse the kit's one-target Makefile. Keep proxy targets and controller inputs unchanged. Require an explicit existing Hermes profile resolved through the Hermes profile interface, and use the owner-selected full Git commit IDs as source inputs. Check Git objects without using dirty worktree files or importing candidate code.
- **FR-002**: `guard-verify` MUST compare `guard.py`, `__init__.py`, and `plugin.yaml` to the pinned predecessor; validate the installed private policy and integrity reference using the established guard file contract; and report missing files, unsafe ownership or permissions, and mismatches. It MUST NOT run the evaluator or probe the gateway or watchdog. A parity result MUST say that live hook identity and enablement were not checked.
- **FR-003**: `guard-preview` MUST run the same predecessor checks and show which managed files would change against the reviewed candidate. It MUST NOT write installed or staging files, display private policy content, or substitute an inferred predecessor.
- **FR-004**: `guard-stage` MUST repeat verification and write only an owner-private package outside every Hermes profile and the worktree. It MUST bind the profile, source revisions, and verified predecessor facts to the candidate and rollback bytes. Use the checked-in candidate source files and the same captured predecessor bytes for verification and the rollback copy. Bind the existing private policy by digest; do not copy it into the package or change it. Include candidate and predecessor integrity references. Publish a completed package only after verification; no incomplete package may appear complete.
- **FR-005**: No target may write the profile, edit scheduler state, enable or disable a plugin, control either service, or alter restart approval. The existing owner cutover and rollback runbook is authoritative. A stage package requires fresh predecessor and policy checks before use and does not attest that a gateway is stopped. No cutover target is in scope.

## Success Criteria

- **SC-001**: Fixture tests distinguish on-disk parity from mismatch, missing inputs, unsafe paths and permissions, and a changed integrity reference. A successful check makes no claim about a serving hook. Covers FR-001 and FR-002.
- **SC-002**: Preview writes no package, stage writes one complete private package matching its manifest, and a failed stage leaves no usable package. Fixtures prove the repository worktree, profile, and watchdog state are unchanged and that stage refuses a changed predecessor or policy reference. Covers FR-003 through FR-005.
- **SC-003**: Offline tests retain the proxy Make interface and reject multi-target calls before dispatch. Operator docs identify the one command for each maintenance step and where owner cutover begins. Covers FR-001 and FR-005.

## Scope and assumptions

This is an upgrade workflow for an existing, pinned installation. First install, policy edits, watchdog distribution changes, live activation, and live attribution remain outside it. Review the specification and implementation plan before source changes. Follow the [guard runbook](../../integrations/hermes/proxy-lifecycle-guard/README.md) for currently supported operations.

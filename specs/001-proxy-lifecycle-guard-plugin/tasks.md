# Tasks: Proxy Lifecycle Guard fail-closed follow-up

**Status:** Draft for maintainer review. Implementation is blocked: the Spec Validate extension is not installed in this worktree. A document review does not clear that gate. No live action is authorized.

**Reviewed plan base:** `25541e044c44de2e81f9880cd6ed29ba58ee6c4b`. Use the [spec](spec.md), [plan](plan.md), [decision model](data-model.md), [research](research.md), [hook contract](contracts/pre-tool-decision.md), and [offline verification guide](quickstart.md). Checklist paths are relative to the repository root. `[P]` marks tasks that can run concurrently once their dependencies are satisfied.

## Phase 1: Setup and authorization

- [ ] T001 Bind maintainer review, operator Spec Validate results, and independent clearance to the exact revisions of `specs/001-proxy-lifecycle-guard-plugin/spec.md` and `specs/001-proxy-lifecycle-guard-plugin/tasks.md`. Use the approved tooling from the separate recovery workstream. Record the maintainer’s review of the exact plan revision named above; if the plan changes, obtain fresh review. Confirm the spec and task hashes and validation results separately, require an allowed feature-gate verdict, and obtain explicit maintainer authorization to implement. Missing tooling, missing or stale evidence, unresolved findings, or a refused gate stop all implementation work. Do not import historical approvals. (FR-004, SC-003)

## Phase 2: Shared offline foundation

- [ ] T002 Run the unchanged offline suite from `README.md` against the pre-change source in the dedicated worktree; record its revision, platform, command, exit status, and sanitized summary before adding tests. Stop and distinguish any pre-existing failure from a new contract assertion. (SC-001, SC-002) (depends on T001)
- [ ] T003 Reuse `tests/conftest.py` and `tests/test_lifecycle_guard.py` to prepare shared fixtures: isolated synthetic policy and discovery, a fake host that registers the real adapter and records dispatch through its hook, working/absent/failed sink modes, and fail-on-call sentinels at external execution and service boundaries. Verify fixture isolation and prove that a safe control reaches the recorder exactly once. Never point any fixture at an installed policy, service, or sink. (FR-003, SC-004) (depends on T002)

## Phase 3: US1 — Bound evaluation without allowing unverified work (P1)

**Independent test:** The batch suite shows that only fully assessed safe batches dispatch. Refusals follow the contract order, obey existing limits, and dispatch nothing regardless of sink state.

- [ ] T004 [P] [US1] Add the bounded decision matrix in `tests/test_guard_batch_contract.py`: complete safe assessment at the cap; count-only refusal above the cap; protection veto over other refusals, and other refusals over count; first-in-order ties within each refusal class; invalid or unavailable items; and existing non-count exhaustion. Use inert item outcomes for precedence. Instrument each applicable budget on allow and refusal paths, including existing scope for nested analysis, and verify that count-limited work never inspects the unassessed remainder. (FR-001, SC-001) (depends on T003)
- [ ] T005 [P] [US1] Add the batch host-boundary checks in `tests/test_lifecycle_guard.py`: use the actual adapter and evaluator with an otherwise harmless synthetic operation designated protected by the fixture, plus the safe dispatch control. For every batch decision class, verify dispatch, sentinel, fixed refusal, and sink assertions as defined below. Do not substitute inert outcomes for the real-policy wiring check. (FR-003, SC-001, SC-004) (depends on T003)
- [ ] T006 [US1] Run `tests/test_guard_batch_contract.py` and the new batch host checks in `tests/test_lifecycle_guard.py` against the pinned pre-change source with only fixture/test changes. Record targeted assertion failures as Red; setup errors or missing tests do not count. Passing controls are not failures. If the existing source already conforms, record that and do not make an unnecessary change. (SC-001, SC-004) (depends on T004, T005)
- [ ] T007 [US1] Implement the ordered whole-batch decision in `integrations/hermes/proxy-lifecycle-guard/guard.py`. Preserve policy, integrity, and hook preconditions, the existing limits and their scope, and the fixed refusal vocabulary. Continue after an ordinary refusal only while budget permits; preserve the first protection veto, else the first other refusal, else `BOUND_COUNT` (`LGV-012`) with smaller-batch guidance when count alone prevents complete assessment. Require affirmative safety for every item before allowing. Update `integrations/hermes/proxy-lifecycle-guard/README.md` with the source change; do not describe the candidate as installed. (FR-001) (depends on T006)
- [ ] T008 [US1] Rerun `tests/test_guard_batch_contract.py`, the batch host checks in `tests/test_lifecycle_guard.py`, and the full `tests/` suite. Record Green and no regressions against the baseline before starting the assignment increment. (SC-001, SC-004) (depends on T007)

## Phase 4: US2 — Distinguish assignment data from execution (P2)

**Independent test:** The assignment suite exercises single and multiple assignments with the evaluator and isolated policy. It distinguishes safe literals and supported read-only operations from protected, unresolved, and malformed effects, and confirms that policy sees assignment effects.

- [ ] T009 [P] [US2] Add the decision matrix in `tests/test_guard_assignment_contract.py` for safe literals that resemble syntax, supported read-only operations, protected effects, unresolved effects, and malformed or unterminated syntax. Cover each class with single and multiple assignments. Use a paired harmless fixture to verify that policy sees the change when a supported assignment changes the operation’s meaning. Observe effects passed to an inert policy double without stubbing assignment classification; separately check existing policy against abstract protected effects under isolated synthetic policy and discovery. Verify literal data is not analyzed as executable text. (FR-002, SC-002) (depends on T008)
- [ ] T010 [P] [US2] Add assignment host-boundary checks in `tests/test_lifecycle_guard.py` using the actual adapter and evaluator, with harmless synthetic operations and isolated fixture policy. For every assignment decision class, verify dispatch, sentinel, fixed refusal, and sink assertions as defined below. (FR-003, SC-004) (depends on T008)
- [ ] T011 [US2] Run `tests/test_guard_assignment_contract.py` and the new assignment host checks in `tests/test_lifecycle_guard.py` with the evaluator unchanged from the verified batch increment. Record Red against that exact pre-assignment revision using the same assertion and control rules as T006; do not modify conforming behavior. (SC-002, SC-004) (depends on T009, T010)
- [ ] T012 [US2] Update `integrations/hermes/proxy-lifecycle-guard/guard.py` to distinguish demonstrated literal data from executable effects for supported assignments. Apply existing protection to every analyzable effect, including a change in the operation’s meaning; refuse unknown or malformed effects. Do not widen the supported grammar or execute input. Update `integrations/hermes/proxy-lifecycle-guard/README.md` alongside the source change. (FR-002) (depends on T011)
- [ ] T013 [US2] Rerun `tests/test_guard_assignment_contract.py`, the assignment host checks in `tests/test_lifecycle_guard.py`, and the full `tests/` suite, including the batch checks. Record Green and no regressions against the pre-assignment revision. (SC-002, SC-004) (depends on T012)

## Phase 5: Cross-cutting qualification

- [ ] T014 [P] Run the focused and complete offline checks from `specs/001-proxy-lifecycle-guard-plugin/quickstart.md` on macOS and Linux for the same exact source candidate. Retain distinct Red and Green summaries; neither a local pass nor matching source digests on disk prove runtime activation. (SC-001, SC-002, SC-004) (depends on T013)
- [ ] T015 [P] Independently review the exact candidate against `specs/001-proxy-lifecycle-guard-plugin/contracts/pre-tool-decision.md`, the fake-host dispatch checks in `tests/test_lifecycle_guard.py`, and `integrations/hermes/proxy-lifecycle-guard/README.md`. Confirm that the adapter interface and sink contract remain unchanged and that the runbook reflects proposed source behavior. Changes to the candidate require fresh checks and review. (FR-003, FR-004) (depends on T013)
- [ ] T016 Assemble the sanitized qualification record per `specs/001-proxy-lifecycle-guard-plugin/quickstart.md`. Confirm the evidence identifies the exact spec, plan, tasks, and source candidate as applicable, including that the implementation gate was cleared before implementation began. Distinguish offline conformance from installed generation and owner-authorized activation. Stop at the offline handoff. (FR-004, SC-003) (depends on T014, T015)

## Shared host-boundary assertions

For each batch and assignment decision class, keep the enforcement result and fixed refusal content identical with a working, absent, or failing sink. Pin expected refusal categories, codes, and guidance independently of evaluator output. Check for zero recorded dispatch on refusal, one for the safe control, and no execution or service sentinel invocations. Verify outside the evaluator that the fixtures reach the intended decision paths and that refusals actually exercise the injected sink failure in failure mode. For a filesystem-backed sink, use a bounded drain outside the hook before inspecting output or cleaning up fixture state. On timeout, fail the test and release or isolate the worker before deleting that state. For a working sink, check the isolated record against the [existing sink contract](../../docs/contracts/decision-sink.md): allowed fields and values on refusal, no record on allow. Neither output surface may contain synthetic raw input, operands, or exception markers. Use only inert data and harmless synthetic operations; do not use live requests or exploit payloads. Keep raw inputs, private state, workstation paths, and transcripts out of shared evidence.

## Dependencies & Execution Order

T001 blocks all test and source work. The batch increment comes first; the assignment increment can be tested independently but follows the verified batch increment because they share the evaluator and host harness. A source task must not proceed on a failed test setup or missing Red evidence for an identified defect. If a story’s new assertions all pass and the checked paths are confirmed, record that story as conforming on the pinned baseline. Mark its source-change step not needed in the evidence (T007 or T012), which satisfies that dependency, then run its focused and full regression checks (T008 or T013) with the evaluator unchanged. If a targeted assertion fails, Red is required before changing the evaluator.

## Execution Wave DAG

- Wave 1: T001
- Wave 2: T002
- Wave 3: T003
- Wave 4: T004, T005
- Wave 5: T006
- Wave 6: T007
- Wave 7: T008
- Wave 8: T009, T010
- Wave 9: T011
- Wave 10: T012
- Wave 11: T013
- Wave 12: T014, T015
- Wave 13: T016

## Implementation Strategy

**MVP:** After authorization, deliver US1 as a separately reviewable offline increment. Keep the assignment change out of it. Do not land a failing baseline as a release candidate.

**Follow-up:** Deliver US2 against that baseline, then qualify both stories together. Keep the staged contract, tests, and runbook aligned; no runtime dependencies, host interface changes, live verification, deployment, or rollback are part of this task list. If approved artifact bytes change, obtain fresh validation and review before relying on the amended artifacts.

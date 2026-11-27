# Implementation Plan: Proxy Lifecycle Guard fail-closed follow-up

**Branch:** `docs/spec-001-guard-fail-closed` | **Spec:** [Spec 001](spec.md) at `4a0910733d44e37942340e7cb5d069464c967455`

**Spec SHA-256:** `04f464ecb152eadd27c01f8801d572b166082e14c3519f63b0f5bcb2b9fffb27`

**Status:** Draft for maintainer review. The maintainer reviewed the pinned spec and authorized planning only. The independent requirements review of that revision found no actionable issues. Neither record clears the implementation gate.

## Summary

Keep the existing pre-tool hook and policy checks. In the evaluator, require affirmative safety evidence for the whole batch and keep the existing refusal codes and analysis limits. For assignments, distinguish proven literal data from executable effects using the existing bounded analysis; refuse unknown effects rather than evaluate or guess. See [research](research.md), the [decision model](data-model.md), and the [hook contract](contracts/pre-tool-decision.md).

## Technical context

- **Runtime:** Existing Python evaluator and Hermes adapter. No new runtime dependency, hook, tool, or storage schema. The optional [decision sink](../../docs/contracts/decision-sink.md) stays advisory.
- **Source:** `integrations/hermes/proxy-lifecycle-guard/guard.py`. Keep `integrations/hermes/proxy-lifecycle-guard/__init__.py` and the host dispatch interface unchanged unless separate review establishes a necessary contract change.
- **Tests:** Reuse `tests/test_lifecycle_guard.py` and the fixture patterns in `tests/conftest.py`. Add focused cases in `tests/test_guard_batch_contract.py` and `tests/test_guard_assignment_contract.py` during implementation. Run the complete offline suite on macOS and Linux before claiming source conformance.
- **Limits:** Preserve each existing limit at its existing scope. A count-limited batch does not justify increasing another budget. No unbounded second pass or new work proportional to the unassessed remainder.
- **Out of scope:** Broad parser changes, expanded support for shell forms, telemetry schema or worker changes, installed policy, restarts, live probes, and activation.

## Constitution check

| Principle | Planning disposition |
|---|---|
| I. Public source, private deployments | Use only synthetic cases and fixed result labels in shareable evidence. Do not copy live inputs, state, or logs. |
| II. Preserve operator authority | Plan in a dedicated worktree; test with a fake host and isolated policy/discovery. No change to installed services or policy. |
| III. Specify and verify offline | Pin the spec, map FR-001 through FR-004 to acceptance, and require independent plan and task review. Missing approval stops implementation. |
| IV. Keep maintenance simple | Reuse the evaluator and test harness. Refresh the guard runbook alongside any corresponding behavior change; do not document it as installed behavior. |

The design satisfies these constraints in scope. Implementation and activation remain gated.

## Delivery sequence

1. **Planning (this phase):** Review the artifacts against the unchanged spec. Skip the optional agent-context refresh to avoid repointing shared instructions. The Spec Validate and Security Review extensions are not installed in this worktree. Independent document review is useful evidence, not a replacement for the formal gates.
2. **Tasks (next phase):** After maintainer review of this plan, generate `tasks.md` with traceability, boundedness checks, and independent review. Bind formal validation and approvals to the exact artifacts they cover. Do not import approval records from other branches or historical revisions.
3. **Implementation, once authorized:** Work red-first. Establish baseline failures using isolated fixtures and inert test doubles for refused item outcomes before changing the evaluator. Address batch decisions and assignment handling in small, separately reviewable changes. Follow each change with focused tests and the complete offline gate in the [quickstart](quickstart.md). Refresh the [guard runbook](../../integrations/hermes/proxy-lifecycle-guard/README.md) in the corresponding source change.
4. **Qualification:** Review the exact candidate independently, including the guard decision and the fake host's dispatch boundary. Offline qualification does not show what generation is running. Leave live verification, deployment, and rollback to the existing owner-controlled workflow under separate authorization.

## Evidence gates

| Gate | Status and completion criterion |
|---|---|
| Maintainer review of the spec | Complete for `4a09107`, for planning only. |
| Independent requirements review | Complete for the pinned spec text; no actionable findings. Formal review state has not been created. |
| Plan and task approval | Open. Maintainer reviews this plan; a later task set must be independently reviewed before guard source changes. |
| Spec Validate and exact-revision approval (FR-004, SC-003) | **Blocked.** Restore the approved tooling through its separate workstream. Obtain and record maintainer validation, independent exact-revision clearance, and task validation as applicable; run the gate against those artifacts. Missing or stale evidence stops implementation. |
| Offline acceptance (SC-001, SC-002, SC-004) | Not run for the proposed behavior. Record red-first and candidate results separately, tied to the revisions tested. |
| Activation | Not authorized. No approval in this plan applies to a live service. |

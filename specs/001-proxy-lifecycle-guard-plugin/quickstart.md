# Offline verification guide

Use this guide after implementation is authorized. The cases below are acceptance obligations, not test results. Do not point tests at installed policy, discovery, services, or a real decision sink.

## Setup and commands

1. Use a dedicated worktree and a short, resolved temporary directory as described in the [kit's offline instructions](../../README.md). Reuse the test harness for isolated input, policy, and discovery. In the host-boundary test, register the existing adapter with a fake host, run its hook against the real evaluator with isolated policy and discovery, and record dispatch attempts. Include an otherwise harmless synthetic operation designated protected by the isolated fixture, alongside a safe control that reaches the dispatch recorder exactly once. Use inert outcomes for the batch precedence matrix, not in place of this hook-level check. Add a sentinel for each external execution or service interaction the fixture could reach. A sentinel invocation fails the test; it never calls the external operation.
2. Run the focused checks for each increment before changing the evaluator: the relevant contract test file plus `tests/test_lifecycle_guard.py`. Capture targeted assertion failures; a failed setup or missing test is not red evidence. Other controls may already pass. Do not execute inspected inputs or replay live requests. After both increments, run all three focused files together:

   ```sh
   uv run --no-project --with pytest --with pyyaml python -m pytest -q \
     tests/test_guard_batch_contract.py tests/test_guard_assignment_contract.py \
     tests/test_lifecycle_guard.py
   ```

3. Once the focused checks pass, run the full offline suite. Require the macOS and Linux checks for the exact source candidate; a local pass covers only the local platform.

   ```sh
   uv run --no-project --with pytest --with pyyaml python -m pytest -q tests
   ```

## Acceptance checks

| Criteria | Evidence required |
|---|---|
| SC-001 / FR-001 | At the count cap, allow only complete affirmative evidence. Above it, exercise count-only refusal and each higher-priority refusal, including competing outcomes and first-in-order ties. Instrument the relevant budget boundaries independently of the returned result and verify that counts stay within existing limits on both outcomes. A fake host records no dispatch for every refusal. |
| SC-002 / FR-002 | For single and multiple assignments, verify safe literal and read-only cases, protected outcomes, unresolved and malformed cases, and the unterminated case. At the evaluator boundary, observe the effects passed to policy using an inert policy test double; do not stub assignment classification. Separately verify that the existing policy refuses abstract protected effects under synthetic policy and discovery. Confirm that literals are not analyzed as executable text. |
| SC-004 / FR-003 | For every decision class above, check that the no-execution sentinel is untouched and that a working, absent, and failed sink yield the same enforcement result. Check returned refusals against the fixed vocabulary. In successful telemetry cases, inspect the isolated sink: refusals produce only the fields and values permitted by the existing sink contract; allows produce no record. Assert neither returned refusals nor records include synthetic raw input or exception markers. Verify fixture state and discovery isolation separately. |
| SC-003 / FR-004 | Attach results to the actual artifact revisions, verify exact-revision reviews and approval records, and label the tests offline only. A passing suite cannot clear an absent implementation gate or authorize activation. |

Record the tested revision, host platform, command, exit status, and summary. Exclude raw inputs, transcripts, policy contents, and workstation paths from any shared report.

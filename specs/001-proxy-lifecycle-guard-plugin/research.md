# Planning decisions

## Keep the evaluator as the enforcement boundary (FR-001, FR-003)

**Decision:** Preserve the existing adapter and synchronous allow/refuse decision. Retain policy, integrity, and hook preconditions. Put the proposed behavior in the current evaluator rather than adding a second guard, dispatch layer, or telemetry dependency.

**Rationale:** The adapter already delegates pre-tool decisions to the evaluator. Reuse that integration boundary and verify its contract with a fake host. Do not use a sink record as evidence that dispatch was blocked.

**Alternatives considered:** A host change would widen scope and introduce a new compatibility and activation dependency. An independent prefilter would duplicate policy and could disagree with the evaluator.

## Require evidence for every item (FR-001)

**Decision:** Retain invocation order, preserve the first refusal of each applicable priority during permitted inspection, and select a batch result once. No count-only validation can turn an unassessed item into a safe one. Keep each existing budget, including its scope, unchanged.

**Rationale:** The evaluator needs both bounded work and predictable outcomes. Finishing permitted inspection after an ordinary refusal can preserve a higher-priority protection veto. In contrast, budget exhaustion provides no authority to do more work or to dispatch any item.

**Alternatives considered:** Stopping at the first ordinary refusal would not preserve the required priority; assessing the full remainder would break the existing limit. Allowing a separately assessed prefix would break the one-decision contract.

## Reuse bounded assignment analysis (FR-002)

**Decision:** Preserve the supported input language and use the existing analysis to distinguish literal operands from executable effects. Keep assignment effects in the safety decision. Refuse unresolved or malformed analysis instead of trying to resolve it by running input.

**Rationale:** The problem is a decision about the meaning of input, not a need for a new shell grammar. Literal data should not inherit an executable classification merely from its text, and unresolved input should not be treated as data.

**Alternatives considered:** Blanket refusal of every assignment would reject supported safe work. Adding a new parser or executing shell input would widen the trust boundary and exceed this spec.

## Separate acceptance from release (FR-003, FR-004)

**Decision:** Verify bounded inspection and no dispatch with an isolated fake host and evaluator instrumentation. Use synthetic policy/discovery fixtures; control telemetry independently. Keep the existing four-field sink contract and refusal vocabulary. Pin approval and verification evidence to the candidate each actually covers.

**Rationale:** A refusal message, green baseline, or installed file digest does not prove the proposed behavior, correct host wiring, or live activation.

**Alternatives considered:** Using real callbacks, installed policy, or captured production input risks acting on live resources or disclosing data. Historical checkmarks cannot substitute for current review.

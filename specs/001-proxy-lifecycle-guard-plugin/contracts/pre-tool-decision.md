# Pre-tool decision contract

This is the proposed behavior for the existing hook, not an installed capability. The [specification](../spec.md) is authoritative.

## Interface

The Hermes adapter passes the tool identity and arguments to the existing evaluator with the existing policy input. The evaluator returns the existing host allow or refusal result. FR-001 and FR-002 change decisions within that interface; they do not change the hook envelope, response schema, tool set, or protection policy. Existing policy, integrity, and hook preconditions still apply before the rules below.

## Deferred batch

Inspect in invocation order only while permitted by existing budgets. An ordinary item refusal does not end inspection while the evaluator can safely continue within those budgets. The first established protection veto is decisive. Apply FR-001 in this order:

1. First protection veto established during inspection.
2. Otherwise, first other refusal established during inspection, including the applicable existing refusal for invalid, unavailable, or analysis-limited input.
3. Otherwise, `BOUND_COUNT` (`LGV-012`) with guidance to submit a smaller batch when the count limit was the only reason some items were not assessed.
4. Otherwise, allow only if every item was assessed and proven safe.

On refusal, the host dispatches none of the batch. Analysis never dispatches anything. Do not raise a budget to obtain a preferred refusal category.

## Assignments and refusal output

Apply existing policy to all analyzable executable effects of a supported operation. Unresolved or malformed executable intent refuses. Proven literal operands do not change a safe outcome merely because they resemble syntax. Use fixed refusal vocabulary without raw commands, operands, or exception strings. The decision and refusal content do not depend on the optional sink being available or working. No inspected input executes during enforcement.

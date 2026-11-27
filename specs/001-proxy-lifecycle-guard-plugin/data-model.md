# Decision model

These are logical records, not new public types or persisted schemas. The [spec](spec.md) defines the normative outcomes.

## Deferred batch and item evidence (FR-001)

- **Batch:** An ordered set of requested operations in one guarded invocation. Preserve the order defined by the existing invocation format. The guard emits one result for the entire batch.
- **Item evidence:** At a visited position, record whether the item is proven safe, refused for a protected operation, or refused for another reason. A skipped item has no affirmative evidence; a partially inspected item cannot be marked safe. Recognition of the tool or envelope is not proof of the operation's safety.
- **Budget state:** The applicable existing limit and the amount of permitted inspection used. Track whether count alone left an unassessed remainder, or another existing refusal occurred. A nested input uses the budgets and scope already assigned to its analysis; do not expand or reset those limits to examine an unassessed item.
- **Aggregate decision:** After preconditions and bounded analysis, select a result in the [contract's order](contracts/pre-tool-decision.md#deferred-batch). The aggregate needs, at most, the earliest applicable refusal evidence in each class and proof of complete safe inspection to allow. Do not retain a copy of each raw argument for this purpose.

## Assignment assessment (FR-002)

The decision distinguishes proven literal data, analyzable executable effects, and unresolved effects. A supported input can contain both data and effects. A safe literal does not prove adjacent content safe. All analyzable effects, including effects of assignments on the operation, remain subject to existing policy. Unknown or malformed effects yield a refusal. The guard derives these facts from analysis, not execution.

## Result and evidence (FR-003, FR-004)

An allow has no refusal content; a refusal carries the existing fixed category, code, and guidance. The optional sink uses its [existing contract](../../docs/contracts/decision-sink.md). Neither the reply nor its sink record carries raw input or exception text.

Review and validation evidence is separate from runtime results. Track the spec digest and later artifact revisions with their respective results; do not reuse a result for changed content. Keep offline conformance and live authorization separate.

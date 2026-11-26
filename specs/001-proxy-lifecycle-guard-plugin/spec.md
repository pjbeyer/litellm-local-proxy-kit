# Feature Specification: Proxy Lifecycle Guard fail-closed follow-up

**Status**: Draft for owner and independent review. This specification does not establish current runtime behavior, approve implementation, or authorize activation.

**Scope**: Resolve the remaining contract questions about bounded batch decisions and shell assignment handling. Other behavior remains outside this follow-up. The [maintenance runbook](../../integrations/hermes/proxy-lifecycle-guard/README.md) governs current operations; the [incidence-reduction contract](../../docs/contracts/guard-incidence-reduction.md) remains separate. For the earlier recovery and its limits, see the [recovery record](analysis.md).

## User Scenarios & Testing

### User Story 1 — Bound evaluation without allowing unverified work (P1)

An operator needs a clear refusal when a batch cannot be fully assessed within the hook's limits. A safe prefix is not evidence about the rest of the batch.

1. **Given** a batch at the configured inspection limit, **when** every item is proven safe, **then** the guard allows it.
2. **Given** a batch beyond that limit, **when** the inspected items are safe but the count limit leaves items unassessed, **then** the guard refuses the whole batch with guidance to reduce its size. It does not allow the inspected prefix separately.
3. **Given** an over-limit batch with a refusal established during inspection, **when** the guard decides, **then** it reports the established refusal according to FR-001 rather than reporting only the size of the batch.

### User Story 2 — Distinguish assignment data from execution (P2)

An operator needs supported, demonstrably data-only assignments to pass without making an executable construct invisible to the guard.

1. **Given** a supported assignment with a quoted literal containing shell-like text, **when** that text is data and the rest of the input is proven safe, **then** the guard allows it without treating the data as executable.
2. **Given** a supported input with more than one assignment, **when** the guard evaluates it, **then** it applies the same lifecycle policy to all executable effects, including effects of the assignments. Unresolved executable intent is refused.

### Edge Cases

- A structurally valid batch item is not necessarily safe. Missing, malformed, unsupported, or unread executable content cannot count as evidence of safety.
- Empty or unterminated syntax is not treated as a benign assignment when its executable effect is unresolved.
- Review, offline verification, and an owner decision to activate the plugin are separate events.

## Requirements

### Functional Requirements

- **FR-001**: The guard MUST allow a deferred batch only when it establishes safety for every item within existing analysis budgets. It MUST NOT infer safety from envelope structure, a safe prefix, or unavailable content. Existing policy and hook precondition checks still apply. Inspect items in invocation order. Where inspection can continue safely within the existing budgets, an item-level refusal other than a protection veto MUST NOT stop the search for a protection veto. Use the first applicable outcome below:
  1. Preserve the first veto for a protected operation established during inspection.
  2. Otherwise preserve the first other refusal established during inspection, in traversal order. An invalid item or unavailable executable content is a refusal. If another analysis budget prevents completing inspection, use the applicable existing refusal; do not count an incomplete assessment as safe.
  3. Otherwise, if the count limit left any item unassessed, refuse the entire batch with `BOUND_COUNT` (`LGV-012`) and guidance to submit a smaller batch.
  4. Allow only if every item was assessed and proven safe.

  The guard MUST NOT dispatch part of a refused batch or raise any budget to select a higher-priority outcome.
- **FR-002**: Assignment handling MUST preserve the distinction between literal data and executable effects, regardless of the number of assignments. The guard MUST apply existing protection rules to executable effects, including when an assignment changes the operation's meaning. If the guard cannot establish executable safety, it MUST refuse. It MUST NOT refuse an otherwise proven-safe operation merely because a literal data operand resembles shell syntax.
- **FR-003**: Enforcement MUST not execute inspected input, depend on the optional decision sink, or include command text or operands in refusals. Offline acceptance MUST use synthetic input and isolated state. It MUST NOT require a live deployment or service transition.
- **FR-004**: Before implementation, the maintainer MUST review the exact spec revision and record the applicable Spec Validate result, and an independent reviewer MUST clear findings against the exact revision. Any later specification edit invalidates that evidence. Implementation planning, task review, and live activation are separate gates; no existing review or approval transfers to this draft.

### Key Entities

- **Deferred batch**: One guarded invocation containing multiple requested operations, with one decision for the batch.
- **Assignment**: Shell syntax whose contents can contain literal data, executable constructs, or both. The guard must preserve that distinction when assessing the invocation.

## Success Criteria

- **SC-001**: The offline acceptance suite verifies all ordered outcomes, including at and above the count limit and when multiple outcomes could apply. Instrumentation confirms that each applicable analysis budget is respected on allow and refusal paths and that no portion of a refused batch is dispatched. Covers FR-001 and FR-003.
- **SC-002**: The offline acceptance suite exercises each decision class with both single and multiple assignments: allow proven-safe literal data and supported read-only operations, preserve vetoes for protected operations, and refuse unresolved or malformed input (including unterminated syntax). Verify that assignment effects are included in the decision. Covers FR-002.
- **SC-003**: The release evidence distinguishes offline conformance from activation and shows exact-revision review and operator validation before implementation. Missing or stale evidence leaves implementation blocked. Covers FR-004.
- **SC-004**: For each batch and assignment decision class, offline acceptance in isolated state verifies that the guard never executes inspected input and that the decision is unchanged when the optional sink is absent or fails. Every refusal must omit command text and operands. Covers FR-003.

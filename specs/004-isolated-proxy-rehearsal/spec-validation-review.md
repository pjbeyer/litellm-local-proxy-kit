# Independent review of the revised rehearsal specification

- **Reviewed revision**: `5877221` (`sha256:0b978f4ba435076126ebfe709af16b440c725fecea9b1044ff7fb673ed4edff2`)
- **Reviewer**: Independent read-only Codex subscription session
- **Decision on reviewed bytes**: Changes requested

The reviewer confirmed the earlier containment, runtime/listener attribution, and configuration identity improvements. Three remaining gaps affect planning:

1. **Review-to-execution binding**: Holding inputs stable after loading is not enough. Compare the inputs read for the run with the operator-reviewed identities before executing candidate code, and test a substitution between review and invocation.
2. **Loss of the rehearsal runner**: The cleanup authority must outlive loss of the candidate-bearing worker, not just the front-end CLI. Create the run record before execution and require closure of the entire owned scope, including descendants. Detect an unresolved record on a subsequent invocation before starting a new candidate.
3. **Coexistence decision**: Require the same managed listener identity at all observations, the same before/after managed artifact digests, and a defined comparison for observed controller state. If a concurrent change precludes attribution, the acceptance result is inconclusive rather than a pass.

These comments tighten the existing boundary; they do not add live authority. An independent read-only Codex re-review **approved** the amended spec (`sha256:586ae2f627c93e1527d6374736d1b7b90147820b870659f957ec90e1d7444161`) and verified the three remediations against the amended requirements and acceptance criteria.

Operator comprehension responses remain private. The attempt to write the task-local Spec Validate receipt for the earlier revision was refused by the lifecycle guard. No successful validation write or cleared gate is recorded. Preserve unchanged quiz responses and validate the changed predicates against the amended bytes before implementation.

# Red team: isolated LiteLLM rehearsal

- **Session**: RT-004-isolated-proxy-rehearsal-2026-11-26
- **Target**: [`spec.md` at original review commit](https://github.com/pjbeyer/litellm-local-proxy-kit/blob/f4225a7ac8d83ddb6fc4c89a5d906c49eaaf8e2b/specs/004-isolated-proxy-rehearsal/spec.md)

The Flex-backed delegation returned no findings because the provider rejected all four requests on budget. Four independent, read-only OpenAI Codex review runs then completed, one per lens in [the catalog](red-team-lenses.yml). The primary fidelity reviewer reported that its own secondary reviewer did not start; no second opinion is credited for that lens. Neither reviewer path exercised the live service or changed code. Counts below are primary findings, including overlaps. No approval or implementation credit is implied.

## Findings and recommended dispositions

| ID | Lens | Severity | Evidence and gap | Accepted disposition |
| --- | --- | --- | --- | --- |
| F-001 | Containment and spend | HIGH | FR-003 names the candidate, not all helpers and fixtures; SC-003 does not require coverage across them. | `spec-fix`: apply deny-by-default egress to all rehearsal execution with only run-owned local connections, and require negative boundary evidence. |
| F-002 | Containment and spend | HIGH | FR-003 says before startup, leaving rendering and verification with possible ambient credentials. | `spec-fix`: enter a clean, credential-inaccessible boundary before processing candidate inputs or invoking candidate-bearing helpers; probe with synthetic credential markers. |
| F-003 | Containment and spend | MEDIUM | FR-005 reports uncertainty but does not require the boundary to outlive an interrupted or failed teardown. | `spec-fix`: keep containment until the teardown owner proves all rehearsal resources are gone; otherwise report incomplete and retain evidence. |
| F-004 | Runtime identity and config fidelity | HIGH | FR-002 invokes provenance and sentinel checks; the [native-runtime contract](../../docs/native-runtime-artifact-contract.md) and sentinel require candidate-code execution. FR-003's boundary starts too late as written. | `spec-fix`: require the same prior boundary as F-002; preserve provenance-before-sentinel ordering. |
| F-005 | Runtime identity and config fidelity | HIGH | FR-002, FR-004, and FR-006 do not bind proven runtime inputs to the owned process serving probe responses. | `spec-fix`: launch from the same sealed verified runtime; compare proof before and after, preserve native import evidence where exercised, and reject drift. |
| F-006 | Runtime identity and config fidelity | MEDIUM | FR-006 does not define whether the effective identity includes launch settings; the [managed-service guide](../../docs/macos-managed-service.md) distinguishes rendered YAML and runtime environment. | `spec-fix`: bind the result to distinct proposal and launch configuration identities; include sanitized settings, substitutions and exclusions. |
| F-007 | Lifecycle and attribution | HIGH | FR-005 leaves supported interrupts and the teardown owner after runner loss undefined. A later invocation has no specified path for reporting an incomplete run. | `spec-fix`: define ordinary interrupt coverage, use a teardown owner that survives runner loss, and retain a run record for unresolved cleanup. Shares the containment rule in F-003. |
| F-008 | Lifecycle and attribution | HIGH | FR-004 does not say whether listener ownership must hold for every probe, allowing one early binding to stand for later observations. | `spec-fix`: attribute every accepted result to the same process instance and endpoint; fail on missing or uncertain attribution. |
| F-009 | Lifecycle and attribution | MEDIUM | FR-002 and FR-006 do not require input identity at launch to match input identity at verification. | `spec-fix`: use F-005 for runtime identity and F-006 for effective config identity; prohibit mid-run input drift. |
| F-010 | Governance and workflow | MEDIUM | FR-001 names a reviewed build but not who authorizes the exact overlay/runtime pairing or unattended operation. | `spec-fix`: the operator approves and explicitly initiates the exact fixture-only proposal; no unattended use in this slice. |
| F-011 | Governance and workflow | MEDIUM | Story 1 calls for independent observation, but SC-002 only requires before/after snapshots. | `spec-fix`: require a read-only observer outside the runner before, during and after; incomplete or ambiguous attribution cannot pass acceptance. Do not claim point samples prove continuous integrity. |
| F-012 | Governance and workflow | MEDIUM | FR-002/FR-003 leave credential isolation during renderer, proof and teardown helpers implicit; the scope note prohibits the managed resolver path without an explicit phase invariant. | `spec-fix`: use the boundary from F-001/F-002/F-004 throughout; require evidence the success path works without access to managed inputs or credential stores. |

## Rejected and verified sound

No primary finding was rejected. The overlap is recorded by ID so it does not produce duplicate controls. We checked the reported gaps against the reviewed spec and the linked native/runtime contracts. The reviews confirmed that the draft requires an explicit candidate, makes successful teardown part of passing, does not authorize real-provider traffic or promotion, and leaves the managed restart controller independent.

## Gate state

The operator approved the proposed amendments. All recommendations were accepted as `spec-fix` and applied to the forward draft contract; overlapping findings share the same control. The original review remains bound to its linked commit. This resolves the findings for planning, not proof of the runtime behavior or completion of operator comprehension. The full workflow still requires Spec Validate on the revised spec, plan and task reviews, and the pre-implementation gate. Keep task-local tooling and private responses out of the product branch.

```yaml
primary_review_runs: 4
findings: 12
high: 6
medium: 6
spec_fix: 12
unresolved: 0
```

# Plan and task security review: isolated rehearsal

Two independent read-only Codex subscription reviews evaluated the initial plan and tasks. The plan pass reported five high and one medium design findings; the tasks pass reported one high and two medium, including overlaps. These were document reviews, not evidence that a backend or candidate ran.

## Dispositions

| Source | Severity | Finding | Disposition and verification task |
| --- | --- | --- | --- |
| Plan 1, Tasks 1 | High | No host NIC does not establish guest-local admission. | Require trusted supervisor, explicit guest-local policy, unprivileged worker and pre-input admission probes. Prove descendant denial with a disposable guest-local sentinel on the actual backend (T002, T005, T009). |
| Plan 2 | High | Input digests inside an unpinned manifest do not bind operator intent or host read scope. | Independently pin exact manifest bytes, restrict host staging to reviewed regular files in designated roots, and carry the same verified bytes into the guest. Add manifest-plus-hash substitution and path tests (T003, T007). |
| Plan 3 | High | Verified package digest was not tied to the prepared runtime tree. | Verify a closed runtime inventory before candidate import and keep it immutable through runtime/probe continuity; exercise a valid bundle with different prepared files (T006). |
| Plan 4, Tasks 3 | High / Medium | A schema-valid guest result is not sufficient evidence of who generated it or what ran. | Restrict the guest reporting channel to the supervisor; require host comparison against the admitted scope and every required identity and check. Reject plausible mismatched receipts (T002, T003, T005, T010). |
| Plan 5, Tasks 2 | High / Medium | Allocation and owner loss had a gap before recoverable identity or terminal closure. | Reserve a durable namespace before allocation, name objects at creation, attest closure before releasing the record, and block the next invocation on uncertainty. Test loss before handle return, after boot and at finalization (T004, T005, T009). |
| Plan 6 | Medium | Environment-only identity acceptance lacked a typed input. | Bind the resolved Boolean `fixture_trace` and the fixture content identity to the effective manifest; check an otherwise identical rehearsal with the other Boolean value (T006, T010). |

The reviewer confirmed separation from controller authority, proof-before-sentinel order, closure as a success condition, and the fixture-first task DAG. All original findings have corresponding changes in the [backend and CLI contract](contracts/rehearsal.md) and [task list](tasks.md). The proposed backend, including a guest-local policy and ownership under interruption, remains to be demonstrated. Do not claim the safety boundary or acceptance is proven from document review.

## Two-tier amendment review

Four independent read-only Codex runs reviewed the eight-file normative packet identified by its repository-relative sorted checksum-manifest SHA-256, `a29161459f7cc459cbfe43774a11e559f8bc5cb611cdbd7dbf33aa26402d14c1`. The reports matched that packet and its spec and task hashes. This summary is not a Spec Validate approval or authorization record; see the [plan’s entry gates](plan.md#gates-and-evidence).

- **Containment:** No material findings in host admission, offline execution, or separation from managed state.
- **Identity:** No material findings in trusted rendering, fixture mapping, or evidence attribution.
- **Lifecycle:** No material findings in reservation, teardown, or durable settlement.
- **Governance:** No material findings in the advisory result or separation of implementation and qualification authority.

Structural checks passed for eight functional requirements, six success criteria, and fourteen tasks. This review covers the documents, not the implementation or backend. Owner comprehension validation of the spec and tasks, separate implementation authorization, and runtime qualification remain open.

## Historical cross-artifact analysis

At the initial review revision, the plan kept the same seven functional requirements and four acceptance criteria as the spec. The task verifier covered those requirements with forward-only dependencies. This paragraph is not coverage evidence for the two-lane amendment. Disposable backend admission gates candidate startup; failure evidence and fixture regression gates the different-build run. Spec Validate and owner plan approval are preconditions on execution, not task completions or acceptance evidence.

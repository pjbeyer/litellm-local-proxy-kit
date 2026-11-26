# Plan and task security review: isolated rehearsal

Two independent read-only Codex subscription reviews evaluated the initial plan and tasks. The plan pass reported five high and one medium design findings; the tasks pass reported one high and two medium, including overlaps. These were document reviews, not evidence that a backend or candidate ran.

## Dispositions

| Source | Severity | Finding | Disposition and verification task |
| --- | --- | --- | --- |
| Plan 1, Tasks 1 | High | No host NIC does not establish guest-local admission. | Require trusted supervisor, explicit guest-local policy, unprivileged worker and pre-input admission probes. Prove descendant denial with a disposable guest-local sentinel on the actual backend (T001, T004, T008). |
| Plan 2 | High | Input digests inside an unpinned manifest do not bind operator intent or host read scope. | Independently pin exact manifest bytes, restrict host staging to reviewed regular files in designated roots, and carry the same verified bytes into the guest. Add manifest-plus-hash substitution and path tests (T002, T006). |
| Plan 3 | High | Verified package digest was not tied to the prepared runtime tree. | Verify a closed runtime inventory before candidate import and keep it immutable through runtime/probe continuity; exercise a valid bundle with different prepared files (T005). |
| Plan 4, Tasks 3 | High / Medium | A schema-valid guest result is not sufficient evidence of who generated it or what ran. | Restrict the guest reporting channel to the supervisor; require host comparison against the admitted scope and every required identity and check. Reject plausible mismatched receipts (T001, T002, T004, T009). |
| Plan 5, Tasks 2 | High / Medium | Allocation and owner loss had a gap before recoverable identity or terminal closure. | Reserve a durable namespace before allocation, name objects at creation, attest closure before releasing the record, and block the next invocation on uncertainty. Test loss before handle return, after boot and at finalization (T003, T004, T008). |
| Plan 6 | Medium | Environment-only identity acceptance lacked a typed input. | Bind the resolved Boolean `fixture_trace` and the fixture content identity to the effective manifest; check an otherwise identical rehearsal with the other Boolean value (T005, T009). |

The reviewer confirmed separation from controller authority, proof-before-sentinel order, closure as a success condition, and the fixture-first task DAG. All original findings have corresponding changes in the [backend and CLI contract](contracts/rehearsal.md) and [task list](tasks.md). The proposed backend, including a guest-local policy and ownership under interruption, remains to be demonstrated. Do not claim the safety boundary or acceptance is proven from document review.

## Cross-artifact analysis

The plan keeps the same seven functional requirements and four acceptance criteria as the spec. The task verifier covers all of them with forward-only dependencies. Disposable backend admission gates candidate startup; failure evidence and fixture regression gates the different-build run. Spec Validate and owner plan approval are preconditions on execution, not task completions or acceptance evidence.

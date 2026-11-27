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

## Stdin bundle amendment — draft reviewed

A read-only Codex review of the documentation diff reported no actionable defects. At review, the eight-file normative packet had sorted checksum-manifest SHA-256 `161f09870cdd34821f466f1310777bdda87906074b0ee620649c4f05f6ca5d83`. This is draft review evidence, not Spec Validate approval or authority to implement. The earlier reviews below do not cover this revision; do not carry forward earlier approvals or waivers.

The bundle removes proposal path traversal from the routine checker; it does not qualify the startup boundary. The workstation feasibility investigation did not establish that the verified Python executable and startup dependencies were the objects actually loaded. The configured dependency came from a writable development environment, and a preflight hash of a pathname did not establish continuity into execution. Keep both entry points unwired until that prerequisite is met or a revised startup contract is reviewed and authorized. Native backend qualification remains separate and was not attempted.

## Read-only toolchain feasibility — not qualified

A disposable UDRO disk image on Apple Silicon macOS contained CPython 3.11.15, its standard library, and PyYAML 6.0.4, copied from the local development tooling for this experiment only. In two offline runs, all 818 regular-file SHA-256 digests matched the staging inventory, the mount rejected file creation with `EROFS`, and an isolated Python launch loaded its file-backed Python modules from the mount. The probe exercised YAML, TOML, and SHA-256 on synthetic data and imported PyYAML's native extension. Mounted-tree inventory and smoke startup took 0.46–0.64 seconds, excluding image creation and attachment, full render work, and OS dependency verification. The image was detached and the disposable tree removed after each run.

The native image listing contained two images under the disposable mount, three under OS paths with regular files, and 393 under OS paths without corresponding regular files. This is a post-start observation, not a byte inventory of loaded images. The exercise did not verify the backing macOS dynamic-loader cache against an independently provisioned trust record before Python started. It also did not establish that an ordinary check invocation could not replace the mounted toolchain, its backing image, or the path to it between verification and launch. The staging inventory is a copy-integrity control, not a trusted installation record. Neither a read-only mount nor a post-launch image listing satisfies startup continuity on its own.

Keep T012 and both routine entry points blocked. To preserve the current contract, qualify a separately provisioned, write-protected install and a pre-launch method that binds the approved exact-byte OS dependency inventory to the loader cache and other native objects used by the child. Refresh that qualification after changes to the toolchain or host OS. Require refusal before interpreter startup when the trust record, mount identity, or OS dependency identity cannot be established. This was a feasibility probe, not Spec Validate approval, end-to-end latency evidence, or a native rehearsal run.

## Loader-cache diagnostic — startup continuity still unproved

Two Python interpreters on the same Apple Silicon workstation reported the same active shared-cache UUID after startup. It matched the backing file’s header UUID, and both reported paths named that same file. This is agreement between two consumers of one host cache, not independent attestation. Apple’s [dyld header](https://github.com/apple-oss-distributions/dyld/blob/c8a445f88f9fc1713db34674e79b00e30723e79d/include/mach-o/dyld_priv.h#L429-L435) describes the UUID API as an observation of the **current process**. The backing file was root-owned on a volume not mounted read-only. That metadata alone does not establish what the platform verified or what a later launch will load.

The probe compared the main header UUID, not exact content. It did not identify the complete loader input set, including any companion files (the metadata count excluded `.map` and `.atlas` files), prove pre-launch selection or continuity, or measure cold-cache or whole-call latency. Neither the UUID nor a directory listing belongs in the trust record. An install protected from writes by the check user would address only part of the gap: an updater can still replace an ordinary package-manager install, and Apple platform controls do not cover third-party Python, PyYAML, or kit code.

Under the current contract, keep T012 and both routine entry points blocked. Do not provision an install just to retest storage. Qualification still needs an independently provisioned startup inventory, evidence that startup consumes those same verified objects, a refusal test for a changed startup dependency before the interpreter runs, and a complete run within the 10-second deadline. Treating the Apple loader and cache as a declared platform trust root instead of measuring their exact bytes would be a separate, owner-approved contract change. It would not remove exact-byte verification or continuity for the kit, Python distribution and PyYAML, and this diagnostic does not establish the platform assurance needed for that change. Spec Validate remains blocked on the current spec digest; this note changes no requirements and grants no approval.

## Historical two-tier amendment review

Four independent read-only Codex runs reviewed the eight-file normative packet identified by its repository-relative sorted checksum-manifest SHA-256, `a29161459f7cc459cbfe43774a11e559f8bc5cb611cdbd7dbf33aa26402d14c1`. The reports matched that packet and its spec and task hashes. This summary is not a Spec Validate approval or authorization record; see the [plan’s entry gates](plan.md#gates-and-evidence).

- **Containment:** No material findings in host admission, offline execution, or separation from managed state.
- **Identity:** No material findings in trusted rendering, fixture mapping, or evidence attribution.
- **Lifecycle:** No material findings in reservation, teardown, or durable settlement.
- **Governance:** No material findings in the advisory result or separation of implementation and qualification authority.

Structural checks passed for eight functional requirements, six success criteria, and fourteen tasks. This review covers the documents, not the implementation or backend. Owner comprehension validation of the spec and tasks, separate implementation authorization, and runtime qualification remain open.

## Historical cross-artifact analysis

At the initial review revision, the plan kept the same seven functional requirements and four acceptance criteria as the spec. The task verifier covered those requirements with forward-only dependencies. This paragraph is not coverage evidence for the two-lane amendment. Disposable backend admission gates candidate startup; failure evidence and fixture regression gates the different-build run. Spec Validate and owner plan approval are preconditions on execution, not task completions or acceptance evidence.

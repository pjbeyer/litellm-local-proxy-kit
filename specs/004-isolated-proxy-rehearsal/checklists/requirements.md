# Requirements quality checklist: isolated rehearsal

**Purpose**: Review the safety and evidence requirements, not implementation status. A checked item records requirements inspection only, not runtime evidence or operator approval.

## Boundary and authorization

- [x] CHK001 Are candidate inputs, their trust assumptions, and prohibited fallbacks specified? [Spec FR-001]
- [x] CHK002 Is separation from the managed controller, authorization, and activation clear and consistent? [Spec FR-007, SC-004]
- [x] CHK003 Does the spec distinguish enforceable isolation from configuration conventions and define the unavailable-isolation result? [Spec FR-003]
- [x] CHK004 Are credential access, provider spend and local dependency boundaries explicit? [Spec FR-003]

## Fidelity and lifecycle

- [x] CHK005 Do the requirements distinguish the proposed configuration, exercised configuration, and untested features? [Spec FR-002, FR-004, FR-006, FR-007]
- [x] CHK006 Is the binding between the verified build and tested listener specified? [Spec FR-002, FR-004]
- [x] CHK007 Do the requirements cover ambiguous ownership, failed probes, interruption and failed teardown? [Spec FR-004, FR-005]

## Acceptance and evidence

- [x] CHK008 Are report fields, failure behavior and prohibited sensitive fields defined? [Spec FR-006]
- [x] CHK009 Can the acceptance criteria distinguish real patched runtime evidence from fixture results? [Spec SC-001]
- [x] CHK010 Is the unchanged-managed-service claim independently measurable, with concurrent changes dispositioned? [Spec SC-002]
- [x] CHK011 Are negative acceptance cases and non-regression obligations enumerated? [Spec SC-003, SC-004]
- [x] CHK012 Is the routine input contract independent of native artifacts and explicit about bounded offline rendering and optional comparison? [Spec FR-001, FR-008, SC-005]
- [x] CHK013 Are advisory results and native receipts mutually exclusive, with no fallback or authority and independently testable routine acceptance? [Spec FR-006, FR-007, FR-008, SC-005, SC-006]
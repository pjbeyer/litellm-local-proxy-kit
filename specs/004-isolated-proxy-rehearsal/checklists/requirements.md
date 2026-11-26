# Requirements quality checklist: isolated rehearsal

**Purpose**: Review the safety and evidence requirements, not implementation status. A checked item means the reviewer found the requirement adequate.

## Boundary and authorization

- [ ] CHK001 Are candidate inputs, their trust assumptions, and prohibited fallbacks specified? [Spec FR-001]
- [ ] CHK002 Is separation from the managed controller, authorization, and activation clear and consistent? [Spec FR-007, SC-004]
- [ ] CHK003 Does the spec distinguish enforceable isolation from configuration conventions and define the unavailable-isolation result? [Spec FR-003]
- [ ] CHK004 Are credential access, provider spend and local dependency boundaries explicit? [Spec FR-003]

## Fidelity and lifecycle

- [ ] CHK005 Do the requirements distinguish the proposed configuration, exercised configuration, and untested features? [Spec FR-002, FR-004, FR-006, FR-007]
- [ ] CHK006 Is the binding between the verified build and tested listener specified? [Spec FR-002, FR-004]
- [ ] CHK007 Do the requirements cover ambiguous ownership, failed probes, interruption and failed teardown? [Spec FR-004, FR-005]

## Acceptance and evidence

- [ ] CHK008 Are report fields, failure behavior and prohibited sensitive fields defined? [Spec FR-006]
- [ ] CHK009 Can the acceptance criteria distinguish real patched runtime evidence from fixture results? [Spec SC-001]
- [ ] CHK010 Is the unchanged-managed-service claim independently measurable, with concurrent changes dispositioned? [Spec SC-002]
- [ ] CHK011 Are negative acceptance cases and non-regression obligations enumerated? [Spec SC-003, SC-004]

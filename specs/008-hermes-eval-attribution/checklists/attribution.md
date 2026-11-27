# Attribution Requirements Checklist

**Purpose**: Review whether the attribution requirements are complete and testable before implementation.
**Created**: 2026-11-24
**Feature**: [Specification](../spec.md)
**Ownership**: The reviewer marks an item only after accepting the quality of the written requirement. A checkmark does not qualify a transport.

## Identity and trust

- [ ] CHK001 Are the evidence for physical exchange identity and the evidence for terminal acceptance distinguished? [Clarity, FR-005, FR-006]
- [ ] CHK002 Are the two durable row identities and proof of same-UID replacement or mutation specified? [Coverage, FR-006]
- [ ] CHK003 Is proxy/client provenance independent of attribution metadata, including when a caller supplies a call ID? [Consistency, FR-003, FR-005]
- [ ] CHK004 Are final dispatch, fallback, redirects and upstream forwarding in the non-egress boundary? [Completeness, FR-002, FR-004, SC-003]

## Coverage and verdict

- [ ] CHK005 Are absent roster outcomes, absent expected SpendLogs and incomplete page reads distinguished from a complete cohort with no eligible samples? [Clarity, FR-008, FR-009]
- [ ] CHK006 Are exclusions defined by affirmative evidence, and is the evidence for excluding a roster member by tier specified? [Coverage, FR-008]
- [ ] CHK007 Are expiry, revision mutation, conflict and disablement covered at the same verdict point? [Consistency, FR-006, FR-007, FR-009, FR-010]
- [ ] CHK008 Is retryable visibility lag distinguished from ambiguous evidence with a bounded retry budget? [Measurability, FR-009, SC-002]

## Operational boundary

- [ ] CHK009 Are disabled mode, residual native SpendLog tags and stale generations accounted for without interrupting inference? [Recovery, FR-007, FR-010, SC-004]
- [ ] CHK010 Are the required endpoints, advertised modes and unsupported-path disposition independently testable? [Dependencies, FR-011, SC-003]
- [ ] CHK011 Do readiness, live authorization and scoring remain distinct, and are sanitized receipts required? [Consistency, FR-010, FR-011, FR-012]

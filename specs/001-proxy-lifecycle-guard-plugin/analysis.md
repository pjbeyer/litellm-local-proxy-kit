# Spec 001 recovery and review status

The [follow-up draft](spec.md) defines the proposed changes, not a complete specification of the existing guard. It does not replace the [operational runbook](../../integrations/hermes/proxy-lifecycle-guard/README.md). The archived revision 5a and the later recovery candidate are separate historical records. Neither is imported here. Their plans, task checkmarks, and test results do not establish approval or conformance of this draft.

## Review dispositions

- **Batch count (historical FR-007):** The draft requires affirmative safety for each item. A valid envelope alone does not establish what its contents will do. The implementation finding remains open.
- **Assignment analysis:** The previous rewrite was not merged. The draft specifies the required decision for executable content and for literal data. Neither runtime finding is closed.
- **Integrity documentation:** The runtime compares installed files with owner-recorded digests, not with a kit checkout. This is existing behavior, not part of the proposed runtime change.

## Gates

- Before a new implementation plan, review the exact draft and record the disposition of independent findings. A review of an earlier revision does not transfer.
- Before changing guard code, complete the plan and task reviews, the operator's Spec Validate checks, and the applicable independent approval for the exact revision. A fresh checkout has no Spec Validate extension or approval record for this draft. That is a blocker, not a waiver.
- Before a live probe or activation, obtain separate owner authorization. Draft review and passing offline tests do not authorize either action.

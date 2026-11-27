# Peer Review

## Command: `speckit.spec-validate.review [spec|tasks]`

Record independent review for one validated artifact. Default to `spec`. Never use one artifact’s approval for the other.

1. Resolve the active feature from `.specify/feature.json` and load the local record with `read-approval-state.sh`. Select the applicable fields:

   | Artifact | Validated digest | Review status | Reviewed digest | Private validator |
   | --- | --- | --- | --- | --- |
   | `spec.md` | `spec_hash` | `review_status` | `reviewed_spec_hash` | `validated_by` |
   | `tasks.md` | `tasks_hash` | `tasks_review_status` | `reviewed_tasks_hash` | `tasks_validated_by` |

2. Confirm the artifact’s comprehension validation is complete and the reviewer is a different person from the recorded validator. If independence cannot be established, stop. Only pending or overdue reviews can be decided by this command. A legacy approval lacking a matching reviewed digest must be reviewed again, not backfilled. A request for changes requires addressing the feedback and validating the revised artifact.
3. Read and hash the complete artifact to be presented. If the digest is missing or differs from the stored validation hash, stop and request fresh validation. Show the artifact, missed item IDs, first-attempt accuracy from the relevant counts, and existing review notes. Do not show private selections or analytics.
4. Ask the reviewer to **approve**, **request changes**, or **add notes**. Before saving any decision, recheck the artifact hash and confirm the current record still refers to the same validation. Re-read and merge into the current record; preserve the other artifact’s fields.
   - **Approve:** set the selected review status to `approved` and its reviewed digest to the verified digest.
   - **Request changes:** set the selected review status to `changes-requested`, remove its reviewed digest, and record the comments. For tasks, also remove `task_review_override`.
   - **Add notes:** record the notes without changing the review decision or digest.
   - Store reviewer, timestamp and comments in `reviewer`, `reviewed_at` and `review_comments` for the spec, or the corresponding `tasks_` fields for tasks. Keep the legacy `approval_status` field `blocked`; it is not the current verdict.
5. Write only to the ignored local record with `write-approval-state.sh`. An overdue review stays blocked until a decision; it is not self-approval. Report the decision and use the gate to report overall implementation eligibility.

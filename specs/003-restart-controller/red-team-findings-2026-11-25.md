# Pre-plan review — 2026-11-25

Read-only independent Codex review of spec SHA-256 `f2b71c3aa9f2e1b59553167fb8d936753158a0d68b7a148a846a659d921583ce` using the four [lenses](red-team-lenses.yml). The reviewer independently confirmed the digest. Review was for the draft, not approval of later edits or live operations. The CLI also read its own workflow instructions; it did not test or inspect installed services.

## Medium findings

- **RT-01 — Trust boundary / race and staleness (`spec-fix`).** The receipt is a local record, not immutable storage (`install_restart_controller.py`, `atomic_json`). An approval's UID check is not an independent human identity check (`engine.py`, `_approval`). Clarified both and retained the explicit no-atomic-snapshot limitation.
- **RT-02 — Configuration and documentation drift (`spec-fix`).** Give the existing proxy verify and controller install verify distinct names and scope; place contract targets in the feature package. Runtime `freeze` reads additional deployment, proxy, and native facts. Require the proposed check to label its narrower scope expressly.

## Rejected

The review also claimed `make apply` returns before installer verification. The source returns early only if staging fails; on success it runs `--verify`. `test_preview_and_apply_delegate_separately` tests both branches. Keep the behavior and clarify that it does not run the separate drift checker.

## Verified sound

The draft separates on-disk observations from qualification and owner approval, denies checkout substitution, acknowledges races between reads, and does not imply historical Spec Kit approval.

## Net effect

Both wording/ownership findings were dispositioned. The owner comprehension gate and exact-candidate implementation review remain separate; this review does not clear either.

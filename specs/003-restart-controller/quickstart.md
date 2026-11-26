# Quickstart: offline verification

Use a dedicated kit worktree. For the real workflow, use [the operations runbook](../../docs/restart-controller-operations.md), not this test checklist.

1. Run `make help` without a policy. It must list the distinct tasks without contacting a service.
2. In a test environment with Python, pytest and PyYAML, run `python3 -m pytest -q tests/test_owner_maintenance.py`. Fixtures provide their own policy, installer, installed client and HTTP result; they never use the installed deployment. For full regression, use the repo's `tests` workflow and a short, real, private `TMPDIR`.
3. With the shared Spec Kit tooling available, select `specs/003-restart-controller` in this worktree only, then run `bash .specify/scripts/bash/check-prerequisites.sh --json --require-spec --require-tasks --include-tasks`. Check that it resolved this feature. Run the two identifier verifiers described in the [index](../README.md). If the tooling is absent, record that prerequisite as unavailable; do not treat a missing gate as a pass.

None of these steps validates a live daemon, canary or owner approval.

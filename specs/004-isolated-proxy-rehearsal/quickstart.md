# Validation quickstart: isolated rehearsal

This document describes planned commands and release checks, not a working command on the planning branch.

1. **Before product code:** review the plan and tasks; run the task-local Spec Validate gate and require `allowed`. Make no changes to the installed proxy, controller, or shared Spec Kit scaffold.
2. **Offline contract suite:** run `uv run --no-project --with pytest --with pyyaml python -m pytest -q tests/test_rehearsal_cli.py tests/test_rehearsal_supervisor.py tests/test_rehearsal_worker.py tests/test_owner_maintenance.py` after implementation. Expect exact manifest/refusal outcomes and the existing owner-maintenance targets to remain green.
3. **Prove the backend before candidate use:** use an approved offline guest, inert test bundle and disposable sentinels. Verify the actual device configuration, guest access denials, cleanup ownership and refusal on incomplete runs. A fixture or configured-but-unbooted host is not proof.
4. **Prove a different real patched build:** stage an explicitly reviewed manifest with exact digests and a provisioned native guest. The operator invokes `make rehearse REHEARSAL_MANIFEST=/path/to/reviewed-rehearsal.json REHEARSAL_SHA256=<reviewed-sha256>`. An independent read-only observer predeclares managed artifacts and records the spec's before/during/after evidence. Expect one `passed` result only if every required check passes and host teardown is proved. Inconclusive managed observations require a controlled rerun; never overwrite active controller state to make them match.

A passing result is not managed restart preparation, production provider evidence, or permission to activate the proposal. See the [contract](contracts/rehearsal.md) for outputs and exit codes.

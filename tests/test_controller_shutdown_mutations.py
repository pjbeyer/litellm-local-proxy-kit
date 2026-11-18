"""T023: executed gate mutation battery — falsify the shutdown/recovery gates.

Each battery case mutates ONE gate anchor in a DISPOSABLE CLONE of this
source tree and requires:
  1. green control     — the paired pinning test passes in the clean clone;
  2. compiled mutation — the mutated module imports cleanly (an import or
                         setup failure does NOT count as a kill);
  3. intended kill     — the paired pinning test fails with an assertion
                         (behavioral), not an error (import/setup);
  4. restored green    — the clean clone still passes afterwards.

A missing source anchor, an escaped mutation (test still green), or a
non-behavioral kill is a FAILED verification. This file runs the
battery directly (subprocess per case) and archives the actual counts.

This file is executed explicitly (python tests/test_controller_shutdown_mutations.py);
it defines no test functions, so normal suite collection collects zero tests
from it and the battery never runs implicitly.
No real launchctl, no live services, no credentials; clones live under
TMPDIR and are removed after each case.
"""
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

# Each case: (name, file, anchor, replacement, paired test)
# The paired tests are the existing pinning tests for each gate; the
# mutation must break the BEHAVIOR they assert, and they must pass on
# the clean clone first (green control).
CASES = [
    # shutdown.py: class Shutdown — the active-handle barrier input:
    # checkpoint must refuse normal work once the latch is requested.
    # Both the admission read AND the post-clock recheck are removed.
    ("shutdown_latch_checkpoint",
     "core/restart_control/shutdown.py",
     '    def checkpoint(self):\n'
     '        """Admit normal work before shutdown; refuse it after the latch."""\n'
     '        with self._lock:\n'
     '            if self._request_state:\n'
     '                raise ShutdownRequested("controller_stopping")\n'
     '            self._now()\n'
     '            if self._request_state:\n'
     '                raise ShutdownRequested("controller_stopping")\n',
     '    def checkpoint(self):\n'
     '        """Admit normal work before shutdown; refuse it after the latch."""\n'
     '        with self._lock:\n'
     '            self._now()\n',
     "tests/test_controller_interruption.py::InterruptionMatrixTest::test_latch_before_prepare_blocks_with_zero_calls"),

    # engine.py: restart admission — the consumed marker plus phase check
    # must refuse a replayed completed request; weakening either leg lets
    # the pinning test's second restart through (a second stop call).
    ("engine_replay_refusal",
     "core/restart_control/engine.py",
     '        if (plan.get("phase") != "prepared" or (self.approvals / (request_id + ".consumed")).exists()):\n'
     '            raise Fault("replayed")\n',
     '        if plan.get("phase") not in ("prepared", "restarted"):\n'
     '            raise Fault("replayed")\n',
     "tests/test_restart_controller.py::ControllerTest::test_approval_required_then_success_and_replay"),

    # recovery.py: spawn_intent — persist the intent BEFORE any outside
    # action; removing the durable write must break the durability test
    # (the outside action reads the intent row from a fresh store).
    ("recovery_spawn_intent_durable_write",
     "core/restart_control/recovery.py",
     '            self._write_spawn(row)\n'
     '            handle = SpawnHandle(spawn_id, ticket.request_id, ticket.binding, purpose)\n',
     '            handle = SpawnHandle(spawn_id, ticket.request_id, ticket.binding, purpose)\n',
     "tests/test_controller_recovery.py::test_spawn_intent_is_durable_before_outside_action_and_identity_follows"),

    # recovery.py: arm_mutation — the fence must be durable and sync'd
    # before primary control; removing the fence write breaks the fence
    # durability test.
    ("recovery_fence_sync_write",
     "core/restart_control/recovery.py",
     '            fence = {"version": 1, "binding": row["binding"].copy(),\n'
     '                     "phase": "restart_pending", "state": "armed"}\n'
     '            try:\n'
     '                self._atomic_json(self.recovery / "mutation.json", fence)\n',
     '            fence = {"version": 1, "binding": row["binding"].copy(),\n'
     '                     "phase": "restart_pending", "state": "armed"}\n'
     '            try:\n'
     '                pass\n',
     "tests/test_controller_recovery.py::test_mutation_pending_and_fence_are_durable_before_outside_control"),

    # engine.py: restart admission — an expired prepared plan must refuse
    # (plan_stale); removing the dual-clock expiry check lets the expired
    # restart through and the pinning test sees a wrong code.
    ("engine_plan_expiry_check",
     "core/restart_control/engine.py",
     '        if plan.get("controller_epoch") != self.epoch or wall >= plan.get("expires_at", -1) or mono >= plan.get("expires_monotonic", -1):\n'
     '            raise Fault("plan_stale")\n',
     '        if plan.get("controller_epoch") != self.epoch:\n'
     '            raise Fault("plan_stale")\n',
     "tests/test_restart_controller.py::ControllerTest::test_epoch_expiry_and_unmanaged"),

    # recovery.py: _check_active — admission must bind the EXACT active
    # journal ticket by identity; accepting any ticket object lets a
    # refreshed or replaced ticket pass and the exact-active pinning
    # test survives.
    ("recovery_exact_active_ticket",
     "core/restart_control/recovery.py",
     '    def _check_active(self, ticket):\n'
     '        _require(ticket is self._active_ticket)\n'
     '        return self._check_ticket(ticket)\n',
     '    def _check_active(self, ticket):\n'
     '        _require(ticket is not None)\n'
     '        return self._check_ticket(ticket)\n',
     "tests/test_controller_recovery.py::test_refreshed_ticket_for_unrelated_owned_row_preserves_exact_active"),

    # runtime.py: qualification — the evidence scope must bind the
    # runtime's exact class (fixture vs intended-controller); accepting
    # every scope lets fixture evidence masquerade as production.
    ("runtime_qualification_scope_binding",
     "core/restart_control/runtime.py",
     "            accepted_scopes = (FIXTURE_EVIDENCE_SCOPES if self.fixture\n"
     "                               else {PRODUCTION_EVIDENCE_SCOPE})\n",
     "            from .canary import EVIDENCE_SCOPES as accepted_scopes\n",
     "tests/test_controller_qualification.py::test_canary_evidence_file_bindings_fail_closed"),
]


def _run(clone, test, quiet=True):
    """Run one pytest node in the clone; return (exit_code, output)."""
    command = [PYTHON, "-B", "-m", "pytest", *test.split(), "-x", "-q"]
    quiet = quiet
    import os
    environment = {"TMPDIR": str(clone/"tmp"), "PATH": "/usr/bin:/bin",
                   "HOME": str(clone/"home")}
    (clone/"tmp").mkdir(parents=True, exist_ok=True)
    (clone/"home").mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, cwd=clone, capture_output=True,
                               text=True, env=environment)
    return completed.returncode, (completed.stdout + completed.stderr)


def _apply_mutation(clone, case):
    """Return True when the anchor was found and replaced exactly once."""
    name, file, anchor, replacement, _test = case
    path = clone/file
    text = path.read_text()
    if text.count(anchor) != 1:
        return False
    path.write_text(text.replace(anchor, replacement))
    return True


def _clone_tree():
    """A disposable clone of the source tree (no VCS metadata)."""
    clone = Path(tempfile.mkdtemp(prefix="t023-", dir=Path(
        __import__("os").environ["TMPDIR"])))
    shutil.copytree(ROOT, clone/"tree", ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", "*.pyc", ".venv*"))
    return clone/"tree"


def one_case(case, verbose=False):
    """Execute one battery case; return (killed, reason)."""
    name, file, anchor, replacement, test = case
    clone = _clone_tree()
    try:
        # 1. Green control in the clean clone.
        code, _out = _run(clone, test)
        if code != 0:
            return False, f"{name}: control not green (exit {code})"
        # 2. Compile check: the mutated module must import cleanly.
        if not _apply_mutation(clone, case):
            return False, f"{name}: anchor missing in {file}"
        import py_compile
        try:
            py_compile.compile(str(clone/file), doraise=True)
        except py_compile.PyCompileError as error:
            return False, f"{name}: mutation does not compile: {error}"
        # 3. Intended kill: behavioral failure, not setup/import error.
        code, out = _run(clone, test)
        if code == 0:
            return False, f"{name}: mutation ESCAPED (paired test still green)"
        if ("errors" in out.split("passed")[0] and
                "FAILED" not in out and "AssertionError" not in out):
            return False, f"{name}: non-behavioral kill (collection/setup error)"
        if "1 error" in out or "2 errors" in out or "ERROR" in out:
            return False, f"{name}: non-behavioral kill (error, not failure)"
        if "failed" not in out:
            return False, f"{name}: non-behavioral kill (no test failure)"
        # 4. Restored green: the paired test passes on a FRESH clean
        # clone after the kill — the gate lives only in the mutation.
        fresh = _clone_tree()
        try:
            code, _out = _run(fresh, test)
            if code != 0:
                return False, f"{name}: restored-green rerun failed (exit {code})"
        finally:
            shutil.rmtree(fresh.parent, ignore_errors=True)
        return True, f"{name}: killed (behavioral assertion failure; restored green verified)"
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)


def battery():
    """Run every case; archive counts; return non-zero on any failure."""
    import tempfile
    results = [one_case(case) for case in CASES]
    killed = sum(1 for killed, _ in results if killed)
    print(f"mutation battery: {killed}/{len(CASES)} killed")
    for killed_flag, reason in results:
        marker = "PASS" if killed_flag else "FAIL"
        print(f"  [{marker}] {reason}")
    archive = ROOT/"tests"/"_mutation_battery_counts.txt"
    archive.write_text(
        f"cases={len(CASES)}\nkilled={killed}\n"
        + "".join(f"{name}={'killed' if ok else 'ESCAPED/FAILED'}\n"
                  for (ok, _r), name in zip(results,
                                            [c[0] for c in CASES])))
    print(f"archived: {archive}")
    return 0 if killed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(battery())
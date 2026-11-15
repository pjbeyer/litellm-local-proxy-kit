"""T020: request/control boundary interruption matrix (SC-008).

FIXTURE SCOPE ONLY. Every case runs the genuine fixture rig — real UDS
transport, real retained HTTP children observed over loopback, real
darwin process identity — with the fake launchctl backend. The shared
shutdown latch is injected at every named prepare/restart/rollback
boundary, including after commitment before OS submission, inside a
helper, and around an unknown control outcome. Assertions cover the
exact call trace, original identity/HTTP preservation before
commitment, no false terminal acceptance, and no subsequent service
control after the latch.
"""

import contextlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_restart_lifecycle_fixtures import FixtureRig, _http_get, _spawn


def _latch_on_phase(rig, phase_name):
    """Latch the shared shutdown right after one journal phase lands.

    The adapter's ``phase`` transition is the durable commitment point;
    latching after it records but before the next backend verb mirrors
    a signal arriving between the durable commitment and the OS
    submission that follows it.
    """
    adapter = rig.controller.recovery
    original = adapter.phase

    def phase(ticket, phase, **kwargs):
        result = original(ticket, phase, **kwargs)
        if phase == phase_name:
            rig.shutdown.request()
        return result
    adapter.phase = phase


@unittest.skipIf(sys.platform != "darwin",
                 "genuine birth/PID observation requires the darwin process ABI")
class InterruptionMatrixTest(unittest.TestCase):
    """Inject the shutdown latch at every named lifecycle boundary."""

    def setUp(self):
        probe = self.rig_probe()
        try:
            import socket
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(probe))
        except OSError as exc:
            import errno
            if exc.errno == errno.EPERM:
                raise unittest.SkipTest("sandbox denies Unix socket bind") from None
            raise
        finally:
            with contextlib.suppress(OSError):
                probe.unlink()
        self.rig = FixtureRig()
        self.addCleanup(self.rig.close)

    @staticmethod
    def rig_probe():
        import uuid
        from test_restart_lifecycle_fixtures import _fixture_base
        return _fixture_base() / ("probe-%s.sock" % uuid.uuid4().hex)

    # -- shared assertions ---------------------------------------------------

    def assert_no_service_control(self):
        calls = self.rig.backend.calls
        for verb in ("stop", "start", "restore_lkg"):
            self.assertNotIn(verb, calls, "service control ran: %r" % calls)

    def assert_primary_preserved(self, child):
        """The untouched original is alive and genuinely serving HTTP."""
        self.assertTrue(child.alive(), "original fixture primary died")
        status, body = _http_get(child.port, "/health")
        self.assertEqual((status, body.get("fixture")), (200, True))
        status, body = _http_get(child.port, "/v1/tiny")
        self.assertEqual((status, body.get("completion")), (200, "fixture-ok"))

    # -- request boundary, before any backend work -------------------------

    def test_latch_before_prepare_blocks_with_zero_calls(self):
        rig = self.rig
        rig.shutdown.request()
        result = rig.op({"op": "prepare"})
        # Wire-level admission refusal: no scope surface, zero backend verbs.
        self.assertEqual((result["status"], result["code"]),
                         ("blocked", "controller_stopping"))
        self.assertEqual(rig.backend.calls, [])
        self.assert_no_service_control()
        self.assertEqual(rig.controller.recovery.operations, {})
        self.assertTrue(rig.primary.alive())
        # Status RPC is never fenced by the latch (read-only path).
        self.assertEqual(rig.op({"op": "status"})["status"], "ok")

    def test_latch_before_restart_blocks_prepared_plan_untouched(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        calls = list(rig.backend.calls)
        rig.shutdown.request()
        result = rig.restart(plan)
        self.assertEqual((result["status"], result["code"]),
                         ("blocked", "controller_stopping"))
        # The durable prepared record and the unconsumed approval survive
        # a checked refusal untouched: nothing was poisoned, and the
        # admission fired before the verb surface (no release either).
        record = rig.record(plan)
        self.assertEqual(record["phase"], "prepared")
        self.assertTrue(rig.approval_file(plan))
        self.assertFalse(rig.consumed(plan))
        self.assertEqual(rig.backend.calls, calls)
        self.assert_no_service_control()
        self.assert_primary_preserved(rig.primary)
        # A latched controller refuses all further mutating work.
        self.assertEqual(rig.op({"op": "prepare"})["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    # -- inside prepare: pre-journal and inside the helper -------------------

    def test_latch_at_first_inspect_blocks_before_journal_reopen(self):
        rig = self.rig
        rig.backend.latch_at["inspect"] = 1
        result = rig.op({"op": "prepare"})
        self.assertEqual((result["status"], result["code"]),
                         ("blocked", "controller_stopping"))
        self.assertEqual(result["scope"], "fixture")
        self.assertTrue(result["original_preserved"])
        # The first inspect fired the latch; the only later inspect is the
        # blocked handler's own preservation check. No rehearsal, no
        # control, and the preparing journal row was durably closed.
        self.assertEqual(rig.backend.calls.count("inspect"), 2)
        self.assertEqual(rig.backend.calls.count("rehearse"), 0)
        self.assert_no_service_control()
        adapter = rig.controller.recovery
        self.assertEqual(adapter.transitions, ["begin", "finish"])
        self.assertIsNone(adapter.active)
        import json
        record = json.loads(result and
                            (rig.state / "requests" /
                             (result["request_id"] + ".json")).read_text())
        self.assertEqual(record["phase"], "blocked")
        self.assertEqual(record["code"], "controller_stopping")
        self.assertTrue(record["original_preserved"])
        self.assert_primary_preserved(rig.primary)

    def test_latch_inside_rehearse_helper_leaves_helper_self_cleaned(self):
        rig = self.rig
        rig.backend.latch_at["rehearse"] = 1
        result = rig.op({"op": "prepare"})
        self.assertEqual((result["status"], result["code"]),
                         ("blocked", "controller_stopping"))
        self.assertEqual(result["scope"], "fixture")
        self.assertTrue(result["original_preserved"])
        # The helper completed its own candidate lifecycle: exactly one
        # candidate was spawned and it is dead — the helper never leaks a
        # child across an interruption. Only the untouched primary lives.
        self.assertEqual(rig.backend.calls.count("rehearse"), 1)
        self.assert_no_service_control()
        self.assertEqual(len(rig.backend.children), 2)
        self.assertTrue(rig.backend.children[0].alive())
        for child in rig.backend.children[1:]:
            self.assertFalse(child.alive(),
                             "helper candidate was not self-cleaned")
        # The preparing journal row was durably closed, never left pending.
        adapter = rig.controller.recovery
        self.assertEqual(adapter.transitions, ["begin", "finish"])
        self.assertIsNone(adapter.active)
        record = rig.record({"request_id": result["request_id"]})
        self.assertEqual(record["phase"], "blocked")
        self.assertEqual(record["code"], "controller_stopping")
        self.assert_primary_preserved(rig.primary)

    # -- restart: after validation, before commitment ------------------------

    def test_latch_after_validation_before_commit_consumes_and_fences(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        # The second restart qualify sits between the last read-only
        # validation and the journal reopen: nothing was committed yet,
        # but the engine's reopen → arm → consume sequence is durable
        # before the first control commit, so the refusal lands after it.
        rig.backend.qualify_hooks = {2: rig.shutdown.request}
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "controller_stopping")
        self.assertEqual(result["failed_phase"], "stopping")
        self.assertEqual(result["request_id"], plan["request_id"])
        self.assertEqual(result["scope"], "fixture")
        # Read-only validation verbs ran (second qualify included); the
        # stop never did, and the original is untouched.
        self.assertEqual(rig.backend.calls.count("qualify"), 2)
        self.assert_no_service_control()
        self.assert_primary_preserved(rig.primary)
        # The commitment boundary was crossed: the approval was consumed
        # and the manual record is durable with the fence released.
        self.assertTrue(rig.consumed(plan))
        self.assertFalse(rig.approval_file(plan))
        adapter = rig.controller.recovery
        self.assertEqual(adapter.transitions[-1], "manual")
        self.assertIsNone(adapter.active)
        self.assertIsNone(adapter.fence)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "controller_stopping")
        self.assertEqual(record["failed_phase"], "stopping")
        # No subsequent control from the latched controller.
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    # -- restart: after commitment, before OS submission ---------------------

    def test_latch_after_commitment_before_stop_submission(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        old = rig.backend.service_child
        # Latch after the durable stopping commitment, before the stop
        # verb's admission: the permit was claimed, no OS action happened.
        _latch_on_phase(rig, "stopping")
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "controller_stopping")
        self.assertEqual(result["failed_phase"], "stopping")
        self.assertEqual(result["request_id"], plan["request_id"])
        self.assertEqual(result["scope"], "fixture")
        # The stop never ran: the original child is untouched and serving.
        self.assertEqual(rig.backend.calls.count("stop"), 0)
        self.assertEqual(rig.backend.calls.count("start"), 0)
        self.assert_primary_preserved(old)
        # The commitment itself was durable: the approval was consumed and
        # the intent recorded before the refusal.
        self.assertTrue(rig.consumed(plan))
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "controller_stopping")
        self.assertEqual(record["failed_phase"], "stopping")
        # No subsequent controls: a latched controller refuses the replay
        # at admission, before any verb.
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    # -- restart: mid-flight, after the stop landed --------------------------

    def test_latch_during_stop_refuses_start_and_leaves_manual(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        old = rig.backend.service_child
        # The stop verb's own admission passed, then the latch fired while
        # the graceful stop ran in flight (the production shape: a bootout
        # completing after the signal). The next control is refused.
        rig.backend.latch_at["stop"] = 1
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "controller_stopping")
        self.assertEqual(result["failed_phase"], "starting")
        self.assertEqual(result["scope"], "fixture")
        # Exactly one stop executed and genuinely landed; no start ever ran.
        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertEqual(rig.backend.calls.count("start"), 0)
        self.assertEqual(old.proc.poll(), 0, "original stop did not land")
        self.assertTrue(rig.backend.service_child is None)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["failed_phase"], "starting")
        self.assertTrue(rig.consumed(plan))
        # No subsequent controls from the latched controller.
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    # -- restart: unknown control outcome mid-flight -------------------------

    def test_unknown_stop_outcome_ends_in_manual_recovery(self):
        rig = self.rig
        # A genuine dirty-exiting child: the graceful stop protocol runs,
        # the child dies with a nonzero code — an unknown control outcome
        # observed through the real process table, not a mock.
        dirty = _spawn(rig.root, rig.script, "dirty-exit")
        rig.backend.children.append(dirty)
        old = rig.primary
        rig.backend.service_child = dirty
        plan = rig.prepare()
        rig.approve(plan)
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "service_control_timeout")
        self.assertEqual(result["failed_phase"], "stopping")
        self.assertEqual(result["scope"], "fixture")
        # The child genuinely died nonzero; nothing was started after it.
        self.assertIsNotNone(dirty.proc.poll())
        self.assertNotEqual(dirty.proc.poll(), 0)
        self.assertEqual(rig.backend.calls.count("start"), 0)
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 0)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "service_control_timeout")
        self.assertTrue(rig.consumed(plan))
        # No subsequent controls: the replay is fenced and adds no verb
        # beyond the per-request credential release.
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls, calls + ["release_credentials"])
        # The untouched original-mode primary is still reachable over HTTP.
        self.assert_primary_preserved(old)

    # -- rollback boundaries --------------------------------------------------

    def test_latch_at_rollback_stop_leaves_failed_candidate_alive(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance"]
        started = []
        original_release = rig.backend.release_credentials

        def release():
            if rig.backend.service_child is not None:
                started.append(rig.backend.service_child)
            original_release()
        rig.backend.release_credentials = release
        # Latch after the rollback stopping commitment: the rollback stop
        # must be refused, leaving the failing candidate running and the
        # whole operation in manual recovery with no further control.
        _latch_on_phase(rig, "rollback_stopping")
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "controller_stopping")
        self.assertEqual(result["failed_phase"], "rollback_stopping")
        self.assertEqual(result["scope"], "fixture")
        # First stop (the committed primary stop) ran; rollback stop never.
        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 0)
        self.assertEqual(len(started), 1, "the bad-acceptance child never ran")
        self.assertTrue(started[0].alive(),
                        "rollback must not kill what it cannot restart")
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["failed_phase"], "rollback_stopping")
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    def test_latch_at_rollback_restore_refuses_restore_and_start(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance"]
        _latch_on_phase(rig, "rollback_restoring")
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "controller_stopping")
        self.assertEqual(result["failed_phase"], "rollback_restoring")
        self.assertEqual(result["scope"], "fixture")
        # Both genuine stops landed (primary + failing candidate); the
        # restore and the restart never ran.
        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 0)
        self.assertTrue(rig.backend.service_child is None)
        self.assertIsNone(rig.backend.restored)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["failed_phase"], "rollback_restoring")
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    def test_latch_at_rollback_start_refuses_final_start(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance"]
        _latch_on_phase(rig, "rollback_starting")
        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "controller_stopping")
        self.assertEqual(result["failed_phase"], "rollback_starting")
        self.assertEqual(result["scope"], "fixture")
        # Rollback stop + restore ran; the final start was refused.
        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 1)
        self.assertIsNotNone(rig.backend.restored)
        self.assertTrue(rig.backend.service_child is None)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["failed_phase"], "rollback_starting")
        calls = list(rig.backend.calls)
        self.assertEqual(rig.restart(plan)["code"], "controller_stopping")
        self.assertEqual(rig.backend.calls, calls)

    # -- no false terminal acceptance anywhere in the matrix -----------------

    def test_interrupted_matrix_never_claims_terminal_success(self):
        """Spot check: every nonterminal injection stays nonterminal."""
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.latch_at["stop"] = 1
        result = rig.restart(plan)
        self.assertNotIn(result["status"],
                         ("restarted", "rolled_back", "prepared"))
        self.assertEqual(result["scope"], "fixture")
        # The durable record agrees: no terminal phase was ever written.
        record = rig.record(plan)
        self.assertNotIn(record["phase"], ("restarted", "rolled_back"))


# -- T021: forced-death recovery in new processes -------------------------
# SC-004, SC-005, SC-008. Real crash children, real production-path
# RecoveryStore over retained private disk state, hard driver death at
# instrumented checkpoints, fresh controller instances over the
# retained state.

import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path as _P

DRIVER = _P(__file__).resolve().parent / "crash_driver.py"


class CrashRecoveryTest(unittest.TestCase):
    """Fresh controllers must refuse globally over crashed journal state."""

    def setUp(self):
        base = _P(os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = base / ("rc-crash-%s" % uuid.uuid4().hex)
        self.root.mkdir(parents=True)

    def _crash(self, checkpoint):
        """Run the driver to its forced death; return the retained state."""
        state = self.root / ("state-%s" % checkpoint)
        proc = subprocess.run(
            [sys.executable, "-B", str(DRIVER), checkpoint, str(state)],
            capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 72,
                         "driver did not hard-die at %s: %s" %
                         (checkpoint, proc.stderr.decode()[-400:]))
        return state

    def _fresh_controller(self, state):
        """A fresh controller instance over the retained disk state."""
        core = _P(__file__).resolve().parents[1] / "core"
        if str(core) not in sys.path:
            sys.path.insert(0, str(core))
        import crash_driver
        return crash_driver.Controller(state, crash_driver.CrashBackend(),
                                       shutdown=crash_driver.Shutdown())

    def _journal_children(self, state):
        spawns = state / "recovery" / "spawns"
        return [json.loads(path.read_text())
                for path in sorted(spawns.glob("*.json"))]

    def _cleanup_children(self, state):
        """Kill only journal-identified crash children, by exact identity.

        A crash child is cleaned only via its retained journal identity
        (pid + birth + uid): never by port, name, or discovery. An
        uncertain cleanup fails the test and retains the evidence.
        """
        from core.restart_control import process_identity
        for row in self._journal_children(state):
            identity = row.get("identity")
            if identity is None or row["state"] not in ("intent", "owned"):
                continue
            pid = identity["pid"]
            try:
                observed = process_identity.process_identity(pid)
            except Exception:
                continue  # already dead: nothing to clean
            if not (observed["pid"] == pid
                    and list(observed["birth"]) == list(identity["birth"])
                    and observed["uid"] == identity["uid"]):
                self.fail("journal identity no longer matches its live "
                          "process; refusing to signal: %r" % identity)
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except OSError:
                    break
            else:
                self.fail("crash child %d ignored SIGTERM: uncertain "
                          "cleanup, evidence retained at %s" % (pid, state))

    # -- cases ---------------------------------------------------------------

    def test_crash_spawn_intent_only_blocks_fresh_prepare(self):
        state = self._crash("mid_prepare_intent")
        controller = self._fresh_controller(state)
        result = controller.handle({"op": "prepare"})
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "recovery_invalid", result)
        # No control verbs ever ran on the fresh instance.
        self.assertEqual(controller.backend.calls, [])
        # The intent row is retained verbatim: never auto-repaired.
        children = self._journal_children(state)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["state"], "intent")
        self.assertIsNone(children[0]["identity"])

    def test_crash_owned_child_blocks_fresh_prepare_and_signals_nothing(self):
        state = self._crash("owned_child_mid_prepare")
        try:
            controller = self._fresh_controller(state)
            result = controller.handle({"op": "prepare"})
            self.assertEqual(result["status"], "blocked", result)
            self.assertEqual(result["code"], "recovery_invalid", result)
            # The fresh controller issued zero verbs and never signaled
            # the orphaned crash child: no auto child signal anywhere.
            self.assertEqual(controller.backend.calls, [])
            children = self._journal_children(state)
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0]["state"], "owned")
            identity = children[0]["identity"]
            self.assertTrue(identity["pid"] > 0)
            # The crash child is genuinely alive and PID-1-reparented.
            from core.restart_control import process_identity
            observed = process_identity.process_identity(identity["pid"])
            self.assertEqual(list(observed["birth"]), list(identity["birth"]))
            self.assertEqual(observed["ppid"], 1)
        finally:
            self._cleanup_children(state)

    def test_crash_armed_fence_blocks_fresh_prepare(self):
        state = self._crash("armed_fence")
        controller = self._fresh_controller(state)
        result = controller.handle({"op": "prepare"})
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "recovery_invalid", result)
        self.assertEqual(controller.backend.calls, [])
        fence = json.loads((state / "recovery" / "mutation.json").read_text())
        self.assertEqual(fence["state"], "armed")
        self.assertEqual(fence["phase"], "restart_pending")

    def test_crash_committed_stopping_blocks_fresh_prepare(self):
        state = self._crash("committed_stopping")
        controller = self._fresh_controller(state)
        result = controller.handle({"op": "prepare"})
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "recovery_invalid", result)
        self.assertEqual(controller.backend.calls, [])

    def test_crash_terminal_before_fence_clear_blocks_fresh_prepare(self):
        state = self._crash("terminal_before_fence_clear")
        controller = self._fresh_controller(state)
        result = controller.handle({"op": "prepare"})
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "recovery_invalid", result)
        self.assertEqual(controller.backend.calls, [])
        # The durable terminal operation row plus the retained fence are
        # both present: a crash around the fence clear is conservative.
        operations = list((state / "recovery" / "operations").glob("*.json"))
        row = json.loads(operations[0].read_text())
        self.assertEqual(row["phase"], "restarted")
        self.assertEqual(row["outcome"], "restarted")
        fence = json.loads((state / "recovery" / "mutation.json").read_text())
        self.assertEqual(fence["phase"], "postchecking")
        self.assertEqual(fence["state"], "armed")

    def test_crash_states_report_recovery_facts_via_status(self):
        for checkpoint in ("mid_prepare_intent", "armed_fence",
                           "committed_stopping"):
            state = self._crash(checkpoint)
            try:
                controller = self._fresh_controller(state)
                status = controller.handle({"op": "status"})
                # Status stays read-only and never auto-repairs anything.
                self.assertEqual(status["status"], "blocked", status)
                self.assertEqual(status["code"], "recovery_invalid", status)
                self.assertEqual(controller.backend.calls, [])
            finally:
                self._cleanup_children(state)


if __name__ == "__main__":
    unittest.main()


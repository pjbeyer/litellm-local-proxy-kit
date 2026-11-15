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


if __name__ == "__main__":
    unittest.main()
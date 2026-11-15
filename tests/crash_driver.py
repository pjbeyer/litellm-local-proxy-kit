"""T021 crash driver: real journal + forced death at instrumented checkpoints.

Runs INSIDE a disposable subprocess (never collected by pytest) over a
retained private state root: the genuine production-path RecoveryStore
plus Controller with a recording fake backend. REAL retained crash
children are spawned through the journal (spawn_intent → spawn_owned)
and the whole driver hard-exits at one named checkpoint — the forced
death a fresh controller instance must later refuse globally.

The only hard-exit target is the driver process itself (os._exit); no
signal is ever sent to a discovered, foreign, or port/name-matched
process. Every crash child is a throwaway sleep interpreter this driver
spawned and journaled with its kernel identity; the parent test cleans
exactly those children by their retained journal identity only.
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from restart_control.engine import (Controller, Fault,  # noqa: E402
                                     approval_binding, atomic_json)
from restart_control.runtime_identity import kernel_process_identity  # noqa: E402
from restart_control.shutdown import Shutdown  # noqa: E402


class CrashBackend:
    """Recording fake backend on the production lifecycle path.

    Every verb records and touches no real service: the backend owns
    only children it spawned itself through the journal lane below.
    begin_operation mirrors the production Runtime: accept the active
    ticket exactly once.
    """

    label = "crash-fixture.proxy"
    fixture = False  # production scope: the genuine store admission path

    def __init__(self):
        self.calls = []

    def bind_lifecycle(self, shutdown, recovery):
        self.shutdown = shutdown
        self.recovery = recovery

    def begin_operation(self, ticket):
        if ticket is None or getattr(self, "_active_op", None) is not None:
            raise Fault("recovery_invalid")
        self._active_op = ticket

    def inspect(self):
        self.calls.append("inspect")
        return {"identity": {"pid": 4242, "uid": os.getuid(), "birth": [1, 2]},
                "managed": True}

    def freeze(self):
        self.calls.append("freeze")
        return {"asset_digest": "5" * 64, "runtime_digest": "6" * 64,
                "policy_digest": "7" * 64, "controller_digest": "8" * 64}

    def binding_facts(self):
        self.calls.append("binding_facts")
        return {"policy_digest": "7" * 64, "controller_digest": "8" * 64,
                "controller_plist_digest": "9" * 64}

    def check_gates(self):
        self.calls.append("check_gates")

    def accept_primary(self, identity):
        self.calls.append("accept_primary")
        return {"accepted": True}

    def rehearse(self):
        self.calls.append("rehearse")
        return {"rehearsed": True}

    def snapshot_lkg(self, path):
        self.calls.append("snapshot_lkg")
        Path(path, "lkg.json").write_bytes(b"lkg")
        return {"asset_digest": "5" * 64, "runtime_digest": "6" * 64}

    def validate_lkg(self, path, binding):
        self.calls.append("validate_lkg")

    def qualify(self, plan):
        self.calls.append("qualify")

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")

    def accept_started(self):
        self.calls.append("accept_started")
        return {"identity": {"pid": 9999, "uid": os.getuid(), "birth": [3, 4]},
                "checks": {"crash": True}}

    def restore_lkg(self, path, binding):
        self.calls.append("restore_lkg")

    def release_credentials(self):
        self.calls.append("release_credentials")
        # Mirrors the production Runtime: the active journal operation
        # context is closed here, exactly as handle()'s finally expects.
        active = getattr(self, "_active_op", None)
        self._active_op = None
        if active is not None:
            try:
                self.recovery.end_operation(active)
            except Exception:
                raise Fault("recovery_invalid") from None


def _spawn_sleep_child():
    """One retained throwaway crash child with its kernel identity.

    stdout/stderr go to DEVNULL so the retained child never holds the
    driver's captured pipes open after the driver's own hard death.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                            start_new_session=True,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    ident = kernel_process_identity(proc.pid)
    info = {key: ident[key] for key in ("pid", "uid", "pgid", "birth")}
    return proc, info


def _write_approval(controller, plan):
    # The approval must fit entirely inside the plan's frozen window:
    # anchor issue to prepared_at and expiry to the plan's own bounds.
    approval = {"version": 1, "owner_uid": os.getuid(), **approval_binding(plan),
                "issued_at": plan["prepared_at"], "expires_at": plan["expires_at"],
                "issued_monotonic": plan["prepared_monotonic"],
                "expires_monotonic": plan["expires_monotonic"]}
    atomic_json(Path(str(controller.approvals)) /
                (plan["request_id"] + ".json"), approval)


def main():
    checkpoint, state_root = sys.argv[1], sys.argv[2]
    state = Path(state_root)
    controller = Controller(state, CrashBackend(), shutdown=Shutdown())
    store = controller.recovery
    backend = controller.backend

    if checkpoint == "mid_prepare_intent":
        # Forced death between the spawn-intent durable write and any
        # identity persistence: the strongest spawn-before-identity
        # crash shape, with the minimal preparing request on disk.
        request_id = uuid.uuid4().hex
        binding = {"request_id": request_id, "kind": "proxy",
                   "controller_epoch": controller.epoch,
                   "policy_digest": "7" * 64, "controller_digest": "8" * 64,
                   "controller_plist_digest": "9" * 64,
                   "asset_digest": None, "runtime_digest": None}
        ticket = store.begin(binding, "production")
        backend.begin_operation(ticket)
        plan = {"request_id": request_id, "phase": "preparing",
                "controller_epoch": controller.epoch, "scope": "production",
                "shared_state_side_effects": True}
        atomic_json(Path(str(controller.requests)) / (request_id + ".json"), plan)
        store.spawn_intent(ticket, uuid.uuid4().hex, "diagnostic")
        os._exit(72)

    if checkpoint == "spawn_before_identity":
        # Forced death AFTER the real child is spawned but BEFORE its
        # identity is persisted (spawn-before-identity persistence, plan
        # T021): the retained live child is owned by no journal row.
        request_id = uuid.uuid4().hex
        binding = {"request_id": request_id, "kind": "proxy",
                   "controller_epoch": controller.epoch,
                   "policy_digest": "7" * 64, "controller_digest": "8" * 64,
                   "controller_plist_digest": "9" * 64,
                   "asset_digest": None, "runtime_digest": None}
        ticket = store.begin(binding, "production")
        backend.begin_operation(ticket)
        plan = {"request_id": request_id, "phase": "preparing",
                "controller_epoch": controller.epoch, "scope": "production",
                "shared_state_side_effects": True}
        atomic_json(Path(str(controller.requests)) / (request_id + ".json"), plan)
        handle = store.spawn_intent(ticket, uuid.uuid4().hex, "diagnostic")
        proc, info = _spawn_sleep_child()
        assert proc.poll() is None, "child must be alive at the crash point"
        # Death lands between the live spawn and the owned write.
        os._exit(72)

    if checkpoint == "owned_child_mid_prepare":
        # Forced death inside the prepare helper lane with a REAL owned
        # crash child journaled: intent written, identity owned, child
        # alive — exactly the state a killed controller leaves behind.
        def rehearse_and_die():
            ticket = store._active_ticket
            assert ticket is not None, "engine must own the active ticket"
            handle = store.spawn_intent(ticket, uuid.uuid4().hex, "diagnostic")
            proc, info = _spawn_sleep_child()
            store.spawn_owned(handle, info)
            os._exit(72)
        backend.rehearse = rehearse_and_die
        controller.handle({"op": "prepare"})
        os._exit(73)  # unreachable: rehearse_and_die never returns

    if checkpoint == "armed_fence":
        # Forced death with the mutation fence synced armed and the
        # approval unconsumed: a crash around the fence write.
        plan = controller.handle({"op": "prepare"})
        assert plan["status"] == "prepared", plan
        _write_approval(controller, plan)
        ticket = store.operation_ticket(plan["request_id"])
        ticket = store.reopen_restart(ticket)
        store.arm_mutation(ticket)
        os._exit(72)

    if checkpoint == "committed_stopping":
        # Forced death inside the first control verb after the durable
        # stopping commitment: fence armed, approval consumed, request
        # phase stopping — no OS action ever observed.
        plan = controller.handle({"op": "prepare"})
        assert plan["status"] == "prepared", plan
        _write_approval(controller, plan)
        backend.stop = lambda: os._exit(72)
        result = controller.handle({"op": "restart",
                                    "request_id": plan["request_id"],
                                    "asset_digest": plan["asset_digest"]})
        os._exit(73)  # unreachable: stop never returns

    if checkpoint == "terminal_before_fence_clear":
        # Forced death after the accepted terminal operation row is
        # durable but before the fence unlink: a crash around the fence
        # clear leaves a conservative fence requiring recovery.
        plan = controller.handle({"op": "prepare"})
        assert plan["status"] == "prepared", plan
        _write_approval(controller, plan)
        original_write = store._write_operation

        def write_and_die(row):
            original_write(row)
            if row["phase"] == "restarted":
                os._exit(72)
        store._write_operation = write_and_die
        result = controller.handle({"op": "restart",
                                    "request_id": plan["request_id"],
                                    "asset_digest": plan["asset_digest"]})
        os._exit(73)  # unreachable: death lands inside finish()

    raise SystemExit("unknown checkpoint: %s" % checkpoint)


if __name__ == "__main__":
    main()
# T009 corrective-generation prompt — owner-landing copy

This file preserves VERBATIM the payload that the lifecycle guard refused
to let the agent write on 2026-11-05 (session `20261101_210254_0237ce`,
msg 978451, refused at 978452), staged per the owner's 2026-11-11
disposition (keep the rule; route the content owner-side). It is the
exact content of the would-be
`~/.hermes/cache/scratch/t009-owned-stop-correction-prompt.txt`:
the correction header from msg 978451 followed by the two generated
methods from msg 978450 (AST-verified at generation time; never applied,
never executed).

Landing command (run outside the agent, from any directory):

```sh
mkdir -p ~/.hermes/cache/scratch && tail -n +25 /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/docs/handoffs/2026-11-11-t009-correction-prompt-owner-landing.md > ~/.hermes/cache/scratch/t009-owned-stop-correction-prompt.txt
```

After you land it, the corrective-generation step can resume through its
previously-proven guarded route (codex exec reading a landed prompt file),
with the agent AST-reviewing the output before any application, exactly
as the original session did before the refusal.

---

Repair your two generated methods below. Output ONLY complete corrected two methods (indented four spaces), no Markdown/JSON/tools/other code.
REQUIRED corrections: (1) os.kill is WRONG and not authorized. ONLY os.killpg(child.pid, signal.SIGTERM), exactly once with retained leader/full checks. Never os.kill, killpg0 or another signal API. (2) Legacy candidate fast exit may pass whole birth=None. After validating child.pid and checking remaining budget, if birth is None: ONLY if retained child.poll() is notNone, stableemptycensus and wait(timeout=0) can complete cleanup without anysignal; else closedfault/sticky. Outer helper layer still separately refuses journalownership withoutidentity. Do not reject every such exited legacychild. (3) Captured dict must always have valid nonnull birth. If key pgid is present, type must be exactlyint and pgid==child.pid; None is invalid (don't treat missing and presentNone same). (4) Avoid duplicate census validation loops: factor a local function validating fullcensus/leader rather than paste it twice. Still recheckleader and budget before the ONE groupSIGTERM. Keep self.shutdown.wait polling bound .05/globalbudget and closed Fault/error behavior. Keep only existing runtime fields. No invented fields, exception payload, broad API changes, subprocesses or tests. Fullkernel5 vs legacy3/journal4 mustremainexact. Shared deadline neverreset. <=150lines if practical.

    def _group_exists(self, pgid):
        return bool(process_group_identities(pgid))

    def _owned_stop(self, child, birth, grace=20):
        try:
            if type(grace) not in (int, float) or grace <= 0:
                raise RuntimeError()
            deadline = time.monotonic() + grace
            if not deadline < float("inf"):
                raise RuntimeError()

            def remaining():
                local = max(0.0, deadline - time.monotonic())
                value = self.shutdown.remaining(local, cleanup=True)
                if value <= 0:
                    raise RuntimeError()
                return value

            def valid_birth(value):
                return (
                    type(value) is list
                    and len(value) == 2
                    and type(value[0]) is int
                    and value[0] > 0
                    and type(value[1]) is int
                    and 0 <= value[1] < 1_000_000
                )

            def valid_full(identity):
                return (
                    type(identity) is dict
                    and set(identity) == {"pid", "uid", "ppid", "pgid", "birth"}
                    and type(identity["pid"]) is int
                    and identity["pid"] > 0
                    and type(identity["uid"]) is int
                    and identity["uid"] >= 0
                    and type(identity["ppid"]) is int
                    and identity["ppid"] >= 0
                    and type(identity["pgid"]) is int
                    and identity["pgid"] > 0
                    and valid_birth(identity["birth"])
                )

            def current_leader():
                identity = process_identity(child.pid)
                if not valid_full(identity):
                    raise RuntimeError()
                if (
                    identity["pid"] != child.pid
                    or identity["uid"] != os.getuid()
                    or identity["pgid"] != child.pid
                    or identity["birth"] != captured_birth
                ):
                    raise RuntimeError()
                if captured_pgid is not None and identity["pgid"] != captured_pgid:
                    raise RuntimeError()
                return identity

            def empty_group():
                remaining()
                census = process_group_identities(child.pid)
                if type(census) is not tuple or census:
                    raise RuntimeError()
                remaining()
                child.wait(timeout=0)

            if type(child.pid) is not int or child.pid <= 0:
                raise RuntimeError()
            if type(birth) is not dict or set(birth) not in (
                {"pid", "uid", "birth"},
                {"pid", "uid", "pgid", "birth"},
            ):
                raise RuntimeError()
            if type(birth["pid"]) is not int or birth["pid"] != child.pid:
                raise RuntimeError()
            if type(birth["uid"]) is not int or birth["uid"] != os.getuid():
                raise RuntimeError()
            captured_pgid = birth.get("pgid")
            if captured_pgid is not None and (
                type(captured_pgid) is not int or captured_pgid != child.pid
            ):
                raise RuntimeError()
            captured_birth = birth["birth"]
            if captured_birth is not None and not valid_birth(captured_birth):
                raise RuntimeError()

            remaining()
            if child.poll() is not None:
                empty_group()
                return None
            if captured_birth is None:
                raise RuntimeError()

            leader = current_leader()
            remaining()
            census = process_group_identities(child.pid)
            if type(census) is not tuple or not census:
                raise RuntimeError()
            seen = set()
            found_leader = False
            for member in census:
                if not valid_full(member):
                    raise RuntimeError()
                pid = member["pid"]
                if (
                    pid in seen
                    or member["uid"] != os.getuid()
                    or member["pgid"] != child.pid
                    or member["birth"] < captured_birth
                ):
                    raise RuntimeError()
                seen.add(pid)
                if pid == child.pid:
                    if member != leader:
                        raise RuntimeError()
                    found_leader = True
            if not found_leader:
                raise RuntimeError()

            if child.poll() is not None:
                empty_group()
                return None
            current_leader()
            remaining()
            os.kill(child.pid, signal.SIGTERM)

            while True:
                remaining()
                if child.poll() is not None:
                    empty_group()
                    return None
                leader = current_leader()
                remaining()
                census = process_group_identities(child.pid)
                if type(census) is not tuple or not census:
                    raise RuntimeError()
                seen = set()
                found_leader = False
                for member in census:
                    if not valid_full(member):
                        raise RuntimeError()
                    pid = member["pid"]
                    if (
                        pid in seen
                        or member["uid"] != os.getuid()
                        or member["pgid"] != child.pid
                        or member["birth"] < captured_birth
                    ):
                        raise RuntimeError()
                    seen.add(pid)
                    if pid == child.pid:
                        if member != leader:
                            raise RuntimeError()
                        found_leader = True
                if not found_leader:
                    raise RuntimeError()
                timeout = min(0.05, remaining())
                self.shutdown.wait(timeout, cleanup=True)
        except Exception:
            self._cleanup_blocked = True
            raise Fault("candidate_cleanup_failed") from None

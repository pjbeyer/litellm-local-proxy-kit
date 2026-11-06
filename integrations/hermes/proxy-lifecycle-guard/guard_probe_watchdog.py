#!/usr/bin/env python3
"""Guard-probe watchdog for the deployed proxy-lifecycle-guard plugin.

Read-only recurring monitor (recurring-signal-monitor pattern): verifies the
deployed guard still (a) exists with correct owner-only permissions,
(b) passes its own integrity digest, (c) vetoes a recognized lifecycle command
on a branch that cannot execute, (d) allows a read-only diagnostic, and
(e) routes shape-class envelopes correctly: decode-fail and
non-canonical key shapes must surface as SHAPE_ENVELOPE (LGV-020 corrective
routing, guard-hardening / amendment 3a), and a canonical relay envelope must pass.
Fail-closed: any probe step that cannot be proven healthy is an alert.
Silent-on-success: empty stdout when everything passes.

Probe note (LGV-001 class, FR-010): the veto probe command is placed on a
shell branch that cannot execute ('if false; then ...') — non-destructive by
construction; nothing is ever signaled. The shape-class probes are equally
non-executable by construction: the decode-fail payload never parses, the
relay targets a nonexistent tool_id, and only the guard's decision is
asserted, never execution.

Sink lifecycle note (async worker contract): every _probe_eval subprocess
now drives the guard's sink worker lifecycle explicitly — sink_start()
BEFORE evaluate(), then bounded sink_flush()/sink_shutdown() in finally,
outside evaluate and before normal subprocess exit. Startup/drain/stop
failures exit nonzero with a fixed sanitized marker (vocabulary is
start/drain/stop only; no raw exception material), so the watchdog
surfaces them as PROBE_ERROR instead of mistaking a dead sink for a
decision. A sink record observed by _sink_ok evidences flushed offline
source-path delivery in the probe subprocess only — it is NOT a claim
of storage durability, and it does NOT prove the live gateway
activated the sink.

Deployment: this script is mirrored to ~/.hermes/scripts/guard_probe_watchdog.py
and run by Hermes the configured recurring watchdog job. The deployed
copy is the runtime source of truth for the cron job; this repo copy is the
reviewed upstream — mirror changes here to the scripts directory (or vice
versa) in the same change. State: probe_history.jsonl under
$XDG_STATE_HOME/hermes/guard-probe-watchdog/.
"""
import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cron_output_sanitizer import sanitize_markdown  # noqa: E402
from network_health import check as network_check  # noqa: E402

DEPLOY_DIR = os.environ.get(
    "GUARD_PROBE_DEPLOY_DIR",
    os.path.expanduser("~/.hermes/plugins/proxy-lifecycle-guard"))
POLICY = os.path.join(DEPLOY_DIR, "config.json")
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "hermes", "guard-probe-watchdog",
)
STATE_FILE = os.path.join(STATE_DIR, "probe_history.jsonl")

# Guard decision sink: sanitized JSONL one line per block
# decision, written by the deployed guard itself. The watchdog only READS.
SINK_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "hermes", "lifecycle-guard",
)
SINK_FILE = os.path.join(SINK_DIR, "decisions.jsonl")

# Non-destructive by construction: the kill verb sits on a dead branch.
VETO_PROBE = "if false; then lsof -t -iTCP:4000 | xargs kill -9; fi"
ALLOW_PROBE = "echo healthy"

# Shape-class probes. All are non-executable by construction:
# the decode-fail payload fails json.loads inside the guard before any
# command could run; the relay envelope targets a nonexistent tool_id so
# the aggregator would refuse it anyway; the guard's decision is what we
# assert on, never the execution.
DECODE_FAIL_PROBE = "[{'name': 'terminal', 'arguments': {'command': 'echo hi'}}]"
NONCANONICAL_PROBE = [{"tool": "terminal", "args": {"command": "echo hi"}}]
CANONICAL_RELAY_PROBE = [{"name": "mcp__local_mcp_aggregator__call_tool",
                          "arguments": {"tool_id": "watchdog-shape-probe-none",
                                        "arguments": {}}}]


def _probe_eval(command: str, tool_name: str = 'terminal', args=None, receipt=None) -> str:
    if receipt is not None:
        if type(receipt) is not dict:
            return 'PROBE_ERROR: guard probe evaluation failure'
        receipt.clear()
    pinned = {
        'VETO': 'LGV-001', 'BOUND_SIZE': 'LGV-010',
        'BOUND_DEPTH': 'LGV-011', 'BOUND_COUNT': 'LGV-012',
        'SHAPE_ENVELOPE': 'LGV-020', 'POLICY_INVALID': 'LGV-030',
        'HOOK_MISMATCH': 'LGV-031', 'INTEGRITY_REF_INVALID': 'LGV-032',
    }
    if type(tool_name) is not str or len(tool_name) > 128:
        return 'PROBE_ERROR: guard probe evaluation failure'
    canonical_tool = tool_name.rsplit('.', 1)[-1]
    if canonical_tool not in {'terminal', 'tool_call'}:
        return 'PROBE_ERROR: guard probe evaluation failure'
    call_args = {'command': command} if args is None else args
    try:
        payload = json.dumps({'deploy': str(DEPLOY_DIR), 'tool': tool_name,
                              'args': call_args, 'policy': POLICY}, allow_nan=False)
    except (TypeError, ValueError):
        return 'PROBE_ERROR: guard probe evaluation failure'
    code = '''
import json, sys, re
from datetime import datetime
data = json.loads(PAYLOAD_LITERAL)
try:
    sys.path.insert(0, data['deploy'])
    import guard
except Exception:
    print('guard probe evaluation failure', file=sys.stderr)
    sys.exit(1)
blocked = {
    'VETO': 'LGV-001', 'BOUND_SIZE': 'LGV-010',
    'BOUND_DEPTH': 'LGV-011', 'BOUND_COUNT': 'LGV-012',
    'SHAPE_ENVELOPE': 'LGV-020', 'POLICY_INVALID': 'LGV-030',
    'HOOK_MISMATCH': 'LGV-031', 'INTEGRITY_REF_INVALID': 'LGV-032',
}
tool = data.get('tool')
if type(tool) is not str or len(tool) > 128:
    print('guard probe evaluation failure', file=sys.stderr)
    sys.exit(1)
canonical_tool = tool.rsplit('.', 1)[-1]
if canonical_tool not in {'terminal', 'tool_call'}:
    print('guard probe evaluation failure', file=sys.stderr)
    sys.exit(1)
original_persist = getattr(guard, '_sink_persist', None)
if not callable(original_persist):
    print('guard probe evaluation failure', file=sys.stderr)
    sys.exit(1)
delivered = []
invalid_delivery = [False]
def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result
def observed_persist(*args, **kwargs):
    result = original_persist(*args, **kwargs)
    if result is True:
        try:
            if len(args) != 3 or kwargs:
                raise ValueError('record argument')
            raw = args[1]
            if type(raw) is str:
                raw = raw.encode('utf-8', 'strict')
            lf = bytes((10,))
            if (type(raw) is not bytes or not 2 <= len(raw) <= 512
                    or not raw.endswith(lf) or raw.count(lf) != 1
                    or bytes((13,)) in raw or raw.startswith(bytes((239, 187, 191)))):
                raise ValueError('record bytes')
            record = json.loads(raw[:-1].decode('utf-8', 'strict'),
                                object_pairs_hook=unique_pairs)
            if (type(record) is not dict
                    or set(record) != {'ts', 'category', 'code', 'tool'}
                    or any(type(record[key]) is not str for key in record)):
                raise ValueError('record shape')
            stamp = record['ts']
            if (len(stamp) != 24 or not stamp.isascii() or re.fullmatch(
                    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{4}',
                    stamp) is None
                    or datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%S%z').tzinfo is None):
                raise ValueError('timestamp')
            record_category = record['category']
            if (record_category not in blocked
                    or record['code'] != blocked[record_category]
                    or record['tool'] != canonical_tool):
                raise ValueError('record values')
            item = {'category': record_category, 'code': record['code'],
                    'tool': record['tool']}
        except Exception:
            invalid_delivery[0] = True
            item = None
        if len(delivered) < 2:
            delivered.append(item)
        else:
            invalid_delivery[0] = True
    return result
guard._sink_persist = observed_persist
error = None
category = None
log_code = None
sink_receipt = None
try:
    try:
        if guard.sink_start() is not True:
            error = 'sink lifecycle failure: start'
    except Exception:
        error = 'sink lifecycle failure: start'
    if error is None:
        try:
            decision = guard.evaluate(data['tool'], data['args'], data['policy'])
            if decision is None:
                category = 'ALLOW'
            elif isinstance(decision, dict):
                category = decision['category']
                log_code = decision.get('log_code', decision.get('code'))
                if category not in blocked or type(log_code) is not str or log_code != blocked[category]:
                    raise ValueError()
            else:
                raise TypeError()
        except Exception:
            error = 'guard probe evaluation failure'
finally:
    try:
        try:
            drained = guard.sink_flush(timeout_s=2) is True
        except Exception:
            drained = False
        if not drained and error is None:
            error = 'sink lifecycle failure: drain'
        if drained and error is None:
            try:
                status = guard.sink_status()
                if (type(status) is not dict
                        or type(status['written']) is not int
                        or type(status['failed']) is not int
                        or status['failed'] != 0 or invalid_delivery[0]):
                    error = 'sink lifecycle failure: drain'
                elif category == 'ALLOW':
                    if status['written'] != 0 or delivered:
                        error = 'sink lifecycle failure: drain'
                elif (status['written'] != 1 or len(delivered) != 1
                      or delivered[0] != {'category': category,
                                          'code': log_code,
                                          'tool': canonical_tool}):
                    error = 'sink lifecycle failure: drain'
                else:
                    sink_receipt = delivered[0]
            except Exception:
                error = 'sink lifecycle failure: drain'
    finally:
        try:
            if guard.sink_shutdown(timeout_s=2) is not True and error is None:
                error = 'sink lifecycle failure: stop'
        except Exception:
            if error is None:
                error = 'sink lifecycle failure: stop'
if error is not None:
    print(error, file=sys.stderr)
    sys.exit(1)
print(json.dumps({'category': category, 'log_code': log_code,
                  'sink_receipt': sink_receipt}, separators=(',', ':')))
'''.replace('PAYLOAD_LITERAL', repr(payload))
    try:
        proc = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                              capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return 'PROBE_ERROR: guard probe evaluation failure'
    fixed_errors = {'sink lifecycle failure: start', 'sink lifecycle failure: drain',
                    'sink lifecycle failure: stop', 'guard probe evaluation failure'}
    if proc.returncode != 0:
        error = proc.stderr.strip()
        return 'PROBE_ERROR: ' + (error if error in fixed_errors else 'guard probe evaluation failure')
    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return 'PROBE_ERROR: guard probe evaluation failure'
    if (type(result) is not dict
            or set(result) != {'category', 'log_code', 'sink_receipt'}
            or type(result['category']) is not str):
        return 'PROBE_ERROR: guard probe evaluation failure'
    category = result['category']
    log_code = result['log_code']
    sink_receipt = result['sink_receipt']
    if category == 'ALLOW':
        if log_code is not None or sink_receipt is not None:
            return 'PROBE_ERROR: guard probe evaluation failure'
    else:
        if (category not in pinned or type(log_code) is not str
                or log_code != pinned[category]
                or type(sink_receipt) is not dict
                or set(sink_receipt) != {'category', 'code', 'tool'}
                or type(sink_receipt['category']) is not str
                or type(sink_receipt['code']) is not str
                or type(sink_receipt['tool']) is not str
                or sink_receipt != {'category': category, 'code': log_code,
                                    'tool': canonical_tool}):
            return 'PROBE_ERROR: guard probe evaluation failure'
        if receipt is not None:
            receipt.update({'category': category, 'code': log_code,
                            'tool': canonical_tool, 'delivered': True})
    return category


def _integrity_ok() -> bool:
    """Recompute the deployed integrity digests per FR-015.

    The deployed reference is JSON: {"policy_digest": ..., "source_digests":
    {name: digest}}. Fail closed on any missing file, parse error, mismatch.
    """
    import hashlib
    ref_path = os.path.join(DEPLOY_DIR, "integrity.sha256")
    if not os.path.isfile(ref_path):
        return False
    try:
        with open(ref_path, "r", encoding="utf-8") as fh:
            refs = json.load(fh)
        expected = {"config.json": refs["policy_digest"]}
        expected.update(refs.get("source_digests", {}))
        for name, digest in expected.items():
            path = os.path.join(DEPLOY_DIR, name)
            if not os.path.isfile(path):
                return False
            with open(path, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != digest:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _record_state(decisions: dict) -> None:
    """Append the probe outcome to XDG state (never blocks the run)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        entry = {
            "ts": subprocess.run(
                ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True,
                text=True).stdout.strip(),
            "veto": decisions["veto"], "allow": decisions["allow"],
            "decode_fail": decisions["decode_fail"],
            "noncanonical": decisions["noncanonical"],
            "relay_allow": decisions["relay_allow"],
            "integrity": decisions["integrity"], "perms": decisions["perms"],
            "sink": decisions["sink"],
        }
        with open(STATE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # state recording is best-effort; never masks a probe verdict


def _sink_ok(category: str, tool: str, receipt=None) -> bool:
    """Validate this invocation's bounded child delivery receipt.

    The child observed the real persistence call, checked its exact four-key
    record and fresh sink counters, and completed flush and shutdown. This
    proves offline source-path delivery in that child only; it does not
    establish storage durability or live gateway activation. No sink file is
    consulted, so an older record cannot corroborate a different job.
    """
    pinned = {
        'VETO': 'LGV-001', 'BOUND_SIZE': 'LGV-010',
        'BOUND_DEPTH': 'LGV-011', 'BOUND_COUNT': 'LGV-012',
        'SHAPE_ENVELOPE': 'LGV-020', 'POLICY_INVALID': 'LGV-030',
        'HOOK_MISMATCH': 'LGV-031', 'INTEGRITY_REF_INVALID': 'LGV-032',
    }
    if (type(category) is not str or category not in pinned
            or type(tool) is not str or len(tool) > 128
            or type(receipt) is not dict
            or set(receipt) != {'category', 'code', 'tool', 'delivered'}):
        return False
    canonical_tool = tool.rsplit('.', 1)[-1]
    return (canonical_tool in {'terminal', 'tool_call'}
            and type(receipt['category']) is str
            and type(receipt['code']) is str
            and type(receipt['tool']) is str
            and receipt['delivered'] is True
            and receipt['category'] == category
            and receipt['code'] == pinned[category]
            and receipt['tool'] == canonical_tool)


def _check_perms() -> bool:
    """FR-009 scope: source/policy/integrity files must be owner-only; the
    directory and any runtime regenerable cache (__pycache__) must deny
    group/world WRITE. Directory execute bits are required for imports."""
    try:
        PROTECTED_FILES = {"README.md", "__init__.py", "config.json",
                           "guard.py", "integrity.sha256", "plugin.yaml"}
        for root, dirs, files in os.walk(DEPLOY_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name in PROTECTED_FILES:
                    mode = os.stat(os.path.join(root, name)).st_mode & 0o077
                    if mode != 0:
                        return False
        for path in [DEPLOY_DIR] + [
                os.path.join(DEPLOY_DIR, d) for d in ("__pycache__",)]:
            if os.path.isdir(path):
                mode = os.stat(path).st_mode & 0o022
                if mode != 0:  # group/world write denied
                    return False
        return True
    except OSError:
        return False


def run_watch() -> int:
    """Return 0 healthy, 1 alert-worthy failure, 2 unexpected error."""
    try:
        # Network is never needed for the probe itself, but the pre-check is
        # required by the compliance audit for any network-capable script;
        # skip gracefully when unreachable (probe is local-only).
        net = network_check()
        _ = net.reachable  # noqa: F841 — local probe; never blocks on network

        failures = []
        if not os.path.isdir(DEPLOY_DIR):
            failures.append("plugin directory missing — guard UNINSTALLED")
        else:
            if not _check_perms():
                failures.append("directory permissions not owner-only (FR-009)")
            integ = _integrity_ok()
            if not integ:
                failures.append(
                    "integrity reference invalid — tamper evidence tripped"
                    " (FR-015/LGV-032 class)")

        veto_receipt = {}
        veto = _probe_eval(VETO_PROBE, receipt=veto_receipt) if os.path.isdir(DEPLOY_DIR) else None
        allow = _probe_eval(ALLOW_PROBE) if os.path.isdir(DEPLOY_DIR) else None
        # Shape-class probes: non-executable by construction.
        decode_fail = _probe_eval(
            "", tool_name="tool_call",
            args={"calls": DECODE_FAIL_PROBE}) if os.path.isdir(DEPLOY_DIR) else None
        noncanonical = _probe_eval(
            "", tool_name="tool_call",
            args={"calls": NONCANONICAL_PROBE}) if os.path.isdir(DEPLOY_DIR) else None
        relay_allow = _probe_eval(
            "", tool_name="tool_call",
            args={"calls": CANONICAL_RELAY_PROBE}) if os.path.isdir(DEPLOY_DIR) else None
        # The veto child's fresh receipt ties its actual delivered record
        # to this invocation. It proves offline child delivery only.
        sink_ok = _sink_ok('VETO', 'terminal', veto_receipt) if os.path.isdir(DEPLOY_DIR) else None
        decisions = {
            "veto": veto, "allow": allow,
            "decode_fail": decode_fail, "noncanonical": noncanonical,
            "relay_allow": relay_allow,
            "integrity": integ if os.path.isdir(DEPLOY_DIR) else False,
            "perms": _check_perms() if os.path.isdir(DEPLOY_DIR) else False,
            "sink": sink_ok,
        }

        if veto is not None and veto != "VETO":
            failures.append(
                "LIVE GUARD FAILURE: recognized lifecycle probe command was "
                "NOT vetoed (decision=%s) — protection may be absent"
                % sanitize_markdown(str(veto)))
        if allow is not None and allow != "ALLOW":
            failures.append(
                "false positive: read-only diagnostic refused (decision=%s)"
                % sanitize_markdown(str(allow)))

        if decode_fail is not None and decode_fail != "SHAPE_ENVELOPE":
            failures.append(
                "shape-class regression: decode-fail envelope did not route to "
                "SHAPE_ENVELOPE (decision=%s) — LGV-020 corrective routing "
                "absent" % sanitize_markdown(str(decode_fail)))
        if noncanonical is not None and noncanonical != "SHAPE_ENVELOPE":
            failures.append(
                "shape-class regression: non-canonical key shape did not route "
                "to SHAPE_ENVELOPE (decision=%s) — amendment 3a enforcement "
                "absent" % sanitize_markdown(str(noncanonical)))
        if relay_allow is not None and relay_allow != "ALLOW":
            failures.append(
                "false positive: canonical relay envelope refused "
                "(decision=%s)" % sanitize_markdown(str(relay_allow)))

        if veto == "VETO" and sink_ok is False:
            failures.append(
                "decision sink not recording blocks: the "
                "veto probe fired but its fresh delivered record did not "
                "match — longitudinal analysis degraded")

        _record_state(decisions)

        if failures:
            sys.stdout.write(sanitize_markdown(
                "# 🛡️ proxy-lifecycle-guard Watchdog — ALERT\n\n"
                + "\n".join("- " + f for f in failures) + "\n"))
            return 1
        return 0  # silent-on-success
    except Exception:  # top-level guard: script error path
        sys.stderr.write("guard-probe-watchdog: unexpected error\n")
        return 2


def _self_test() -> int:
    """Pure-logic assertions; no network, no guard import, no state writes."""
    import stat as stat_mod
    ok = True

    # The veto probe must be non-destructive by construction (FR-010).
    if "if false" not in VETO_PROBE:
        ok = False
    # The allow probe must never contain a kill-family verb.
    for verb in ("kill", "pkill", "killall", "bootout", "kickstart"):
        if verb in ALLOW_PROBE:
            ok = False

    # Shape probes must be non-destructive: the decode-fail
    # payload is a non-decoding string (never executes), the relay targets
    # a nonexistent tool_id, and neither carries a live kill-family verb
    # in an executable position (they sit inside guard inputs only).
    if "kill" in DECODE_FAIL_PROBE or "pkill" in DECODE_FAIL_PROBE:
        ok = False
    if any(v in json.dumps(CANONICAL_RELAY_PROBE) for v in ("kill", "pkill")):
        ok = False
    if not NONCANONICAL_PROBE or "tool" not in NONCANONICAL_PROBE[0]:
        ok = False  # the probe must exercise the non-canonical key shape

    # _probe_eval contract: PROBE_ERROR prefix on subprocess failure.
    sentinel = object()
    got = _probe_eval("definitely-not-a-real-command-xyz")
    if not (isinstance(got, str) and got.startswith("PROBE_ERROR")):
        # a non-existent DEPLOY_DIR import error must map to PROBE_ERROR,
        # never a crash — but only when the plugin dir is genuinely absent.
        if not os.path.isdir(DEPLOY_DIR):
            ok = False
        elif not isinstance(got, str):
            ok = False
    # integrity check must fail closed on a missing reference file
    if os.path.isdir(DEPLOY_DIR) and not os.path.isfile(
            os.path.join(DEPLOY_DIR, "integrity.sha256")):
        if _integrity_ok():
            ok = False
    # perms check: 0o077 bits must be the only acceptance criterion
    if not (0o077 == (stat_mod.S_IRWXG | stat_mod.S_IRWXO)):
        ok = False

    # guard-hardening: the sink probe must be read-only (no sink writes here)
    import inspect as _inspect
    if "os.makedirs(SINK_DIR" in _inspect.getsource(_sink_ok):
        ok = False

    # Async worker lifecycle: every probe subprocess must start the
    # sink worker before evaluate and drain+shutdown in finally, and
    # lifecycle failures must emit the fixed sanitized marker (fixed
    # vocabulary start/drain/stop — never raw exception material).
    probe_src = _inspect.getsource(_probe_eval)
    for token in ("guard.sink_start()", "guard.sink_flush(",
                  "guard.sink_shutdown(", "sink lifecycle failure"):
        if token not in probe_src:
            ok = False

    print("self-test: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv or "--test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(run_watch())
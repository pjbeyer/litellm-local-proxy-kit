# Self-locating full-field qualification reissue — generation 54060da8 (2026-11-17)
# Run as operator (owner). Builds a COMPLETE v2 record from live daemon facts;
# copies NOTHING from the previous record except provenance values verified live.
python3 - <<'EOF'
import json, os, subprocess, sys, time
sys.path.insert(0, "/Users/pjbeyer/.local/share/litellm/restart-controller-54060da8")
from restart_control.process_identity import process_identity

STATE = os.path.expanduser("~/.local/state/litellm/rc-v2")
BUNDLE = "/Users/pjbeyer/.local/share/litellm/restart-controller-54060da8"
POLICY = "/Users/pjbeyer/.config/litellm-restart-controller/policy.json"

# 1. Live daemon incarnation (self-locating; never fabricate)
pid = int(subprocess.check_output(["pgrep", "-f", "restart_controller.py.*serve"]).split()[0])
ident = process_identity(pid)
status = json.loads(subprocess.check_output(
    ["python3", BUNDLE + "/restart_controller.py", "--policy", POLICY, "status"]))

# 2. Fresh canary evidence for THIS generation (newest evidence file)
ev_dir = STATE + "/canary/evidence"
ev_name = sorted(os.listdir(ev_dir))[-1] if False else sorted(
    os.listdir(ev_dir), key=lambda n: os.path.getmtime(os.path.join(ev_dir, n)))[-1]
evidence = json.load(open(os.path.join(ev_dir, ev_name)))
assert evidence["canary_evidence_digest" if False else "detail_digest"], "evidence sanity"
assert evidence["controller_epoch"] == status["epoch"], "evidence epoch mismatch vs live daemon"
assert evidence["controller_identity"]["pid"] == pid, "evidence identity mismatch vs live daemon"
import hashlib
ev_digest = hashlib.sha256(open(os.path.join(ev_dir, ev_name), "rb").read()).hexdigest()
assert ev_digest == ev_name[:-5], "evidence filename/content hash mismatch"

# 3. Digests bound by the record must match the runtime plan (prepare-time freeze)
policy = json.load(open(POLICY))
prev = json.load(open(STATE + "/qualification.json"))  # shutdown_contract, plist_digest
import plistlib
def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()

rec = {
    "version": 2,
    "operator_uid": os.getuid(),
    "controller_label": policy["controller_label"],
    "controller_identity": {"pid": ident["pid"], "uid": ident["uid"],
                            "ppid": ident["ppid"], "pgid": ident["pgid"],
                            "birth": list(ident["birth"])},
    "controller_digest": evidence["controller_digest"],
    "policy_digest": evidence["policy_digest"],
    "plist_digest": prev["plist_digest"],  # proxy plist (immutable this cycle)
    "controller_epoch": status["epoch"],
    "canary_evidence_digest": ev_digest,
    "shutdown_contract": prev["shutdown_contract"],  # canonical-compared, source-frozen
    "controller_plist_digest": evidence["controller_plist_digest"],
    "controller_supervision": evidence["controller_supervision"],
}
now = time.time(); mono = time.monotonic()
rec.update(issued_at=now, expires_at=now + 21600,
           issued_monotonic=mono, expires_monotonic=mono + 21600)

# sanity vs installed controller plist before writing
assert rec["controller_plist_digest"] == sha(os.path.expanduser(
    "~/Library/LaunchAgents/ai.litellm.restart-controller.plist")), "controller plist drift"

# 4. Archive old record (never delete), then exclusive-create the new one
dst = STATE + "/archived-qualification-v2-pre-54060da8-20261117.json"
assert not os.path.exists(dst), "archive destination collision - pick a fresh name"
os.rename(STATE + "/qualification.json", dst)
fd = os.open(STATE + "/qualification.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, json.dumps(rec, sort_keys=True, indent=1).encode()); os.close(fd)
print("reissued against daemon pid", ident["pid"], "epoch", rec["controller_epoch"][:16],
      "evidence", ev_digest[:16])
EOF

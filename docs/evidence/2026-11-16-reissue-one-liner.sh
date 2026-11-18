# Run as operator (owner). Copies every other binding from the existing record.
python3 - <<'EOF'
import json, os, sys, time
sys.path.insert(0, "~/.local/share/litellm/restart-controller-f7cf523")
from restart_control.process_identity import process_identity
# Find the live daemon: the pid serving 'serve' under our label.
import subprocess
pid = int(subprocess.check_output(["pgrep", "-f", "restart_controller.py.*serve"]).split()[0])
ident = process_identity(pid)  # pid, uid, ppid, pgid, birth — never fabricate
prev = json.load(open(os.path.expanduser("~/.local/state/litellm/rc-v2/qualification.json")))
rec = {**prev,
       "controller_identity": {"pid": ident["pid"], "uid": ident["uid"],
                               "ppid": ident["ppid"], "pgid": ident["pgid"],
                               "birth": list(ident["birth"])},
       "controller_epoch": json.loads(subprocess.check_output(
           ["python3", "~/.local/share/litellm/restart-controller-f7cf523/restart_controller.py",
            "--policy", "~/.config/litellm-restart-controller/policy.json", "status"]))["epoch"]}
now = time.time(); mono = time.monotonic()
rec.update(issued_at=now, expires_at=now+21600,
           issued_monotonic=mono, expires_monotonic=mono+21600)
os.remove(os.path.expanduser("~/.local/state/litellm/rc-v2/qualification.json"))
fd = os.open(os.path.expanduser("~/.local/state/litellm/rc-v2/qualification.json"),
             os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
os.write(fd, json.dumps(rec, sort_keys=True, indent=1).encode()); os.close(fd)
print("reissued against daemon pid", ident["pid"], "epoch", rec["controller_epoch"][:16])
EOF

"""Bounded LiteLLM proxy lifecycle guard for Hermes tool calls.

Evaluates a tool call before execution and blocks patterns that could stop or
disrupt a protected LiteLLM proxy service. Practical command-pattern
protection only — this is intentionally NOT an arbitrary-code sandbox.

Public API (used by the parent plugin shim):

    evaluate(tool_name, args, policy_path) -> None | {"action": "block", "message": ...}

Policy config (JSON, strict schema, loaded fresh on every evaluate call so
PID/session configuration is never stale):

    {"version": 1, "port": 4000,
     "labels": ["example.litellm"],
     "protected_sessions": ["fixture-session"]}

Coverage:
- terminal command strings, execute_code/browser_exec code strings, and
  deferred tool_call payloads ({'calls': [{'name', 'arguments'}, ...]},
  recursing through nested tool_call entries and covering terminal and
  process_manage calls inside them).
- kill/pkill/killall (literal PIDs, process groups, command substitutions,
  lsof/fuser/ps pipelines), fuser -k, launchctl mutators targeting configured
  labels/plists or a whole GUI domain, brew services stop/restart/kill
  targeting litellm or --all, os.kill/os.killpg and kill-family subprocess
  argv in inline Python.
- sudo/env/nohup/timeout wrappers and compound commands (hazard in any
  segment blocks the whole call).

Dynamic discovery (read-only, bounded) runs only for potential process-kill
operations that need PID resolution: /usr/sbin/lsof -t -nP -iTCP:<port>
-sTCP:LISTEN with a timeout, then /bin/ps -p <pids> -o pid=,pgid=,comm=,args=.
Discovery output is never printed. No environment variables are read.

Intentional limitations (documented, not hidden):
- Residual risk: hidden/imported/encoded code, indirection the matcher cannot
  resolve, and model-controlled edits to the policy file itself remain
  outside this guarantee.
- kill targets that cannot be resolved deterministically (shell variables,
  unbound command substitutions, failed process discovery) are refused
  conservatively; read-only probes (kill -0, ps, lsof, health checks) pass.
- Broad interpreter selectors (python, uvicorn, ...) in pkill/killall are
  blocked because they could match the live proxy interpreter.
- No auto-approve tokens, no bypass, and no restart-controller surface.
- Every internal failure fails closed for execution tools: exceptions never
  open the gate.
"""

from __future__ import annotations

import json
import ast
import hashlib
import os
import re
import subprocess
import shlex
from dataclasses import dataclass
from pathlib import Path

__all__ = ["evaluate", "load_policy", "discover_snapshot", "Policy",
           "Snapshot", "BLOCK_MESSAGE", "CATEGORIES", "DECISION_MESSAGES",
           "LOG_CODES", "log_decision"]

import logging

logger = logging.getLogger("proxy_lifecycle_guard")

# ---------------------------------------------------------------------------
# T004: refusal-category vocabulary (spec rev 3, research D2, FR-002/SC-008).
# Fixed enum of sanitized decision categories, each with a stable LGV-NNN
# log code and a fixed sanitized message template. Zero argument/path/PID
# leakage; every template is byte-identical for all refusals in a category.
# INTEGRITY_REF_INVALID / LGV-032 is the reconciliation choice for FR-015's
# distinct integrity-reference failure category (spec pins existence, not
# the name) — flagged in the wave report.
# ---------------------------------------------------------------------------

CATEGORIES = {
    "VETO": "LGV-001",
    "BOUND_SIZE": "LGV-010",
    "BOUND_DEPTH": "LGV-011",
    "BOUND_COUNT": "LGV-012",
    "SHAPE_ENVELOPE": "LGV-020",
    "POLICY_INVALID": "LGV-030",
    "HOOK_MISMATCH": "LGV-031",
    "INTEGRITY_REF_INVALID": "LGV-032",
}

DECISION_MESSAGES = {
    "VETO": None,  # assigned below: the veto template IS BLOCK_MESSAGE
    "BOUND_SIZE": (
        "Blocked by the LiteLLM proxy lifecycle guard: the input is too "
        "large to prove safe; reduce the input size and resubmit."
    ),
    "BOUND_DEPTH": (
        "Blocked by the LiteLLM proxy lifecycle guard: the call nesting is "
        "too deep to prove safe; reduce the call nesting and resubmit."
    ),
    "BOUND_COUNT": (
        "Blocked by the LiteLLM proxy lifecycle guard: the batch is "
        "too large to prove safe; reduce the batch size and resubmit."
    ),
    "SHAPE_ENVELOPE": (
        "Blocked by the LiteLLM proxy lifecycle guard: the call could not "
        "be proven safe; restructure it."
    ),
    "POLICY_INVALID": (
        "Blocked by the LiteLLM proxy lifecycle guard: the guard's policy "
        "must be repaired by the owner — do not modify guard files."
    ),
    "HOOK_MISMATCH": (
        "Blocked by the LiteLLM proxy lifecycle guard: the call could not "
        "be attributed to a known tool; contact the owner."
    ),
    "INTEGRITY_REF_INVALID": (
        "Blocked by the LiteLLM proxy lifecycle guard: the guard's "
        "integrity reference is missing or unreadable and must be "
        "re-established by the owner — do not modify guard files."
    ),
}

LOG_CODES = dict(CATEGORIES)


def log_decision(category, tool_name=""):
    """Hook-contract rule 6: emit our own log line per veto/refuse."""
    code = CATEGORIES.get(category, "LGV-000")
    logger.info("lifecycle-guard decision: category=%s code=%s tool=%s",
                category, code, tool_name or "<unknown>")


# Fixed, sanitized block message. Deliberately identical for every block
# reason: no arguments, paths, PIDs, or exception details ever leak.
BLOCK_MESSAGE = (
    "Blocked by the LiteLLM proxy lifecycle guard: this request could stop "
    "the protected proxy, or its safety could not be established. Use the "
    "owner-reviewed controlled restart workflow; do not substitute another "
    "kill command or execution path."
)

DECISION_MESSAGES["VETO"] = BLOCK_MESSAGE

EXECUTION_TOOLS = frozenset({"terminal", "execute_code", "browser_exec", "tool_call"})
PROCESS_MUTATING_ACTIONS = frozenset({"kill", "interrupt", "write", "submit", "close", "handoff"})

LAUNCHCTL_MUTATORS = frozenset({
    "bootout", "bootstrap", "kickstart", "kill", "unload", "load",
    "stop", "start", "remove", "enable", "disable",
})
BREW_SERVICE_BLOCK_VERBS = frozenset({"stop", "restart", "kill"})

# Broad selectors that could match the live proxy interpreter or its server.
BROAD_SELECTORS = (
    "python", "python2", "python3", "uv", "uvicorn", "gunicorn",
    "node", "sh", "bash", "zsh", "ruby", "perl", "java",
)

WRAPPER_VERBS = frozenset({"sudo", "env", "nohup", "nice", "time", "timeout", "stdbuf",
                           "command", "builtin", "exec"})

LSOF_CANDIDATES = ("/usr/sbin/lsof", "/sbin/lsof", "/usr/bin/lsof")
PS_CANDIDATES = ("/bin/ps", "/usr/bin/ps")

# Bounds: keep every evaluation fast and bounded under the hook budget.
# Legacy module constants remain for backwards compatibility of direct
# helper calls; the rev-3 bounds live in the policy (bounds object) and
# DEFAULT_BOUNDS below.
MAX_TEXT_CHARS = 65536
MAX_CHUNKS = 200
MAX_STAGES = 40
MAX_TOKENS = 64
MAX_TOOL_CALL_DEPTH = 8
DISCOVERY_TIMEOUT_S = 3.0
MAX_PIDS = 50

_SIGNAL_OPT_RE = re.compile(r"-[A-Za-z]\w*")
_SIGNED_INT_RE = re.compile(r"[+-]?\d+")
_OS_KILL_RE = re.compile(r"\bos\s*\.\s*kill(?:pg)?\s*\(")
_PSUTIL_KILL_RE = re.compile(r"\.\s*(?:kill|terminate)\s*\(")
_DOMAIN_ROOT_RE = re.compile(r"(?:gui|user)/\d+\s*$")
_DOMAIN_SUBST_RE = re.compile(r"(?:gui|user)/\$\(")
_SHELL_KEYWORDS = ("kill", "pkill", "killall", "lsof", "fuser", "launchctl",
                   "brew services", "xargs")

# FR-007 bound decision table support: over-bound inputs get a bounded
# head-scan; provably intent-free benign shapes pass (row a), unprovable
# shapes refuse with the bound's category (row c).
HEAD_SCAN_CHARS = 8192
# A batch larger than this cannot be analyzed within the hook budget at
# all: the uninspectable remainder makes it unprovable (FR-007 row c).
HARD_BATCH_CAP = 2048

BENIGN_VERBS = frozenset({
    "printf", "echo", "cat", "ls", "git", "pip", "grep", "awk", "sed",
    "head", "tail", "wc", "sort", "uniq", "tr", "cut", "find", "cp",
    "mv", "touch", "mkdir", "date", "true", "false", "test", "curl",
    "ps", "which", "whoami", "id", "pwd", "env", "export", "set",
})

# Shell control-flow keywords stripped from segment heads so wrapped
# kill-family verbs are still analyzed (F-001/F-006).
CONTROL_KEYWORDS = frozenset({
    "then", "do", "else", "{", "}", "!", "in", ";;", "fi", "done",
    "esac", "if", "while", "until", "for", "case",
})

# F-017: the guard's own deployed files are owner-immutable; mutation
# verbs targeting the plugin directory veto.
PLUGIN_DIR_MARKER = "plugins/proxy-lifecycle-guard"
MUTATING_VERBS = frozenset({
    "rm", "mv", "cp", "tee", "ln", "install", "chmod", "chown", "sed",
    "rsync", "dd", "shred", "truncate", "red", "write",
})


class _Block(Exception):
    """Internal: raise anywhere analysis determines the call must be blocked."""


class _BoundRefuse(_Block):
    """Internal: over-bound and unprovable — refuse with a bound category.

    Carries the FR-007 bound category (BOUND_SIZE / BOUND_DEPTH /
    BOUND_COUNT) so the entry point returns that category's sanitized
    message and LGV code instead of the generic veto.
    """

    def __init__(self, category):
        super().__init__(category)
        self.category = category


class PolicyError(Exception):
    """Policy config is missing, unreadable, tampered, or fails its strict schema."""


class IntegrityRefError(Exception):
    """The owner-established integrity reference is missing, unreadable,
    or unparseable (FR-015): its own fail-closed category, never a skip."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_integrity_ref(policy_path):
    """Load the owner-established integrity reference (FR-015/D3).

    The reference lives beside the policy as ``integrity.sha256`` and is
    owner-established / owner-updated only; the guarded agent can never
    author it. Missing, unreadable, or unparseable raises
    IntegrityRefError — never treated as a skip.
    """
    ref_path = Path(policy_path).with_name("integrity.sha256")
    try:
        raw = ref_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise IntegrityRefError("unreadable integrity reference") from exc
    if (not isinstance(data, dict)
            or not isinstance(data.get("policy_digest"), str)
            or not isinstance(data.get("source_digests"), dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in data["source_digests"].items())):
        raise IntegrityRefError("integrity reference schema mismatch")
    return data


def verify_policy_integrity(policy_path, ref):
    """FR-015: the policy bytes must match the owner-recorded digest."""
    digest = sha256_bytes(Path(policy_path).read_bytes())
    return digest == ref["policy_digest"]


def verify_source_integrity(policy_path, ref):
    """FR-015/F-019: deployed source must match owner-recorded digests.

    Self-contained (D3): compares the deployed source files beside the
    policy against the digests the owner recorded at install — no runtime
    dependency on the kit checkout. Byte-for-byte; any mismatch is a
    tampered deployment.
    """
    directory = Path(policy_path).parent
    for name, expected in ref["source_digests"].items():
        source = directory / name
        try:
            if sha256_bytes(source.read_bytes()) != expected:
                return False
        except Exception:
            return False
    return True


@dataclass(frozen=True)
class Policy:
    port: int
    labels: frozenset
    protected_sessions: frozenset
    bounds: "dict | None" = None  # rev-3 bounds object (data-model.md)

    def __post_init__(self):
        if self.bounds is None:
            object.__setattr__(self, "bounds", dict(DEFAULT_BOUNDS))


# Rev-3 bounds with schema caps (contracts/policy-schema.json). The caps
# ARE the load-time hook-budget guard (FR-007/AR-008): a schema-valid
# policy cannot configure the guard past its runtime budget.
SCHEMA_BOUND_CAPS = {
    "input_text_bytes": (1024, 262144),
    "max_chunks": (1, 512),
    "max_depth": (1, 8),
    "discovery_timeout_ms": (100, 2000),
    "max_snapshot_pids": (1, 256),
}
DEFAULT_BOUNDS = {
    "input_text_bytes": 262144,
    "max_chunks": 512,
    "max_depth": 8,
    "discovery_timeout_ms": 2000,
    "max_snapshot_pids": 256,
}


def _check_owner_only_permissions(path, label):
    """FR-009/T006: owner-only file permissions on policy AND reference."""
    mode = path.stat().st_mode & 0o777
    if mode & 0o022:  # group-writable or world-writable
        raise PolicyError(f"{label} not owner-only")


def load_policy(policy_path):
    """Strictly parse and authenticate the policy JSON (rev-3, FR-008).

    Digest-first ordering (T006): the integrity reference is read and the
    policy digest verified BEFORE the schema is trusted. Re-read from disk
    on every evaluation — no caching (D5).
    """
    path = Path(policy_path)
    # 0. missing/unreadable policy is a POLICY_INVALID-class failure first
    #    (FR-005): when the policy itself is absent, the policy failure
    #    dominates — the integrity-ref category applies to a present
    #    policy whose reference is missing/broken (FR-015)
    try:
        path.stat()
    except OSError as exc:
        raise PolicyError("unreadable policy") from exc
    # 1. integrity reference (own category, FR-015)
    ref = read_integrity_ref(path)
    # 2. owner-only permissions on policy AND reference (FR-009)
    _check_owner_only_permissions(path, "policy")
    _check_owner_only_permissions(path.with_name("integrity.sha256"),
                                  "integrity reference")
    # 3. policy digest must match the owner-recorded digest (FR-015)
    if not verify_policy_integrity(path, ref):
        raise PolicyError("tampered policy")
    # 3b. deployed source digests (F-019/T005): the reference MUST
    # carry digests for both deployed source files; an empty or partial
    # source_digests map would skip verification entirely (codex review
    # P2) — a divergent deployed copy could pass as untampered. Fail
    # closed unless the reference is complete.
    REQUIRED_SOURCE_DIGESTS = ("guard.py", "__init__.py")
    digests = ref.get("source_digests")
    if (not isinstance(digests, dict)
            or not set(REQUIRED_SOURCE_DIGESTS) <= set(digests)):
        raise PolicyError("incomplete integrity reference")
    if not verify_source_integrity(path, ref):
        raise PolicyError("tampered deployed source")
    # 4. strict schema (FR-008)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # missing, unreadable, or invalid JSON
        raise PolicyError("unreadable policy") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")
    expected = {"schema_version", "protected_port", "protected_labels",
                "protected_session_handles", "bounds",
                "refusal_vocabulary_version"}
    if set(data.keys()) != expected:
        raise PolicyError("policy schema mismatch")
    version = data["schema_version"]
    if isinstance(version, bool) or version != 1:
        raise PolicyError("unsupported policy version")
    vocab = data["refusal_vocabulary_version"]
    if isinstance(vocab, bool) or not isinstance(vocab, int) or vocab < 1:
        raise PolicyError("unsupported refusal vocabulary version")
    port = data["protected_port"]
    if (isinstance(port, bool) or not isinstance(port, int)
            or not (1 <= port <= 65535)):
        raise PolicyError("invalid port")
    labels = data["protected_labels"]
    sessions = data["protected_session_handles"]
    for seq in (labels, sessions):
        if not isinstance(seq, list) or not all(
            isinstance(item, str) and item for item in seq
        ):
            raise PolicyError("invalid labels/sessions")
    bounds = data["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != set(SCHEMA_BOUND_CAPS):
        raise PolicyError("invalid bounds")
    for key, (lo, hi) in SCHEMA_BOUND_CAPS.items():
        val = bounds[key]
        if (isinstance(val, bool) or not isinstance(val, int)
                or not (lo <= val <= hi)):
            raise PolicyError("bound out of budget range")
    return Policy(
        port=port,
        labels=frozenset(label.lower() for label in labels),
        protected_sessions=frozenset(sessions),
        bounds=dict(bounds),
    )


@dataclass(frozen=True)
class Snapshot:
    """Read-only process discovery result. Empty sets are a determinate
    'nothing is listening' answer; None (from discover_snapshot) means the
    discovery itself failed and callers must refuse conservatively."""
    pids: frozenset = frozenset()
    pgids: frozenset = frozenset()
    comms: frozenset = frozenset()
    args: tuple = ()


def _block(category="VETO", tool_name=""):
    """One Evaluation Decision (data-model.md): block with category + code.

    Sanitized fixed template per category (FR-002); the plugin emits its
    own log line so distinguishability does not depend on runtime
    surfacing (hook-contract rule 6).
    """
    message = DECISION_MESSAGES.get(category, BLOCK_MESSAGE)
    log_decision(category, tool_name)
    return {"action": "block", "message": message,
            "category": category, "log_code": LOG_CODES[category]}





def _find_binary(candidates):
    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def discover_snapshot(port, timeout=DISCOVERY_TIMEOUT_S):
    """Read-only discovery of the process listening on ``port``.

    Returns a Snapshot (possibly empty — a determinate answer), or None when
    discovery itself failed (caller must refuse conservatively). Never
    prints discovery output.
    """
    lsof = _find_binary(LSOF_CANDIDATES)
    ps = _find_binary(PS_CANDIDATES)
    if lsof is None or ps is None:
        return None
    try:
        probe = subprocess.run(
            [lsof, "-t", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if probe.returncode not in (0, 1):  # 1 == nothing listening (determinate)
        return None
    raw_pids = probe.stdout.split()
    if (len(raw_pids) > MAX_PIDS or any(not p.isdigit() for p in raw_pids)
            or (probe.returncode == 0 and not raw_pids)
            or (probe.returncode == 1 and (raw_pids or probe.stderr.strip()))):
        return None
    pids = sorted(set(map(int, raw_pids)))
    snap_pids, snap_pgids, snap_comms, snap_args = set(), set(), set(), []
    if pids:
        try:
            detail = subprocess.run(
                [ps, "-p", ",".join(str(p) for p in pids),
                 "-o", "pid=,pgid=,comm=,args="],
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception:
            return None
        if detail.returncode != 0:
            return None
        for line in detail.stdout.splitlines()[:MAX_PIDS]:
            parts = line.split(None, 3)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                snap_pids.add(int(parts[0]))
                snap_pgids.add(int(parts[1]))
                if len(parts) >= 3:
                    snap_comms.add(parts[2].lower())
                if len(parts) == 4:
                    snap_args.append(parts[3])
    if set(pids) != snap_pids:
        return None  # incomplete/malformed ps output is not an empty listener
    return Snapshot(pids=frozenset(snap_pids), pgids=frozenset(snap_pgids),
                    comms=frozenset(snap_comms), args=tuple(snap_args))


# ---------------------------------------------------------------------------
# Bounded shell-text scanning (pure helpers; injectable via evaluate kwargs)
# ---------------------------------------------------------------------------

_CHUNK_SEPS = ("&&", "||", ";", "\n", "\r", "&")


def _split_segments(text, separators, limit=MAX_CHUNKS):
    """Quote-aware split on shell separators. Bounded by ``limit``."""
    parts, buf = [], []
    quote = None
    i, n = 0, len(text)
    seps = sorted(separators, key=len, reverse=True)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            if i + 1 < n:
                buf.append(text[i + 1])
            i += 2
            continue
        matched = None
        for sep in seps:
            if text.startswith(sep, i):
                if sep == "&":
                    nxt = text[i + 1] if i + 1 < n else " "
                    prv = text[i - 1] if i > 0 else " "
                    # bare background '&', not '2>&1' and not '&&'
                    if (nxt.isspace() or nxt in (";", "\n", "\r")) and prv not in "<>0123456789":
                        matched = sep
                else:
                    matched = sep
                break
        if matched:
            parts.append("".join(buf))
            buf = []
            i += len(matched)
            if len(parts) >= limit:
                parts.append(text[i:])
                return parts
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _tokenize(segment, limit=MAX_TOKENS):
    """Decode ordinary shell quoting without executing expansions."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        raise _Block() from None
    return tokens[:limit], len(tokens) >= limit


def _strip_wrappers(tokens):
    """Peel sudo/env/nohup/timeout-style wrappers (bounded)."""
    for _ in range(8):
        if tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
            tokens = tokens[1:]
            continue
        if not tokens:
            return tokens
        head = tokens[0]
        base = head.rsplit("/", 1)[-1]
        if base not in WRAPPER_VERBS:
            return tokens
        tokens = tokens[1:]
        if base == "env":
            for _ in range(MAX_TOKENS):
                if not tokens:
                    break
                token = tokens[0]
                if token in ('-u', '--unset', '-C', '--chdir'):
                    if len(tokens) < 2:
                        raise _Block()
                    tokens = tokens[2:]
                elif token in ('-i', '--ignore-environment', '--') or re.fullmatch(
                        r"[A-Za-z_]\w*=.*", token) or token.startswith(('--unset=', '--chdir=')):
                    tokens = tokens[1:]
                elif token.startswith('-'):
                    raise _Block()  # unsupported wrapper option, not a safe command
                else:
                    break
        elif base == "timeout":
            if tokens and re.fullmatch(r"[\d.]+[smh]?", tokens[0]):
                tokens = tokens[1:]
            elif tokens and tokens[0].startswith('-'):
                raise _Block()
        elif base in ("sudo", "nice", "stdbuf", "command", "builtin", "exec"):
            operands = {'sudo': {'-u', '-g', '-h', '-p', '-C', '--user', '--group'},
                        'nice': {'-n'}, 'exec': {'-a'}}.get(base, set())
            for _ in range(MAX_TOKENS):
                if not tokens or not tokens[0].startswith('-'):
                    break
                if tokens[0] in operands:
                    if len(tokens) < 2:
                        raise _Block()
                    tokens = tokens[2:]
                else:
                    tokens = tokens[1:]
    return tokens


def _mentions_target(low_text, policy):
    if "litellm" in low_text:
        return True
    if any(label in low_text for label in policy.labels):
        return True
    if re.search(r"(?<!\d)%d(?!\d)" % policy.port, low_text):
        return True
    return False


def _analyze_pattern_kill(verb, tokens, policy, get_snapshot):
    """pkill/killall: block litellm/label/broad-selector/uid-sweeper patterns."""
    if any(tok in ("-u", "-U", "--uid", "--user") for tok in tokens):
        raise _Block()  # user-wide sweep could kill the proxy user's processes
    pattern_toks = [t for t in tokens if not t.startswith("-")] or tokens
    pattern = " ".join(pattern_toks).strip("'\"").lower()
    if not pattern:
        return
    # Identity match against the configured proxy identity. Token match,
    # NOT substring: "audit-not-litellm" is its own word and does not
    # hit the label "example.litellm" (F-012/F-013 over-blocking fix).
    # A pattern containing regex metacharacters whose LITERAL projection
    # (dots→any-char) still matches the proxy identity is a match: a dot
    # in "litellm.*proxy" is literal-adjacent to the real identity.
    snap = get_snapshot()
    if snap is None:
        raise _Block()  # conservative refusal on discovery failure
    identity_pieces = set()
    for label in policy.labels:
        identity_pieces.add(label)
        identity_pieces.update(label.split("."))
    identity_pieces.add("litellm")
    pattern_words = set(re.split(r"[^a-z0-9._-]+", pattern))
    for piece in identity_pieces:
        if piece in pattern_words:
            raise _Block()
    # dot-as-any-char projection, bounded: pattern-word dots act as
    # single-char wildcards, so "l.tellm" hits "litellm" (F-013: only
    # pattern-side dots project; identity pieces match literally)
    for word in pattern_words:
        if "." not in word:
            continue
        rx = re.compile("".join(
            re.escape(c) if c != "." else "." for c in word))
        for piece in identity_pieces:
            if piece and rx.fullmatch(piece):
                raise _Block()
    # literal substring against the LIVE snapshot args/comms: the
    # pattern matching the running proxy's argv/comm is a direct hit
    for argline in (*snap.args, *snap.comms):
        if pattern in argline.lower():
            raise _Block()
    # The python family needs version-suffix handling (python, python3, 3.15):
    # a broad interpreter selector could kill the live proxy interpreter.
    if re.search(r"(?<![\w./-])python[23]?(?:\.\d+)?(?![\w.-])", pattern):
        raise _Block()
    for selector in BROAD_SELECTORS:
        if selector == "python":
            continue
        if re.search(r"(?<![\w./-])%s(?![\w.-])" % re.escape(selector), pattern):
            raise _Block()
    # Refuse regex selectors rather than evaluate attacker-controlled
    # regexes against process data (overbroad matches, regex CPU burn).
    # A lone dot is NOT refused here (F-013: dotted module paths are
    # common literal patterns); the identity check above already ran.
    if any(c in pattern for c in "*+?[](){}|^$\\"):
        raise _Block()


def _analyze_kill_stage(stage_text, tokens, policy, get_snapshot):
    sig = None
    targets = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            targets.extend(tokens[i + 1:])
            break
        if tok in ("-s", "-n") and i + 1 < len(tokens):
            sig = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1 and tok[1:].isdigit():
            if sig is None:
                sig = tok[1:]
            else:
                targets.append(tok)  # negative PID == process group
            i += 1
            continue
        if sig is None and _SIGNAL_OPT_RE.fullmatch(tok):
            sig = tok[1:]
            i += 1
            continue
        targets.append(tok)
        i += 1
    if sig is not None and str(sig).lstrip("-").upper() in ("0", "SIGZERO", "ZERO"):
        return True  # read-only liveness probe
    if not targets:
        return  # bare kill in a pipeline; handled by the pipeline cross-check
    for target in targets:
        if _SIGNED_INT_RE.fullmatch(target):
            pid = int(target)
            if pid in (0, -1):
                raise _Block()  # current group / every permitted process
            snap = get_snapshot()
            if snap is None:
                raise _Block()  # conservative refusal on discovery failure
            if pid < 0:
                if -pid in snap.pgids:
                    raise _Block()
            elif pid in snap.pids:
                raise _Block()
            continue
        if target.startswith("$(") or target.startswith("`"):
            # Another port or a number in lsof/ps text does not prove that the
            # resulting PID is unrelated. Never execute a target substitution.
            raise _Block()
        if target.startswith("$") or target.startswith("${"):
            raise _Block()  # shell variable target: unresolvable, refuse
        # job specs (%1) and words: not PID-shaped, ignore


def _analyze_launchctl(tokens, policy):
    if not tokens:
        return
    verb = tokens[0].lower()
    if verb not in LAUNCHCTL_MUTATORS:
        return  # print/list/blame/get and friends are read-only
    rest = " ".join(tokens[1:]).lower().strip()
    if "litellm" in rest or any(label in rest for label in policy.labels):
        raise _Block()
    # FR-006: variable or substitution operands are unresolved — refuse
    # rather than guess (launchctl bootout gui/501/$LABEL etc.)
    for tok in tokens[1:]:
        if "$" in tok or "`" in tok:
            raise _Block()
    if _DOMAIN_ROOT_RE.fullmatch(rest):
        raise _Block()  # whole GUI/user domain bootout/bootstrap
    if _DOMAIN_SUBST_RE.search(rest):
        raise _Block()  # gui/$(id -u) style whole-domain targeting


def _analyze_brew(tokens, policy):
    if "services" not in tokens:
        return
    idx = tokens.index("services")
    sub_verb = tokens[idx + 1].lower() if idx + 1 < len(tokens) else ""
    if sub_verb not in BREW_SERVICE_BLOCK_VERBS:
        return
    rest = " ".join(tokens[idx + 2:]).lower()
    if "--all" in tokens or "litellm" in rest:
        raise _Block()
    # FR-006: unresolved service-name operands refuse (brew services
    # stop $SVC — the operand cannot be proven unrelated)
    for tok in tokens[idx + 2:]:
        if "$" in tok or "`" in tok:
            raise _Block()


# Control-flow keywords that become chunk separators so wrapped bodies
# are analyzed (F-001/F-006). Quote-aware splitting downstream keeps
# quoted text safe from false segmentation.
_CF_SUB_RE = re.compile(r"\b(?:then|do|else|done|fi|esac)\b|;;")
_CASE_WORD_RE = re.compile(r"\w+\)")


def _strip_control_flow(tokens):
    """Strip shell control-flow residue so wrapped verbs are analyzed.

    Single mutation point for the Constitution III control-flow scan
    branch (T013): disabling this function (and the _CF_SUB_RE
    substitution) must demonstrably flip the control-flow-wrapped veto
    outcomes to allow — proving the battery measures the real guard.
    """
    while tokens:
        t0 = tokens[0].lower()
        if t0 == "case":
            try:
                tokens = tokens[tokens.index("in") + 1:]
            except ValueError:
                tokens = []
            continue
        if (t0 in CONTROL_KEYWORDS or t0 == "in"
                or _CASE_WORD_RE.fullmatch(tokens[0])):
            tokens = tokens[1:]
            continue
        break
    # "time -p kill ..." / "time kill ...": time is a wrapper verb
    if tokens and tokens[0].lower() == "time":
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[1:]
    return tokens


def _analyze_chunk(chunk, policy, get_snapshot, depth=0):
    """Analyze one command chunk (may be a pipeline). Hazard anywhere blocks."""
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    chunk = chunk.strip()
    if chunk.startswith("(") and chunk.endswith(")"):
        _analyze_shell_text(chunk[1:-1], policy, get_snapshot, depth + 1)
        return
    # F-017: mutation of the guard's own deployed plugin directory (or
    # self-disable/uninstall through the runtime CLI) vetoes
    low_chunk = chunk.lower()
    if PLUGIN_DIR_MARKER in low_chunk:
        words = low_chunk.split()
        first_word = words[0] if words else ""
        if (first_word in MUTATING_VERBS or ">" in chunk
                or any("plugin" in w for w in words[:1])):
            raise _Block()
    # control-flow bodies: turn keyword boundaries into chunk separators
    chunk = _CF_SUB_RE.sub(";", chunk)
    stages = [s for s in _split_segments(chunk, ("|",), limit=MAX_STAGES) if s.strip()]
    if len(stages) >= MAX_STAGES:
        raise _Block()
    kill_like = False
    stage_texts = []
    for stage in stages:
        stage_texts.append(stage)
        # each pipeline stage may itself contain control-flow segments
        for sub in _split_segments(stage, _CHUNK_SEPS):
            if not sub.strip():
                continue
            tokens, truncated = _tokenize(sub)
            if truncated:
                # FR-007: over the token bound — the veto is never lost
                # (intent scan), and intent-free truncation allows the
                # bounded head analysis
                if _full_text_kill_intent(sub, policy):
                    raise _Block()
            # strip control-flow residue so wrapped verbs get analyzed
            tokens = _strip_control_flow(tokens)
            # variable command indirection is unresolvable — refuse (FR-006)
            if tokens and (tokens[0].startswith("$") or tokens[0].startswith("`")):
                raise _Block()
            # assignment whose value carries kill-family intent
            if tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
                if _full_text_kill_intent(tokens[0].split("=", 1)[1], policy):
                    raise _Block()
            tokens = _strip_wrappers(tokens)
            if not tokens:
                continue
            verb = tokens[0].rsplit("/", 1)[-1].lower()
            rest = tokens[1:]
            if verb == "eval":
                raise _Block()  # FR-003: eval payloads are never evaluated
            if (verb == "hermes"
                    and any("plugin" in t.lower() for t in rest)):
                sub_l = [t.lower() for t in rest]
                if ("disable" in sub_l or "uninstall" in sub_l) and \
                        "proxy-lifecycle-guard" in " ".join(sub_l):
                    raise _Block()  # F-017 self-disable through the CLI
            if verb in ("sh", "bash", "zsh") and any(
                    t.startswith("-") and "c" in t for t in rest):
                idx = next(i for i, t in enumerate(rest)
                           if t.startswith("-") and "c" in t)
                if idx + 1 >= len(rest):
                    raise _Block()
                _analyze_shell_text(rest[idx + 1], policy, get_snapshot, depth + 1)
            elif re.fullmatch(r"python(?:[23](?:\.\d+)?)?", verb) and "-c" in rest:
                idx = rest.index("-c")
                if idx + 1 >= len(rest):
                    raise _Block()
                _analyze_code_text(rest[idx + 1], policy, get_snapshot)
            elif verb == "kill":
                probe_only = _analyze_kill_stage(sub, rest, policy, get_snapshot)
                kill_like = kill_like or not probe_only
            elif verb in ("pkill", "killall"):
                _analyze_pattern_kill(verb, rest, policy, get_snapshot)
                kill_like = True
            elif verb == "xargs":
                if any(t.rsplit('/', 1)[-1] in ("kill", "pkill", "killall")
                       for t in rest):
                    raise _Block()  # stdin targets unresolved; no PID proof
                # F-007: xargs spawning a shell with a payload is a kill
                # pipeline shape (listener discovery feeding a signal)
                if any(t.rsplit('/', 1)[-1].lower() in
                       ("sh", "bash", "zsh") for t in rest) and \
                        any(t == "-c" or t.startswith("-c") or t.startswith("-I")
                            for t in rest):
                    raise _Block()
                if any(re.fullmatch(r"python[23]?", t.rsplit('/', 1)[-1].lower())
                       for t in rest):
                    raise _Block()
            elif verb == "fuser":
                if any(t == "-k" or t.startswith("-k") for t in rest):
                    for tok in rest:
                        if tok.split("/")[0] == str(policy.port):
                            raise _Block()
            elif verb == "launchctl":
                _analyze_launchctl(rest, policy)
            elif verb == "brew":
                _analyze_brew(rest, policy)
    if kill_like and len(stages) > 1:
        # The kill stage's targets may arrive via stdin from another
        # lsof/ps/grep stage: block when any stage references the
        # configured port or the proxy name/label. Single-stage kills
        # were already decided by their own target/pattern analysis —
        # the cross-check must not re-veto on the pattern's own text.
        joined = " ".join(stage_texts).lower()
        if _mentions_target(joined, policy):
            raise _Block()


def _command_substitutions(text):
    """Yield executable substitutions, excluding escaped/single-quoted text."""
    quote = None
    i = count = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "'" and quote is None:
            quote = c
            i += 1
            continue
        if c == '"':
            quote = None if quote == '"' else '"'
            i += 1
            continue
        tick = c == '`'
        if tick or text.startswith('$(', i) or (
                quote is None and text[i:i + 2] in ('<(', '>(')):
            start = i + (1 if tick else 2)
            j, depth, inner_quote = start, 1, None
            while j < len(text):
                ch = text[j]
                if ch == "\\" and inner_quote != "'":
                    j += 2
                    continue
                if tick and ch == '`':
                    break
                if inner_quote:
                    if ch == inner_quote:
                        inner_quote = None
                elif ch in ("'", '"'):
                    inner_quote = ch
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(text):
                raise _Block()
            count += 1
            if count > MAX_CHUNKS:
                raise _Block()
            yield text[start:j]
            i = j + 1
            continue
        i += 1


_HEAD_INTENT_RESIDUE_RE = re.compile(
    r"\b(?:kill|pkill|killall|fuser|launchctl|brew|sudo|sh|bash|zsh|eval)\b")


def _analyze_chunk_scanonly(head_text, policy, get_snapshot, depth):
    """FR-007 rows (b)/(c): bounded head-scan of an over-bound input.

    Kill-family intent in the head (row b) keeps the veto — the veto is
    never lost to the bound. An intent-free head that cannot be PARSED
    (unbalanced constructs like ${a[$((i++))]}) is unprovable (row c):
    refuse BOUND_SIZE instead of veto.
    """
    if _full_text_kill_intent(head_text, policy):
        _analyze_shell_text(head_text, policy, get_snapshot, depth + 1)
        return
    try:
        _analyze_shell_text(head_text, policy, get_snapshot, depth + 1)
    except _Block:
        raise _BoundRefuse("BOUND_SIZE")


# Row (b) full-text intent scan: word-boundary regex so shell
# whitespace variants (kill\t123, brew\tservices) cannot evade the
# literal-substring scan the way plain "kill " could (codex review P1).
_FULL_TEXT_INTENT_RE = re.compile(
    r"\b(?:pkill|killall|kill|fuser|launchctl|eval|xargs"
    r"|os\.kill|process_manage)\b"
    r"|\b(?:brew\s+services|sh\s+-c|kill\s+-|process\s+group)\b"
)


def _full_text_kill_intent(text, policy):
    """Row (b) fallback: whole-text keyword scan for kill-family intent.

    Bounded by a single regex pass, immune to regex burn, and tolerant
    of ANY shell whitespace between a verb and its operands. If any
    kill-family marker appears anywhere in the over-bound input, the
    veto is never lost to the bound.
    """
    return _FULL_TEXT_INTENT_RE.search(text.lower()) is not None


def _analyze_shell_text(text, policy, get_snapshot, depth=0):
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    if not isinstance(text, str):
        raise _Block()
    # FR-007 bound decision table: over input_text_bytes, run a bounded
    # head-scan. (a) provably intent-free → allow; (b) kill-family
    # intent in the inspectable head → veto (never lost to the bound);
    # (c) head unprovable → refuse BOUND_SIZE.
    size_bound = policy.bounds["input_text_bytes"] if policy and policy.bounds else MAX_TEXT_CHARS
    if len(text.encode("utf-8")) > size_bound:
        head = text[:HEAD_SCAN_CHARS]
        # (b) intent found in the inspectable head → the veto fires
        _analyze_chunk_scanonly(head, policy, get_snapshot, depth)
        # (a) head provably intent-free AND full-text benign keyword scan
        if _full_text_kill_intent(text, policy):
            raise _Block()
        # (c) benign keywords everywhere BUT unresolved shell constructs
        # ($((...)) / ${...}) make the remainder unprovable → BOUND_SIZE;
        # fully-resolvable benign shapes are row (a) allows
        if "${" in head or "$((" in head or "$[" in head:
            raise _BoundRefuse("BOUND_SIZE")
        return
    if len(text) > MAX_TEXT_CHARS:
        raise _Block()
    for substitution in _command_substitutions(text):
        _analyze_shell_text(substitution, policy, get_snapshot, depth + 1)
    chunks = _split_segments(text, _CHUNK_SEPS)
    if len(chunks) >= MAX_CHUNKS:
        raise _Block()
    for chunk in chunks:
        if chunk.strip():
            _analyze_chunk(chunk, policy, get_snapshot, depth)


# ---------------------------------------------------------------------------
# Python code scanning (execute_code / browser_exec / inline subprocess)
# ---------------------------------------------------------------------------

def _iter_string_literals(text, limit=80):
    """Use Python's parser for multiline/escaped literals and literal argv.

    Unknown argv operands become unresolved shell operands, never evaluated.
    Imported/generated code remains outside this bounded static contract.
    """
    tree = ast.parse(text)
    count = 0
    for position, node in enumerate(ast.walk(tree)):
        if position > 10000:
            raise _Block()
        value = None
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = node.value.decode('utf-8') if isinstance(node.value, bytes) else node.value
        elif isinstance(node, ast.JoinedStr):
            value = ''.join(part.value if isinstance(part, ast.Constant) and
                            isinstance(part.value, str) else '$UNRESOLVED'
                            for part in node.values)
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                executable = first.value.rsplit('/', 1)[-1]
                if executable in WRAPPER_VERBS or executable in (
                        'sh', 'bash', 'zsh', 'kill', 'pkill', 'killall',
                        'fuser', 'launchctl', 'brew'):
                    values = [str(part.value) if isinstance(part, ast.Constant)
                              and isinstance(part.value, (str, int)) else '$UNRESOLVED'
                              for part in node.elts]
                    value = shlex.join(values)
        if value is not None:
            count += 1
            if count >= limit:
                raise _Block()
            yield value


_SIGNAL0_FORMS = frozenset({"0", "signal.SIG_ZERO", "SIG_ZERO", "signal.SIG_0"})


def _all_os_kills_signal_zero(text):
    """True only when EVERY os.kill/killpg call in text uses signal 0."""
    for m in _OS_KILL_RE.finditer(text):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if depth:  # unbalanced — not provable
            return False
        inner = text[start:i - 1]
        parts, buf, d = [], [], 0
        for ch in inner:
            if ch in "([":
                d += 1
            elif ch in ")]":
                d -= 1
            if ch == "," and d == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf))
        if len(parts) < 2:
            return False  # no visible signal operand — not provable
        sig = parts[1].strip()
        if sig not in _SIGNAL0_FORMS:
            return False
    return True


def _analyze_code_text(text, policy, get_snapshot):
    if not isinstance(text, str):
        raise _Block()
    if len(text) > MAX_TEXT_CHARS:
        raise _Block()
    if _OS_KILL_RE.search(text):  # os.kill( / os.killpg(
        # F-014 carve-out: signal-0 liveness probes pass; any real
        # signal — or an unprovable call shape — vetoes
        if not _all_os_kills_signal_zero(text):
            raise _Block()
    if "psutil" in text and _PSUTIL_KILL_RE.search(text):
        raise _Block()
    # F-017: writing to the guard's own deployed files via Python veto
    low = text.lower()
    if PLUGIN_DIR_MARKER in low and ("write" in low or "open(" in low):
        raise _Block()
    for literal in _iter_string_literals(text):
        low = literal.lower()
        if any(keyword in low for keyword in _SHELL_KEYWORDS):
            _analyze_shell_text(literal, policy, get_snapshot)


# ---------------------------------------------------------------------------
# Tool-call recursion and process_manage policy
# ---------------------------------------------------------------------------

def _check_process_manage(arguments, policy):
    if not isinstance(arguments, dict):
        raise _Block()
    action = str(arguments.get("action", "")).lower()
    if action not in PROCESS_MUTATING_ACTIONS:
        return  # poll/status/wait/list are read-only
    session = arguments.get("session_id", arguments.get("session", ""))
    if not session:
        raise _Block()  # mutating action without a determinate session
    candidate = str(session)
    # FR-006: unresolved operands (variables/substitutions) can never be
    # proven unrelated to the protected session — refuse, never guess
    if "$" in candidate or "`" in candidate:
        raise _Block()
    if any(full.startswith(candidate) or
           full.removeprefix('proc_').startswith(candidate.removeprefix('proc_'))
           for full in policy.protected_sessions):
        raise _Block()


def _analyze_tool_call(arguments, policy, get_snapshot, depth=0):
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _BoundRefuse("BOUND_DEPTH")
    if not isinstance(arguments, dict):
        raise _Block()
    calls = arguments.get("calls")
    if calls is None and str(arguments.get('name') or '').strip():
        calls = [{'name': arguments['name'], 'arguments': arguments.get('arguments')}]
    # F-009/US1-S7: JSON-string-encoded call lists unwrap iteratively
    # (bounded); a non-decoding string is an unprovable shape (lpkit-1cz:
    # SHAPE_ENVELOPE, never a VETO — a hand-typed envelope that fails
    # json.loads is a shape defect with corrective guidance, not a
    # lifecycle threat)
    for _ in range(4):
        if not isinstance(calls, str):
            break
        if len(calls) > MAX_TEXT_CHARS:
            raise _BoundRefuse("BOUND_SIZE")
        try:
            calls = json.loads(calls)
        except Exception:
            raise _BoundRefuse("SHAPE_ENVELOPE")
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or not calls:
        raise _Block()
    # FR-007 bound decision table for count: over max_chunks, analyze the
    # inspectable head; intent-free benign batches pass (row a), intent
    # vetoes (row b), unprovable shapes refuse BOUND_COUNT (row c)
    max_chunks = (policy.bounds["max_chunks"] if policy and policy.bounds
                  else MAX_CHUNKS)
    if len(calls) > max_chunks:
        if len(calls) > HARD_BATCH_CAP:
            raise _BoundRefuse("BOUND_COUNT")
        head, tail = calls[:max_chunks], calls[max_chunks:]
        # analyze the head fully; a head hazard vetoes regardless of size
        _analyze_calls_list(head, policy, get_snapshot, depth)
        # every tail element must be independently provable: same shape
        # as the benign head element → row (a) allow; anything else is
        # an uninspectable remainder → row (c) refuse BOUND_COUNT
        tail_ok = all(_call_is_benign_shape(c) for c in tail)
        if not tail_ok:
            raise _BoundRefuse("BOUND_COUNT")
        return
    _analyze_calls_list(calls, policy, get_snapshot, depth)


def _call_is_benign_shape(call):
    """A provably intent-free call for FR-007 row (a) tail analysis.

    Amendment 3a (lpkit-6u6): only the CANONICAL name/arguments key
    shape is eligible for benign-tail analysis. A non-canonical shape
    (alternate keys such as tool/args) is UNPROVABLE — absence of
    recognized keys is never evidence of absence of executable content —
    so the oversized batch falls to FR-007 row (c) BOUND_COUNT.
    """
    if not isinstance(call, dict):
        return False
    name = str(call.get("name") or "").strip().lower()
    args = call.get("arguments")
    if "arguments" not in call or not name:
        return False  # non-canonical key shape: never benign
    if not isinstance(args, dict):
        return False
    if _call_has_executable_content(call):
        return False  # unprovable extra keys: never benign (amendment 3a)
    action = str(args.get("action", "")).lower()
    if name == "process_manage":
        return action not in PROCESS_MUTATING_ACTIONS
    if name in EXECUTION_TOOLS:
        return False  # execution payloads are never assumed benign
    return action == "" and not _executable_looking(args)


def _inert_plain(value):
    """A plain (non-executable-looking) scalar/structure (lpkit-vf0).

    Inert: None, bool, int, float, plain strings, and containers (dict/
    list/tuple) whose EVERY element is inert — arbitrary nesting. The
    recursion is bounded by the caller's depth/size envelope: it only
    runs over the values of one inner call's unknown keys, and the
    _executable_looking scan for dicts is the same heuristic used for
    canonical arguments.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        # key-sensitive: a nested dict carrying command/code/calls
        # (executable-looking keys) is NOT inert, same heuristic as the
        # top-level unknown-key branch (_executable_looking).
        if _executable_looking(value):
            return False
        return all(_inert_plain(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_inert_plain(v) for v in value)
    return False  # unknown type: unprovable


def _call_has_executable_content(call):
    """Amendment 3a (lpkit-6u6): executable-looking content in ANY key
    beyond the canonical name/arguments/calls trio.

    An unrecognized key carrying executable-looking content makes the
    whole call unprovable — absence of recognized keys is never evidence
    of absence of executable content.

    lpkit-vf0 (amended Edge Case): a list/tuple under an unknown key is
    inert when EVERY element is plain (string/number/inert dict,
    arbitrarily nested); only lists containing executable-looking
    content refuse. Previously any non-empty list refused — over-strict
    vs the amended Edge Case, which lets plain inert content pass.
    """
    for key, val in call.items():
        if key in ("name", "arguments", "calls"):
            continue  # canonical keys flow through their own analysis
        if isinstance(val, dict) and _executable_looking(val):
            return True
        if isinstance(val, str) and val.strip() and key in ("command", "code"):
            return True
        if isinstance(val, (list, tuple)) and val:
            # lpkit-vf0: inspect the elements — inert plain lists pass
            # (tags, ids, matrices); executable-looking content refuses.
            if not _inert_plain(val):
                return True
    return False


def _analyze_calls_list(calls, policy, get_snapshot, depth):
    """The per-call loop shared by the in-bound and over-bound paths."""
    for call in calls:
        if not isinstance(call, dict):
            raise _Block()
        name = str(call.get("name") or "").strip()
        call_args = call.get("arguments")
        if call_args is None or (isinstance(call_args, str) and not call_args.strip()):
            call_args = {}
        for _ in range(3):
            if not isinstance(call_args, str):
                break
            if len(call_args) > MAX_TEXT_CHARS:
                raise _BoundRefuse("BOUND_SIZE")
            try:
                call_args = json.loads(call_args)
            except Exception:
                # lpkit-1cz: a non-decoding arguments string is an
                # unprovable shape, not a lifecycle threat — corrective
                # SHAPE_ENVELOPE guidance, never the VETO message.
                raise _BoundRefuse("SHAPE_ENVELOPE")
        if not isinstance(call_args, dict):
            raise _Block()
        # Amendment 3a (lpkit-6u6): keys beyond the canonical pair are
        # unprovable — an unrecognized key (tool/args, extra, ...) carrying
        # executable-looking content is a SHAPE_ENVELOPE refusal, never a
        # silent skip of argument inspection.
        if _call_has_executable_content(call):
            raise _BoundRefuse("SHAPE_ENVELOPE")
        # hook-contract rule 1: the name must be attributable to a known
        # tool. Case-variants normalize; unknown or empty names carrying
        # executable-looking arguments are HOOK_MISMATCH, never a pass.
        low_name = name.lower()
        if not name:
            # an envelope-shaped body ({"calls": ...}) recurses; rule 1
            # applies to the inner calls (F-009 double-wrap/nameless)
            if isinstance(call.get("calls"), (list, dict, str)):
                _analyze_tool_call(call, policy, get_snapshot, depth + 1)
                continue
            if _executable_looking(call_args):
                raise _HookMismatch()
            continue
        if low_name not in (EXECUTION_TOOLS | {"process_manage"}
                            | READONLY_TOOLS):
            if _executable_looking(call_args):
                raise _HookMismatch()
            continue  # unknown name, nothing executable-looking: no hazard
        if low_name in READONLY_TOOLS:
            if _executable_looking(call_args):
                raise _HookMismatch()  # case-variant read-only carrying payload
            continue
        _dispatch_execution_tool(low_name, call_args, policy, get_snapshot,
                                 depth + 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Tools the runtime recognizes as read-only for hook-contract rule 1:
# a case-variant of one of these carrying executable-looking arguments is
# a HOOK_MISMATCH, while a case-variant of an EXECUTION_TOOL dispatches
# to the real analyzer (US1-S8, T010).
READONLY_TOOLS = frozenset({
    "read_file", "write_file", "patch", "search_files", "web_search",
    "web_extract", "gws_gmail_read", "gws_docs", "gws_sheets_read",
    "vision_analyze", "session_search", "skills_list", "skill_view",
})

def _executable_looking(args):
    """Arguments that could carry executable payload (rule 1 heuristic)."""
    if not isinstance(args, dict):
        return False
    for key in ("command", "code", "calls"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return True
        if val is not None and not isinstance(val, str):
            return True
    return False

def _dispatch_execution_tool(name, args, policy, get_snapshot, depth=0):
    """Case-insensitive dispatch of an execution-tool name (T010)."""
    low = name.lower()
    if name in EXECUTION_TOOLS:
        canonical = name
    elif low in EXECUTION_TOOLS:
        canonical = low  # case-variant of a known execution tool
    elif low == "process_manage":
        _check_process_manage(args, policy)
        return None
    else:
        # rule 1: unknown name carrying executable-looking arguments
        if _executable_looking(args):
            raise _HookMismatch()
        return None  # unknown name, nothing executable-looking
    if canonical == "terminal":
        _analyze_shell_text(args.get("command"), policy, get_snapshot)
    elif canonical in ("execute_code", "browser_exec"):
        _analyze_code_text(args.get("code"), policy, get_snapshot)
    elif canonical == "process_manage":
        _check_process_manage(args, policy)
    else:  # tool_call
        _analyze_tool_call(args, policy, get_snapshot, depth)
    return None


class _HookMismatch(Exception):
    """Hook-contract rule 1: unattributable tool name, fail closed."""


class _Refuse(Exception):
    """Internal: refuse with a bound/shape category (FR-007)."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


def evaluate(tool_name, args, policy_path, *, discover=None):
    """Return None to allow, or a block decision dict with category+code.

    ``discover`` is injectable for tests (defaults to the real read-only
    lsof/ps discovery). Non-execution tools (read_file, search_files, ...)
    are never blocked, even with a missing or bad policy file (FR-005
    split). Policy is re-read and re-authenticated from disk on EVERY
    evaluation — no caching (FR-008/D5).
    """
    try:
        name = str(tool_name)
    except Exception:
        return _block("HOOK_MISMATCH", "")
    if name not in EXECUTION_TOOLS and name.lower() != "process_manage":
        # top-level unknown/empty name carrying executable-looking
        # arguments is a hook-contract mismatch and fails closed (rule 1,
        # Edge Cases, F-005); recognized read-only names pass unmodified
        if _executable_looking(args) and (
                not name or name.lower() not in READONLY_TOOLS):
            return _block("HOOK_MISMATCH", name)
        return None
    name = name.lower()
    try:
        if not isinstance(args, dict):
            raise _Block()
        policy = load_policy(policy_path)
    except IntegrityRefError:
        # FR-015: its own distinct fail-closed category + owner repair
        if name == "process_manage":
            action = str(args.get("action", "")).lower()
            if action in PROCESS_MUTATING_ACTIONS:
                return _block("INTEGRITY_REF_INVALID", name)
            return None
        return _block("INTEGRITY_REF_INVALID", name)
    except _HookMismatch:
        return _block("HOOK_MISMATCH", name)
    except _Block:
        return _block("VETO", name)
    except Exception:
        # Missing/bad config: block execution tools with the sanitized
        # category message. process_manage read-only actions still pass.
        if name == "process_manage":
            action = str(args.get("action", "")).lower() if isinstance(args, dict) else ""
            if action in PROCESS_MUTATING_ACTIONS:
                return _block("POLICY_INVALID", name)
            return None
        return _block("POLICY_INVALID", name)
    try:
        if name == "process_manage":
            _check_process_manage(args, policy)
            return None
        snapshot_box = []

        def get_snapshot():
            if not snapshot_box:
                snapshot_box.append(
                    discover(policy.port) if discover is not None
                    else discover_snapshot(policy.port)
                )
            return snapshot_box[0]

        _dispatch_execution_tool(name, args, policy, get_snapshot)
        return None
    except _HookMismatch:
        return _block("HOOK_MISMATCH", name)
    except _BoundRefuse as refuse:
        return _block(refuse.category, name)
    except _Block:
        return _block("VETO", name)
    except Exception:
        return _block("VETO", name)  # never fail open for execution tools
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
import os
import re
import subprocess
import shlex
from dataclasses import dataclass
from pathlib import Path

__all__ = ["evaluate", "load_policy", "discover_snapshot", "Policy", "Snapshot",
           "BLOCK_MESSAGE"]

# Fixed, sanitized block message. Deliberately identical for every block
# reason: no arguments, paths, PIDs, or exception details ever leak.
BLOCK_MESSAGE = (
    "Blocked by the LiteLLM proxy lifecycle guard: this request could stop "
    "the protected proxy, or its safety could not be established. Use the "
    "owner-reviewed controlled restart workflow; do not substitute another "
    "kill command or execution path."
)

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


class _Block(Exception):
    """Internal: raise anywhere analysis determines the call must be blocked."""


class PolicyError(Exception):
    """Policy config is missing, unreadable, or does not match the strict schema."""


@dataclass(frozen=True)
class Policy:
    port: int
    labels: frozenset
    protected_sessions: frozenset


@dataclass(frozen=True)
class Snapshot:
    """Read-only process discovery result. Empty sets are a determinate
    'nothing is listening' answer; None (from discover_snapshot) means the
    discovery itself failed and callers must refuse conservatively."""
    pids: frozenset = frozenset()
    pgids: frozenset = frozenset()
    comms: frozenset = frozenset()
    args: tuple = ()


def _block():
    return {"action": "block", "message": BLOCK_MESSAGE}


def load_policy(policy_path):
    """Strictly parse the policy JSON. Raises PolicyError on any deviation."""
    path = Path(policy_path)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # missing, unreadable, or invalid JSON
        raise PolicyError("unreadable policy") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")
    expected = {"version", "port", "labels", "protected_sessions"}
    if set(data.keys()) != expected:
        raise PolicyError("policy schema mismatch")
    version = data["version"]
    if isinstance(version, bool) or version != 1:
        raise PolicyError("unsupported policy version")
    port = data["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise PolicyError("invalid port")
    labels = data["labels"]
    sessions = data["protected_sessions"]
    for seq in (labels, sessions):
        if not isinstance(seq, list) or not all(
            isinstance(item, str) and item for item in seq
        ):
            raise PolicyError("invalid labels/sessions")
    return Policy(
        port=port,
        labels=frozenset(label.lower() for label in labels),
        protected_sessions=frozenset(sessions),
    )


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
    if "litellm" in pattern or any(label in pattern for label in policy.labels):
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
    snap = get_snapshot()
    if snap is None:
        raise _Block()
    # Refuse regex selectors rather than evaluate attacker-controlled regexes
    # against process data (both overbroad matches and regex CPU exhaustion).
    if any(c in pattern for c in ".*+?[](){}|^$\\"):
        raise _Block()
    for argline in (*snap.args, *snap.comms):
        if pattern in argline.lower():
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


def _analyze_chunk(chunk, policy, get_snapshot, depth=0):
    """Analyze one command chunk (may be a pipeline). Hazard anywhere blocks."""
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    chunk = chunk.strip()
    if chunk.startswith("(") and chunk.endswith(")"):
        _analyze_shell_text(chunk[1:-1], policy, get_snapshot, depth + 1)
        return
    stages = [s for s in _split_segments(chunk, ("|",), limit=MAX_STAGES) if s.strip()]
    if len(stages) >= MAX_STAGES:
        raise _Block()
    kill_like = False
    stage_texts = []
    for stage in stages:
        stage_texts.append(stage)
        tokens, truncated = _tokenize(stage)
        if truncated:
            raise _Block()  # pathological segment; bounded and conservative
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        verb = tokens[0].rsplit("/", 1)[-1].lower()
        rest = tokens[1:]
        if verb == "kill":
            probe_only = _analyze_kill_stage(stage, rest, policy, get_snapshot)
            kill_like = kill_like or not probe_only
        elif verb in ("sh", "bash", "zsh") and any(
                t.startswith("-") and "c" in t for t in rest):
            idx = next(i for i, t in enumerate(rest) if t.startswith("-") and "c" in t)
            if idx + 1 >= len(rest):
                raise _Block()
            _analyze_shell_text(rest[idx + 1], policy, get_snapshot, depth + 1)
        elif re.fullmatch(r"python(?:[23](?:\.\d+)?)?", verb) and "-c" in rest:
            idx = rest.index("-c")
            if idx + 1 >= len(rest):
                raise _Block()
            _analyze_code_text(rest[idx + 1], policy, get_snapshot)
        elif verb in ("pkill", "killall"):
            _analyze_pattern_kill(verb, rest, policy, get_snapshot)
            kill_like = True
        elif verb == "xargs":
            if any(t.rsplit('/', 1)[-1] in ("kill", "pkill", "killall") for t in rest):
                raise _Block()  # stdin targets are unresolved; no safe PID proof
        elif verb == "fuser":
            if any(t == "-k" or t.startswith("-k") for t in rest):
                for tok in rest:
                    if tok.split("/")[0] == str(policy.port):
                        raise _Block()
        elif verb == "launchctl":
            _analyze_launchctl(rest, policy)
        elif verb == "brew":
            _analyze_brew(rest, policy)
    if kill_like:
        # The kill stage's targets may arrive via stdin from an earlier
        # lsof/ps/grep stage: block when any stage references the configured
        # port or the proxy name/label.
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


def _analyze_shell_text(text, policy, get_snapshot, depth=0):
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    if not isinstance(text, str):
        raise _Block()
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


def _analyze_code_text(text, policy, get_snapshot):
    if not isinstance(text, str):
        raise _Block()
    if len(text) > MAX_TEXT_CHARS:
        raise _Block()
    if _OS_KILL_RE.search(text):  # os.kill( / os.killpg( — textual, conservative
        raise _Block()
    if "psutil" in text and _PSUTIL_KILL_RE.search(text):
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
    if any(full.startswith(candidate) or
           full.removeprefix('proc_').startswith(candidate.removeprefix('proc_'))
           for full in policy.protected_sessions):
        raise _Block()


def _analyze_tool_call(arguments, policy, get_snapshot, depth=0):
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    if not isinstance(arguments, dict):
        raise _Block()
    calls = arguments.get("calls")
    if calls is None and str(arguments.get('name') or '').strip():
        calls = [{'name': arguments['name'], 'arguments': arguments.get('arguments')}]
    if isinstance(calls, str):
        if len(calls) > MAX_TEXT_CHARS:
            raise _Block()
        calls = json.loads(calls)
    # Double-wrap unwrapping: a model may JSON-encode the whole {"calls":
    # [...]} envelope (or a nameless single-call object) AS the `calls`
    # value, arbitrarily nested. Unwrap while the shape is a calls-shaped
    # mapping; anything else falls through to the fail-closed checks below.
    # Treat encoded envelopes as envelopes, not nameless single calls,
    # so benign inner calls remain usable without allowing hidden mutations.
    _unwraps = 0
    while isinstance(calls, dict) and "calls" in calls and "name" not in calls:
        _unwraps += 1
        if _unwraps > MAX_TOOL_CALL_DEPTH:
            raise _Block()
        inner = calls["calls"]
        if isinstance(inner, str):
            if len(inner) > MAX_TEXT_CHARS:
                raise _Block()
            inner = json.loads(inner)
        calls = inner
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or not calls or len(calls) > MAX_CHUNKS:
        raise _Block()
    for call in calls:
        if not isinstance(call, dict):
            raise _Block()
        name = str(call.get("name") or "").strip()
        if not name:
            raise _Block()
        call_args = call.get("arguments")
        if call_args is None or (isinstance(call_args, str) and not call_args.strip()):
            call_args = {}
        if isinstance(call_args, str):
            if len(call_args) > MAX_TEXT_CHARS:
                raise _Block()
            call_args = json.loads(call_args)
        if not isinstance(call_args, dict):
            raise _Block()
        if name == "terminal":
            command = call_args.get("command") if isinstance(call_args, dict) else None
            _analyze_shell_text(command, policy, get_snapshot)
        elif name in ("execute_code", "browser_exec"):
            code = call_args.get("code") if isinstance(call_args, dict) else None
            _analyze_code_text(code, policy, get_snapshot)
        elif name == "process_manage":
            _check_process_manage(call_args, policy)
        elif name == "tool_call":
            _analyze_tool_call(call_args, policy, get_snapshot, depth + 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def evaluate(tool_name, args, policy_path, *, discover=None):
    """Return None to allow, or {'action': 'block', 'message': ...} to block.

    ``discover`` is injectable for tests (defaults to the real read-only
    lsof/ps discovery). Non-execution tools (read_file, search_files, ...)
    are never blocked, even with a missing or bad policy file.
    """
    try:
        name = str(tool_name)
    except Exception:
        return _block()
    if name not in EXECUTION_TOOLS and name != "process_manage":
        return None
    try:
        if not isinstance(args, dict):
            raise _Block()
        policy = load_policy(policy_path)
    except _Block:
        return _block()
    except Exception:
        # Missing/bad config: block execution tools with the fixed sanitized
        # message. process_manage read-only actions still pass.
        if name == "process_manage":
            action = str(args.get("action", "")).lower() if isinstance(args, dict) else ""
            if action in PROCESS_MUTATING_ACTIONS:
                return _block()
            return None
        return _block()
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

        if name == "terminal":
            _analyze_shell_text(args.get("command"), policy, get_snapshot)
        elif name in ("execute_code", "browser_exec"):
            _analyze_code_text(args.get("code"), policy, get_snapshot)
        else:  # tool_call
            _analyze_tool_call(args, policy, get_snapshot)
        return None
    except _Block:
        return _block()
    except Exception:
        return _block()  # never fail open for execution tools
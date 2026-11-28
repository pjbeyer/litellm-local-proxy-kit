"""In-memory check worker, invoked only by the installation bootstrap.

The bootstrap owns native startup and dependency continuity before launch.
The fixture bootstrap used in tests does not establish that guarantee.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
from types import ModuleType

HOME = "/rehearsal-home"
BUNDLE_LIMIT = 768 * 1024
RENDER_LIMIT = 1024 * 1024
DEADLINE_SECONDS = 10
ROOT = Path(__file__).resolve().parent
PARSER_PATHS = ("check.py", "schemas/configuration_check.py")
CONTEXT_KEYS = {"version", "renderer_sha256", "template_sha256",
                "parser_inventory_sha256", "python", "pyyaml", "settings"}
BASE = {"version": 1, "mode": "configuration_check", "runtime_not_exercised": True}


class Deadline(BaseException):
    pass


class OutputLimit(Exception):
    pass


def _alarm(_signum, _frame):
    raise Deadline


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _unique_pairs(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


def _is_digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _retain(path, limit):
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError
    return raw


def _load_context(record_path):
    record = json.loads(_retain(record_path, 8192).decode("utf-8"),
                        object_pairs_hook=_unique_pairs)
    if (set(record) != CONTEXT_KEYS or type(record["version"]) is not int
            or record["version"] != 1 or record["settings"] != {"HOME": HOME}):
        raise ValueError
    python, pyyaml = record["python"], record["pyyaml"]
    if (set(python) != {"implementation", "version", "executable_sha256", "inventory_sha256"}
            or set(pyyaml) != {"version", "inventory_sha256"}
            or not isinstance(pyyaml["version"], str) or not pyyaml["version"]
            or not _is_digest(python["inventory_sha256"])
            or not _is_digest(pyyaml["inventory_sha256"])
            or python["implementation"] != sys.implementation.name
            or python["version"] != sys.version.split()[0]):
        raise ValueError
    with Path(sys.executable).open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != python["executable_sha256"]:
            raise ValueError
    sources = {name: _retain(ROOT / name, 64 * 1024) for name in PARSER_PATHS}
    renderer = _retain(ROOT.parent / "core/render_config.py", 64 * 1024)
    template = _retain(ROOT.parent / "core/config.template.header.txt", RENDER_LIMIT)
    if (record["renderer_sha256"] != _digest(renderer)
            or record["template_sha256"] != _digest(template)
            or record["parser_inventory_sha256"] != _digest(_canonical({
                name: _digest(raw) for name, raw in sources.items()}))):
        raise ValueError
    return record, sources[PARSER_PATHS[1]], renderer, template


def _load_source(name, filename, source):
    # Compile the retained bytes, not a path-based import or cached bytecode.
    module = ModuleType(name)
    module.__file__ = str(filename)
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec", dont_inherit=True), module.__dict__)
    return module


def _read_pipe():
    if not stat.S_ISFIFO(os.fstat(0).st_mode):
        raise ValueError
    raw = bytearray()
    while len(raw) <= BUNDLE_LIMIT:
        chunk = os.read(0, min(65536, BUNDLE_LIMIT + 1 - len(raw)))
        if not chunk:
            if not raw:
                raise ValueError
            return bytes(raw)
        raw.extend(chunk)
    raise ValueError


def _render(schema, renderer, overlay, template):
    try:
        raw = renderer.render(overlay, template, home=HOME, max_bytes=RENDER_LIMIT).encode("utf-8")
    except renderer.RenderLimit:
        raise OutputLimit from None
    if len(raw) > RENDER_LIMIT:
        raise OutputLimit
    doc = schema.parse_bounded_yaml(raw, limit=RENDER_LIMIT, nodes=65536, depth=24)
    if not isinstance(doc, dict) or set(doc) - set(schema.CATEGORIES):
        raise ValueError
    return _digest(raw), doc


def _terminal(status, reason):
    return (78 if status == "refused" else 1,
            _canonical(dict(BASE, status=status, reason=reason)) + b"\n")


def _check(argv):
    # The two internal operands are supplied by the installation bootstrap.
    if len(argv) != 4 or argv[2] != "--bundle-sha256" or not _is_digest(argv[3]):
        return _terminal("refused", "input_invalid")
    record_path, package_root = map(Path, argv[:2])
    if not all(p.is_absolute() for p in (record_path, package_root)):
        return _terminal("failed", "render_failed")

    context, parser_bytes, renderer_bytes, template_bytes = _load_context(record_path)
    schema = _load_source("_rehearsal_schema", ROOT / PARSER_PATHS[1], parser_bytes)
    try:
        raw = _read_pipe()
    except (OSError, ValueError):
        return _terminal("refused", "input_invalid")
    try:
        bundle = schema.admit_bundle(raw, argv[3])
        schema.parse_pin(bundle.pin)
        # The launcher supplies the already admitted package location. Do not
        # process site hooks, PYTHONPATH or a caller-selected dependency root.
        sys.path.insert(0, str(package_root))
        import yaml
        if yaml.__version__ != context["pyyaml"]["version"]:
            return _terminal("failed", "render_failed")
        proposal = schema.parse_overlay(bundle.overlay)
        baseline_raw = bundle.baseline_overlay
        baseline = None if baseline_raw is None else schema.parse_overlay(baseline_raw)
    except schema.InputError as error:
        reason = "digest_mismatch" if str(error) == "digest_mismatch" else "input_invalid"
        return _terminal("refused", reason)

    renderer = _load_source("_rehearsal_renderer", ROOT.parent / "core/render_config.py",
                            renderer_bytes)
    template = template_bytes.decode("utf-8")
    rendered_digest, proposed_doc = _render(schema, renderer, proposal, template)
    comparison = None
    if baseline_raw is not None:
        baseline_digest, baseline_doc = _render(schema, renderer, baseline, template)
        comparison = {
            "baseline_overlay_sha256": _digest(baseline_raw),
            "baseline_rendered_sha256": baseline_digest,
            "changed_categories": [name for name in schema.CATEGORIES
                                   if proposed_doc.get(name) != baseline_doc.get(name)],
        }
    return 0, schema.encode_result(dict(
        BASE, status="checked", bundle_sha256=argv[3], manifest_sha256=bundle.manifest_sha256,
        overlay_sha256=_digest(bundle.overlay), pin_sha256=_digest(bundle.pin),
        template_sha256=context["template_sha256"], render_context_sha256=_digest(_canonical(context)),
        rendered_sha256=rendered_digest, comparison=comparison,
        categories_present=[name for name in schema.CATEGORIES if name in proposed_doc],
    ))


def main():
    signal.signal(signal.SIGALRM, _alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
        try:
            if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
                code, payload = _terminal("failed", "render_failed")
            else:
                code, payload = _check(sys.argv[1:])
        except Deadline:
            # No time remains to wait for the consumer. A ready pipe can still
            # receive the small failure record; backpressure means no result.
            return _emit_deadline()
        except OutputLimit:
            code, payload = _terminal("failed", "output_limit")
        except Exception:
            code, payload = _terminal("failed", "render_failed")
        return _emit(code, payload)
    except Deadline:
        # A write may already have started. Never append a second result.
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _emit(code, payload):
    try:
        return code if os.write(1, payload) == len(payload) else 1
    except OSError:
        return 1


def _emit_deadline():
    try:
        blocking = os.get_blocking(1)
        if blocking:
            os.set_blocking(1, False)
        try:
            return _emit(*_terminal("failed", "deadline"))
        finally:
            if blocking:
                os.set_blocking(1, True)
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())

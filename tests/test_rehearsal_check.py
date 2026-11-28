"""Offline tests for the configuration-check data contract."""
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def schema():
    return importlib.import_module("rehearsal.schemas.configuration_check")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest(root):
    return {
        "version": 1,
        "mode": "configuration_check",
        "overlay": {"path": str(root / "proposal.yaml"), "sha256": "a" * 64},
        "pin": {"path": str(root / "pin.toml"), "sha256": "b" * 64},
    }


def load_manifest(schema, value):
    data = json.dumps(value).encode()
    return schema.parse_manifest(data, digest(data))


def test_manifest_checks_independent_digest_and_retains_optional_baseline(schema, tmp_path):
    value = manifest(tmp_path)
    value["baseline_overlay"] = {"path": str(tmp_path / "baseline.yaml"), "sha256": "c" * 64}
    raw = json.dumps(value).encode()
    assert schema.parse_manifest(raw, digest(raw)) == value
    with pytest.raises(schema.InputError, match="^digest_mismatch$"):
        schema.parse_manifest(raw, "0" * 64)


@pytest.mark.parametrize("change", [
    {"version": True}, {"mode": "rehearsal"}, {"guest_image": {}},
    {"baseline_overlay": None}, {"pin": {"path": "pin.toml", "sha256": "b" * 64}},
    {"pin": {"path": "/pin.toml", "sha256": "B" * 64}},
    {"pin": {"path": "/pin.toml", "sha256": "b" * 64, "extra": 1}},
])
def test_manifest_is_closed(schema, tmp_path, change):
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        load_manifest(schema, {**manifest(tmp_path), **change})


@pytest.mark.parametrize("raw", [
    b'{"version":1,"version":1}', b'{"x": NaN}', b'\xff', b' ' * 16385,
    b'[' * 2000 + b']' * 2000, b'{"x": {"path": 1, "path": 2}}',
])
def test_manifest_rejects_malformed_or_unbounded_json(schema, raw):
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.parse_manifest(raw, digest(raw))


OVERLAY = b"""master_key_env: LOCAL_MASTER
upstream:
  api_key_env: LOCAL_UPSTREAM
  api_base: https://provider.example.invalid/v1
  models:
    - name: example/one
      timeout: 10
    - name: example/two
  wildcard: true
  wildcard_timeouts: false
litellm_settings:
  telemetry: false
router_fallbacks:
  - from: example/one
    to: [example/two]
"""


def test_closed_overlay_and_opaque_pin(schema):
    overlay = schema.parse_overlay(OVERLAY)
    assert overlay["upstream"]["models"][0] == {"name": "example/one", "timeout": 10}
    assert overlay["upstream"]["wildcard_timeouts"] is False
    assert overlay["router_fallbacks"][0]["to"] == ["example/two"]
    raw_pin = b'base_version = "1.2.3"\npatch_tip = "abcdef0"\nsource = "unresolved.toml"\n'
    assert schema.parse_pin(raw_pin) == {"base_version": "1.2.3", "patch_tip": "abcdef0"}


@pytest.mark.parametrize("raw", [
    OVERLAY + b"callbacks: []\n", OVERLAY + b"master_key_env: ALTERNATE\n",
    OVERLAY + b"1: value\n", OVERLAY + b"<<: {x: 1}\n",
    OVERLAY.replace(b"LOCAL_MASTER", b"!!str LOCAL_MASTER"),
    OVERLAY.replace(b"LOCAL_MASTER", b"&decl LOCAL_MASTER"),
    OVERLAY.replace(b"LOCAL_MASTER", b"*decl"),
    OVERLAY.replace(b"LOCAL_MASTER", b"null"),
    OVERLAY.replace(b"LOCAL_MASTER", b"lowercase"),
    OVERLAY.replace(b"timeout: 10", b"timeout: true"),
    OVERLAY.replace(b"timeout: 10", b"timeout: 0"),
    OVERLAY.replace(b"telemetry: false", b"telemetry: 'false'"),
    OVERLAY.replace(b"wildcard: true", b"wildcard: false"),
    OVERLAY.replace(b"name: example/two", b"name: example/one"),
    OVERLAY.replace(b"name: example/two", b"name: '*'"),
    OVERLAY.replace(b"to: [example/two]", b"to: [missing]"),
    OVERLAY.replace(b"to: [example/two]", b"to: [example/two, example/two]"),
    OVERLAY.replace(b"https://provider.example.invalid/v1", b"https://provider.example.invalid/?mode=debug"),
    OVERLAY.replace(b"https://provider.example.invalid/v1", b"https://provider.example.invalid/${NAME}"),
    b"[" * 25 + b"0" + b"]" * 25, b"[" + b"0," * 4096 + b"]",
    b" " * (256 * 1024 + 1),
])
def test_overlay_refuses_unsupported_input(schema, raw):
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.parse_overlay(raw)


def test_result_is_closed_bounded_and_advisory(schema):
    value = {"version": 1, "mode": "configuration_check", "runtime_not_exercised": True,
             "status": "checked", "comparison": None,
             "categories_present": ["model_list", "general_settings"]}
    for name in ("bundle", "manifest", "overlay", "pin", "template", "render_context", "rendered"):
        value[f"{name}_sha256"] = "a" * 64
    raw = schema.encode_result(value)
    assert len(raw) <= 4096 and json.loads(raw) == value
    for missing in ("bundle_sha256", "manifest_sha256"):
        with pytest.raises(schema.ResultError):
            schema.encode_result({key: val for key, val in value.items() if key != missing})
    for change in ({"runtime_not_exercised": False}, {"status": "passed"},
                   {"categories_present": ["general_settings", "model_list"]},
                   {"comparison": {"changed_categories": []}}, {"receipt": {}},
                   {"rendered_sha256": None}):
        with pytest.raises(schema.ResultError):
            schema.encode_result({**value, **change})
    failed = {"version": 1, "mode": "configuration_check", "runtime_not_exercised": True,
              "status": "failed", "reason": "render_failed"}
    assert json.loads(schema.encode_result(failed)) == failed
    for change in ({"status": "refused"}, {"reason": "free-form"}, {"rendered_sha256": "a" * 64},
                   {"status": []}, {"reason": []}):
        with pytest.raises(schema.ResultError):
            schema.encode_result({**failed, **change})


@pytest.fixture
def admission():
    return importlib.import_module("rehearsal.schemas.reviewed_bytes")


def test_retained_bytes_match_independent_digest(admission, tmp_path, schema):
    path = tmp_path / "reviewed"
    raw = b"reviewed data\n"
    path.write_bytes(raw)
    with path.open("rb") as handle:
        admitted = []
        fd = handle.fileno()
        snapshot = admission.read_verified(fd, limit=len(raw), expected=digest(raw),
                                           require_admitted=admitted.append)
        assert admitted == [fd, fd]
        with pytest.raises(schema.InputError, match="^digest_mismatch$"):
            admission.read_verified(fd, limit=len(raw), expected=digest(b"other data"),
                                    require_admitted=admitted.append)
        # The caller owns the descriptor, and snapshotting does not consume its offset.
        assert handle.read() == raw
    path.write_bytes(b"changed data\n")
    assert snapshot == raw and isinstance(snapshot, bytes)


@pytest.mark.parametrize("kind", ["directory", "pipe", "hard_link", "oversize", "invalid_digest"])
def test_unsuitable_descriptor_refuses_without_read(admission, tmp_path, monkeypatch, kind, schema):
    raw = b"reviewed data\n"
    path = tmp_path / "input"
    path.write_bytes(raw)
    if kind == "hard_link":
        os.link(path, tmp_path / "other")
    if kind == "pipe":
        fd, writer = os.pipe()
        os.close(writer)
    else:
        fd = os.open(tmp_path if kind == "directory" else path, os.O_RDONLY)
    attempts = []

    def no_read(*args):
        attempts.append(args)
        pytest.fail("content read before metadata admission")

    monkeypatch.setattr(os, "pread", no_read)
    try:
        with pytest.raises(schema.InputError, match="^input_invalid$"):
            admission.read_verified(fd, limit=len(raw) - (kind == "oversize"),
                                    expected="invalid" if kind == "invalid_digest" else digest(raw),
                                    require_admitted=lambda _: None)
    finally:
        os.close(fd)
    assert not attempts


def test_descriptor_change_during_read_is_not_admissible(admission, tmp_path, monkeypatch, schema):
    raw = b"reviewed data\n"
    path = tmp_path / "input"
    path.write_bytes(raw)
    pread = os.pread
    calls = []

    def observe_change(fd, count, offset):
        calls.append(offset)
        observed = pread(fd, count, offset)
        if len(calls) == 1:
            path.write_bytes(raw + b"more")
        return observed

    monkeypatch.setattr(os, "pread", observe_change)
    with path.open("rb") as handle, pytest.raises(schema.InputError, match="^input_invalid$"):
        admission.read_verified(handle.fileno(), limit=len(raw) + 4, expected=digest(raw),
                                require_admitted=lambda _: None)
    assert calls and calls[0] == 0


def test_missing_storage_proof_refuses_before_read(admission, tmp_path, monkeypatch, schema):
    path = tmp_path / "input"
    raw = b"reviewed data\n"
    path.write_bytes(raw)

    def refuse(_):
        raise schema.InputError("input_invalid")

    def no_read(*_):
        pytest.fail("content read without storage proof")

    monkeypatch.setattr(os, "pread", no_read)
    with path.open("rb") as handle, pytest.raises(schema.InputError, match="^input_invalid$"):
        admission.read_verified(handle.fileno(), limit=len(raw), expected=digest(raw),
                                require_admitted=refuse)


@pytest.mark.parametrize("path", [
    "/staging/input.yaml", "/canonical/staging/input.yaml",
])
def test_relative_selection_uses_only_the_two_admitted_root_spellings(admission, path):
    assert admission.relative_parts(path, given_root="/staging",
                                    canonical_root="/canonical/staging") == ("input.yaml",)


@pytest.mark.parametrize("path", [
    "staging/input.yaml", "/elsewhere/input.yaml", "/staging-sibling/input.yaml",
    "/other-alias/staging/input.yaml", "/staging", "/staging/", "/staging//input.yaml",
    "/staging/./input.yaml", "/staging/directory/../input.yaml", "/staging/input\0.yaml",
])
def test_relative_selection_refuses_ambiguous_or_outside_paths(admission, schema, path):
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        admission.relative_parts(path, given_root="/staging", canonical_root="/canonical/staging")


@pytest.mark.parametrize("root", [None, "", "/", "relative", "/staging/", "/../staging"])
def test_relative_selection_requires_two_explicit_root_paths(admission, schema, root):
    for roots in ((root, "/canonical/staging"), ("/staging", root)):
        with pytest.raises(schema.InputError, match="^input_invalid$"):
            admission.relative_parts("/staging/input.yaml", given_root=roots[0], canonical_root=roots[1])


def test_relative_selection_refuses_ambiguous_nested_spellings(admission, schema):
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        admission.relative_parts("/staging/staged/input.yaml", given_root="/staging",
                                 canonical_root="/staging/staged")


PIN = b'base_version = "1.2.3"\npatch_tip = "abcdef0"\n'


@pytest.mark.parametrize("change,reason,reads", [
    ("none", None, ["manifest", "proposal.yaml", "pin.toml", "baseline.yaml"]),
    ("no_baseline", None, ["manifest", "proposal.yaml", "pin.toml"]),
    ("invalid_expected", "input_invalid", []),
    ("manifest", "digest_mismatch", ["manifest"]),
    ("proposal", "digest_mismatch", ["manifest", "proposal.yaml"]),
    ("baseline", "input_invalid", ["manifest", "proposal.yaml", "pin.toml", "baseline.yaml"]),
])
def test_load_reviewed_check(admission, schema, tmp_path, change, reason, reads):
    files = {"proposal.yaml": OVERLAY, "pin.toml": PIN, "baseline.yaml": OVERLAY}
    doc = manifest(tmp_path)
    for field, name in zip(("overlay", "pin", "baseline_overlay"), files):
        doc[field] = {"path": str(tmp_path / name), "sha256": digest(files[name])}
    if change == "baseline":
        files["baseline.yaml"] += b"unsupported: true\n"
        doc["baseline_overlay"]["sha256"] = digest(files["baseline.yaml"])
    if change == "no_baseline":
        del doc["baseline_overlay"]
    files["manifest"] = json.dumps(doc).encode()
    reviewed_digest = "invalid" if change == "invalid_expected" else digest(files["manifest"])
    if change == "manifest":
        files["manifest"] = json.dumps(doc, indent=2).encode()
    if change == "proposal":
        files["proposal.yaml"] += b"# changed\n"
    observed = []
    for name, raw in files.items():
        (tmp_path / name).write_bytes(raw)

    def fixture_reader(path, *, limit, expected):
        name = os.path.basename(path)
        assert path == str(tmp_path / name)
        observed.append(name)
        with (tmp_path / name).open("rb") as handle:
            return admission.read_verified(handle.fileno(), limit=limit, expected=expected,
                                            require_admitted=lambda _: None)

    if reason is None:
        check = schema.load_reviewed_check(fixture_reader, str(tmp_path / "manifest"), reviewed_digest)
        assert check.manifest_sha256 == reviewed_digest
        assert check.overlay == OVERLAY and check.pin == PIN
        assert check.baseline_overlay == (None if change == "no_baseline" else OVERLAY)
    else:
        with pytest.raises(schema.InputError, match=f"^{reason}$"):
            schema.load_reviewed_check(fixture_reader, str(tmp_path / "manifest"), reviewed_digest)
    assert observed == reads


def routine_members(baseline=None):
    members = {"overlay": OVERLAY, "pin": PIN}
    doc = {"version": 1, "mode": "configuration_check",
           "overlay": {"sha256": digest(OVERLAY)}, "pin": {"sha256": digest(PIN)}}
    if baseline is not None:
        members["baseline_overlay"] = baseline
        doc["baseline_overlay"] = {"sha256": digest(baseline)}
    members["manifest"] = json.dumps(doc).encode()
    return members


def encode_bundle(members):
    return json.dumps({key: base64.b64encode(value).decode()
                       for key, value in members.items()}).encode()


BASELINE = OVERLAY.replace(b"wildcard: true", b"wildcard: false").replace(
    b"\n  wildcard_timeouts: false", b"")


def member_bundle(**changes):
    """A digest-consistent bundle with the named members replaced."""
    members = routine_members()
    doc = json.loads(members["manifest"])
    for role, value in changes.items():
        members[role] = value
        doc[role] = {"sha256": digest(value)}
    members["manifest"] = json.dumps(doc).encode()
    return encode_bundle(members)


def test_bundle_admission_retains_verified_members(schema):
    members = routine_members()
    raw = encode_bundle(members)
    bundle = schema.admit_bundle(raw, digest(raw))
    assert bundle.overlay == OVERLAY and bundle.pin == PIN
    assert bundle.baseline_overlay is None
    assert bundle.manifest_sha256 == digest(members["manifest"])
    with pytest.raises(schema.InputError, match="^digest_mismatch$"):
        schema.admit_bundle(raw, digest(b"other"))


def test_bundle_admission_with_baseline_loads_closed_members(schema):
    members = routine_members(BASELINE)
    raw = encode_bundle(members)
    bundle = schema.load_bundle(raw, digest(raw))
    assert bundle.baseline_overlay == BASELINE
    assert bundle.manifest_sha256 == digest(members["manifest"])


def test_bundle_refuses_unusable_streams(schema):
    members = routine_members()
    raw = encode_bundle(members)
    for stream, expected in ((b"", digest(raw)),
                             (b"x" * (768 * 1024 + 1), digest(b"x" * (768 * 1024 + 1))),
                             (raw, "not-a-digest"),
                             (raw, digest(b"other"))):
        with pytest.raises(schema.InputError) as raised:
            schema.admit_bundle(stream, expected)
        assert str(raised.value) == ("input_invalid" if expected == "not-a-digest"
                                     or stream in (b"", b"x" * (768 * 1024 + 1))
                                     else "digest_mismatch")


def test_bundle_refuses_altered_envelopes_with_recomputed_digests(schema):
    # Internal consistency is not acceptance: the independently supplied
    # digest still binds the original reviewed bytes.
    members = routine_members()
    doc = json.loads(members["manifest"])
    doc["overlay"]["sha256"] = digest(OVERLAY + b"extra: true\n")
    members["manifest"] = json.dumps(doc).encode()
    raw = encode_bundle(members)
    with pytest.raises(schema.InputError, match="^digest_mismatch$"):
        schema.admit_bundle(raw, digest(encode_bundle(routine_members())))
    # A member/manifest mismatch is caught even when the outer digest matches.
    other = dict(routine_members(), overlay=OVERLAY + b"# changed\n")
    raw = encode_bundle(other)
    with pytest.raises(schema.InputError, match="^digest_mismatch$"):
        schema.admit_bundle(raw, digest(raw))


def test_bundle_refuses_structural_violations(schema):
    members = routine_members()
    envelope = json.loads(encode_bundle(members).decode())
    doc = json.loads(members["manifest"])

    def raw_for(envelope_value):
        raw = envelope_value if isinstance(envelope_value, bytes) \
            else json.dumps(envelope_value).encode()
        return raw, digest(raw)

    def with_manifest(manifest_value):
        if not isinstance(manifest_value, bytes):
            manifest_value = json.dumps(manifest_value).encode()
        return raw_for({**envelope, "manifest": base64.b64encode(manifest_value).decode()})

    duplicate = (b'{"manifest":"' + base64.b64encode(members["manifest"]) + b'",'
                 b'"overlay":"' + base64.b64encode(OVERLAY) + b'",'
                 b'"pin":"' + base64.b64encode(PIN) + b'",'
                 b'"manifest":"' + base64.b64encode(members["manifest"]) + b'"}')
    without_pin = {key: value for key, value in envelope.items() if key != "pin"}
    cases = [
        (raw_for(duplicate), "input_invalid"),
        (raw_for(without_pin), "input_invalid"),
        (raw_for({**envelope, "extra_member": "eA=="}), "input_invalid"),
        (raw_for({**envelope, "manifest": 42}), "input_invalid"),
        (raw_for({**envelope, "overlay": {"nested": {}}}), "input_invalid"),
        (raw_for({**envelope, "manifest": " aGVsbG8="}), "input_invalid"),
        (raw_for({**envelope, "manifest": "eA"}), "input_invalid"),
        (raw_for({**envelope, "manifest": "eB=="}), "input_invalid"),
        (raw_for({**envelope, "manifest": "eA==\n"}), "input_invalid"),
        (with_manifest({**doc, "version": 2}), "input_invalid"),
        (with_manifest({**doc, "mode": "native_rehearsal"}), "input_invalid"),
        (with_manifest({**doc, "overlay": {"path": "/proposal.yaml",
                                           "sha256": digest(OVERLAY)}}), "input_invalid"),
        (with_manifest({**doc, "overlay": {"sha256": digest(OVERLAY), "note": "x"}}),
         "input_invalid"),
        (with_manifest(b"{"), "input_invalid"),
        (with_manifest(b'{"x": NaN}'), "input_invalid"),
    ]
    for (raw, expected), reason in cases:
        with pytest.raises(schema.InputError, match=f"^{reason}$"):
            schema.admit_bundle(raw, expected)
    # Baseline presence must agree between envelope and manifest.
    raw, expected = raw_for({**envelope, "baseline_overlay":
                             base64.b64encode(BASELINE).decode()})
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.admit_bundle(raw, expected)
    doc_with_baseline = {**doc, "baseline_overlay": {"sha256": digest(BASELINE)}}
    raw = encode_bundle(dict(routine_members(),
                             manifest=json.dumps(doc_with_baseline).encode()))
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.admit_bundle(raw, digest(raw))
    # A baseline with a stale member digest is a mismatch, not a format error.
    stale = dict(routine_members(BASELINE),
                 manifest=json.dumps({**json.loads(routine_members()["manifest"]),
                                      "baseline_overlay": {"sha256": digest(OVERLAY)}}).encode())
    raw = encode_bundle(stale)
    with pytest.raises(schema.InputError, match="^digest_mismatch$"):
        schema.admit_bundle(raw, digest(raw))


def test_bundle_enforces_role_byte_limits(schema):
    oversized = b" " * (256 * 1024 + 1)
    raw = member_bundle(overlay=oversized)
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.admit_bundle(raw, digest(raw))


def test_load_bundle_validates_both_overlays_and_the_pin(schema):
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.load_bundle(member_bundle(overlay=OVERLAY + b"callbacks: []\n"),
                           digest(member_bundle(overlay=OVERLAY + b"callbacks: []\n")))
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.load_bundle(member_bundle(pin=b'base_version = "1.2.3"\n'),
                           digest(member_bundle(pin=b'base_version = "1.2.3"\n')))
    members = routine_members(BASELINE + b"unsupported: true\n")
    raw = encode_bundle(members)
    with pytest.raises(schema.InputError, match="^input_invalid$"):
        schema.load_bundle(raw, digest(raw))


# --- T012: local experimental boundary; the fixture does not qualify startup. ---

_REPO = Path(__file__).resolve().parents[1]
_CATEGORIES = ["model_list", "litellm_settings", "general_settings", "router_settings"]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


@pytest.fixture
def experiment(tmp_path):
    import shlex
    import yaml

    for name in ("Makefile", "rehearsal-check", "rehearsal/check.py",
                 "rehearsal/schemas/configuration_check.py", "core/render_config.py",
                 "core/config.template.header.txt"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO / name, dest)

    # Test-installed reference, created before invocation and not sourced from
    # stdin. The two runtime inventory digests are fixture identities, NOT proof
    # of verified-object continuity into native interpreter startup.
    def provision():
        def hash_file(name):
            return digest((tmp_path / name).read_bytes())

        parser_names = ("check.py", "schemas/configuration_check.py")
        context = {
            "version": 1,
            "renderer_sha256": hash_file("core/render_config.py"),
            "template_sha256": hash_file("core/config.template.header.txt"),
            "parser_inventory_sha256": digest(canonical({
                name: hash_file(f"rehearsal/{name}") for name in parser_names})),
            "python": {"implementation": sys.implementation.name,
                       "version": sys.version.split()[0],
                       "executable_sha256": digest(Path(sys.executable).read_bytes()),
                       "inventory_sha256": digest(b"synthetic Python inventory")},
            "pyyaml": {"version": yaml.__version__,
                       "inventory_sha256": digest(b"synthetic PyYAML inventory")},
            "settings": {"HOME": "/rehearsal-home"},
        }
        record = tmp_path / "fixture-context.json"
        record.write_bytes(canonical(context))
        return context

    launcher = tmp_path / "rehearsal/.check-bootstrap"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([
        "/usr/bin/env", "-i", "HOME=/rehearsal-home", sys.executable, "-I", "-S", "-B",
        str(tmp_path / "rehearsal/check.py"), str(tmp_path / "fixture-context.json"),
        str(Path(yaml.__file__).resolve().parent.parent),
    ]) + ' "$@"\n')
    launcher.chmod(0o700)
    return tmp_path, provision


@pytest.fixture(params=["cli", "make"])
def check_command(request, experiment):
    root, _ = experiment

    def command(expected, *extra):
        if request.param == "make":
            return ["make", "-s", "-j1", "rehearse-check", f"REHEARSAL_BUNDLE_SHA256={expected}", *extra]
        return [str(root / "rehearsal-check"), "--bundle-sha256", expected, *extra]

    return root, command, {0: 0, 1: 2, 78: 2} if request.param == "make" else {0: 0, 1: 1, 78: 78}


def invoke(check_command, expected, raw, *, env=None):
    root, command, _ = check_command
    return subprocess.run(command(expected), cwd=root, input=raw,
                          capture_output=True, timeout=15, env=env)


def result_of(process, exit_code):
    assert process.returncode == exit_code, process.stderr
    assert 0 < len(process.stdout) <= 4096
    result = json.loads(process.stdout)
    assert result["version"] == 1 and result["mode"] == "configuration_check"
    assert result["runtime_not_exercised"] is True
    if result["status"] != "checked":
        assert set(result) == {"version", "mode", "runtime_not_exercised", "status", "reason"}
    return result


def test_command_checks_exact_stream_and_reviewed_baseline(experiment, check_command):
    import yaml
    from core.render_config import render

    root, provision = experiment
    _, _, exits = check_command
    context = provision()
    members = routine_members(BASELINE)
    raw = encode_bundle(members) + b"\r\n\t "
    template = (root / "core/config.template.header.txt").read_bytes().decode()
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    first = invoke(check_command, digest(raw), raw)
    altered_env = dict(os.environ, HOME="/unused-for-test", PYTHONPATH="/unused-for-test")
    second = invoke(check_command, digest(raw), raw, env=altered_env)
    assert not first.stderr and not second.stderr and first.stdout == second.stdout
    assert result_of(first, exits[0]) == {
        "version": 1, "mode": "configuration_check", "status": "checked",
        "runtime_not_exercised": True, "bundle_sha256": digest(raw),
        "manifest_sha256": digest(members["manifest"]), "overlay_sha256": digest(OVERLAY),
        "pin_sha256": digest(PIN), "template_sha256": context["template_sha256"],
        "render_context_sha256": digest(canonical(context)),
        "rendered_sha256": digest(render(yaml.safe_load(OVERLAY), template,
                                         home="/rehearsal-home").encode()),
        "categories_present": _CATEGORIES,
        "comparison": {
            "baseline_overlay_sha256": digest(BASELINE),
            "baseline_rendered_sha256": digest(render(yaml.safe_load(BASELINE), template,
                                                      home="/rehearsal-home").encode()),
            "changed_categories": ["model_list"],
        },
    }
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_command_comparison_null_equal_and_ordered(experiment, check_command):
    _, provision = experiment
    provision()
    _, _, exits = check_command
    changed = BASELINE.replace(b"telemetry: false", b"telemetry: true").replace(
        b"to: [example/two]", b"to: [example/two, example/one]")
    for baseline, categories in ((None, None), (OVERLAY, []),
                                  (changed, ["model_list", "litellm_settings", "router_settings"])):
        raw = encode_bundle(routine_members(baseline))
        comparison = result_of(invoke(check_command, digest(raw), raw), exits[0])["comparison"]
        assert (comparison if comparison is None else comparison["changed_categories"]) == categories


def test_command_refuses_invalid_arguments_and_streams(experiment, check_command):
    root, provision = experiment
    provision()
    _, command, exits = check_command
    raw = encode_bundle(routine_members())
    oversize = raw + b" " * (768 * 1024 + 1 - len(raw))
    for stream, expected, reason in (
        (raw, f" {digest(raw)} ", "input_invalid"),
        (raw, "literal'quote\"and whitespace", "input_invalid"),
        (raw, "", "input_invalid"),
        (raw + b"\n", digest(raw), "digest_mismatch"),
        (b"not json", digest(b"not json"), "input_invalid"),
        (b"", digest(b""), "input_invalid"),
        (oversize, digest(oversize), "input_invalid"),
    ):
        result = result_of(invoke(check_command, expected, stream), exits[78])
        assert (result["status"], result["reason"]) == ("refused", reason)
    with (root / "regular-input").open("wb+") as handle:
        handle.write(raw)
        handle.seek(0)
        completed = subprocess.run(command(digest(raw)), cwd=root, stdin=handle,
                                   capture_output=True, timeout=15)
    assert result_of(completed, exits[78])["reason"] == "input_invalid"
    if exits[78] == 78:
        for args in ([], ["--bundle-sha256", digest(raw)] * 2, ["--manifest", "old"]):
            done = subprocess.run([str(root / "rehearsal-check"), *args], cwd=root,
                                  input=raw, capture_output=True, timeout=15)
            assert result_of(done, 78)["reason"] == "input_invalid"


def test_no_eof_does_not_admit_valid_prefix(experiment, check_command):
    _, provision = experiment
    provision()
    root, command, exits = check_command
    raw = encode_bundle(routine_members())
    with subprocess.Popen(command(digest(raw)), cwd=root, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            assert child.stdin is not None
            child.stdin.write(raw)
            child.stdin.flush()
            child.wait(timeout=13)  # deliberately keep the input pipe open
            stdout, stderr = child.communicate()
        finally:
            if child.poll() is None:
                child.kill()
    done = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
    assert result_of(done, exits[1])["reason"] == "deadline"


def test_command_fails_when_startup_fixture_or_expected_identity_is_missing(experiment, check_command):
    root, provision = experiment
    _, _, exits = check_command
    raw = encode_bundle(routine_members())
    for setup in (lambda: None, provision):
        setup()
        renderer = root / "core/render_config.py"
        with renderer.open("ab") as handle:
            handle.write(b"\nraise AssertionError('fixture code must not execute')\n")
        result = result_of(invoke(check_command, digest(raw), raw), exits[1])
        assert (result["status"], result["reason"]) == ("failed", "render_failed")
    (root / "rehearsal/.check-bootstrap").unlink()
    assert result_of(invoke(check_command, digest(raw), raw), exits[1])["reason"] == "render_failed"


@pytest.mark.parametrize("stage", ["context", "render"])
def test_worker_deadline_covers_context_and_render(experiment, check_command, stage):
    root, provision = experiment
    worker = root / "rehearsal/check.py"
    worker.write_text(worker.read_text().replace("DEADLINE_SECONDS = 10", "DEADLINE_SECONDS = 2"))
    renderer = root / "core/render_config.py"
    if stage == "render":
        with renderer.open("a") as handle:
            handle.write("\n_original_render = render\ndef render(*args, **kwargs):\n"
                         "    import time\n    time.sleep(5)\n"
                         "    return _original_render(*args, **kwargs)\n")
    provision()
    if stage == "context":
        context = root / "fixture-context.json"
        context.unlink()
        os.mkfifo(context)
    raw = encode_bundle(routine_members())
    _, _, exits = check_command
    assert result_of(invoke(check_command, digest(raw), raw), exits[1])["reason"] == "deadline"


def test_worker_keeps_deadline_armed_while_delivering_result(monkeypatch):
    from rehearsal import check

    state = {"armed": False}

    def set_timer(_kind, seconds):
        state["armed"] = bool(seconds)

    def emit(_code, _payload):
        assert state["armed"]
        raise check.Deadline

    monkeypatch.setattr(check.signal, "signal", lambda *_: None)
    monkeypatch.setattr(check.signal, "setitimer", set_timer)
    monkeypatch.setattr(check, "_emit", emit)
    assert check.main() == 1
    assert not state["armed"]


def test_renderer_stops_writing_at_utf8_budget():
    from core.render_config import RenderLimit, render
    import yaml

    overlay = yaml.safe_load(OVERLAY)
    template = "# café\n"
    rendered = render(overlay, template, home="/rehearsal-home")
    size = len(rendered.encode("utf-8"))
    assert render(overlay, template, home="/rehearsal-home", max_bytes=size) == rendered
    with pytest.raises(RenderLimit):
        render(overlay, template, home="/rehearsal-home", max_bytes=size - 1)


def test_command_reports_render_errors_and_limits(experiment, check_command):
    root, provision = experiment
    renderer = root / "core/render_config.py"
    original = renderer.read_bytes()
    raw = encode_bundle(routine_members())
    _, _, exits = check_command
    for body, reason in ((b"raise ValueError('synthetic failure')", "render_failed"),
                         (b"return ' ' * (1024 * 1024 + 1)", "output_limit")):
        renderer.write_bytes(original + b"\ndef render(*args, **kwargs):\n    " + body + b"\n")
        provision()
        done = invoke(check_command, digest(raw), raw)
        assert b"synthetic failure" not in done.stderr
        assert result_of(done, exits[1])["reason"] == reason


@pytest.mark.parametrize("native_goal", ["rehearse-check", "rehearse"])
@pytest.mark.parametrize("reverse", [True, False])
def test_make_refuses_mixed_rehearsal_goals_before_any_recipe(experiment, native_goal, reverse):
    root, _ = experiment
    with (root / "Makefile").open("a") as handle:
        handle.write("\nrehearse inert:\n\t@printf 'unexpected recipe\\n'\n")
    goals = [native_goal, "inert"]
    if reverse:
        goals.reverse()
    done = subprocess.run(["make", "-s", "-j1", *goals], cwd=root,
                          capture_output=True, timeout=15)
    assert done.returncode != 0 and done.stdout == b""
    assert b"Run one rehearsal target at a time" in done.stderr

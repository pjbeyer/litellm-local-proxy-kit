"""Offline tests for the configuration-check data contract."""
import hashlib
import importlib
import json
import os

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
    for name in ("manifest", "overlay", "pin", "template", "render_context", "rendered"):
        value[f"{name}_sha256"] = "a" * 64
    raw = schema.encode_result(value)
    assert len(raw) <= 4096 and json.loads(raw) == value
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

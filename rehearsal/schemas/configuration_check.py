"""Closed v1 data model for the advisory configuration check."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import tomllib
from typing import Protocol
from urllib.parse import urlsplit

MODE = "configuration_check"
LIMITS = {"manifest": 16 * 1024, "overlay": 256 * 1024, "pin": 32 * 1024,
          "baseline_overlay": 256 * 1024}
CATEGORIES = ("model_list", "litellm_settings", "general_settings", "router_settings")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]*")
_NAME = re.compile(r"[A-Za-z0-9._/-]+")
_RESULT_HASHES = {f"{name}_sha256" for name in
                  ("manifest", "overlay", "pin", "template", "render_context", "rendered")}
_REASONS = {"refused": ("input_invalid", "digest_mismatch"),
            "failed": ("render_failed", "output_limit", "deadline")}


class InputError(ValueError):
    """An input cannot be admitted; its exception text is a fixed reason."""


class ResultError(ValueError):
    """Result shape or encoded size violated the closed contract."""


class ReviewedReader(Protocol):
    def __call__(self, path: str, *, limit: int, expected: str) -> bytes:
        """Return retained bytes from an admitted object, or raise InputError."""
        ...


@dataclass(frozen=True)
class ReviewedCheck:
    manifest_sha256: str
    overlay: bytes
    pin: bytes
    baseline_overlay: bytes | None


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _closed(value, required, optional=()) -> bool:
    return isinstance(value, dict) and set(required) <= value.keys() <= set(required).union(optional)


def _version(value) -> bool:
    return type(value) is int and value == 1


def require_digest(raw: bytes, expected: str) -> None:
    if not is_digest(expected):
        raise InputError("input_invalid")
    if sha256(raw) != expected:
        raise InputError("digest_mismatch")


def _pairs(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError
    return result


def _reject_constant(_):
    raise ValueError


def parse_manifest(raw: bytes, expected: str) -> dict:
    if len(raw) > LIMITS["manifest"]:
        raise InputError("input_invalid")
    require_digest(raw, expected)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_reject_constant)
        if not _closed(value, ("version", "mode", "overlay", "pin"), ("baseline_overlay",)):
            raise ValueError
        if not _version(value["version"]) or value["mode"] != MODE:
            raise ValueError
        for key in ("overlay", "pin", "baseline_overlay"):
            if key not in value:
                continue
            entry = value[key]
            if not _closed(entry, ("path", "sha256")) or not is_digest(entry["sha256"]):
                raise ValueError
            path = entry["path"]
            if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
                raise ValueError
            path.encode("utf-8")
    except (ValueError, RecursionError):
        raise InputError("input_invalid") from None
    return value


def parse_pin(raw: bytes) -> dict:
    if len(raw) > LIMITS["pin"]:
        raise InputError("input_invalid")
    try:
        value = tomllib.loads(raw.decode("utf-8"))
        version, tip = value.get("base_version"), value.get("patch_tip")
        if not isinstance(version, str) or not version.strip():
            raise ValueError
        if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{7,40}", tip):
            raise ValueError
    except (ValueError, RecursionError):
        raise InputError("input_invalid") from None
    return {"base_version": version, "patch_tip": tip}


def parse_bounded_yaml(raw: bytes, *, limit: int, nodes: int, depth: int):
    """Preflight representation events before SafeLoader constructs values."""
    import yaml
    from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, ScalarEvent
    from yaml.nodes import ScalarNode

    class Loader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            keys = set()
            for key, _ in node.value:
                if (not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str"
                        or key.value == "<<" or key.value in keys):
                    raise ValueError
                keys.add(key.value)
            return super().construct_mapping(node, deep=deep)

    if len(raw) > limit:
        raise ValueError
    text = raw.decode("utf-8")
    level = count = 0
    for event in yaml.parse(text, Loader=Loader):
        if isinstance(event, AliasEvent):
            raise ValueError
        if isinstance(event, (ScalarEvent, CollectionStartEvent)):
            count += 1
            if event.anchor is not None or event.tag is not None or count > nodes or level >= depth:
                raise ValueError
        if isinstance(event, CollectionStartEvent):
            level += 1
        elif isinstance(event, CollectionEndEvent):
            level -= 1
    try:
        return yaml.load(text, Loader=Loader)
    except (OverflowError, TypeError, RecursionError):
        raise ValueError from None


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _url(value):
    if (not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in ("$", "{", "}", "?", "#", "\\"))):
        return False
    url = urlsplit(value)
    if url.port is not None and not url.port:
        return False
    return url.scheme in ("https", "http") and bool(url.hostname) and url.username is None


def _bools(value, keys):
    return all(type(value[key]) is bool for key in keys if key in value)


def _positives(value, keys):
    return all(type(value[key]) is int and value[key] > 0 for key in keys if key in value)


def _overlay(value):
    if not _closed(value, ("master_key_env", "upstream"), ("litellm_settings", "router_fallbacks")):
        raise ValueError
    upstream = value["upstream"]
    if not _closed(upstream, ("api_key_env", "api_base"), ("models", "wildcard", "wildcard_timeouts")):
        raise ValueError
    if not (_matches(_ENV, value["master_key_env"]) and _matches(_ENV, upstream["api_key_env"])
            and _url(upstream["api_base"]) and _bools(upstream, ("wildcard", "wildcard_timeouts"))):
        raise ValueError
    models, wildcard = upstream.get("models", []), upstream.get("wildcard", False)
    if not isinstance(models, list) or (not wildcard and (not models or "wildcard_timeouts" in upstream)):
        raise ValueError
    names = set()
    timeout_fields = ("timeout", "stream_timeout")
    for model in models:
        if not _closed(model, ("name",), timeout_fields):
            raise ValueError
        name = model["name"]
        if not _matches(_NAME, name) or name in names or not _positives(model, timeout_fields):
            raise ValueError
        names.add(name)
    settings = value.get("litellm_settings", {})
    settings_bools = ("drop_params", "modify_params", "telemetry")
    if (not _closed(settings, (), (*settings_bools, "request_timeout"))
            or not _bools(settings, settings_bools) or not _positives(settings, ("request_timeout",))):
        raise ValueError
    fallbacks = value.get("router_fallbacks", [])
    if not isinstance(fallbacks, list):
        raise ValueError
    sources = set()
    for fallback in fallbacks:
        if not _closed(fallback, ("from", "to")):
            raise ValueError
        source, targets = fallback["from"], fallback["to"]
        if (not isinstance(source, str) or source not in names or source in sources
                or not isinstance(targets, list) or not targets
                or any(not isinstance(t, str) or t not in names for t in targets)
                or len(set(targets)) != len(targets)):
            raise ValueError
        sources.add(source)
    return value


def parse_overlay(raw: bytes) -> dict:
    import yaml

    try:
        parsed = parse_bounded_yaml(raw, limit=LIMITS["overlay"], nodes=4096, depth=24)
        return _overlay(parsed)
    except (ValueError, OverflowError, RecursionError, yaml.YAMLError):
        raise InputError("input_invalid") from None


def load_reviewed_check(reader: ReviewedReader, manifest_path: str, expected: str) -> ReviewedCheck:
    """Validate retained inputs through one trusted, root-bound reader."""
    if not is_digest(expected):
        raise InputError("input_invalid")
    raw = reader(manifest_path, limit=LIMITS["manifest"], expected=expected)
    manifest = parse_manifest(raw, expected)

    def read_entry(field):
        entry = manifest[field]
        return reader(entry["path"], limit=LIMITS[field], expected=entry["sha256"])

    overlay = read_entry("overlay")
    parse_overlay(overlay)
    pin = read_entry("pin")
    parse_pin(pin)
    baseline = None
    if "baseline_overlay" in manifest:
        baseline = read_entry("baseline_overlay")
        parse_overlay(baseline)
    return ReviewedCheck(expected, overlay, pin, baseline)


def _categories(value):
    return isinstance(value, list) and value == [name for name in CATEGORIES if name in value]


def encode_result(value: dict) -> bytes:
    base = {"version", "mode", "runtime_not_exercised", "status"}
    if not (isinstance(value, dict) and _version(value.get("version"))
            and value.get("mode") == MODE and value.get("runtime_not_exercised") is True):
        raise ResultError("render_failed")
    status = value.get("status")
    if status == "checked":
        if not (_closed(value, base | _RESULT_HASHES | {"categories_present", "comparison"})
                and all(is_digest(value[key]) for key in _RESULT_HASHES)
                and _categories(value["categories_present"])):
            raise ResultError("render_failed")
        baseline = value["comparison"]
        hashes = ("baseline_overlay_sha256", "baseline_rendered_sha256")
        if baseline is not None and not (_closed(baseline, (*hashes, "changed_categories"))
                                         and all(is_digest(baseline[k]) for k in hashes)
                                         and _categories(baseline["changed_categories"])):
            raise ResultError("render_failed")
    elif not (isinstance(status, str) and status in _REASONS
              and _closed(value, base | {"reason"}) and value["reason"] in _REASONS[status]):
        raise ResultError("render_failed")
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(raw) > 4096:
        raise ResultError("output_limit")
    return raw

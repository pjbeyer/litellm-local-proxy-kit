"""End-to-end tests for core/harvest-filter-metrics.sh (provider-agnostic)."""
import json
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
HARVEST = KIT / "core" / "harvest-filter-metrics.sh"

FILTER_LOG = (
    "2026-10-24 01:00:01 - LiteLLM Proxy:INFO: hook.py:254 - "
    "Semantic tool filter: 894->7 MCP tools (0 native preserved, 1 total)\n"
    "2026-10-24 01:00:05 - LiteLLM Proxy:INFO: hook.py:261 - "
    "Semantic tool filter: all 11 tools are native, no MCP filtering applied\n"
    "2026-10-24 01:00:10 - LiteLLM:INFO: distractor mentioning 894->2 mid-text\n"
    "2026-10-24 01:00:12 - LiteLLM Proxy:WARNING: hook.py:290 - "
    "Failed to emit semantic filter metadata: boom\n"
)

CONFIG_ON = """\
litellm_settings:
  mcp_semantic_tool_filter:
    enabled: true
    embedding_model: openai/text-embedding-3-small
    top_k: 6
    similarity_threshold: 0.45
"""

CONFIG_OFF = "litellm_settings:\n  drop_params: true\n"


def _run(tmp: Path, config: Path):
    env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(tmp),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "LITELLM_PROXY_LOG_DIR": str(tmp / "data"),
        "LITELLM_FILTER_CONFIG": str(config),
    }
    return subprocess.run(
        ["bash", str(HARVEST)], env=env, capture_output=True, text=True, timeout=60
    )


def _jsonl(tmp: Path) -> list[dict]:
    out = tmp / "data" / "litellm" / "filter-metrics.jsonl"
    return [json.loads(line) for line in out.read_text().splitlines()]


def test_harvest_parses_events_and_echoes_config(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "data" / "proxy.log").write_text(FILTER_LOG)
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_ON)

    r = _run(tmp_path, config)
    assert r.returncode == 0, r.stderr
    rows = _jsonl(tmp_path)
    kinds = [row["event"] for row in rows]
    # 2 filter events + 1 heartbeat; distractor line must NOT create an event.
    assert kinds.count("filter") == 1
    assert kinds.count("native_only") == 1
    assert kinds.count("heartbeat") == 1
    flt = next(row for row in rows if row["event"] == "filter")
    assert flt["before"] == 894 and flt["after"] == 7
    # Provider echo is informational and provider-agnostic: any model string.
    assert flt["embedding_model"] == "openai/text-embedding-3-small"
    assert flt["top_k"] == 6 and flt["similarity_threshold"] == 0.45


def test_harvest_idempotent_and_heartbeat_on_quiet(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "data" / "proxy.log").write_text(FILTER_LOG)
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_ON)
    _run(tmp_path, config)
    r2 = _run(tmp_path, config)
    assert r2.returncode == 0
    rows = _jsonl(tmp_path)
    # Second run harvests nothing new: exactly one more heartbeat, no dup events.
    assert len([r for r in rows if r["event"] == "filter"]) == 1
    assert len([r for r in rows if r["event"] == "heartbeat"]) == 2


def test_harvest_survives_rotation_shrink(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    log = tmp_path / "data" / "proxy.log"
    log.write_text(FILTER_LOG)
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_ON)
    _run(tmp_path, config)
    # Simulate copy+truncate rotation: file shrinks below the cursor offset.
    log.write_text("2026-10-24 02:00:00 INFO: fresh boot line\n")
    _run(tmp_path, config)
    # Third run must not skip or crash; total filter events stays 1 (no re-read
    # of old lines because the shrink reset re-read only the new small file).
    _run(tmp_path, config)
    rows = _jsonl(tmp_path)
    assert len([r for r in rows if r["event"] == "filter"]) == 1


def test_harvest_records_feature_disabled(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "data" / "proxy.log").write_text("unrelated line\n")
    config = tmp_path / "config-off.yaml"
    config.write_text(CONFIG_OFF)
    r = _run(tmp_path, config)
    assert r.returncode == 0, r.stderr
    rows = _jsonl(tmp_path)
    assert len(rows) == 1
    hb = rows[0]
    assert hb["event"] == "heartbeat"
    assert hb["filter_enabled"] == "absent"
    assert hb["embedding_model"] is None
    assert hb["filter_events"] == 0


def test_harvest_disabled_by_env(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "LITELLM_PROXY_LOG_DIR": str(tmp_path / "data"),
        "LITELLM_FILTER_HARVEST": "0",
    }
    r = subprocess.run(
        ["bash", str(HARVEST)], env=env, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0
    assert not (tmp_path / "data" / "litellm" / "filter-metrics.jsonl").exists()
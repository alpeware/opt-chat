"""Tests for OptChat token telemetry tracking and SQLite storage engine."""

import os
from pathlib import Path
import tempfile
import pytest

from optchat.telemetry import TelemetryStore, estimate_cost_usd, MODEL_PRICING


def test_estimate_cost_usd():
    # Flash tier
    cost_flash = estimate_cost_usd("gemini-3.8-flash-low", input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    expected_flash = 0.075 + 0.30 + 0.01875
    assert abs(cost_flash - round(expected_flash, 6)) < 1e-5

    # Argon tier
    cost_argon = estimate_cost_usd("Gemini 4 Argon (Medium)", input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=0)
    expected_argon = 1.25 + 5.00
    assert abs(cost_argon - round(expected_argon, 6)) < 1e-5

    # Fallback default
    cost_unknown = estimate_cost_usd("unknown-model", input_tokens=10_000, output_tokens=2_000)
    assert cost_unknown > 0.0


def test_telemetry_store_lifecycle(tmp_path: Path):
    store = TelemetryStore(chat_dir=tmp_path)
    assert store.db_path.is_file()

    # Initial empty summary
    summary = store.get_summary()
    assert summary["total_requests"] == 0
    assert summary["total_tokens"] == 0
    assert summary["total_cost_usd"] == 0.0

    # Record first turn
    row_id1 = store.record_turn(
        workspace="opt-chat",
        model="gemini-3.8-flash-low",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        req_id="req-101",
        source="web",
        role="main",
        provider="agy",
        duration_ms=450,
        status="ok",
    )
    assert row_id1 > 0

    # Record second turn in different workspace
    row_id2 = store.record_turn(
        workspace="jacquard",
        model="gemini-4-argon-medium",
        input_tokens=5000,
        output_tokens=1000,
        cache_read_tokens=2000,
        req_id="req-102",
        source="hook_external",
        role="main",
        provider="agy",
        duration_ms=1200,
        status="ok",
    )
    assert row_id2 > row_id1

    # Check summary across all
    summary_all = store.get_summary()
    assert summary_all["total_requests"] == 2
    assert summary_all["total_input_tokens"] == 6000
    assert summary_all["total_output_tokens"] == 1200
    assert summary_all["total_tokens"] == 7200
    assert summary_all["total_cache_read_tokens"] == 2500
    assert summary_all["total_cost_usd"] > 0

    # Filter summary by workspace
    summary_ws = store.get_summary(workspace="opt-chat")
    assert summary_ws["total_requests"] == 1
    assert summary_ws["total_tokens"] == 1200

    # Filter summary by model
    summary_model = store.get_summary(model="gemini-4-argon-medium")
    assert summary_model["total_requests"] == 1
    assert summary_model["total_tokens"] == 6000

    # Daily usage
    daily = store.get_daily_usage(days=7)
    assert len(daily) >= 1
    assert daily[0]["total_tokens"] == 7200

    # Workspace breakdown
    by_ws = store.get_workspace_breakdown()
    assert len(by_ws) == 2
    assert by_ws[0]["workspace"] == "jacquard"  # 6000 > 1200 total tokens
    assert by_ws[1]["workspace"] == "opt-chat"

    # Model breakdown
    by_model = store.get_model_breakdown()
    assert len(by_model) == 2

    # Lookup recent requests
    recent = store.get_recent_requests(limit=10)
    assert len(recent) == 2
    assert recent[0]["req_id"] == "req-102"
    assert recent[1]["req_id"] == "req-101"

    # Lookup by request ID
    req1 = store.get_request_by_id("req-101")
    assert req1 is not None
    assert req1["workspace"] == "opt-chat"
    assert req1["input_tokens"] == 1000

    req_none = store.get_request_by_id("non-existent")
    assert req_none is None


def test_compactor_and_subagent_telemetry(tmp_path: Path):
    from optchat.daemon import extract_cumulative_tokens_from_transcript
    import json

    store = TelemetryStore(chat_dir=tmp_path)

    # 1. Compactor turn tracking
    comp_row = store.record_turn(
        workspace="internal",
        model="gemini-3.8-flash-low",
        input_tokens=420,
        output_tokens=65,
        cache_read_tokens=100,
        source="compactor",
        role="compactor",
        provider="agy",
        duration_ms=300,
        status="ok",
    )
    assert comp_row > 0

    # 2. Subagent turn tracking
    sub_row = store.record_turn(
        workspace="opt-chat",
        model="gemini-4-argon-medium",
        input_tokens=1500,
        output_tokens=300,
        cache_read_tokens=500,
        conversation_id="subagent-conv-123",
        source="subagent",
        role="coder",
        provider="agy",
        duration_ms=800,
        status="ok",
    )
    assert sub_row > comp_row

    # 3. Test extract_cumulative_tokens_from_transcript helper
    transcript_file = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "USER_INPUT", "content": "hello"}),
        json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 50}),
        json.dumps({"type": "TOOL_CALL", "content": "run_command"}),
        json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 200, "output_tokens": 40, "cache_read_tokens": 100}),
    ]
    transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tokens = extract_cumulative_tokens_from_transcript(str(transcript_file))
    assert tokens["input_tokens"] == 300
    assert tokens["output_tokens"] == 60
    assert tokens["cache_read_tokens"] == 150

    # 4. Check summary reflects compactor and subagent
    summary = store.get_summary()
    assert summary["total_requests"] == 2
    assert summary["total_input_tokens"] == 1920
    assert summary["total_output_tokens"] == 365
    assert summary["total_tokens"] == 2285

    # 5. Check recent requests
    recent = store.get_recent_requests(limit=5)
    assert len(recent) == 2
    assert recent[0]["source"] == "subagent"
    assert recent[1]["source"] == "compactor"


def test_cache_hit_rate_bounded_and_model_normalization(tmp_path: Path):
    from optchat.telemetry import normalize_model_name
    from optchat.daemon import resolve_turn_model, extract_cumulative_tokens_from_transcript
    import json

    store = TelemetryStore(chat_dir=tmp_path)
    # In Antigravity transcripts, input_tokens is uncached input and cache_read_tokens is cached input.
    # When cache_read_tokens (80,000) > input_tokens (20,000), cache_hit_rate should be 80,000 / 100,000 = 0.8 (80%), never > 1.0.
    store.record_turn(
        workspace="bitecho",
        model="Gemini 4 Hydrogen (High)",
        input_tokens=20_000,
        output_tokens=1_000,
        cache_read_tokens=80_000,
    )
    summary = store.get_summary()
    assert summary["cache_hit_rate"] == 0.8
    assert summary["cache_hit_rate"] <= 1.0

    # Verify model normalization maps display names and aliases to canonical IDs
    assert normalize_model_name("Gemini 4 Argon (Medium)") == "gemini-4-argon-medium"
    assert normalize_model_name("Gemini 4 Hydrogen (High)") == "gemini-4-hydrogen-high"
    assert normalize_model_name("Gemini 4 Carbon (High)") == "gemini-4-carbon-high"
    assert normalize_model_name("Claude Opus 5.5 (High)") == "claude-opus-5-5-high"

    by_model = store.get_model_breakdown()
    assert len(by_model) == 1
    assert by_model[0]["model"] == "gemini-4-hydrogen-high"

    # Verify resolve_turn_model reads HookArgsCommon's modelName and subagent descriptors
    assert resolve_turn_model({"modelName": "Claude Opus 5.5 (High)"}) == "claude-opus-5-5-high"
    assert resolve_turn_model({"modelName": "Gemini 4 Argon (Medium)"}, sub_info={"role": "Opus Architect", "typeName": "opus-architect"}) == "claude-opus-5-5-high"
    assert resolve_turn_model({"modelName": "Gemini 4 Argon (Medium)"}, sub_info={"role": "Carbon Reviewer", "typeName": "carbon-reviewer"}) == "gemini-4-carbon-high"
    assert resolve_turn_model({"modelName": "Gemini 4 Argon (Medium)"}, sub_info={"role": "Explorer", "model": "flash"}) == "gemini-4-hydrogen-high"

    # Verify extract_cumulative_tokens_from_transcript respects min_step_index
    tpath = tmp_path / "sub_transcript.jsonl"
    tpath.write_text(
        "\n".join([
            json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50}),
            json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "input_tokens": 250, "output_tokens": 25, "cache_read_tokens": 125}),
        ]) + "\n",
        encoding="utf-8",
    )
    tok_all = extract_cumulative_tokens_from_transcript(str(tpath), min_step_index=-1)
    assert tok_all["input_tokens"] == 350
    assert tok_all["max_step_index"] == 3

    tok_delta = extract_cumulative_tokens_from_transcript(str(tpath), min_step_index=1)
    assert tok_delta["input_tokens"] == 250
    assert tok_delta["output_tokens"] == 25
    assert tok_delta["cache_read_tokens"] == 125
    assert tok_delta["max_step_index"] == 3
    tok_empty = extract_cumulative_tokens_from_transcript(str(tpath), min_step_index=3)
    assert tok_empty["input_tokens"] == 0
    assert tok_empty["output_tokens"] == 0


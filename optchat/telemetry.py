"""Telemetry and Token Consumption Tracking Engine for OptChat.

Stores per-request token metrics in an embedded SQLite database (WAL mode)
under ~/.optchat/telemetry.db to power fine-grained analysis, per-model and
per-workspace breakdowns, and time-series trends over time.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("optchat.telemetry")

# Standard pricing estimates (USD per 1M tokens: input, output, cache_read)
MODEL_PRICING: Dict[str, Tuple[float, float, float]] = {
    # Gemini 3.8 Flash tier
    "gemini-3.8-flash-low": (0.075, 0.30, 0.01875),
    "gemini-3.8-flash-medium": (0.075, 0.30, 0.01875),
    "gemini-3.8-flash-high": (0.075, 0.30, 0.01875),
    "gemini-1.5-flash": (0.075, 0.30, 0.01875),
    # Gemini 4 Hydrogen (Flash / Fast Coding tier)
    "gemini-4-hydrogen-low": (0.15, 0.60, 0.0375),
    "gemini-4-hydrogen-medium": (0.15, 0.60, 0.0375),
    "gemini-4-hydrogen-high": (0.15, 0.60, 0.0375),
    "gemini-4-hydrogen-xhigh": (0.15, 0.60, 0.0375),
    # Gemini 4 Argon / Pro tier
    "gemini-4-argon-low": (1.25, 5.00, 0.3125),
    "gemini-4-argon-medium": (1.25, 5.00, 0.3125),
    "gemini-4-argon-high": (1.25, 5.00, 0.3125),
    "gemini-pro-orchestrator-argon": (1.25, 5.00, 0.3125),
    "gemini-1.5-pro": (1.25, 5.00, 0.3125),
    # Gemini 4 Carbon / Pro tier
    "gemini-4-carbon-low": (1.25, 5.00, 0.3125),
    "gemini-4-carbon-medium": (1.25, 5.00, 0.3125),
    "gemini-4-carbon-high": (1.25, 5.00, 0.3125),
    "gemini-pro-orchestrator": (1.25, 5.00, 0.3125),
    # Claude Sonnet tier
    "claude-sonnet-5-5-low": (3.00, 15.00, 0.30),
    "claude-sonnet-5-5-medium": (3.00, 15.00, 0.30),
    "claude-sonnet-5-5-high": (3.00, 15.00, 0.30),
    "claude-3-5-sonnet": (3.00, 15.00, 0.30),
    # Claude Opus tier
    "claude-opus-5-5-low": (15.00, 75.00, 1.50),
    "claude-opus-5-5-medium": (15.00, 75.00, 1.50),
    "claude-opus-5-5-high": (15.00, 75.00, 1.50),
    # Open-weights / Fast OSS
    "gpt-oss-120b-medium": (0.50, 1.50, 0.10),
    # Default fallback
    "default": (0.10, 0.40, 0.025),
}

MODEL_ALIASES: Dict[str, str] = {
    "orchestrator-carbon": "gemini-pro-orchestrator",
    "orchestrator-argon": "gemini-pro-orchestrator-argon",
    "claude-opus-5.5-low": "claude-opus-5-5-low",
    "claude-opus-5.5-medium": "claude-opus-5-5-medium",
    "claude-opus-5.5-high": "claude-opus-5-5-high",
    "claude-sonnet-5.5-low": "claude-sonnet-5-5-low",
    "claude-sonnet-5.5-medium": "claude-sonnet-5-5-medium",
    "claude-sonnet-5.5-high": "claude-sonnet-5-5-high",
}


def normalize_model_name(model: Optional[str]) -> str:
    """Normalize a display name or raw model string into a canonical model slug ID."""
    if not model or not isinstance(model, str):
        return "unknown"
    raw = model.strip().lower()
    if not raw:
        return "unknown"
    clean = re.sub(r"[^a-z0-9.]+", "-", raw).strip("-")
    if clean in MODEL_ALIASES:
        return MODEL_ALIASES[clean]
    if clean in MODEL_PRICING and clean != "default":
        return clean
    # Normalize 5.5 -> 5-5 for claude models
    clean_dash = clean.replace("5.5", "5-5")
    if clean_dash in MODEL_PRICING:
        return clean_dash
    return clean or raw


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> float:
    """Calculate estimated cost in USD based on model pricing table."""
    norm_model = normalize_model_name(model)
    pricing = MODEL_PRICING.get(norm_model)
    if not pricing:
        if "opus" in norm_model:
            pricing = MODEL_PRICING["claude-opus-5-5-high"]
        elif "sonnet" in norm_model:
            pricing = MODEL_PRICING["claude-sonnet-5-5-high"]
        elif "carbon" in norm_model:
            pricing = MODEL_PRICING["gemini-4-carbon-high"]
        elif "hydrogen" in norm_model:
            pricing = MODEL_PRICING["gemini-4-hydrogen-high"]
        elif "argon" in norm_model:
            pricing = MODEL_PRICING["gemini-4-argon-medium"]
        elif "flash" in norm_model:
            pricing = MODEL_PRICING["gemini-3.8-flash-medium"]
        else:
            for k, v in MODEL_PRICING.items():
                if k != "default" and (k in norm_model or norm_model.startswith(k)):
                    pricing = v
                    break
    if not pricing:
        pricing = MODEL_PRICING["default"]

    in_rate, out_rate, cache_rate = pricing
    cost = (
        (input_tokens / 1_000_000.0) * in_rate
        + (output_tokens / 1_000_000.0) * out_rate
        + (cache_read_tokens / 1_000_000.0) * cache_rate
    )
    return round(cost, 6)


class TelemetryStore:
    """SQLite-backed telemetry store with WAL mode and indexing."""

    def __init__(self, db_path: Optional[Path] = None, chat_dir: Optional[Path] = None):
        if db_path is not None:
            self.db_path = Path(db_path).resolve()
        elif chat_dir is not None:
            self.db_path = Path(chat_dir).resolve() / "telemetry.db"
        else:
            default_dir = Path(os.environ.get("OPTCHAT_DIR", Path.home() / ".optchat")).resolve()
            self.db_path = default_dir / "telemetry.db"

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables, indexes, and views with WAL journal mode."""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        req_id TEXT,
                        conversation_id TEXT,
                        step_index INTEGER DEFAULT 0,
                        message_id INTEGER,
                        timestamp TEXT NOT NULL,
                        workspace TEXT NOT NULL,
                        device TEXT,
                        source TEXT NOT NULL,
                        role TEXT DEFAULT 'main',
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'ok',
                        cost_usd REAL DEFAULT 0.0
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ws_time ON token_usage(workspace, timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_model_time ON token_usage(model, timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_req_id ON token_usage(req_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_time ON token_usage(timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_source ON token_usage(source);")
        finally:
            conn.close()

    def record_turn(
        self,
        workspace: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        req_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        step_index: int = 0,
        message_id: Optional[int] = None,
        source: str = "web",
        role: str = "main",
        provider: str = "agy",
        duration_ms: int = 0,
        status: str = "ok",
        device: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        """Record an individual LLM turn / request."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        total_tokens = input_tokens + output_tokens
        cost = estimate_cost_usd(model, input_tokens, output_tokens, cache_read_tokens)
        ws_clean = (workspace or "unknown").strip().lower()
        model_clean = normalize_model_name(model)

        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO token_usage (
                        req_id, conversation_id, step_index, message_id,
                        timestamp, workspace, device, source, role,
                        provider, model, input_tokens, output_tokens,
                        cache_read_tokens, total_tokens, duration_ms,
                        status, cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        req_id,
                        conversation_id,
                        step_index,
                        message_id,
                        ts,
                        ws_clean,
                        device,
                        source,
                        role,
                        provider,
                        model_clean,
                        input_tokens,
                        output_tokens,
                        cache_read_tokens,
                        total_tokens,
                        duration_ms,
                        status,
                        cost,
                    ),
                )
                return cur.lastrowid or 0
        except Exception as e:
            logger.error("Failed to record token telemetry: %s", e)
            return 0
        finally:
            conn.close()

    def get_summary(
        self,
        days: Optional[int] = None,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate total tokens, cost, requests, and latency."""
        conn = self._get_connection()
        try:
            where_clauses: List[str] = []
            params: List[Any] = []

            if days is not None and days > 0:
                where_clauses.append("timestamp >= datetime('now', ? || ' days')")
                params.append(f"-{days}")
            if workspace and workspace.lower() != "all":
                where_clauses.append("workspace = ?")
                params.append(workspace.lower().strip())
            if model and model.lower() != "all":
                where_clauses.append("model = ?")
                params.append(normalize_model_name(model))

            where_str = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            query = f"""
                SELECT
                    COUNT(*) as total_requests,
                    COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as total_cache_read_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                    COALESCE(AVG(duration_ms), 0) as avg_duration_ms
                FROM token_usage
                {where_str}
            """
            row = conn.execute(query, params).fetchone()
            if not row:
                return {
                    "total_requests": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cache_read_tokens": 0,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "avg_duration_ms": 0,
                    "cache_hit_rate": 0.0,
                }

            in_tok = row["total_input_tokens"]
            cache_tok = row["total_cache_read_tokens"]
            total_prompt_tok = in_tok + cache_tok
            cache_rate = (cache_tok / total_prompt_tok) if total_prompt_tok > 0 else 0.0

            return {
                "total_requests": row["total_requests"],
                "total_input_tokens": in_tok,
                "total_output_tokens": row["total_output_tokens"],
                "total_cache_read_tokens": cache_tok,
                "total_tokens": row["total_tokens"],
                "total_cost_usd": round(row["total_cost_usd"], 4),
                "avg_duration_ms": int(row["avg_duration_ms"]),
                "cache_hit_rate": round(cache_rate, 4),
            }
        finally:
            conn.close()

    def get_daily_usage(
        self,
        days: int = 30,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return daily token metrics for time-series trend analysis."""
        conn = self._get_connection()
        try:
            where_clauses: List[str] = ["timestamp >= datetime('now', ? || ' days')"]
            params: List[Any] = [f"-{days}"]

            if workspace and workspace.lower() != "all":
                where_clauses.append("workspace = ?")
                params.append(workspace.lower().strip())
            if model and model.lower() != "all":
                where_clauses.append("model = ?")
                params.append(normalize_model_name(model))

            where_str = " WHERE " + " AND ".join(where_clauses)

            query = f"""
                SELECT
                    substr(timestamp, 1, 10) as date,
                    COUNT(*) as requests,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                    COALESCE(AVG(duration_ms), 0) as avg_duration_ms
                FROM token_usage
                {where_str}
                GROUP BY substr(timestamp, 1, 10)
                ORDER BY date ASC
            """
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "date": r["date"],
                    "requests": r["requests"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "total_tokens": r["total_tokens"],
                    "cost_usd": round(r["cost_usd"], 4),
                    "avg_duration_ms": int(r["avg_duration_ms"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_workspace_breakdown(self, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return token consumption grouped by workspace."""
        conn = self._get_connection()
        try:
            where_str = ""
            params: List[Any] = []
            if days is not None and days > 0:
                where_str = " WHERE timestamp >= datetime('now', ? || ' days')"
                params.append(f"-{days}")

            query = f"""
                SELECT
                    workspace,
                    COUNT(*) as requests,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as cost_usd
                FROM token_usage
                {where_str}
                GROUP BY workspace
                ORDER BY total_tokens DESC
            """
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "workspace": r["workspace"],
                    "requests": r["requests"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "total_tokens": r["total_tokens"],
                    "cost_usd": round(r["cost_usd"], 4),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_model_breakdown(self, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return token consumption grouped by model."""
        conn = self._get_connection()
        try:
            where_str = ""
            params: List[Any] = []
            if days is not None and days > 0:
                where_str = " WHERE timestamp >= datetime('now', ? || ' days')"
                params.append(f"-{days}")

            query = f"""
                SELECT
                    model,
                    COUNT(*) as requests,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                    COALESCE(AVG(duration_ms), 0) as avg_duration_ms
                FROM token_usage
                {where_str}
                GROUP BY model
                ORDER BY total_tokens DESC
            """
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "model": r["model"],
                    "requests": r["requests"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "total_tokens": r["total_tokens"],
                    "cost_usd": round(r["cost_usd"], 4),
                    "avg_duration_ms": int(r["avg_duration_ms"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_recent_requests(
        self,
        limit: int = 50,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent individual request telemetry records."""
        conn = self._get_connection()
        try:
            where_clauses: List[str] = []
            params: List[Any] = []

            if workspace and workspace.lower() != "all":
                where_clauses.append("workspace = ?")
                params.append(workspace.lower().strip())
            if model and model.lower() != "all":
                where_clauses.append("model = ?")
                params.append(normalize_model_name(model))

            where_str = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            params.append(limit)

            query = f"""
                SELECT
                    id, req_id, conversation_id, step_index, message_id,
                    timestamp, workspace, device, source, role,
                    provider, model, input_tokens, output_tokens,
                    cache_read_tokens, total_tokens, duration_ms,
                    status, cost_usd
                FROM token_usage
                {where_str}
                ORDER BY id DESC
                LIMIT ?
            """
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_request_by_id(self, req_id: str) -> Optional[Dict[str, Any]]:
        """Lookup individual request by correlation ID."""
        if not req_id:
            return None
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM token_usage WHERE req_id = ? ORDER BY id DESC LIMIT 1",
                (req_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_tokens_for_message_ids(self, message_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Lookup token consumption metrics for a list of message IDs."""
        if not message_ids:
            return {}
        conn = self._get_connection()
        try:
            placeholders = ",".join(["?"] * len(message_ids))
            query = f"""
                SELECT message_id, input_tokens, output_tokens, cache_read_tokens, total_tokens, cost_usd
                FROM token_usage
                WHERE message_id IN ({placeholders})
            """
            rows = conn.execute(query, message_ids).fetchall()
            res: Dict[int, Dict[str, Any]] = {}
            for r in rows:
                mid = r["message_id"]
                if mid is not None:
                    res[mid] = {
                        "input": r["input_tokens"],
                        "output": r["output_tokens"],
                        "cache_read": r["cache_read_tokens"],
                        "total": r["total_tokens"],
                        "cost_usd": r["cost_usd"],
                    }
            return res
        finally:
            conn.close()


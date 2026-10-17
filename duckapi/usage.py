"""Persistent local usage estimates for chat and image requests.

Duck.ai does not return billable token usage. Token counts here are rough
UTF-8-byte estimates and costs are optional reference estimates configured by
the dashboard; prompts and completions are never written to the database.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


_DEFAULT_DB = Path(__file__).resolve().with_name("usage.sqlite3")
_DB_PATH = Path(os.getenv("DUCKAI_USAGE_DB", str(_DEFAULT_DB))).expanduser()
_LOCK = threading.RLock()
_RANGES = {"24h": 24 * 60 * 60, "7d": 7 * 24 * 60 * 60, "30d": 30 * 24 * 60 * 60}


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _initialize() -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                model TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                kind TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER,
                input_bytes INTEGER NOT NULL,
                output_bytes INTEGER NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp
            ON usage_events(timestamp DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_usage_events_model_timestamp
            ON usage_events(model, timestamp DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_rates (
                model TEXT PRIMARY KEY,
                input_usd_per_million REAL NOT NULL,
                output_usd_per_million REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
    try:
        os.chmod(_DB_PATH, 0o600)
    except OSError:
        pass


def estimate_tokens_from_bytes(byte_count: int) -> int:
    """Return a coarse token estimate (~4 UTF-8 bytes per token)."""
    return max(0, (max(0, int(byte_count)) + 3) // 4)


def estimate_tokens(text: str) -> int:
    return estimate_tokens_from_bytes(
        len((text or "").encode("utf-8", errors="replace"))
    )


def _rates_map(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    return {
        row["model"]: (row["input_usd_per_million"], row["output_usd_per_million"])
        for row in conn.execute(
            "SELECT model, input_usd_per_million, output_usd_per_million FROM usage_rates"
        )
    }


def record_usage(
    *,
    model: str,
    endpoint: str,
    input_text: str,
    output_text: Optional[str] = "",
    output_bytes: Optional[int] = None,
    kind: str = "chat",
    image_count: int = 0,
    status: str = "success",
) -> dict:
    """Persist estimates and return the stored summary row.

    Pass ``output_text=None`` for modalities where output tokens do not apply,
    such as image generation. For streams, pass the accumulated UTF-8 byte size
    through ``output_bytes`` instead of retaining the response body.
    """
    if not model or len(model) > 128:
        raise ValueError("model must contain 1–128 characters")
    if not endpoint or len(endpoint) > 128:
        raise ValueError("endpoint must contain 1–128 characters")
    if kind not in {"chat", "image"}:
        raise ValueError("kind must be chat or image")
    if status not in {"success", "error", "partial"}:
        raise ValueError("status must be success, error, or partial")

    input_bytes = len((input_text or "").encode("utf-8", errors="replace"))
    input_tokens = estimate_tokens_from_bytes(input_bytes)
    if output_bytes is not None:
        output_size = max(0, int(output_bytes))
    elif output_text is not None:
        output_size = len(output_text.encode("utf-8", errors="replace"))
    else:
        output_size = 0
    output_tokens = (
        estimate_tokens_from_bytes(output_size)
        if output_text is not None or output_bytes is not None
        else None
    )
    image_count = max(0, int(image_count))

    with _LOCK, _connect() as conn:
        cost: Optional[float] = None
        rate = _rates_map(conn).get(model)
        if kind == "chat" and output_tokens is not None and rate is not None:
            input_rate, output_rate = rate
            cost = (
                input_tokens * input_rate + output_tokens * output_rate
            ) / 1_000_000

        cursor = conn.execute(
            """
            INSERT INTO usage_events (
                timestamp, model, endpoint, kind, input_tokens, output_tokens,
                input_bytes, output_bytes, image_count, estimated_cost_usd, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                model,
                endpoint,
                kind,
                input_tokens,
                output_tokens,
                input_bytes,
                output_size,
                image_count,
                cost,
                status,
            ),
        )
        row = conn.execute(
            "SELECT * FROM usage_events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


class UsageMeter:
    """Accumulate approximate output size for a streaming request."""

    def __init__(
        self,
        *,
        model: str,
        endpoint: str,
        input_text: str,
        kind: str = "chat",
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.input_text = input_text
        self.kind = kind
        self.output_bytes = 0
        self._recorded = False

    def add_output(self, text: str) -> None:
        self.output_bytes += len(
            (text or "").encode("utf-8", errors="replace")
        )

    def finish(self, status: str = "success") -> Optional[dict]:
        if self._recorded:
            return None
        self._recorded = True
        return record_usage(
            model=self.model,
            endpoint=self.endpoint,
            input_text=self.input_text,
            output_text=None if self.kind == "image" else "",
            output_bytes=None if self.kind == "image" else self.output_bytes,
            kind=self.kind,
            status=status,
        )


def get_rates() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT model, input_usd_per_million, output_usd_per_million, updated_at
            FROM usage_rates ORDER BY model
            """
        ).fetchall()
    return [dict(row) for row in rows]


def save_rates(rates: list[dict]) -> list[dict]:
    """Replace reference rates. New rates apply to future usage events only."""
    if len(rates) > 128:
        raise ValueError("at most 128 model rates can be saved")

    normalized: list[tuple[str, float, float, float]] = []
    seen: set[str] = set()
    for rate in rates:
        model = str(rate.get("model") or "").strip()
        if not model or len(model) > 128:
            raise ValueError("each rate needs a model id of 1–128 characters")
        if model in seen:
            raise ValueError(f"duplicate rate for model: {model}")
        seen.add(model)

        values = []
        for field in ("input_usd_per_million", "output_usd_per_million"):
            value = float(rate[field])
            if not math.isfinite(value) or value < 0 or value > 1_000_000:
                raise ValueError(f"{field} must be between 0 and 1000000")
            values.append(value)
        normalized.append((model, values[0], values[1], time.time()))

    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM usage_rates")
        conn.executemany(
            """
            INSERT INTO usage_rates (
                model, input_usd_per_million, output_usd_per_million, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            normalized,
        )
    return get_rates()


def query_usage(
    *,
    period: str = "7d",
    model: Optional[str] = None,
    limit: int = 200,
) -> dict:
    if period not in {*_RANGES, "all"}:
        raise ValueError("range must be 24h, 7d, 30d, or all")
    limit = max(1, min(int(limit), 500))
    now = time.time()
    cutoff = now - _RANGES[period] if period in _RANGES else None
    clauses: list[str] = []
    params: list[object] = []
    if cutoff is not None:
        clauses.append("timestamp >= ?")
        params.append(cutoff)
    if model:
        clauses.append("model = ?")
        params.append(model)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    with _LOCK, _connect() as conn:
        summary_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS request_count,
                SUM(CASE WHEN kind = 'chat' THEN 1 ELSE 0 END) AS chat_requests,
                SUM(image_count) AS image_requests,
                SUM(input_tokens) AS input_tokens,
                SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                SUM(estimated_cost_usd) AS estimated_cost_usd,
                SUM(CASE WHEN kind = 'chat' AND estimated_cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced_requests,
                SUM(CASE WHEN kind = 'chat' AND estimated_cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_requests
            FROM usage_events{where}
            """,
            params,
        ).fetchone()
        daily_rows = conn.execute(
            f"""
            SELECT
                strftime('%Y-%m-%d', timestamp, 'unixepoch') AS day,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                SUM(estimated_cost_usd) AS estimated_cost_usd,
                SUM(CASE WHEN estimated_cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced_requests
            FROM usage_events{where}
            GROUP BY day ORDER BY day DESC
            """,
            params,
        ).fetchall()
        recent_rows = conn.execute(
            f"""
            SELECT id, timestamp, model, endpoint, kind, input_tokens, output_tokens,
                   estimated_cost_usd, status, image_count
            FROM usage_events{where}
            ORDER BY timestamp DESC LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        model_rows = conn.execute(
            "SELECT DISTINCT model FROM usage_events ORDER BY model"
        ).fetchall()
        rate_rows = conn.execute(
            """
            SELECT model, input_usd_per_million, output_usd_per_million, updated_at
            FROM usage_rates ORDER BY model
            """
        ).fetchall()

    keys = (
        "request_count", "chat_requests", "image_requests", "input_tokens",
        "output_tokens", "priced_requests", "unpriced_requests",
    )
    summary = {key: int(summary_row[key] or 0) for key in keys}
    summary["total_tokens"] = summary["input_tokens"] + summary["output_tokens"]
    summary["estimated_cost_usd"] = summary_row["estimated_cost_usd"]
    return {
        "range": period,
        "summary": summary,
        "daily": [dict(row) for row in daily_rows],
        "recent": [dict(row) for row in recent_rows],
        "models": [row["model"] for row in model_rows],
        "rates": [dict(row) for row in rate_rows],
    }


_initialize()

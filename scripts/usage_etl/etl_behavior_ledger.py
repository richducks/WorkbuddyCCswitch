#!/usr/bin/env python3
"""Incrementally update the usage ledger and publish its aggregate JSON snapshot.

CC Switch is optional; its read-only proxy log is counted in USD. WorkBuddy
gateway requests are counted in credits. Neither source is modified.
"""
import ast
import datetime
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

import usage_charts

REPO = Path(__file__).resolve().parents[2]
BJ = datetime.timezone(datetime.timedelta(hours=8))
TZ = "'unixepoch', '+8 hours'"
INSERT = """INSERT OR IGNORE INTO requests (
    request_id, created_ts, model, input_tokens, output_tokens, cache_read_tokens,
    total_cost_usd, latency_ms, first_token_ms, status_code, error_message,
    session_id, chain, cost_credit, resolved_model
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def init_ledger(db):
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT, created_ts INTEGER, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        total_cost_usd REAL, latency_ms INTEGER, first_token_ms INTEGER,
        status_code INTEGER, error_message TEXT, session_id TEXT,
        PRIMARY KEY (request_id, created_ts))""")
    cols = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
    for name, definition in (("chain", "TEXT NOT NULL DEFAULT 'codex'"),
                             ("cost_credit", "REAL"), ("resolved_model", "TEXT")):
        if name not in cols:
            db.execute(f"ALTER TABLE requests ADD COLUMN {name} {definition}")
    db.execute("DROP VIEW IF EXISTS v_daily")
    db.execute(f"""CREATE VIEW v_daily AS
        SELECT date(created_ts, {TZ}) AS day, COUNT(*) AS requests,
            SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok,
            SUM(cache_read_tokens) AS cache_tok, ROUND(SUM(total_cost_usd), 4) AS cost_usd,
            ROUND(SUM(cost_credit), 4) AS cost_credit, SUM(status_code >= 400) AS errors
        FROM requests GROUP BY day""")
    db.execute("DROP VIEW IF EXISTS v_hourly")
    db.execute(f"""CREATE VIEW v_hourly AS
        SELECT strftime('%H', created_ts, {TZ}) AS hour, COUNT(*) AS requests,
            ROUND(SUM(total_cost_usd), 4) AS cost_usd, ROUND(SUM(cost_credit), 4) AS cost_credit
        FROM requests GROUP BY hour""")


def import_codex(db, source):
    if not source:
        return 0
    checkpoint = db.execute("SELECT v FROM meta WHERE k='last_created_ts'").fetchone()
    last_ts = int(checkpoint[0]) if checkpoint else 0
    uri = Path(source).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as src:
        rows = src.execute(
            """SELECT request_id, created_at, model, input_tokens, output_tokens,
                      cache_read_tokens, total_cost_usd, latency_ms, first_token_ms,
                      status_code, error_message, session_id
               FROM proxy_request_logs WHERE created_at >= ? ORDER BY created_at""",
            (last_ts,),
        ).fetchall()
    inserted = db.executemany(INSERT, ((*row, "codex", None, None) for row in rows)).rowcount
    if rows:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_created_ts', ?)", (str(rows[-1][1]),))
    return inserted


def model_id(value):
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            value = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            return "unknown"
    if isinstance(value, dict):
        value = value.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def gateway_rows(path):
    """Parse gateway stats into (rows, error).

    A source with any invalid entry cannot be trusted to be complete, so the
    first problem is returned as the reason and the caller rejects the whole
    batch instead of silently ingesting a prefix.
    """
    try:
        with Path(path).open(encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:  # A new gateway has not handled a request yet.
        return [], None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [], f"无法读取网关用量文件：{exc}"
    if not isinstance(document, dict):
        return [], "网关用量文件格式不正确"
    requests = document.get("requests")
    if requests is None:
        return [], None
    if not isinstance(requests, list):
        return [], "网关用量请求列表格式不正确"
    seen = Counter()
    rows = []
    for index, row in enumerate(requests, 1):
        if not isinstance(row, dict):
            return [], f"网关用量第 {index} 条记录格式不正确"
        try:
            at_ms = int(row.get("at") or 0)
            status = int(row.get("status") or 0)
        except (TypeError, ValueError):
            at_ms, status = 0, 0
        account = row.get("account")
        if not isinstance(account, str) or not account or not 0 < at_ms < 253402300800000:
            return [], f"网关用量第 {index} 条记录缺少账号或有效时间"
        error = f"HTTP {status}" if status >= 400 else None
        if error is None and not row.get("complete", True):
            error = "incomplete"
        resolved = row.get("resolvedModel")
        resolved = resolved.strip() if isinstance(resolved, str) else ""
        # Preserve the old ID for the first request; don't merge later requests in the same millisecond.
        key = f"gw:{account[:16]}:{at_ms}"
        seen[key] += 1
        request_id = key if seen[key] == 1 else f"{key}:{seen[key]}"
        rows.append((request_id, at_ms // 1000, model_id(row.get("model")),
                     row.get("inputTokens") or 0, row.get("outputTokens") or 0, row.get("cachedTokens") or 0,
                     None, round(row.get("latencyMs") or 0), round(row.get("ttftMs") or 0),
                     status, error, None, "gateway", row.get("credit") or 0, resolved or None))
    return rows, None


def report(db, codex_new, gateway_new, stats, snapshot, gateway_error=None):
    gateway_total, gateway_credit = db.execute(
        "SELECT COUNT(*), ROUND(SUM(cost_credit), 1) FROM requests WHERE chain='gateway'"
    ).fetchone()
    requests, cost, cache_rate = db.execute("""SELECT COUNT(*), ROUND(SUM(total_cost_usd), 2),
        ROUND(100.0 * SUM(cache_read_tokens) /
            NULLIF(COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(input_tokens), 0), 0), 1)
        FROM requests""").fetchone()
    today = datetime.datetime.now(BJ).date()
    today_stats = db.execute(
        "SELECT requests, cost_usd, cost_credit FROM v_daily WHERE day=?", (today.isoformat(),)
    ).fetchone() or (0, None, None)
    week = db.execute("""SELECT COALESCE(SUM(requests), 0), ROUND(SUM(cost_usd), 2),
        ROUND(SUM(cost_credit), 2) FROM v_daily WHERE day BETWEEN ? AND ?""",
        ((today - datetime.timedelta(days=6)).isoformat(), today.isoformat())).fetchone()
    peak = db.execute("SELECT hour, requests FROM v_hourly ORDER BY requests DESC, hour LIMIT 1").fetchone()
    return {
        "new_rows": codex_new, "gateway_new_rows": gateway_new, "gateway_total": gateway_total,
        "total_requests": requests, "total_cost_usd": cost, "gateway_credit": gateway_credit,
        "cache_hit_rate_pct": cache_rate,
        "today": dict(zip(("requests", "cost_usd", "cost_credit"), today_stats)),
        "last_7d": dict(zip(("requests", "cost_usd", "cost_credit"), week)),
        "peak_hour": f"{peak[0]}点 ({peak[1]}次)" if peak else None,
        "gateway_source": str(stats), "gateway_error": gateway_error,
        "snapshot_path": str(snapshot), "snapshot_error": None,
    }


def main():
    default_source = Path.home() / ".cc-switch" / "cc-switch.db"
    source = os.environ.get("CC_SWITCH_DB", str(default_source) if default_source.is_file() else "")
    stats = os.environ.get("GW_STATS", str(REPO / "data/gateway/stats.json"))
    ledger = Path(os.environ.get("BEHAVIOR_LEDGER_DB", Path.home() / ".hermes/behavior_ledger.db"))
    snapshot = os.environ.get("WB_USAGE_SNAPSHOT", str(REPO / "data/web/usage_snapshot.json"))
    os.umask(0o077)
    with closing(sqlite3.connect(ledger)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")  # Serialize migration, imports and snapshot construction.
            init_ledger(db)
            codex_new = import_codex(db, source)
            db.commit()  # The codex chain lands even when the gateway source is unusable.
            db.execute("BEGIN IMMEDIATE")
            rows, gateway_error = gateway_rows(stats)
            gateway_new = 0
            if gateway_error is None:
                gateway_new = db.executemany(INSERT, rows).rowcount if rows else 0
                for (bad,) in db.execute("SELECT DISTINCT model FROM requests WHERE chain='gateway' AND model LIKE '{%'"):
                    db.execute("UPDATE requests SET model=? WHERE chain='gateway' AND model=?", (model_id(bad), bad))
                db.commit()
            else:
                # A batch with an invalid entry is rejected wholesale; no snapshot is published.
                db.rollback()
            summary = report(db, codex_new, gateway_new, stats, snapshot, gateway_error)
            payload = usage_charts.build_payload(usage_charts.load(db))
        except BaseException:
            db.rollback()
            raise

    if gateway_error is not None:
        print(f"用量刷新失败：{gateway_error}", file=sys.stderr)
        sys.exit(1)
    usage_charts.write_json(snapshot, payload)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"用量刷新失败：{exc}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""behavior_ledger ETL: 把两条用量链的原始流水增汇到 behavior_ledger.db。

本文件是唯一维护副本 (仓库 scripts/usage_etl/)，宿主机 ~/.hermes/scripts/ 下的同名文件
只是转发 shim；控制台「刷新数据」按钮会在容器内对本脚本以环境变量重跑。

链路(两条互不相交的转发路径, 无重复计数):
- codex 链:   ~/.cc-switch/cc-switch.db proxy_request_logs (Codex CLI → CC Switch 代理), 成本单位 = 美元
- gateway 链: WorkbuddyCCswitch/data/gateway/stats.json (各客户端 → 本地网关 8317 /v1), 成本单位 = credits 点数

设计原则:
- 幂等: 主键去重 + INSERT OR IGNORE, 重跑不产生重复。
  codex 链用 last_created_ts 水位线增量; gateway 链每次全量重扫 stats.json
  (防网关侧截断历史/乱序, 万级记录重扫代价可忽略)。
- 原始流水为主: 指标全部用查询实时计算, 图表口径以后可随便改。
- 只读源库 (mode=ro), 不影响 CC Switch / 网关运行。
- 成本单位不混算: total_cost_usd 只存 codex 链, cost_credit 只存网关链。
- resolved_model (v3): 网关链新数据带 auto 实际解析到的上游模型名; codex 链与旧网关数据为 NULL, 模型维度展示时回退 model。
时区: 统一按 UTC+8 (Asia/Shanghai) 聚合。

用法: python3 etl_behavior_ledger.py  (stdout 输出一行 JSON 摘要)
汇总完成后自动刷新控制台用量快照 (WB_USAGE_SNAPSHOT, 可用环境变量改路径)。
"""
import ast
import datetime
import json
import os
import sqlite3
import sys

SRC = os.environ.get("CC_SWITCH_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))
GW_STATS = os.environ.get(
    "GW_STATS",
    os.path.expanduser("~/Documents/CodexProjects/WorkbuddyCCswitch/data/gateway/stats.json"),
)
DST = os.environ.get("BEHAVIOR_LEDGER_DB", os.path.expanduser("~/.hermes/behavior_ledger.db"))
TZ = "+8 hours"  # 本机固定 CST

src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst = sqlite3.connect(DST)
dst.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
dst.execute(
    """CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT, created_ts INTEGER, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        total_cost_usd REAL, latency_ms INTEGER, first_token_ms INTEGER,
        status_code INTEGER, error_message TEXT, session_id TEXT,
        PRIMARY KEY (request_id, created_ts))"""
)

# --- v2 迁移: 链路来源 + 网关点数列; v3: auto 实际解析到的上游模型 (已存在则跳过); 视图口径变了必须重建 ---
cols = {r[1] for r in dst.execute("PRAGMA table_info(requests)")}
if "chain" not in cols:
    dst.execute("ALTER TABLE requests ADD COLUMN chain TEXT NOT NULL DEFAULT 'codex'")
if "cost_credit" not in cols:
    dst.execute("ALTER TABLE requests ADD COLUMN cost_credit REAL")
if "resolved_model" not in cols:
    dst.execute("ALTER TABLE requests ADD COLUMN resolved_model TEXT")
for v in ("v_daily", "v_hourly"):
    dst.execute(f"DROP VIEW IF EXISTS {v}")
dst.execute(
    """CREATE VIEW v_daily AS
       SELECT date(created_ts, 'unixepoch', '{tz}') AS day, COUNT(*) AS requests,
              SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok,
              SUM(cache_read_tokens) AS cache_tok, ROUND(SUM(total_cost_usd), 4) AS cost_usd,
              ROUND(SUM(cost_credit), 2) AS cost_credit,
              SUM(status_code >= 400) AS errors
       FROM requests GROUP BY day""".replace("{tz}", TZ)
)
dst.execute(
    """CREATE VIEW v_hourly AS
       SELECT strftime('%H', created_ts, 'unixepoch', '{tz}') AS hour, COUNT(*) AS requests,
              ROUND(SUM(total_cost_usd), 4) AS cost_usd,
              ROUND(SUM(cost_credit), 2) AS cost_credit
       FROM requests GROUP BY hour""".replace("{tz}", TZ)
)


def num(v, default=0):
    return default if v is None else v


# --- codex 链增量 ---
row = dst.execute("SELECT v FROM meta WHERE k='last_created_ts'").fetchone()
last_ts = int(row[0]) if row else 0

rows = src.execute(
    """SELECT request_id, created_at, model, input_tokens, output_tokens,
              cache_read_tokens, total_cost_usd, latency_ms, first_token_ms,
              status_code, error_message, session_id
       FROM proxy_request_logs WHERE created_at >= ? ORDER BY created_at""",
    (last_ts,),
).fetchall()

dst.executemany(
    """INSERT OR IGNORE INTO requests
       (request_id, created_ts, model, input_tokens, output_tokens, cache_read_tokens,
        total_cost_usd, latency_ms, first_token_ms, status_code, error_message,
        session_id, chain, cost_credit, resolved_model)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'codex',NULL,NULL)""", rows
)
if rows:
    dst.execute(
        "INSERT OR REPLACE INTO meta VALUES ('last_created_ts', ?)", (str(rows[-1][1]),)
    )

# --- gateway 链全量重扫 (主键去重, 幂等) ---
gw_new = 0
try:
    with open(GW_STATS, encoding="utf-8") as fh:
        stats = json.load(fh)
    gw_rows = []
    for r in stats.get("requests", []):
        at_ms = int(r.get("at") or 0)
        status = int(num(r.get("status"), 0))
        err = None
        if status >= 400:
            err = f"HTTP {status}"
        elif not r.get("complete", True):
            err = "incomplete"
        model = r.get("model")
        if isinstance(model, dict):          # 客户端把 model 当对象传 (含 id/context_length 配置)
            model = model.get("id") or "unknown"
        elif isinstance(model, str) and model.strip().startswith(("{",)):  # 网关把配置对象 repr 成字符串落盘
            try:
                model = ast.literal_eval(model).get("id") or "unknown"
            except (ValueError, SyntaxError):
                pass
        resolved = r.get("resolvedModel")
        if not isinstance(resolved, str) or not resolved.strip():
            resolved = None  # 旧网关数据无此字段
        gw_rows.append((
            f"gw:{r.get('account', '')[:16]}:{at_ms}",   # 网关流水无请求 id, 账号+毫秒时间戳作自然键
            at_ms // 1000,
            model,
            num(r.get("inputTokens")), num(r.get("outputTokens")), num(r.get("cachedTokens")),
            None,                                        # 网关计价是 credits 点数, 不折算美元
            round(num(r.get("latencyMs"))),
            round(num(r.get("ttftMs"))),
            status,
            err,
            None,                                        # session_id
            num(r.get("credit")),
            resolved,
        ))
    cur = dst.total_changes
    dst.executemany(
        """INSERT OR IGNORE INTO requests
           (request_id, created_ts, model, input_tokens, output_tokens, cache_read_tokens,
            total_cost_usd, latency_ms, first_token_ms, status_code, error_message,
            session_id, chain, cost_credit, resolved_model)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'gateway',?,?)""", gw_rows
    )
    gw_new = dst.total_changes - cur  # executemany 不返回行数, 用计数器差值统计新增
    # 回填历史已入库的配置串模型名 (与上面的规范化同一规则)
    fixed = 0
    for (bad,) in dst.execute(
            "SELECT DISTINCT model FROM requests WHERE chain='gateway' AND model LIKE '{%'").fetchall():
        try:
            good = ast.literal_eval(bad).get("id") or "unknown"
        except (ValueError, SyntaxError):
            continue
        fixed += dst.execute("UPDATE requests SET model=? WHERE chain='gateway' AND model=?",
                             (good, bad)).rowcount
except Exception as exc:  # noqa: BLE001 — 网关流水缺失不应阻塞 codex 链入库
    GW_ERROR = f"{type(exc).__name__}: {exc}"
    gw_new = 0
else:
    GW_ERROR = None

dst.commit()
gw_total = dst.execute("SELECT COUNT(*) FROM requests WHERE chain='gateway'").fetchone()[0]

total = dst.execute("SELECT COUNT(*), ROUND(SUM(total_cost_usd),2) FROM requests").fetchone()
gw_credit = dst.execute("SELECT ROUND(SUM(cost_credit),1) FROM requests WHERE chain='gateway'").fetchone()[0]
cache_rate = dst.execute(
    "SELECT ROUND(100.0*SUM(cache_read_tokens)/(SUM(cache_read_tokens)+SUM(input_tokens)+1),1) FROM requests"
).fetchone()[0]
today = datetime.date.today().isoformat()
today_stats = dst.execute(
    "SELECT requests, cost_usd, cost_credit FROM v_daily WHERE day=?", (today,)
).fetchone() or (0, None, None)
hourly = dst.execute(
    "SELECT hour, requests, cost_usd, cost_credit FROM v_hourly ORDER BY requests DESC LIMIT 1"
).fetchone()
week = dst.execute(
    """SELECT COUNT(*), ROUND(SUM(cost_usd),2), ROUND(SUM(cost_credit),2) FROM v_daily
       WHERE day >= date('now','-6 day','localtime')"""
).fetchone() or (0, None, None)

# 控制台「用量复盘」快照: 口径集中在 usage_charts.py, 这里只负责触发刷新。
# 失败不回滚 ETL 本身, 错误随摘要输出便于 cron 日志排查。
SNAPSHOT = os.environ.get(
    "WB_USAGE_SNAPSHOT",
    "/home/sducks/Documents/CodexProjects/WorkbuddyCCswitch/data/web/usage_snapshot.json",
)
snapshot_error = None
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import usage_charts

    ledger = sqlite3.connect(f"file:{DST}?mode=ro", uri=True)
    try:
        usage_charts.write_json(SNAPSHOT, usage_charts.build_payload(usage_charts.load(ledger)))
    finally:
        ledger.close()
except Exception as exc:  # noqa: BLE001 — 快照失败不应让每日 ETL 报错退出
    snapshot_error = f"{type(exc).__name__}: {exc}"

print(json.dumps({
    "new_rows": len(rows), "gateway_new_rows": gw_new, "gateway_total": gw_total,
    "total_requests": total[0], "total_cost_usd": total[1], "gateway_credit": gw_credit,
    "cache_hit_rate_pct": cache_rate,
    "today": {"requests": today_stats[0], "cost_usd": today_stats[1], "cost_credit": today_stats[2]},
    "last_7d": {"requests": week[0], "cost_usd": week[1], "cost_credit": week[2]},
    "peak_hour": (f"{hourly[0]}点 ({hourly[1]}次)" if hourly else None),
    "gateway_source": GW_STATS, "gateway_error": GW_ERROR,
    "snapshot_path": SNAPSHOT, "snapshot_error": snapshot_error,
}, ensure_ascii=False))

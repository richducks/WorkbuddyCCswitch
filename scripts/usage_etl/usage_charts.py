#!/usr/bin/env python3
"""behavior_ledger 用量看板: 从 ~/.hermes/behavior_ledger.db 生成自包含 HTML 图表。

v2 双链口径 (两条转发链, 成本单位不同, 不混算):
- codex 链 (WorkBuddy CLI → CC Switch 代理): 成本 = total_cost_usd (美元)
- gateway 链 (各客户端 → 本地网关 8317 /v1): 成本 = cost_credit (credits 点数)
请求次数 / token / 缓存命中率 / 时段模式 = 两链合并; 成本一律按链分带展示。

设计原则:
- 只读账本 (mode=ro), 指标口径全部在本脚本内, 不改视图、不回写底账。
- 输出零依赖 HTML (内联 JS+SVG, 无 CDN), 双击可开, 也可进 Hermes ::preview。
- 所有时区统一 UTC+8, 与 ETL 口径一致 (date/strftime + 'unixepoch' + '+8 hours')。

用法: python3 usage_charts.py [--db PATH] [--out PATH] [--json PATH]  (stdout 输出一行 JSON 摘要)
输出: ~/.hermes/reports/usage_dashboard.html
"""
import argparse
import datetime
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

DB = os.path.expanduser("~/.hermes/behavior_ledger.db")
OUT = os.path.expanduser("~/.hermes/reports/usage_dashboard.html")
TZ = "'unixepoch', '+8 hours'"
BJ = datetime.timezone(datetime.timedelta(hours=8))

WD_ZH = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 0: "周日"}


def q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def load(db):
    tz = TZ
    # KPI 总览 (两链合并, 成本按链拆开)
    (n_req, cost, credit, in_tok, out_tok, cache_tok, max_ts) = q(db, f"""
        SELECT COUNT(*), ROUND(SUM(total_cost_usd), 4), ROUND(SUM(cost_credit), 4),
               SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),
               MAX(created_ts) FROM requests""")[0]
    (codex_req, gw_req) = q(db, """
        SELECT SUM(chain='codex'), SUM(chain='gateway') FROM requests""")[0]
    # 模型构成按链分组: cost 字段按链取对应单位 (codex=美元, gateway=点数)
    # 模型名取 resolved_model 优先 (auto 实际解析到的上游模型, 网关链新数据), 缺失回退请求模型
    models = []
    for chain, model, req, usd, credit_ in q(db, f"""
            SELECT chain, COALESCE(NULLIF(resolved_model, ''), model) AS model, COUNT(*),
                   COALESCE(ROUND(SUM(total_cost_usd), 2), 0),
                   COALESCE(ROUND(SUM(cost_credit), 1), 0)
            FROM requests GROUP BY chain, COALESCE(NULLIF(resolved_model, ''), model)"""):
        models.append({"model": model, "chain": chain, "req": req,
                       "cost": usd if chain == "codex" else credit_})

    # 按天 × 链 × 模型 (成本构成): 模型名同 KPI, resolved_model 优先
    dm = q(db, f"""
        SELECT date(created_ts, {tz}) AS day, chain,
               COALESCE(NULLIF(resolved_model, ''), model) AS model, COUNT(*) AS req,
               COALESCE(SUM(total_cost_usd), 0) AS usd,
               COALESCE(SUM(cost_credit), 0) AS credit
        FROM requests GROUP BY day, chain, COALESCE(NULLIF(resolved_model, ''), model) ORDER BY day""")
    # 按天 token (缓存命中率, 两链合并)
    dt = q(db, f"""
        SELECT date(created_ts, {tz}) AS day,
               COALESCE(SUM(input_tokens), 0) AS in_tok, COALESCE(SUM(cache_read_tokens), 0) AS cache_tok
        FROM requests GROUP BY day ORDER BY day""")

    # 时段 (0-23) 全程汇总: (请求数, 美元, 点数)
    hourly = {r[0]: (r[1], r[2], r[3]) for r in q(db, f"""
        SELECT strftime('%H', created_ts, {tz}) AS hour, COUNT(*),
               COALESCE(ROUND(SUM(total_cost_usd), 4), 0),
               COALESCE(ROUND(SUM(cost_credit), 4), 0)
        FROM requests GROUP BY hour""")}

    # 星期 × 时段 矩阵: (请求数, 美元, 点数) — 热度按请求数 (单位无关)
    wh = {}
    for wd, hh, req, usd, credit_ in q(db, f"""
            SELECT strftime('%w', created_ts, {tz}) AS wd,
                   strftime('%H', created_ts, {tz}) AS hh, COUNT(*),
                   COALESCE(SUM(total_cost_usd), 0),
                   COALESCE(SUM(cost_credit), 0)
            FROM requests GROUP BY wd, hh"""):
        wh[(int(wd), int(hh))] = (req, usd, credit_)

    # 按天 × 链 × 时段: 控制台「点击日期柱 → 当天分时段明细」的数据源
    day_hour = {}
    for d, chain, hh, req, usd, credit_ in q(db, f"""
            SELECT date(created_ts, {tz}) AS day, chain,
                   strftime('%H', created_ts, {tz}) AS hh, COUNT(*),
                   COALESCE(ROUND(SUM(total_cost_usd), 4), 0),
                   COALESCE(ROUND(SUM(cost_credit), 4), 0)
            FROM requests GROUP BY day, chain, hh"""):
        day_hour.setdefault(d, {"codex": [], "gateway": []})[chain].append(
            (int(hh), req, usd, credit_))

    return {
        "kpi": {"requests": n_req, "codex_requests": codex_req or 0,
                "gateway_requests": gw_req or 0, "cost": cost, "credit": credit,
                "in_tok": in_tok, "out_tok": out_tok, "cache_tok": cache_tok,
                "max_ts": max_ts, "models": models},
        "day_model": [dict(day=d, chain=c, model=m, req=r, usd=u, credit=cr)
                      for d, c, m, r, u, cr in dm],
        "day_hour": day_hour,
        "day_tok": [dict(day=d, in_tok=i, cache_tok=c) for d, i, c in dt],
        "hourly": hourly,
        "wh": {f"{wd},{hh:02d}": v for (wd, hh), v in wh.items()},
    }


def build_payload(raw):
    """补零成连续序列, 预计算均线/命中率, 输出图表直接可用的 JSON。

    days[i]: cost=WorkBuddy链美元, credit=网关链点数, parts/credit_parts 各自按本单位排序。
    wh rows: [请求数24, 美元24, 点数24] — 热度用请求数 (两链成本单位不同, 不能混着上色)。
    """
    kpi = dict(raw["kpi"])
    max_dt = datetime.datetime.fromtimestamp(kpi["max_ts"], BJ) if kpi["max_ts"] is not None else None
    last_day = max_dt.date() if max_dt else None

    # --- 连续日期轴 (缺数据日补零) ---
    dm = {}
    for r in raw["day_model"]:
        dm.setdefault(r["day"], []).append(r)
    tok = {r["day"]: r for r in raw["day_tok"]}
    days = []
    d = datetime.date.fromisoformat(min(dm)) if dm else None
    while d is not None and d <= last_day:
        ds = d.isoformat()
        rows = dm.get(ds, [])
        parts = sorted((r for r in rows if r["chain"] == "codex"), key=lambda r: -r["usd"])
        credit_parts = sorted((r for r in rows if r["chain"] == "gateway"), key=lambda r: -r["credit"])
        cost = round(sum(r["usd"] for r in parts), 4)
        credit = round(sum(r["credit"] for r in credit_parts), 4)
        req = sum(r["req"] for r in rows)
        t = tok.get(ds)
        cache_rate = round(100.0 * t["cache_tok"] / (t["cache_tok"] + t["in_tok"]), 1) if t and (t["cache_tok"] + t["in_tok"]) > 0 else None
        days.append({"day": ds, "label": ds[5:], "cost": cost, "credit": credit, "req": req,
                     "parts": [{"model": r["model"], "cost": round(r["usd"], 4)} for r in parts],
                     "credit_parts": [{"model": r["model"], "credit": round(r["credit"], 4)} for r in credit_parts],
                     "cache_rate": cache_rate,
                     "partial": bool(d == last_day and max_dt.hour < 23)})
        d += datetime.timedelta(days=1)

    # 7 日移动平均 (窗口不足 7 时用现有天数) — 美元 ma7 / 点数 ma7g
    for key, out in (("cost", "ma7"), ("credit", "ma7g")):
        for i, e in enumerate(days):
            win = [x[key] for x in days[max(0, i - 6):i + 1]]
            e[out] = round(sum(win) / len(win), 4)

    # --- 时段 0-23 补零 ---
    hourly = [{"hour": h, "req": raw["hourly"].get(f"{h:02d}", (0, 0, 0))[0],
               "cost": raw["hourly"].get(f"{h:02d}", (0, 0, 0))[1],
               "credit": raw["hourly"].get(f"{h:02d}", (0, 0, 0))[2]} for h in range(24)]

    # --- 星期×时段: 行=周一..周日, 列=0..23, rows=[请求数, 美元, 点数] ---
    order = [1, 2, 3, 4, 5, 6, 0]
    wh = [{"wd": WD_ZH[wd],
           "rows": [[raw["wh"].get(f"{wd},{hh:02d}", (0, 0, 0))[i] for hh in range(24)]
                    for i in range(3)]}
          for wd in order]

    # --- 按日明细: 控制台「点击日期柱」穿透用 (分时段 + 按模型, 按链分开, 单位互不混算) ---
    # hours[chain] = [[时, 成本(本单位), 次数], ...] 仅含有时段有数据的项 (稀疏, 省体积)
    # models[chain] = [[模型, 成本(本单位), 次数], ...] 按本单位成本降序
    day_detail = {}
    for ds, rows in dm.items():
        det = {"hours": {}, "models": {}}
        for chain, idx in (("codex", 2), ("gateway", 3)):
            hrs = sorted(raw["day_hour"].get(ds, {}).get(chain, []))
            if hrs:
                # 元组 = (时, 次数, 美元, 点数); 只保留有量的小时 (稀疏)
                det["hours"][chain] = [[r[0], round(r[idx], 4), r[1]] for r in hrs]
            mrows = sorted((r for r in rows if r["chain"] == chain),
                           key=lambda r: -(r["usd"] if chain == "codex" else r["credit"]))
            if mrows:
                det["models"][chain] = [[r["model"],
                                         round(r["usd"] if chain == "codex" else r["credit"], 4),
                                         r["req"]] for r in mrows]
        if det["hours"] or det["models"]:
            day_detail[ds] = det

    tot_cache = kpi["cache_tok"] or 0
    tot_in = kpi["in_tok"] or 0
    kpi["cache_rate"] = round(100.0 * tot_cache / (tot_cache + tot_in), 1) if (tot_cache + tot_in) else None
    kpi["max_dt"] = max_dt.strftime("%Y-%m-%d %H:%M") if max_dt else None
    kpi["range"] = f"{days[0]['day']} ~ {days[-1]['day']}" if days else ""
    return {"kpi": kpi, "days": days, "hourly": hourly, "wh": wh,
            "day_detail": day_detail,
            "generated": datetime.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")}


HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy 用量看板</title>
<style>
  :root { --c1:#4e79a7; --c2:#f28e2b; --c3:#59a14f; --c4:#b07aa1; --c5:#9c755f; }
  body { margin:0; color:var(--foreground); }
  .kpis { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
  .kpi { flex:1 1 120px; background:var(--card); border:1px solid var(--border); border-radius:8px; padding:10px 14px; }
  .kpi .v { font-size:20px; font-weight:600; }
  .kpi .k { font-size:11px; color:var(--muted-foreground); margin-top:2px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:12px 14px 8px; margin-bottom:14px; }
  .card h3 { margin:0 0 2px; font-size:14px; }
  .sub { font-size:11px; color:var(--muted-foreground); margin-bottom:6px; }
  svg { display:block; width:100%; }
  .legend { display:flex; gap:12px; flex-wrap:wrap; font-size:11px; color:var(--muted-foreground); margin:2px 0 4px; }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px; }
  #tip { position:fixed; display:none; z-index:9; pointer-events:none; background:var(--card);
         border:1px solid var(--border); border-radius:6px; padding:6px 9px; font-size:12px;
         color:var(--foreground); box-shadow:0 2px 10px rgba(0,0,0,.18); max-width:260px; }
  .meta { font-size:11px; color:var(--muted-foreground); padding:2px 2px 8px; line-height:1.6; }
  code { font-size:11px; }
</style>
</head>
<body>
<div class="kpis" id="kpis"></div>
<div class="card"><h3>每日成本与请求量</h3><div class="sub">上带 = WorkBuddy 链成本($) 柱 + 虚线7日均线 + 橙线请求次数 · 下带 = 网关链用量(点数) 柱</div><div id="chDaily"></div><div id="chDailyG"></div></div>
<div class="card"><h3>每日成本按模型构成</h3><div class="sub" id="modelSub"></div><div class="legend" id="modelLegend"></div><div id="chModel"></div><div id="chModelG"></div></div>
<div class="card"><h3>时段分布(全程汇总)</h3><div class="sub">上带 = WorkBuddy 链成本($) + 橙线请求次数 · 下带 = 网关链用量(点数)</div><div id="chHour"></div><div id="chHourG"></div></div>
<div class="card"><h3>星期 × 时段热力图</h3><div class="sub">颜色 = 请求次数热度(两链合计) · 悬停看次数与两种成本</div><div id="chHeat"></div></div>
<div class="card"><h3>缓存命中率(按天)</h3><div class="sub">缓存读 tokens ÷ (缓存读 + 未缓存输入), 两链合并 — 越高越省, 掉下去通常是长间隔后冷启动</div><div id="chCache"></div></div>
<div class="meta" id="meta"></div>
<div id="tip"></div>
<script>
const D = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const COLORS = ["var(--c1)","var(--c2)","var(--c3)","var(--c4)","var(--c5)"];
const MUTED = "var(--muted-foreground)", GRID = "var(--border)", FG = "var(--foreground)", ACC = "var(--accent)";
const $ = s => document.querySelector(s);
const fmtMoney = v => v == null || !v ? "-" : "$" + (v >= 100 ? v.toFixed(0) : v.toFixed(2));
const fmtCredit = v => v == null || !v ? "-" : (v >= 1000 ? v.toFixed(0) : v.toFixed(1)) + " 点";
const fmtInt = v => (v == null ? "-" : v.toLocaleString("en-US"));

function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
// tooltip
const tip = $("#tip");
function showTip(html, ev) { tip.innerHTML = html; tip.style.display = "block"; moveTip(ev); }
function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}
function moveTip(ev) {
  const pad = 12, r = tip.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { tip.style.display = "none"; }
function bindTip(e, html) {
  e.addEventListener("mousemove", ev => showTip(html, ev));
  e.addEventListener("mouseleave", hideTip);
}

// 通用坐标架: 返回 {svg, w, h, ml, mr, mt, mb, pw, ph}
function frame(sel, h, mr) {
  const box = $(sel); box.innerHTML = "";
  const w = Math.max(box.clientWidth, 320);
  const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, height: h }, box);
  const ml = 44, mt = 10, mb = 22;
  return { svg, w, h, ml, mr: mr || 10, mt, mb, pw: w - ml - (mr || 10), ph: h - mt - mb };
}
function yGrid(fr, y, vals, fmt, ml, pw) {
  vals.forEach(v => {
    const yy = y(v);
    el("line", { x1: ml, x2: ml + pw, y1: yy, y2: yy, stroke: GRID, "stroke-width": 1 }, fr.owner);
    el("text", { x: ml - 6, y: yy + 4, "text-anchor": "end", "font-size": 10.5, fill: MUTED }, fr.owner).textContent = fmt(v);
  });
}
// 柱带通用画法: 每天一根柱 + 可选均线/请求线, x 轴为 D.days
function drawDailyBand(sel, valKey, fmt, barColor, opts) {
  const days = D.days, H = 200;
  const f = frame(sel, H, opts.reqLine ? 44 : 10), { svg, ml, mr, mt, pw, ph } = f;
  const vals = days.map(d => d[valKey]);
  const maxV = niceMax(Math.max(...vals, 0.1));
  const yV = v => mt + ph - (v / maxV) * ph;
  f.owner = svg;
  yGrid(f, yV, Array.from({length: 5}, (_, i) => maxV * i / 4), fmt, ml, pw);
  if (opts.reqLine) {
    const maxR = niceMax(Math.max(...days.map(d => d.req), 1));
    const yR = v => mt + ph - (v / maxR) * ph;
    Array.from({length: 5}, (_, i) => maxR * i / 4).forEach(v =>
      el("text", { x: ml + pw + 6, y: yR(v) + 4, "font-size": 10.5, fill: MUTED }, svg).textContent = Math.round(v));
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
    el("path", { d: days.map((d, i) => `${i ? "L" : "M"}${ml + pw / days.length * i + pw / days.length / 2},${yR(d.req)}`).join(""),
      fill: "none", stroke: "var(--c2)", "stroke-width": 1.6 }, svg);
  } else {
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
  }
  const n = days.length, band = pw / n, bw = Math.max(band * 0.72, 1.5);
  const step = Math.max(1, Math.ceil(n / 10));
  days.forEach((d, i) => {
    const x = ml + band * i + (band - bw) / 2, y0 = mt + ph, v = d[valKey];
    if (v > 0) {
      const bar = el("rect", { x, y: yV(v), width: bw, height: Math.max(y0 - yV(v), 0.5),
        fill: barColor, rx: 1.5, "fill-opacity": d.partial ? 0.4 : 0.85 }, svg);
      bindTip(bar, `<b>${d.day}</b>${d.partial ? " · 数据不完整" : ""}<br>${opts.name} <b>${fmt(v)}</b>`);
    }
    if (opts.maKey && d[opts.maKey] > 0)
      el("circle", { cx: x + bw / 2, cy: yV(d[opts.maKey]), r: 0 }, svg);
    if (i % step === 0 || i === n - 1)
      el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
  });
  if (opts.maKey) {
    el("path", { d: days.map((d, i) => `${i ? "L" : "M"}${ml + band * i + band / 2},${yV(d[opts.maKey])}`).join(""),
      fill: "none", stroke: MUTED, "stroke-width": 1.5, "stroke-dasharray": "4 3" }, svg);
  }
  // 悬停列: 两链合计一并列出
  days.forEach((d, i) => {
    const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
    bindTip(hit, `<b>${d.day}</b>${d.partial ? " · 数据不完整" : ""}<br>WorkBuddy <b>${fmtMoney(d.cost)}</b> · 网关 <b>${fmtCredit(d.credit)}</b><br>${fmtInt(d.req)} 次`);
  });
}
// 堆叠柱带: 按 key 取每段的值
function drawStackBand(sel, partsKey, valKey, fmt, legendBox, chainName) {
  const days = D.days, H = 200;
  const names = D.kpi.models.filter(m => m.chain === chainName).map(m => m.model);
  const f = frame(sel, H), { svg, ml, mt, pw, ph } = f;
  const maxV = niceMax(Math.max(...days.map(d => d[valKey]), 0.1));
  const yV = v => mt + ph - (v / maxV) * ph;
  f.owner = svg;
  yGrid(f, yV, Array.from({length: 5}, (_, i) => maxV * i / 4), fmt, ml, pw);
  el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
  const n = days.length, band = pw / n, bw = Math.max(band * 0.72, 1.5);
  const step = Math.max(1, Math.ceil(n / 10));
  days.forEach((d, i) => {
    const x = ml + band * i + (band - bw) / 2;
    let acc = 0;
    const segs = (d[partsKey] || []).map((p, j) => ({ ...p, j })).filter(p => p[valKey] > 0);
    segs.forEach(p => {
      const y1 = yV(acc), y2 = yV(acc + p[valKey]);
      const r = el("rect", { x, y: y2, width: bw, height: Math.max(y1 - y2, 0.4),
        fill: COLORS[names.indexOf(p.model) >= 0 ? names.indexOf(p.model) : p.j % 5],
        rx: 0, "fill-opacity": d.partial ? 0.45 : 0.9 }, svg);
      bindTip(r, `<b>${d.day}</b><br>${escapeHtml(p.model)}: ${fmt(p[valKey])}`);
      acc += p[valKey];
    });
    if (acc > 0) {
      const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
      bindTip(hit, `<b>${d.day}</b>${d.partial ? " · 数据不完整" : ""}<br>合计 <b>${fmt(acc)}</b><br>` +
        segs.map(p => `${escapeHtml(p.model)} ${fmt(p[valKey])}`).join("<br>"));
    }
    if (i % step === 0 || i === n - 1)
      el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
  });
  if (legendBox) {
    const box = $(legendBox);
    if (box.dataset[chainName] !== "1") {
      box.insertAdjacentHTML("beforeend", names.map((m, i) =>
        `<span><i style="background:${COLORS[i % COLORS.length]}"></i>${escapeHtml(m)}</span>`).join(""));
      box.dataset[chainName] = "1";
    }
  }
}

// KPI
function renderKpis() {
  const k = D.kpi, box = $("#kpis");
  const items = [
    [fmtMoney(k.cost), "总成本 · WorkBuddy 链($)"],
    [fmtCredit(k.credit), "网关链消耗(点数)"],
    [fmtInt(k.requests), "请求次数(两链)"],
    [k.cache_rate == null ? "-" : k.cache_rate + "%", "缓存命中率(两链)"],
    [k.codex_requests ? fmtMoney(k.cost / k.codex_requests) : "-", "WorkBuddy 均价/请求"],
  ];
  box.innerHTML = items.map(([v, t]) => `<div class="kpi"><div class="v">${v}</div><div class="k">${t}</div></div>`).join("");
  const gwModels = k.models.filter(m => m.chain === "gateway").length;
  $("#modelSub").textContent = `上带 = WorkBuddy 链($) · 下带 = 网关链(点数, ${gwModels} 个模型)`;
  $("#meta").textContent = k.requests
    ? `数据范围 ${k.range} (UTC+8) · 截至 ${k.max_dt} · 生成于 ${D.generated} · 浅色日期的数据可能不完整`
    : "尚无用量记录。";
}

// 图1: 每日成本双带 + 请求线(上带右轴) + 两条 MA7 虚线
function renderDaily() {
  drawDailyBand("#chDaily", "cost", fmtMoney, ACC, { reqLine: true, maKey: "ma7", name: "WorkBuddy 链" });
  drawDailyBand("#chDailyG", "credit", fmtCredit, "var(--c4)", { reqLine: false, maKey: "ma7g", name: "网关链" });
}
// 图2: 模型堆叠双带
function renderModel() {
  drawStackBand("#chModel", "parts", "cost", fmtMoney, "#modelLegend", "codex");
  drawStackBand("#chModelG", "credit_parts", "credit", fmtCredit, "#modelLegend", "gateway");
}
// 图3: 时段分布双带
function renderHour() {
  const hs = D.hourly, H = 200;
  function band(sel, valKey, fmt, color, reqLine) {
    const f = frame(sel, H, reqLine ? 44 : 10), { svg, ml, mt, pw, ph } = f;
    const maxV = niceMax(Math.max(...hs.map(h => h[valKey]), 0.1));
    const yV = v => mt + ph - (v / maxV) * ph;
    f.owner = svg;
    yGrid(f, yV, Array.from({length: 5}, (_, i) => maxV * i / 4), fmt, ml, pw);
    if (reqLine) {
      const maxR = niceMax(Math.max(...hs.map(h => h.req), 1));
      const yR = v => mt + ph - (v / maxR) * ph;
      Array.from({length: 5}, (_, i) => maxR * i / 4).forEach(v =>
        el("text", { x: ml + pw + 6, y: yR(v) + 4, "font-size": 10.5, fill: MUTED }, svg).textContent = Math.round(v));
      el("path", { d: hs.map((h, i) => `${i ? "L" : "M"}${ml + pw / 24 * i + pw / 48},${yR(h.req)}`).join(""),
        fill: "none", stroke: "var(--c2)", "stroke-width": 1.6 }, svg);
    }
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
    const bw = pw / 24 * 0.66;
    hs.forEach((h, i) => {
      const x = ml + pw / 24 * i + (pw / 24 - bw) / 2;
      if (h[valKey] > 0) {
        const r = el("rect", { x, y: yV(h[valKey]), width: bw, height: mt + ph - yV(h[valKey]), fill: color, rx: 1.5, "fill-opacity": 0.85 }, svg);
        bindTip(r, `<b>${String(h.hour).padStart(2, "0")}:00–${String(h.hour).padStart(2, "0")}:59</b><br>WorkBuddy <b>${fmtMoney(h.cost)}</b> · 网关 <b>${fmtCredit(h.credit)}</b> · ${fmtInt(h.req)} 次`);
      }
      if (i % 2 === 0)
        el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = String(h.hour).padStart(2, "0");
    });
    hs.forEach((h, i) => {
      const hit = el("rect", { x: ml + pw / 24 * i, y: mt, width: pw / 24, height: ph, fill: "transparent" }, svg);
      bindTip(hit, `<b>${String(h.hour).padStart(2, "0")}:00 时段(全程)</b><br>WorkBuddy <b>${fmtMoney(h.cost)}</b> · 网关 <b>${fmtCredit(h.credit)}</b><br>${fmtInt(h.req)} 次`);
    });
  }
  band("#chHour", "cost", fmtMoney, ACC, true);
  band("#chHourG", "credit", fmtCredit, "var(--c4)", false);
}
// 图4: 星期×时段热力图 (颜色=请求数, 两链成本单位不同不能混着上色)
function renderHeat() {
  const rows = D.wh, H = 30 + rows.length * 26;
  const f = frame("#chHeat", H), { svg, ml, mt, pw, ph } = f;
  const cells = rows.flatMap(r => r.rows[0]);
  const maxV = Math.max(...cells, 0.01);
  const cw = pw / 24, ch = ph / rows.length;
  f.owner = svg;
  for (let h = 0; h < 24; h += 2)
    el("text", { x: ml + cw * h + cw / 2, y: mt - 2, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg)
      .textContent = String(h).padStart(2, "0");
  rows.forEach((r, ri) => {
    const y = mt + ch * ri;
    el("text", { x: ml - 8, y: y + ch / 2 + 4, "text-anchor": "end", "font-size": 11, fill: MUTED }, svg).textContent = r.wd;
    r.rows[0].forEach((v, hi) => {
      const t = v / maxV;
      const c = el("rect", { x: ml + cw * hi + 1, y: y + 1.5, width: cw - 2, height: ch - 3,
        fill: ACC, rx: 2.5, "fill-opacity": v > 0 ? 0.1 + 0.9 * t : 0.05,
        stroke: v > 0 ? "none" : GRID, "stroke-width": 0.5 }, svg);
      bindTip(c, `<b>${r.wd} ${String(hi).padStart(2, "0")}:00–${String(hi).padStart(2, "0")}:59</b><br><b>${fmtInt(v)}</b> 次<br>WorkBuddy ${fmtMoney(r.rows[1][hi])} · 网关 ${fmtCredit(r.rows[2][hi])}`);
    });
  });
  el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: GRID, "stroke-width": 1 }, svg);
}
// 图5: 缓存命中率 (两链合并)
function renderCache() {
  const days = D.days.filter(d => d.cache_rate != null), H = 200;
  const f = frame("#chCache", H), { svg, ml, mt, pw, ph } = f;
  const y = v => mt + ph - (v / 100) * ph;
  f.owner = svg;
  yGrid(f, y, [0, 25, 50, 75, 100], v => v + "%", ml, pw);
  el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
  const avg = D.kpi.cache_rate ?? 0;
  el("line", { x1: ml, x2: ml + pw, y1: y(avg), y2: y(avg), stroke: ACC, "stroke-width": 1, "stroke-dasharray": "5 3" }, svg);
  el("text", { x: ml + pw - 4, y: y(avg) - 4, "text-anchor": "end", "font-size": 10.5, fill: ACC }, svg).textContent = "全程 " + avg + "%";
  const n = days.length, band = pw / Math.max(n, 1);
  const step = Math.max(1, Math.ceil(n / 10));
  const path = days.map((d, i) => `${i ? "L" : "M"}${ml + band * i + band / 2},${y(d.cache_rate)}`).join("");
  el("path", { d: path, fill: "none", stroke: "var(--c3)", "stroke-width": 1.8 }, svg);
  days.forEach((d, i) => {
    const cx = ml + band * i + band / 2, cy = y(d.cache_rate);
    const dot = el("circle", { cx, cy, r: 3, fill: "var(--c3)", "fill-opacity": 0 }, svg);
    const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
    bindTip(hit, `<b>${d.day}</b><br>缓存命中率 <b>${d.cache_rate}%</b>`);
    dot.addEventListener("mouseenter", () => dot.setAttribute("fill-opacity", 1));
    dot.addEventListener("mouseleave", () => dot.setAttribute("fill-opacity", 0));
    if (i % step === 0 || i === n - 1)
      el("text", { x: cx, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
  });
}

function renderAll() { renderKpis(); renderDaily(); renderModel(); renderHour(); renderHeat(); renderCache(); }
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(renderAll, 150); });
renderAll();
</script>
</body>
</html>
"""


def write_json(path, payload):
    """原子写快照 JSON (控制台容器直接读该文件, 不能让它读到半截数据)。"""
    doc = dict(payload, summary=summarize(payload))
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".usage-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path


def summarize(p):
    """给 stdout 的复盘摘要, 供周报/会话引用。

    成本双单位: *_cost_usd / peak_hour_by_cost / top_cost_days / model_avg_cost_per_req
    只统计 WorkBuddy 链 (美元); 网关链用对应的 *_credit / *_by_credit 字段。
    """
    days = p["days"]
    k = p["kpi"]

    def win(a, b, key="cost"):
        seg = [d for d in days if a <= d["day"] <= b]
        return (round(sum(d[key] for d in seg), 2), sum(d["req"] for d in seg))

    last7 = prev7 = last7c = (0, 0)
    if days:
        last = datetime.date.fromisoformat(days[-1]["day"])
        prev_end = (last - datetime.timedelta(days=7)).isoformat()
        prev_start = (last - datetime.timedelta(days=13)).isoformat()
        last7 = win((last - datetime.timedelta(days=6)).isoformat(), days[-1]["day"])
        prev7 = win(prev_start, prev_end)
        last7c = win((last - datetime.timedelta(days=6)).isoformat(), days[-1]["day"], "credit")
    top_days = sorted(days, key=lambda d: -d["cost"])[:3]
    top_credit_days = sorted(days, key=lambda d: -d["credit"])[:3]
    peak_hour = max(p["hourly"], key=lambda h: h["cost"])
    peak_credit_hour = max(p["hourly"], key=lambda h: h["credit"])
    peak_req_hour = max(p["hourly"], key=lambda h: h["req"])
    cells = [(r["wd"], hi, r["rows"][0][hi], r["rows"][1][hi]) for r in p["wh"] for hi in range(24)]
    peak_cell = max(cells, key=lambda c: c[2])
    mreq = [{"model": m["model"], "avg_usd": round(m["cost"] / m["req"], 4) if m["req"] else None,
             "req": m["req"], "cost": m["cost"]}
            for m in k["models"] if m["chain"] == "codex"]
    mgw = [{"model": m["model"], "avg_credit": round(m["cost"] / m["req"], 4) if m["req"] else None,
            "req": m["req"], "credit": m["cost"]}
           for m in k["models"] if m["chain"] == "gateway"]
    return {
        "range": k["range"], "as_of": k["max_dt"],
        "total_cost_usd": k["cost"], "requests": k["requests"],
        "codex_requests": k["codex_requests"], "gateway_requests": k["gateway_requests"],
        "total_cost_credit": k["credit"],
        "cache_rate_pct": k["cache_rate"],
        "last7_cost_req": last7, "prev7_cost_req": prev7, "last7_credit_req": last7c,
        "top_cost_days": [{"day": d["day"], "cost": d["cost"], "req": d["req"]} for d in top_days],
        "top_credit_days": [{"day": d["day"], "credit": d["credit"], "req": d["req"]} for d in top_credit_days],
        "peak_hour_by_cost": {"hour": peak_hour["hour"], "cost": peak_hour["cost"]},
        "peak_hour_by_credit": {"hour": peak_credit_hour["hour"], "credit": peak_credit_hour["credit"]},
        "peak_hour_by_req": {"hour": peak_req_hour["hour"], "req": peak_req_hour["req"]},
        "peak_weekday_hour": {"when": f"{peak_cell[0]} {peak_cell[1]:02d}时", "req": peak_cell[2], "cost_usd": peak_cell[3]},
        "model_avg_cost_per_req": mreq, "model_avg_credit_per_req": mgw,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--json", dest="json_out", default=None,
                    help="额外输出图表数据 JSON 快照 (供控制台 /api/usage 读取)")
    a = ap.parse_args()
    with closing(sqlite3.connect(Path(a.db).resolve().as_uri() + "?mode=ro", uri=True)) as db, db:
        db.execute("BEGIN")
        payload = build_payload(load(db))
    html = HTML.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    result = {"out": a.out, "summary": summarize(payload)}
    if a.json_out:
        result["json"] = write_json(a.json_out, payload)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

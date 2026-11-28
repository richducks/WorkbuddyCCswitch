// Render the usage snapshot. The backend keeps CC Switch USD and gateway credits separate.
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const COLORS = ["#58a6ff", "#f28e2b", "#3fb950", "#bc8cff", "#e3b341"];
  const ACC = COLORS[0], C2 = COLORS[1], C3 = COLORS[2], C4 = COLORS[3];
  const MUTED = "#8b949e", GRID = "#30363d", FG = "#e6edf3";
  const $ = (s) => document.querySelector(s);
  const fmtMoney = (v) => v == null ? "—" : "$" + v.toFixed(v > 0 && v < 0.01 ? 4 : 2);
  const fmtCredit = (v) => v == null ? "—" : fmtInt(v) + " 点";
  const fmtInt = (v) => (v == null ? "—" : v.toLocaleString("en-US"));
  let D = null;
  let loaded = false;
  let loading = false;
  let selDay = null; // 当前穿透展示的日期（图1柱点击）
  const tip = $("#usageTip");
  const card = $("#usageCard");
  const picker = $("#usageDay");
  const refreshBtn = $("#usageRefreshBtn");
  const feedback = $("#usageRefreshInfo");
  const empty = $("#usageUnavailable");
  const hasUsd = () => (D.kpi.codex_requests ?? (D.kpi.cost != null)) > 0;
  const hasCredits = () => (D.kpi.gateway_requests ?? (D.kpi.credit != null)) > 0;

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
  function bindTip(e, html) {
    e.addEventListener("mousemove", (ev) => {
      tip.innerHTML = html;
      tip.style.display = "block";
      const pad = 12, r = tip.getBoundingClientRect();
      let x = ev.clientX + pad, y = ev.clientY + pad;
      if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
      if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    });
    e.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  }
  function frame(sel, h, mr) {
    const box = $(sel);
    box.textContent = "";
    const w = Math.max(box.clientWidth || 0, 220);
    const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, height: h }, box);
    const ml = 44, mt = 10, mb = 22;
    return { svg, w, h, ml, mr: mr || 10, mt, mb, pw: w - ml - (mr || 10), ph: h - mt - mb };
  }
  function yGrid(f, y, vals, fmt) {
    vals.forEach((v) => {
      const yy = y(v);
      el("line", { x1: f.ml, x2: f.ml + f.pw, y1: yy, y2: yy, stroke: GRID, "stroke-width": 1 }, f.svg);
      el("text", { x: f.ml - 6, y: yy + 4, "text-anchor": "end", "font-size": 10.5, fill: MUTED }, f.svg).textContent = fmt(v);
    });
  }
  // KPI 卡片 + 页脚口径说明
  function renderKpis() {
    const k = D.kpi, box = $("#usageKpis");
    const items = [[fmtInt(k.requests), "请求次数"]];
    if (hasCredits()) items.push([fmtCredit(k.credit), "WorkBuddy 网关消耗"]);
    if (hasUsd()) items.push([fmtMoney(k.cost), "CC Switch 成本"]);
    if (k.cache_rate != null) items.push([k.cache_rate + "%", "缓存命中率"]);
    box.textContent = "";
    for (const [v, t] of items) {
      const div = document.createElement("div");
      div.className = "usage-kpi";
      const val = document.createElement("div"); val.className = "v"; val.textContent = v;
      const cap = document.createElement("div"); cap.className = "k"; cap.textContent = t;
      div.append(val, cap);
      box.appendChild(div);
    }
    $("#usageMeta").textContent = k.max_dt ? `截至 ${k.max_dt}` : "";
    $("#usageFootnote").textContent =
      `${k.range}（北京时间）· 上次统计：${D.generated}`;
  }
  // 通用日柱带：一根柱/天 + 可选请求线（右轴）+ 可选 MA7 虚线
  function drawDailyBand(sel, valKey, fmt, barColor, opts) {
    const days = D.days, H = 200;
    const f = frame(sel, H, opts.reqLine ? 44 : 10), { svg, ml, mt, pw, ph } = f;
    const maxV = niceMax(Math.max(...days.map((d) => d[valKey]), 0.1));
    const yV = (v) => mt + ph - (v / maxV) * ph;
    yGrid(f, yV, Array.from({ length: 5 }, (_, i) => (maxV * i) / 4), fmt);
    if (opts.reqLine) {
      const maxR = niceMax(Math.max(...days.map((d) => d.req), 1));
      const yR = (v) => mt + ph - (v / maxR) * ph;
      Array.from({ length: 5 }, (_, i) => (maxR * i) / 4).forEach((v) => {
        el("text", { x: ml + pw + 6, y: yR(v) + 4, "font-size": 10.5, fill: MUTED }, svg).textContent = Math.round(v);
      });
      el("path", { d: days.map((d, i) => `${i ? "L" : "M"}${ml + (pw / days.length) * i + pw / days.length / 2},${yR(d.req)}`).join(""),
                   fill: "none", stroke: C2, "stroke-width": 1.6 }, svg);
    }
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
    const n = days.length, band = pw / n, bw = Math.max(band * 0.72, 1.5);
    const step = Math.max(1, Math.ceil(n / 10));
    days.forEach((d, i) => {
      const x = ml + band * i + (band - bw) / 2, y0 = mt + ph, v = d[valKey];
      const sel = d.day === selDay;
      if (v > 0) {
        const bar = el("rect", { x, y: yV(v), width: bw, height: Math.max(y0 - yV(v), 0.5),
                                 fill: barColor, rx: 1.5, "fill-opacity": d.partial ? 0.4 : 0.85,
                                 stroke: sel ? FG : "none", "stroke-width": sel ? 1.2 : 0 }, svg);
        bindTip(bar, `<b>${esc(d.day)}</b>${d.partial ? " · 数据不完整" : ""}${sel ? " · 已选中" : ""}<br>${opts.name} <b>${fmt(v)}</b>`);
      }
      if (i % step === 0 || i === n - 1)
        el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: sel ? FG : MUTED }, svg).textContent = d.label;
    });
    if (opts.maKey) {
      el("path", { d: days.map((d, i) => `${i ? "L" : "M"}${ml + band * i + band / 2},${yV(d[opts.maKey])}`).join(""),
                   fill: "none", stroke: MUTED, "stroke-width": 1.5, "stroke-dasharray": "4 3" }, svg);
    }
    // 悬停列：一条列出两链，免得来回对上下带；点击穿透到当天明细
    days.forEach((d, i) => {
      const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
      if (d.req) hit.style.cursor = "pointer";
      bindTip(hit, `<b>${esc(d.day)}</b>${d.partial ? " · 数据不完整" : ""}<br>CC Switch <b>${fmtMoney(d.cost)}</b> · 网关 <b>${fmtCredit(d.credit)}</b><br>${fmtInt(d.req)} 次`);
      if (d.req) hit.addEventListener("click", () => selectDay(selDay === d.day ? "" : d.day));
    });
  }
  // 通用堆叠柱带：按链取各自的 parts 数组与单位
  function drawStackBand(sel, partsKey, valKey, fmt, chainName) {
    const days = D.days, H = 200;
    const names = (D.kpi.models || []).filter((m) => m.chain === chainName).map((m) => m.model);
    const f = frame(sel, H), { svg, ml, mt, pw, ph } = f;
    const maxV = niceMax(Math.max(...days.map((d) => d[valKey]), 0.1));
    const yV = (v) => mt + ph - (v / maxV) * ph;
    yGrid(f, yV, Array.from({ length: 5 }, (_, i) => (maxV * i) / 4), fmt);
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
    const n = days.length, band = pw / n, bw = Math.max(band * 0.72, 1.5);
    const step = Math.max(1, Math.ceil(n / 10));
    days.forEach((d, i) => {
      const x = ml + band * i + (band - bw) / 2;
      let acc = 0;
      const segs = (d[partsKey] || []).map((p, j) => ({ ...p, j })).filter((p) => p[valKey] > 0);
      segs.forEach((p) => {
        const y1 = yV(acc), y2 = yV(acc + p[valKey]);
        const ci = names.indexOf(p.model) >= 0 ? names.indexOf(p.model) : p.j % 5;
        const r = el("rect", { x, y: y2, width: bw, height: Math.max(y1 - y2, 0.4), fill: COLORS[ci % 5],
                               "fill-opacity": d.partial ? 0.45 : 0.9 }, svg);
        bindTip(r, `<b>${esc(d.day)}</b><br>${esc(p.model)}: ${fmt(p[valKey])}`);
        acc += p[valKey];
      });
      if (acc > 0) {
        const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
        bindTip(hit, `<b>${esc(d.day)}</b>${d.partial ? " · 数据不完整" : ""}<br>合计 <b>${fmt(acc)}</b><br>` +
          segs.map((p) => `${esc(p.model)} ${fmt(p[valKey])}`).join("<br>"));
      }
      if (i % step === 0 || i === n - 1)
        el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
    });
  }
  // 图1: 每日成本双带 + 请求线（上带右轴）+ 两条 MA7 虚线；点击柱穿透
  function renderDaily() {
    const usd = hasUsd(), credits = hasCredits();
    $("#dailyUsdLabel").hidden = $("#chDaily").hidden = !usd;
    $("#dailyCreditLabel").hidden = $("#chDailyG").hidden = !credits;
    if (usd) drawDailyBand("#chDaily", "cost", fmtMoney, ACC, { reqLine: true, maKey: "ma7", name: "CC Switch" });
    if (credits) drawDailyBand("#chDailyG", "credit", fmtCredit, C4, { reqLine: !usd, maKey: "ma7g", name: "网关" });
  }
  // ---- 穿透：当天分时段柱 + 按模型明细表（数据来自快照 day_detail） ----
  const WD_ZH = ["日", "一", "二", "三", "四", "五", "六"];
  function selectDay(day) {
    selDay = day || null;
    picker.value = day;
    $("#usageDetail").hidden = !selDay;
    tip.style.display = "none";
    renderDaily();
    if (selDay) drawDetail(selDay);
  }
  // 单链分时柱：hours = [[时, 本单位成本, 次数], ...]（稀疏）
  function drawDetailHour(sel, hours, fmt, color) {
    const f = frame(sel, 150), { svg, ml, mt, pw, ph } = f;
    const vals = (hours || []).map((h) => h[1]);
    const maxV = niceMax(Math.max(...vals, 0.1));
    const yV = (v) => mt + ph - (v / maxV) * ph;
    yGrid(f, yV, Array.from({ length: 3 }, (_, i) => (maxV * i) / 2), fmt);
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
    const bw = (pw / 24) * 0.66;
    for (let h = 0; h < 24; h++) {
      if (h % 2 === 0)
        el("text", { x: ml + (pw / 24) * h + pw / 48, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = String(h).padStart(2, "0");
      const row = (hours || []).find((r) => r[0] === h);
      if (!row) continue;
      const r = el("rect", { x: ml + (pw / 24) * h + (pw / 24 - bw) / 2, y: yV(row[1]), width: bw,
                             height: Math.max(mt + ph - yV(row[1]), 0.5), fill: color, rx: 1.5, "fill-opacity": 0.85 }, svg);
      bindTip(r, `<b>${String(h).padStart(2, "0")}:00–${String(h).padStart(2, "0")}:59</b><br><b>${fmt(row[1])}</b> · ${fmtInt(row[2])} 次`);
    }
    if (!(hours || []).length)
      el("text", { x: ml + pw / 2, y: mt + ph / 2, "text-anchor": "middle", "font-size": 11, fill: MUTED }, svg).textContent = "该链当日无请求";
  }
  // 单链按模型表：rows = [[模型, 本单位成本, 次数], ...]（按本单位成本降序）
  function fillDetailTable(sel, rows, fmt) {
    const tb = $(sel);
    tb.textContent = "";
    const mk = (tag, txt, cls) => { const c = document.createElement(tag); c.textContent = txt; if (cls) c.className = cls; return c; };
    const head = tb.insertRow();
    ["模型", "成本", "次数"].forEach((t, i) => { const th = document.createElement("th"); th.textContent = t; if (i === 0) th.style.textAlign = "left"; head.appendChild(th); });
    if (!(rows || []).length) {
      const tr = tb.insertRow(); const td = mk("td", "无请求"); td.colSpan = 3; tr.appendChild(td);
      return;
    }
    rows.forEach(([model, val, req]) => {
      const tr = tb.insertRow();
      const tdM = mk("td", model); tdM.className = "usage-model";
      tr.append(tdM, mk("td", fmt(val)), mk("td", fmtInt(req)));
    });
    const tot = tb.insertRow(); tot.className = "total";
    const sumV = rows.reduce((a, r) => a + r[1], 0), sumR = rows.reduce((a, r) => a + r[2], 0);
    tot.append(mk("td", "合计"), mk("td", fmt(sumV)), mk("td", fmtInt(sumR)));
  }
  function drawDetail(day) {
    const det = (D.day_detail || {})[day];
    const dt = new Date(day + "T12:00:00+08:00");
    $("#detailTitle").textContent = `${day.slice(5)}（周${WD_ZH[dt.getUTCDay()]}）分时段与模型明细 · 北京时间`;
    $("#detailSub").textContent = "这份快照没有当日明细，请刷新数据。";
    const usd = Boolean(det?.models?.codex?.length);
    const credits = Boolean(det?.models?.gateway?.length);
    $("#detailSub").hidden = usd || credits;
    $("#detailUsd").hidden = !usd;
    $("#detailCredit").hidden = !credits;
    if (usd) {
      drawDetailHour("#chDetailC", det.hours?.codex, fmtMoney, ACC);
      fillDetailTable("#tbDetailC", det.models.codex, fmtMoney);
    }
    if (credits) {
      drawDetailHour("#chDetailG", det.hours?.gateway, fmtCredit, C4);
      fillDetailTable("#tbDetailG", det.models.gateway, fmtCredit);
    }
  }
  // 图2: 模型堆叠双带，图例按链分组
  function renderModel() {
    const lg = $("#modelLegend");
    const models = D.kpi.models || [];
    lg.textContent = "";
    for (const [chain, label] of [["codex", "CC Switch（美元）"], ["gateway", "WorkBuddy 网关（点数）"]]) {
      const names = models.filter((m) => m.chain === chain).map((m) => m.model);
      if (!names.length) continue;
      const tag = document.createElement("b");
      tag.textContent = label + "：";
      lg.appendChild(tag);
      names.forEach((m, i) => {
        const span = document.createElement("span");
        const dot = document.createElement("i");
        dot.style.background = COLORS[i % 5];
        span.append(dot, document.createTextNode(m));
        lg.appendChild(span);
      });
    }
    $("#chModel").hidden = !hasUsd();
    $("#chModelG").hidden = !hasCredits();
    if (hasUsd()) drawStackBand("#chModel", "parts", "cost", fmtMoney, "codex");
    if (hasCredits()) drawStackBand("#chModelG", "credit_parts", "credit", fmtCredit, "gateway");
  }
  // 图3: 时段分布双带
  function renderHour() {
    function band(sel, valKey, fmt, color, reqLine) {
      const hs = D.hourly, H = 200;
      const f = frame(sel, H, reqLine ? 44 : 10), { svg, ml, mt, pw, ph } = f;
      const maxV = niceMax(Math.max(...hs.map((h) => h[valKey]), 0.1));
      const yV = (v) => mt + ph - (v / maxV) * ph;
      yGrid(f, yV, Array.from({ length: 5 }, (_, i) => (maxV * i) / 4), fmt);
      if (reqLine) {
        const maxR = niceMax(Math.max(...hs.map((h) => h.req), 1));
        const yR = (v) => mt + ph - (v / maxR) * ph;
        Array.from({ length: 5 }, (_, i) => (maxR * i) / 4).forEach((v) =>
          el("text", { x: ml + pw + 6, y: yR(v) + 4, "font-size": 10.5, fill: MUTED }, svg).textContent = Math.round(v));
        el("path", { d: hs.map((h, i) => `${i ? "L" : "M"}${ml + (pw / 24) * i + pw / 48},${yR(h.req)}`).join(""),
                     fill: "none", stroke: C2, "stroke-width": 1.6 }, svg);
      }
      el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
      const bw = (pw / 24) * 0.66;
      hs.forEach((h, i) => {
        const x = ml + (pw / 24) * i + (pw / 24 - bw) / 2;
        if (h[valKey] > 0) {
          const r = el("rect", { x, y: yV(h[valKey]), width: bw, height: mt + ph - yV(h[valKey]), fill: color, rx: 1.5, "fill-opacity": 0.85 }, svg);
          bindTip(r, `<b>${String(h.hour).padStart(2, "0")}:00–${String(h.hour).padStart(2, "0")}:59</b><br>CC Switch <b>${fmtMoney(h.cost)}</b> · 网关 <b>${fmtCredit(h.credit)}</b> · ${fmtInt(h.req)} 次`);
        }
        if (i % 2 === 0)
          el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = String(h.hour).padStart(2, "0");
      });
      hs.forEach((h, i) => {
        const hit = el("rect", { x: ml + (pw / 24) * i, y: mt, width: pw / 24, height: ph, fill: "transparent" }, svg);
        bindTip(hit, `<b>${String(h.hour).padStart(2, "0")}:00 时段（全程）</b><br>CC Switch <b>${fmtMoney(h.cost)}</b> · 网关 <b>${fmtCredit(h.credit)}</b><br>${fmtInt(h.req)} 次`);
      });
    }
    $("#chHour").hidden = !hasUsd();
    $("#chHourG").hidden = !hasCredits();
    if (hasUsd()) band("#chHour", "cost", fmtMoney, ACC, true);
    if (hasCredits()) band("#chHourG", "credit", fmtCredit, C4, !hasUsd());
  }
  // 图4: 星期×时段热力图（颜色=请求次数，两链成本单位不同不能混着上色；rows=[次数,美元,点数]）
  function renderHeat() {
    const rows = D.wh, H = 30 + rows.length * 26;
    const f = frame("#chHeat", H), { svg, ml, mt, pw, ph } = f;
    const cells = rows.flatMap((r) => r.rows[0]);
    const maxV = Math.max(...cells, 0.01);
    const cw = pw / 24, ch = ph / rows.length;
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
      bindTip(c, `<b>${esc(r.wd)} ${String(hi).padStart(2, "0")}:00–${String(hi).padStart(2, "0")}:59</b><br><b>${fmtInt(v)}</b> 次<br>CC Switch ${fmtMoney(r.rows[1][hi])} · 网关 ${fmtCredit(r.rows[2][hi])}`);
      });
    });
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: GRID, "stroke-width": 1 }, svg);
  }
  // 图5: 缓存命中率（两链合并）
  function renderCache() {
    const days = D.days.filter((d) => d.cache_rate != null), H = 200;
    const f = frame("#chCache", H), { svg, ml, mt, pw, ph } = f;
    const y = (v) => mt + ph - (v / 100) * ph;
    yGrid(f, y, [0, 25, 50, 75, 100], (v) => v + "%");
    el("line", { x1: ml, x2: ml + pw, y1: mt + ph, y2: mt + ph, stroke: FG, "stroke-width": 1 }, svg);
    const avg = D.kpi.cache_rate ?? 0;
    el("line", { x1: ml, x2: ml + pw, y1: y(avg), y2: y(avg), stroke: ACC, "stroke-width": 1, "stroke-dasharray": "5 3" }, svg);
    el("text", { x: ml + pw - 4, y: y(avg) - 4, "text-anchor": "end", "font-size": 10.5, fill: ACC }, svg).textContent = "全程 " + avg + "%";
    const n = days.length, band = pw / Math.max(n, 1);
    const step = Math.max(1, Math.ceil(n / 10));
    el("path", { d: days.map((d, i) => `${i ? "L" : "M"}${ml + band * i + band / 2},${y(d.cache_rate)}`).join(""),
                 fill: "none", stroke: C3, "stroke-width": 1.8 }, svg);
    days.forEach((d, i) => {
      const cx = ml + band * i + band / 2, cy = y(d.cache_rate);
      const dot = el("circle", { cx, cy, r: 3, fill: C3, "fill-opacity": 0 }, svg);
      const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
      bindTip(hit, `<b>${esc(d.day)}</b><br>缓存命中率 <b>${fmtInt(d.cache_rate)}%</b>`);
      dot.addEventListener("mouseenter", () => dot.setAttribute("fill-opacity", 1));
      dot.addEventListener("mouseleave", () => dot.setAttribute("fill-opacity", 0));
      if (i % step === 0 || i === n - 1)
        el("text", { x: cx, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
    });
  }
  function renderAll() {
    renderKpis(); renderDaily(); renderModel(); renderHour(); renderHeat(); renderCache();
    if (selDay && !$("#usageDetail").hidden) drawDetail(selDay); // resize 后重画穿透图
  }
  function announce(message) {
    feedback.textContent = message;
    feedback.hidden = !message;
  }

  async function refresh(runEtl) {
    if (loading || (runEtl && !summary?.permissions?.usage_refresh)) return;
    loading = true;
    refreshBtn.disabled = true;
    if (runEtl) refreshBtn.textContent = "统计中…";
    announce(runEtl ? "正在统计用量…" : "正在读取用量…");
    try {
      const result = runEtl ? await post("/api/usage/refresh", {}) : await api("/api/usage");
      if (runEtl && !result.ok) throw new Error(result.error || "没有生成新的用量数据");
      const doc = runEtl ? result.snapshot : result;
      if (!doc?.available || !doc.payload?.kpi?.requests) {
        D = null;
        $("#usageMeta").textContent = "";
        empty.textContent = doc?.available
          ? "暂无用量记录。通过网关发出请求后，可回来刷新查看。"
          : doc?.hint || "尚未生成用量数据。";
        empty.hidden = false;
        $("#usageBody").hidden = true;
      } else {
        D = doc.payload;
        const days = D.days.filter(day => day.req).map(day => day.day).reverse();
        picker.replaceChildren(new Option("选择日期", ""), ...days.map(day => new Option(day, day)));
        if (!days.includes(selDay)) selDay = null;
        picker.value = selDay || "";
        $("#usageDetail").hidden = !selDay;
        empty.hidden = true;
        $("#usageBody").hidden = false;
        renderAll();
      }
      if (runEtl) {
        const s = result.summary;
        const added = (s.new_rows || 0) + (s.gateway_new_rows || 0);
        announce(`已刷新 · 新增 ${fmtInt(added)} 条 · 共 ${fmtInt(s.total_requests)} 次请求`);
      } else {
        announce("");
      }
      loaded = true;
    } catch (e) {
      const prefix = runEtl ? "刷新未完成：" : "无法读取用量：";
      announce(prefix + e.message + (runEtl && D ? " 仍显示上一次数据。" : ""));
    } finally {
      loading = false;
      refreshBtn.disabled = false;
      refreshBtn.textContent = "刷新数据";
    }
  }
  picker.onchange = () => selectDay(picker.value);
  $("#usageDetailClose").onclick = () => { selectDay(""); picker.focus(); };
  refreshBtn.onclick = () => refresh(true);
  document.addEventListener("wb:tab", (event) => {
    if (event.detail !== card.id) return;
    if (!loaded) refresh(false);
    else if (D) renderAll();
  });
  if (!card.hidden) refresh(false);
  let rt;
  addEventListener("resize", () => {
    clearTimeout(rt);
    if (D && !card.hidden) rt = setTimeout(() => {
      if (D && !card.hidden) renderAll();
    }, 150);
  });
})();

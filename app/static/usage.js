// 用量复盘：从 /api/usage 读取宿主机每日 ETL 生成的快照（~/.hermes/behavior_ledger.db 聚合），
// 数据口径集中在 usage_charts.py，这里只做渲染（移植自其本地看板模板）。
// 首次进入标签页时拉取，之后用「刷新数据」按钮。颜色用控制台深色主题的固定值。
//
// v2 双链口径（成本单位不同，不混算，按带展示）：
//   - codex 链（WorkBuddy CLI → CC Switch 代理）：成本 = 美元
//   - gateway 链（各客户端 → 本地网关 8317 /v1）：成本 = credits 点数
// 请求次数 / token / 缓存命中率 / 时段热度 = 两链合并；wh.rows = [次数, 美元, 点数]。
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const COLORS = ["#58a6ff", "#f28e2b", "#3fb950", "#bc8cff", "#e3b341"];
  const ACC = COLORS[0], C2 = COLORS[1], C3 = COLORS[2], C4 = COLORS[3];
  const MUTED = "#8b949e", GRID = "#30363d", FG = "#e6edf3";
  const $ = (s) => document.querySelector(s);
  const fmtMoney = (v) => v == null ? "—" : "$" + (v >= 100 ? v.toFixed(0) : v.toFixed(2));
  const fmtCredit = (v) => v == null || !v ? "—" : (v >= 1000 ? v.toFixed(0) : v.toFixed(1)) + " 点";
  const fmtInt = (v) => (v == null ? "—" : v.toLocaleString("en-US"));
  let D = null;
  let loaded = false;
  let loading = false;
  const tip = $("#usageTip");

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
    const w = Math.max(box.clientWidth || 0, 320);
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
    const items = [
      [fmtMoney(k.cost), "总成本 · WorkBuddy 链 ($)"],
      [fmtCredit(k.credit), "网关链消耗（点数）"],
      [fmtInt(k.requests), "请求次数（两链）"],
      [k.cache_rate == null ? "—" : k.cache_rate + "%", "缓存命中率（两链）"],
      [k.codex_requests ? fmtMoney(k.cost / k.codex_requests) : "—", "WorkBuddy 均价 / 请求"],
    ];
    box.textContent = "";
    for (const [v, t] of items) {
      const div = document.createElement("div");
      div.className = "usage-kpi";
      const val = document.createElement("div"); val.className = "v"; val.textContent = v;
      const cap = document.createElement("div"); cap.className = "k"; cap.textContent = t;
      div.append(val, cap);
      box.appendChild(div);
    }
    $("#usageMeta").textContent = `截至 ${k.max_dt}`;
    $("#usageFootnote").textContent =
      `数据范围 ${k.range}（北京时间）· 末日浅色柱因源库同步滞后可能不完整 · 快照生成于 ${D.generated} · ` +
      "链路：WorkBuddy CLI→CC Switch 代理（计美元）+ 各客户端→本地网关 8317（计点数），两者互不重叠 · " +
      "宿主机每日 03:00 ETL 自动刷新，也可点右上角「刷新数据」重新读取";
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
      if (v > 0) {
        const bar = el("rect", { x, y: yV(v), width: bw, height: Math.max(y0 - yV(v), 0.5),
                                 fill: barColor, rx: 1.5, "fill-opacity": d.partial ? 0.4 : 0.85 }, svg);
        bindTip(bar, `<b>${d.day}</b>${d.partial ? " · 数据不完整" : ""}<br>${opts.name} <b>${fmt(v)}</b>`);
      }
      if (i % step === 0 || i === n - 1)
        el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
    });
    if (opts.maKey) {
      el("path", { d: days.map((d, i) => `${i ? "L" : "M"}${ml + band * i + band / 2},${yV(d[opts.maKey])}`).join(""),
                   fill: "none", stroke: MUTED, "stroke-width": 1.5, "stroke-dasharray": "4 3" }, svg);
    }
    // 悬停列：一条列出两链，免得来回对上下带
    days.forEach((d, i) => {
      const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
      bindTip(hit, `<b>${d.day}</b>${d.partial ? " · 数据不完整" : ""}<br>WorkBuddy <b>${fmtMoney(d.cost)}</b> · 网关 <b>${fmtCredit(d.credit)}</b><br>${fmtInt(d.req)} 次`);
    });
  }
  // 通用堆叠柱带：按链取各自的 parts 数组与单位
  function drawStackBand(sel, partsKey, valKey, fmt, chainName) {
    const days = D.days, H = 200;
    const names = D.kpi.models.filter((m) => m.chain === chainName).map((m) => m.model);
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
        bindTip(r, `<b>${d.day}</b><br>${p.model}: ${fmt(p[valKey])}`);
        acc += p[valKey];
      });
      if (acc > 0) {
        const hit = el("rect", { x: ml + band * i, y: mt, width: band, height: ph, fill: "transparent" }, svg);
        bindTip(hit, `<b>${d.day}</b>${d.partial ? " · 数据不完整" : ""}<br>合计 <b>${fmt(acc)}</b><br>` +
          segs.map((p) => `${p.model} ${fmt(p[valKey])}`).join("<br>"));
      }
      if (i % step === 0 || i === n - 1)
        el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
    });
  }
  // 图1: 每日成本双带 + 请求线（上带右轴）+ 两条 MA7 虚线
  function renderDaily() {
    drawDailyBand("#chDaily", "cost", fmtMoney, ACC, { reqLine: true, maKey: "ma7", name: "WorkBuddy 链" });
    drawDailyBand("#chDailyG", "credit", fmtCredit, C4, { reqLine: false, maKey: "ma7g", name: "网关链" });
  }
  // 图2: 模型堆叠双带，图例按链分组
  function renderModel() {
    const lg = $("#modelLegend");
    lg.textContent = "";
    for (const [chain, label] of [["codex", "WorkBuddy 链 ($)"], ["gateway", "网关链（点数）"]]) {
      const names = D.kpi.models.filter((m) => m.chain === chain).map((m) => m.model);
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
    const gwN = D.kpi.models.filter((m) => m.chain === "gateway").length;
    $("#modelSub").textContent =
      `上带 = WorkBuddy 链（$） · 下带 = 网关链（点数${gwN ? `，${gwN} 个模型` : ""}） · 各链内按每日本单位成本堆叠`;
    drawStackBand("#chModel", "parts", "cost", fmtMoney, "codex");
    drawStackBand("#chModelG", "credit_parts", "credit", fmtCredit, "gateway");
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
          bindTip(r, `<b>${String(h.hour).padStart(2, "0")}:00–${String(h.hour).padStart(2, "0")}:59</b><br>WorkBuddy <b>${fmtMoney(h.cost)}</b> · 网关 <b>${fmtCredit(h.credit)}</b> · ${fmtInt(h.req)} 次`);
        }
        if (i % 2 === 0)
          el("text", { x: x + bw / 2, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = String(h.hour).padStart(2, "0");
      });
      hs.forEach((h, i) => {
        const hit = el("rect", { x: ml + (pw / 24) * i, y: mt, width: pw / 24, height: ph, fill: "transparent" }, svg);
        bindTip(hit, `<b>${String(h.hour).padStart(2, "0")}:00 时段（全程）</b><br>WorkBuddy <b>${fmtMoney(h.cost)}</b> · 网关 <b>${fmtCredit(h.credit)}</b><br>${fmtInt(h.req)} 次`);
      });
    }
    band("#chHour", "cost", fmtMoney, ACC, true);
    band("#chHourG", "credit", fmtCredit, C4, false);
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
        bindTip(c, `<b>${r.wd} ${String(hi).padStart(2, "0")}:00–${String(hi).padStart(2, "0")}:59</b><br><b>${fmtInt(v)}</b> 次<br>WorkBuddy ${fmtMoney(r.rows[1][hi])} · 网关 ${fmtCredit(r.rows[2][hi])}`);
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
      bindTip(hit, `<b>${d.day}</b><br>缓存命中率 <b>${d.cache_rate}%</b>`);
      dot.addEventListener("mouseenter", () => dot.setAttribute("fill-opacity", 1));
      dot.addEventListener("mouseleave", () => dot.setAttribute("fill-opacity", 0));
      if (i % step === 0 || i === n - 1)
        el("text", { x: cx, y: mt + ph + 14, "text-anchor": "middle", "font-size": 10, fill: MUTED }, svg).textContent = d.label;
    });
  }
  function renderAll() { renderKpis(); renderDaily(); renderModel(); renderHour(); renderHeat(); renderCache(); }
  async function refresh() {
    if (loading) return;
    loading = true;
    const btn = $("#usageRefreshBtn");
    if (btn) { btn.disabled = true; btn.textContent = "刷新中…"; }
    try {
      const doc = await api("/api/usage");
      if (!doc.available) {
        $("#usageUnavailable").textContent = doc.hint || "用量数据尚未生成。";
        $("#usageUnavailable").hidden = false;
        $("#usageBody").hidden = true;
        if (btn) btn.hidden = true;
        return;
      }
      D = doc.payload;
      $("#usageUnavailable").hidden = true;
      $("#usageBody").hidden = false;
      if (btn) btn.hidden = false;
      renderAll();
      loaded = true;
    } catch (e) {
      $("#usageUnavailable").textContent = "用量数据读取失败：" + e.message;
      $("#usageUnavailable").hidden = false;
    } finally {
      loading = false;
      if (btn) { btn.disabled = false; btn.textContent = "刷新数据"; }
    }
  }
  const link = document.querySelector('#sidenav a[href="#usageCard"]');
  if (link) link.addEventListener("click", () => { if (!loaded && !loading) refresh(); });
  const refreshBtn = $("#usageRefreshBtn");
  if (refreshBtn) refreshBtn.onclick = () => refresh();
  let rt;
  addEventListener("resize", () => {
    if (!loaded || !D) return;
    clearTimeout(rt);
    rt = setTimeout(renderAll, 150);
  });
})();

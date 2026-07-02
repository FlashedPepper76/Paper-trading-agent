// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: orange; icon-glyph: magic;
// Command Deck — Trading Agent Widget (Scriptable)
//
// SETUP:
// 1. Install Scriptable (free, App Store).
// 2. Paste this whole file into a new script named "TradingAgentWidget".
// 3. Add it as a Home Screen widget (small/medium/large all supported).
// 4. Long-press the widget → Edit Widget → Parameter:
//      - blank          → comparison view of all three agents (Plutus / Helios / Hermes)
//      - "plutus"       → detailed single-agent view for Plutus
//      - "helios"       → detailed single-agent view for Helios
//      - "hermes"       → detailed single-agent view for Hermes
// 5. Tap the widget to open your dashboard (set DASHBOARD_URL below).
//
// AUTO-UPDATE: On every refresh this script fetches its latest version from
// GitHub. If the file changed it is written to disk; the update takes effect
// on the next widget refresh (no action required).
//
// CHART: The comparison chart uses the same day-slot xFrac system as the web
// dashboard's /compare page: each calendar date gets equal horizontal width;
// intraday runs are evenly spaced within their day-slot. VTI is read from the
// same Supabase benchmark_prices table the dashboard uses, so the lines here
// will match what you see on /compare exactly.
//
// Note: Hermes started at $10k (vs $100k for Plutus/Helios). Dollar amounts
// for Hermes are scaled ×10 in the widget so all three agents are comparable
// on screen. Return % is unaffected by the scale.

const SUPABASE_URL    = "https://edmysxanjsskjrdfkmaw.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVkbXlzeGFuanNza2pyZGZrbWF3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI0ODA3ODAsImV4cCI6MjA5ODA1Njc4MH0.gebHV4qcqPxdFAvwqEgtGKTS_u_PhzgznXqnEqXmBoA";
const DASHBOARD_URL   = "https://trading-agent-dashboard-mu.vercel.app";
const GITHUB_RAW_URL  = "https://raw.githubusercontent.com/FlashedPepper76/Paper-trading-agent/main/TradingAgentWidget.js";

const AGENT_COLORS = {
  plutus: new Color("#ffffff"), // white — brightest line
  helios: new Color("#9ca3af"), // mid-gray
  hermes: new Color("#34d399"), // emerald green
};
const AGENT_LABELS       = { plutus: "Plutus", helios: "Helios", hermes: "Hermes" };
const AGENT_DISPLAY_SCALE = { plutus: 1, helios: 1, hermes: 10 };

const BENCHMARK_SYMBOL = "VTI";
const BENCHMARK_COLOR  = new Color("#475569"); // darkest, dashed

const agentFilter = args.widgetParameter ? args.widgetParameter.trim().toLowerCase() : null;

// ── Self-update from GitHub ───────────────────────────────────────────────────
// Silently fetches the latest version from GitHub and writes it to disk if it
// has changed. The update takes effect on the next widget refresh.

async function checkForUpdate() {
  try {
    let fm;
    try { fm = FileManager.iCloud(); } catch (_) { fm = FileManager.local(); }
    const selfPath = fm.joinPath(fm.documentsDirectory(), Script.name() + ".js");
    const req = new Request(GITHUB_RAW_URL);
    req.timeoutInterval = 6;
    const latest = await req.loadString();
    if (!latest || latest.length < 200) return; // guard against empty/error responses
    const current = fm.readString(selfPath);
    if (latest !== current) {
      fm.writeString(selfPath, latest);
      // Next refresh will pick up the new version automatically.
    }
  } catch (_) {
    // Silent — never break the widget if GitHub is unreachable.
  }
}

// ── Supabase helpers ──────────────────────────────────────────────────────────

function buildQuery(params) {
  return Object.entries(params)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
}

async function supabaseGet(table, params) {
  const qs  = buildQuery(params);
  const req = new Request(`${SUPABASE_URL}/rest/v1/${table}?${qs}`);
  req.headers = {
    apikey:        SUPABASE_ANON_KEY,
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
  };
  const json   = await req.loadJSON();
  const status = req.response ? req.response.statusCode : null;
  if (status && status >= 400) {
    const msg = (json && (json.message || json.error || json.hint)) || JSON.stringify(json);
    throw new Error(`[${table}] HTTP ${status}: ${msg}`);
  }
  return json;
}

async function loadLatestRun(agentId) {
  const runs = await supabaseGet("trading_agent_runs", {
    select:   "*",
    agent_id: `eq.${agentId}`,
    order:    "id.desc",
    limit:    "1",
  });
  return runs && runs[0];
}

// Returns [{run_at, equity}] oldest → newest.
async function loadEquityHistory(agentId, limit) {
  const rows = await supabaseGet("trading_agent_runs", {
    select:   "id,account_equity,run_at",
    agent_id: `eq.${agentId}`,
    order:    "id.desc",
    limit:    String(limit),
  });
  return rows
    .filter((r) => r.account_equity != null)
    .map((r) => ({ run_at: r.run_at, equity: r.account_equity }))
    .reverse();
}

async function loadLatestDecision(agentId) {
  const run = await loadLatestRun(agentId);
  if (!run) return null;
  const decisions = await supabaseGet("trading_agent_decisions", {
    select:  "*",
    run_id:  `eq.${run.id}`,
    order:   "id.desc",
    limit:   "1",
  });
  return decisions && decisions[0];
}

// VTI from Supabase benchmark_prices (same source as the web dashboard).
// Returns [{date: ISO-string, close: number}] sorted oldest → newest.
async function loadBenchmarkPrices(startDate) {
  const rows = await supabaseGet("benchmark_prices", {
    select:      "price_time,close",
    symbol:      `eq.${BENCHMARK_SYMBOL}`,
    price_time:  `gte.${startDate}`,
    order:       "price_time.asc",
    limit:       "10000",
  });
  return rows.map((r) => ({ date: r.price_time, close: r.close }));
}

// ── Day-slot xFrac series builders ────────────────────────────────────────────
//
// This exactly mirrors the web dashboard's buildPerAgentPctSeries /
// buildBenchmarkPctSeries from compare-helpers.tsx. Each calendar date in the
// shared axis gets equal horizontal width (1/daySlotCount). Runs within a day
// are evenly spaced by count within that slot. This eliminates the long
// diagonal "straight lines" that appear in raw calendar-time xFrac when agents
// don't run overnight — a 16-hour gap would otherwise span ~30% of the chart.

// [{run_at, equity}] → [{xFrac, pct}]  (day-slot version)
function buildPerAgentPctSeriesDaySlot(history, daySlotIndex, daySlotCount) {
  if (!history.length) return [];
  const chronological = history.filter((r) => r.equity != null);
  const base = chronological[0]?.equity;
  if (!base) return [];

  // Group runs by calendar date
  const byDate = new Map();
  for (const r of chronological) {
    const d = r.run_at.slice(0, 10);
    if (!byDate.has(d)) byDate.set(d, []);
    byDate.get(d).push(r);
  }

  const points = [];
  for (const [dateStr, dateRuns] of byDate) {
    const slotIdx = daySlotIndex[dateStr] ?? 0;
    const n = dateRuns.length;
    dateRuns.forEach((r, i) => {
      const inSlotFrac = n > 1 ? i / (n - 1) : 0.5;
      points.push({
        xFrac: (slotIdx + inSlotFrac) / daySlotCount,
        pct:   ((r.equity - base) / base) * 100,
      });
    });
  }
  return points;
}

// [{date, close}] → [{xFrac, pct}]  (day-slot version, same logic as dashboard)
function buildBenchmarkPctSeriesDaySlot(points, rangeStartMs, daySlotIndex, daySlotCount) {
  if (!points.length) return [];
  const sorted = points.slice().sort((a, b) => a.date.localeCompare(b.date));

  // Anchor % to the first VTI price at/after the agents' start date
  const rangeStartDate = new Date(rangeStartMs).toISOString().slice(0, 10);
  const basePoint = sorted.find((p) => p.date.slice(0, 10) >= rangeStartDate) ?? sorted[0];
  const base = basePoint.close;

  const byDate = new Map();
  for (const p of sorted) {
    const d = p.date.slice(0, 10);
    if (!byDate.has(d)) byDate.set(d, []);
    byDate.get(d).push(p);
  }

  const result = [];
  for (const [dateStr, dayPoints] of byDate) {
    const slotIdx = daySlotIndex[dateStr];
    if (slotIdx === undefined) continue; // outside agent date range
    const n = dayPoints.length;
    dayPoints.forEach((p, i) => {
      const inSlotFrac = n > 1 ? i / (n - 1) : 0.5;
      result.push({
        xFrac: (slotIdx + inSlotFrac) / daySlotCount,
        pct:   ((p.close - base) / base) * 100,
      });
    });
  }
  return result;
}

// ── Formatting helpers ────────────────────────────────────────────────────────

function colorForAction(action) {
  if (action === "buy")  return new Color("#4ade80");
  if (action === "sell") return new Color("#f87171");
  return new Color("#94a3b8");
}

function fmtMoney(n) {
  if (n == null) return "—";
  return "$" + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function fmtPct(n) {
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function pctColor(n) {
  if (n == null) return new Color("#94a3b8");
  return n >= 0 ? new Color("#4ade80") : new Color("#f87171");
}

function addLabel(stack, text, size, color, bold) {
  const t = stack.addText(text);
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  return t;
}

// ── Chart drawing ─────────────────────────────────────────────────────────────
//
// All series share the same [{xFrac, pct}] format so the renderer doesn't
// need to know whether a series is an agent or the benchmark. Scriptable has
// no native dash-pattern support, so the dashed benchmark line is built by
// hand: alternate short runs of segments and gaps within a single Path.

function drawComparisonChart(series, width, height) {
  const ctx = new DrawContext();
  ctx.size = new Size(width, height);
  ctx.opaque = false;
  ctx.respectScreenScale = true;

  const all = series.flatMap((s) => s.points.map((p) => p.pct));
  if (!all.length) return null;
  const min   = Math.min(0, ...all);
  const max   = Math.max(0, ...all);
  const range = max - min || 1;
  const pad   = 4;

  function toY(v)    { return height - pad - ((v - min) / range) * (height - pad * 2); }
  function toX(xFrac) { return pad + xFrac * (width - pad * 2); }

  // zero line
  const zeroPath = new Path();
  zeroPath.move(new Point(0, toY(0)));
  zeroPath.addLine(new Point(width, toY(0)));
  ctx.setStrokeColor(new Color("#334155"));
  ctx.setLineWidth(1);
  ctx.addPath(zeroPath);
  ctx.strokePath();

  function plotSeries(points, color, lineWidth, dashed) {
    if (points.length < 2) return;
    const screenPts = points.map((p) => new Point(toX(p.xFrac), toY(p.pct)));
    const path = new Path();
    if (!dashed) {
      path.move(screenPts[0]);
      for (let i = 1; i < screenPts.length; i++) path.addLine(screenPts[i]);
    } else {
      const on = 2, off = 2, cycle = on + off;
      for (let i = 0; i < screenPts.length - 1; i++) {
        if (i % cycle < on) {
          path.move(screenPts[i]);
          path.addLine(screenPts[i + 1]);
        }
      }
    }
    ctx.setStrokeColor(color);
    ctx.setLineWidth(lineWidth);
    ctx.addPath(path);
    ctx.strokePath();
  }

  // Benchmark drawn first so agent lines sit on top
  for (const s of series) {
    plotSeries(s.points, s.color, s.lineWidth || 2, !!s.dashed);
  }

  return ctx.getImage();
}

// ── Single-agent detailed view ────────────────────────────────────────────────

async function buildSingleAgentWidget(w, agentId, family) {
  const scale = AGENT_DISPLAY_SCALE[agentId] || 1;
  const run   = await loadLatestRun(agentId);
  if (!run) {
    addLabel(w, `No runs logged for ${agentId}`, 13, Color.white(), false);
    return;
  }
  const decisions = await supabaseGet("trading_agent_decisions", {
    select:  "*",
    run_id:  `eq.${run.id}`,
    order:   "id.desc",
    limit:   "6",
  });

  const header = w.addStack();
  header.centerAlignContent();
  addLabel(header, (run.agent_id || agentId).toUpperCase(), 12, new Color("#64748b"), true);
  header.addSpacer();
  addLabel(
    header,
    run.market_open ? "● OPEN" : "● CLOSED",
    10,
    run.market_open ? new Color("#4ade80") : new Color("#64748b"),
    true
  );

  w.addSpacer(6);
  addLabel(w, fmtMoney(run.account_equity * scale), 28, Color.white(), true);

  const sub = w.addStack();
  addLabel(sub, `cash ${fmtMoney(run.account_cash * scale)}`, 11, new Color("#94a3b8"), false);
  sub.addSpacer(10);
  addLabel(sub, `${run.num_open_positions ?? "—"} open`, 11, new Color("#94a3b8"), false);

  if (run.error) {
    w.addSpacer(6);
    const err = w.addText(`⚠ ${run.error}`);
    err.font = Font.systemFont(10);
    err.textColor = new Color("#f87171");
    err.lineLimit = 2;
  }

  if (family !== "small") {
    w.addSpacer(8);
    if (run.overall_reasoning) {
      const reasoning = w.addText(run.overall_reasoning);
      reasoning.font = Font.italicSystemFont(11);
      reasoning.textColor = new Color("#cbd5e1");
      reasoning.lineLimit = family === "large" ? 3 : 2;
    }

    w.addSpacer(8);
    const maxRows = family === "large" ? 5 : 2;
    for (const d of decisions.slice(0, maxRows)) {
      const row = w.addStack();
      row.centerAlignContent();
      addLabel(row, d.action.toUpperCase(), 11, colorForAction(d.action), true);
      row.addSpacer(6);
      addLabel(row, d.symbol, 11, Color.white(), true);
      if (d.qty) {
        row.addSpacer(4);
        addLabel(row, `x${d.qty}`, 10, new Color("#64748b"), false);
      }
      row.addSpacer();
      addLabel(row, d.confidence || "", 9, new Color("#64748b"), false);
      if (family === "large") w.addSpacer(2);
    }
  }
}

// ── Comparison view ───────────────────────────────────────────────────────────

async function buildComparisonWidget(w, family) {
  const historyLimit = 2000;

  const [
    plutusRun, heliosRun, hermesRun,
    plutusHistory, heliosHistory, hermesHistory,
  ] = await Promise.all([
    loadLatestRun("plutus"),
    loadLatestRun("helios"),
    loadLatestRun("hermes"),
    loadEquityHistory("plutus",  historyLimit),
    loadEquityHistory("helios",  historyLimit),
    loadEquityHistory("hermes",  historyLimit),
  ]);

  addLabel(w, "PAPER TRADING · PLUTUS · HELIOS · HERMES", 9, new Color("#64748b"), true);
  w.addSpacer(6);

  // ── Agent header rows ────────────────────────────────────────────────────────
  // % here is all-time: (latest equity - first equity) / first equity.
  // This matches the "Since first log" tab total return on the dashboard.
  function lastPct(history) {
    const base = history[0]?.equity;
    const last = history[history.length - 1]?.equity;
    if (!base || !last) return null;
    return ((last - base) / base) * 100;
  }

  function agentRow(agentId, run, changePct) {
    const scale = AGENT_DISPLAY_SCALE[agentId] || 1;
    const row   = w.addStack();
    row.centerAlignContent();
    const dot = row.addText("●");
    dot.font = Font.boldSystemFont(11);
    dot.textColor = AGENT_COLORS[agentId];
    row.addSpacer(4);
    addLabel(row, AGENT_LABELS[agentId], 12, Color.white(), true);
    row.addSpacer();
    const equity = run ? run.account_equity * scale : null;
    addLabel(row, fmtMoney(equity),   12, new Color("#cbd5e1"), false);
    row.addSpacer(8);
    addLabel(row, fmtPct(changePct),  11, pctColor(changePct), true);
  }

  agentRow("plutus", plutusRun, lastPct(plutusHistory));
  w.addSpacer(2);
  agentRow("helios", heliosRun, lastPct(heliosHistory));
  w.addSpacer(2);
  agentRow("hermes", hermesRun, lastPct(hermesHistory));

  if (family === "small") return;

  // ── Chart (medium / large) ───────────────────────────────────────────────────

  w.addSpacer(6);

  // Date range — anchor to agents with meaningful history so a new agent
  // doesn't pull the VTI start date forward to today.
  const MIN_BENCHMARK_RUNS = 50;
  const allHistories = [plutusHistory, heliosHistory, hermesHistory];
  const anchorHistories = allHistories.filter((h) => h.length >= MIN_BENCHMARK_RUNS);
  const rangeSource = anchorHistories.length ? anchorHistories : allHistories;
  const allTimes = rangeSource
    .flatMap((h) => h.map((r) => new Date(r.run_at).getTime()))
    .filter((t) => !isNaN(t));
  const rangeStartMs   = allTimes.length ? Math.min(...allTimes) : Date.now() - 30 * 24 * 60 * 60 * 1000;
  const rangeStartDate = new Date(rangeStartMs).toISOString().slice(0, 10);

  // VTI from Supabase (same source as the web dashboard)
  let rawVTI = [];
  try {
    rawVTI = await loadBenchmarkPrices(rangeStartDate);
  } catch (e) {
    console.error("Supabase VTI fetch failed: " + e);
  }

  // Build shared day-slot index — each unique calendar date that appears in
  // any agent's run history or VTI data gets one slot with equal width.
  const allDateStrs = new Set();
  for (const history of allHistories) {
    for (const r of history) if (r.equity != null) allDateStrs.add(r.run_at.slice(0, 10));
  }
  for (const p of rawVTI) allDateStrs.add(p.date.slice(0, 10));
  const daySlots     = Array.from(allDateStrs).sort();
  const daySlotIndex = Object.fromEntries(daySlots.map((d, i) => [d, i]));
  const daySlotCount = daySlots.length || 1;

  // Build series using day-slot xFrac — exactly as the dashboard does
  const plutusPct    = buildPerAgentPctSeriesDaySlot(plutusHistory,  daySlotIndex, daySlotCount);
  const heliosPct    = buildPerAgentPctSeriesDaySlot(heliosHistory,  daySlotIndex, daySlotCount);
  const hermesPct    = buildPerAgentPctSeriesDaySlot(hermesHistory,  daySlotIndex, daySlotCount);
  const benchmarkPct = buildBenchmarkPctSeriesDaySlot(rawVTI, rangeStartMs, daySlotIndex, daySlotCount);

  const chartWidth  = family === "large" ? 290 : 260;
  const chartHeight = family === "large" ? 80  : 48;

  const series = [];
  if (benchmarkPct.length >= 2) {
    series.push({ points: benchmarkPct, color: BENCHMARK_COLOR, lineWidth: 1.5, dashed: true });
  }
  if (plutusPct.length >= 2) series.push({ points: plutusPct, color: AGENT_COLORS.plutus, lineWidth: 2 });
  if (heliosPct.length >= 2) series.push({ points: heliosPct, color: AGENT_COLORS.helios, lineWidth: 2 });
  if (hermesPct.length >= 2) series.push({ points: hermesPct, color: AGENT_COLORS.hermes, lineWidth: 2 });

  const image = drawComparisonChart(series, chartWidth, chartHeight);
  if (image) {
    const iw = w.addImage(image);
    iw.imageSize = new Size(chartWidth, chartHeight);
  }

  const caption = w.addText(
    "white=Plutus  gray=Helios  green=Hermes" +
    (benchmarkPct.length >= 2 ? "  dashed=VTI" : "")
  );
  caption.font      = Font.systemFont(8);
  caption.textColor = new Color("#475569");

  // Latest decisions (large only)
  if (family === "large") {
    w.addSpacer(6);
    const [plutusDecision, heliosDecision, hermesDecision] = await Promise.all([
      loadLatestDecision("plutus"),
      loadLatestDecision("helios"),
      loadLatestDecision("hermes"),
    ]);
    function decisionLine(agentId, d) {
      if (!d) return;
      const row = w.addStack();
      row.centerAlignContent();
      addLabel(row, AGENT_LABELS[agentId], 10, AGENT_COLORS[agentId], true);
      row.addSpacer(4);
      addLabel(row, d.action.toUpperCase(), 10, colorForAction(d.action), true);
      row.addSpacer(4);
      addLabel(row, d.symbol, 10, Color.white(), true);
    }
    decisionLine("plutus", plutusDecision);
    decisionLine("helios", heliosDecision);
    decisionLine("hermes", hermesDecision);
  }
}

// ── Entry point ───────────────────────────────────────────────────────────────

async function buildWidget() {
  // Fire-and-forget update check: runs in parallel with data fetch so it
  // doesn't slow down the widget. Result is written to disk; takes effect
  // on the next refresh cycle.
  await checkForUpdate();

  const w = new ListWidget();
  w.backgroundColor = new Color("#0b1120");
  w.setPadding(14, 14, 14, 14);
  if (DASHBOARD_URL) w.url = DASHBOARD_URL;

  const family = config.widgetFamily || "medium";

  try {
    if (agentFilter === "plutus" || agentFilter === "helios" || agentFilter === "hermes") {
      await buildSingleAgentWidget(w, agentFilter, family);
    } else {
      await buildComparisonWidget(w, family);
    }
  } catch (e) {
    console.error(e);
    addLabel(w, "Error:", 12, new Color("#f87171"), true);
    const errText = w.addText(String(e.message || e));
    errText.font      = Font.systemFont(10);
    errText.textColor = new Color("#f87171");
    errText.lineLimit = 5;
  }

  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  return w;
}

const widget = await buildWidget();
if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();

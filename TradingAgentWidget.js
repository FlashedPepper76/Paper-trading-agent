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
// The comparison chart plots a dashed gray reference line for VTI (total
// U.S. stock market) over the same calendar stretch as the agents' full run
// history, so you can tell agent underperformance from a down market —
// same approach as the web dashboard's /compare page.
// Note: Hermes started at $10k (vs $100k for Plutus/Helios). Dollar amounts
// for Hermes are scaled ×10 in the widget so all three agents are comparable
// on screen. Return % is unaffected by the scale.

const SUPABASE_URL = "https://edmysxanjsskjrdfkmaw.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVkbXlzeGFuanNza2pyZGZrbWF3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI0ODA3ODAsImV4cCI6MjA5ODA1Njc4MH0.gebHV4qcqPxdFAvwqEgtGKTS_u_PhzgznXqnEqXmBoA";
const DASHBOARD_URL = "https://trading-agent-dashboard-mu.vercel.app";

// Three luminance levels for color-filter-safe readability, plus green for
// Hermes which is distinct enough in both color and luminance from the others.
const AGENT_COLORS = {
  plutus: new Color("#ffffff"), // white — brightest line
  helios: new Color("#9ca3af"), // mid-gray
  hermes: new Color("#34d399"), // emerald green
};
const AGENT_LABELS = { plutus: "Plutus", helios: "Helios", hermes: "Hermes" };
// Hermes paper account is $10k; multiply by 10 for apples-to-apples display
// alongside Plutus/Helios ($100k). % return is unaffected — scale it only
// when showing raw dollar amounts (equity, cash).
const AGENT_DISPLAY_SCALE = { plutus: 1, helios: 1, hermes: 10 };

const BENCHMARK_SYMBOL = "VTI"; // total U.S. stock market, not just large-caps
const BENCHMARK_COLOR = new Color("#475569"); // darkest of the three, dashed

const agentFilter = args.widgetParameter ? args.widgetParameter.trim().toLowerCase() : null;

// --------------------------------------------------------------------------
// Supabase helpers
// --------------------------------------------------------------------------

function buildQuery(params) {
  return Object.entries(params)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
}

async function supabaseGet(table, params) {
  const qs = buildQuery(params);
  const req = new Request(`${SUPABASE_URL}/rest/v1/${table}?${qs}`);
  req.headers = {
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
  };
  const json = await req.loadJSON();
  const status = req.response ? req.response.statusCode : null;
  if (status && status >= 400) {
    const msg = (json && (json.message || json.error || json.hint)) || JSON.stringify(json);
    throw new Error(`[${table}] HTTP ${status}: ${msg}`);
  }
  return json;
}

async function loadLatestRun(agentId) {
  const runs = await supabaseGet("trading_agent_runs", {
    select: "*",
    agent_id: `eq.${agentId}`,
    order: "id.desc",
    limit: "1",
  });
  return runs && runs[0];
}

// Returns [{ run_at, equity }, ...] oldest -> newest. limit is set high
// enough upstream to cover the agent's entire run history, same as the
// dashboard's compare page (getRuns(2000, ...)).
async function loadEquityHistory(agentId, limit) {
  const rows = await supabaseGet("trading_agent_runs", {
    select: "id,account_equity,run_at",
    agent_id: `eq.${agentId}`,
    order: "id.desc",
    limit: String(limit),
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
    select: "*",
    run_id: `eq.${run.id}`,
    order: "id.desc",
    limit: "1",
  });
  return decisions && decisions[0];
}

// --------------------------------------------------------------------------
// Benchmark (total market) helpers — Yahoo's chart endpoint, no key needed
// --------------------------------------------------------------------------

// Yahoo only serves fine-grained intraday bars for recent history (5-minute
// bars cover up to ~6 days, 30-minute up to ~55 days, hourly up to ~2
// years). Pick the finest interval the range allows so the benchmark line
// shows real intraday movement instead of a flat step between day closes.
function pickInterval(period1Sec, period2Sec) {
  const days = (period2Sec - period1Sec) / 86400;
  if (days <= 6) return "5m";
  if (days <= 55) return "30m";
  if (days <= 700) return "60m";
  return "1d";
}

async function fetchBenchmarkSeries(symbol, rangeStartMs, rangeEndMs) {
  const period1 = Math.floor(rangeStartMs / 1000) - 86400;
  const period2 = Math.floor(rangeEndMs / 1000);

  // ---- stooq.com (primary) ----
  // Returns CSV: Date,Open,High,Low,Close,Volume — reliable from iOS.
  try {
    const fmt = (sec) => {
      const d = new Date(sec * 1000);
      return `${d.getUTCFullYear()}${String(d.getUTCMonth()+1).padStart(2,"0")}${String(d.getUTCDate()).padStart(2,"0")}`;
    };
    const url = `https://stooq.com/q/d/l/?s=${encodeURIComponent(symbol.toLowerCase())}.us&d1=${fmt(period1)}&d2=${fmt(period2)}&i=d`;
    const req = new Request(url);
    req.headers = { "User-Agent": "Mozilla/5.0 (compatible; CommandDeckWidget/1.0)" };
    const csv = await req.loadString();
    if (csv && !csv.trim().startsWith("No data") && csv.trim().length > 20) {
      const lines = csv.trim().split("\n");
      const points = [];
      for (const line of lines.slice(1)) {
        const cols = line.split(",");
        if (cols.length < 5) continue;
        const t = new Date(cols[0].trim() + "T12:00:00Z").getTime();
        const c = parseFloat(cols[4]);
        if (!isNaN(t) && !isNaN(c)) points.push({ t, close: c });
      }
      if (points.length > 1) return points;
    }
  } catch (e) {
    console.error("stooq fetch failed: " + e);
  }

  // ---- Yahoo Finance (fallback) ----
  const interval = pickInterval(period1, period2);
  for (const host of ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]) {
    try {
      const url =
        `https://${host}/v8/finance/chart/${encodeURIComponent(symbol)}` +
        `?period1=${period1}&period2=${period2}&interval=${interval}&includePrePost=false`;
      const req = new Request(url);
      req.headers = {
        "User-Agent":
          "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) " +
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        Accept: "application/json",
      };
      const json = await req.loadJSON();
      const result = json && json.chart && json.chart.result && json.chart.result[0];
      if (!result) continue;
      const timestamps = result.timestamp || [];
      const closes =
        (result.indicators && result.indicators.quote &&
         result.indicators.quote[0] && result.indicators.quote[0].close) || [];
      const points = [];
      for (let i = 0; i < timestamps.length; i++) {
        const c = closes[i];
        if (c != null) points.push({ t: timestamps[i] * 1000, close: c });
      }
      if (points.length > 1) return points;
    } catch (e) {
      console.error(`Yahoo ${host} failed: ${e}`);
    }
  }
  return [];
}

// --------------------------------------------------------------------------
// Formatting helpers
// --------------------------------------------------------------------------

function colorForAction(action) {
  if (action === "buy") return new Color("#4ade80");
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

// --------------------------------------------------------------------------
// Chart drawing
// --------------------------------------------------------------------------
//
// Every series is plotted off its own list of { xFrac, pct } points, where
// xFrac (0..1) is each point's horizontal position. Agent series get an
// index-based xFrac (their own run history, oldest -> newest, no calendar
// alignment between agents — same as before). The benchmark series gets a
// calendar-time-based xFrac instead, since it has one point per
// interval-bar rather than one per agent run. Drawing everything off xFrac
// lets all three lines share one chart without the renderer needing to
// know which kind of series it's looking at.

// equity history ([{run_at, equity}], oldest -> newest) -> [{xFrac, pct}]
function toPctXFracSeries(history) {
  if (!history.length) return [];
  const base = history[0].equity;
  if (!base) return [];
  const n = history.length;
  return history.map((h, i) => ({
    xFrac: n > 1 ? i / (n - 1) : 0.5,
    pct: ((h.equity - base) / base) * 100,
  }));
}

// raw benchmark points ([{t, close}]) -> [{xFrac, pct}], xFrac by calendar
// time within [rangeStartMs, rangeEndMs].
function toBenchmarkXFracSeries(points, rangeStartMs, rangeEndMs) {
  if (!points.length) return [];
  const sorted = points.slice().sort((a, b) => a.t - b.t);
  const base = sorted[0].close;
  const span = rangeEndMs - rangeStartMs || 1;
  return sorted.map((p) => ({
    xFrac: Math.max(0, Math.min(1, (p.t - rangeStartMs) / span)),
    pct: ((p.close - base) / base) * 100,
  }));
}

// series: [{ points: [{xFrac, pct}], color, lineWidth?, dashed? }]
//
// Scriptable's DrawContext has no native dash-pattern support, so a dashed
// line is built by hand: walk the point list and only draw every other
// short run of segments, leaving gaps in between (a single Path can hold
// multiple disconnected move/addLine runs — stroking it draws them all).
function drawComparisonChart(series, width, height) {
  const ctx = new DrawContext();
  ctx.size = new Size(width, height);
  ctx.opaque = false;
  ctx.respectScreenScale = true;

  const all = series.flatMap((s) => s.points.map((p) => p.pct));
  if (!all.length) return null;
  const min = Math.min(0, ...all);
  const max = Math.max(0, ...all);
  const range = max - min || 1;
  const pad = 4;

  function toY(v) {
    return height - pad - ((v - min) / range) * (height - pad * 2);
  }
  function toX(xFrac) {
    return pad + xFrac * (width - pad * 2);
  }

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
      const onSegments = 2;
      const offSegments = 2;
      const cycle = onSegments + offSegments;
      for (let i = 0; i < screenPts.length - 1; i++) {
        if (i % cycle < onSegments) {
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

  // benchmark drawn first so the agent lines sit on top of it
  for (const s of series) {
    plotSeries(s.points, s.color, s.lineWidth || 2, !!s.dashed);
  }

  return ctx.getImage();
}

// --------------------------------------------------------------------------
// Single-agent detailed view (unchanged from before)
// --------------------------------------------------------------------------

async function buildSingleAgentWidget(w, agentId, family) {
  const scale = AGENT_DISPLAY_SCALE[agentId] || 1;
  const run = await loadLatestRun(agentId);
  if (!run) {
    addLabel(w, `No runs logged for ${agentId}`, 13, Color.white(), false);
    return;
  }
  const decisions = await supabaseGet("trading_agent_decisions", {
    select: "*",
    run_id: `eq.${run.id}`,
    order: "id.desc",
    limit: "6",
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

// --------------------------------------------------------------------------
// Dual-agent comparison view
// --------------------------------------------------------------------------

async function buildComparisonWidget(w, family) {
  // Full run history — same as the web dashboard's /compare page.
  const historyLimit = 2000;

  const [
    plutusRun, heliosRun, hermesRun,
    plutusHistory, heliosHistory, hermesHistory,
  ] = await Promise.all([
    loadLatestRun("plutus"),
    loadLatestRun("helios"),
    loadLatestRun("hermes"),
    loadEquityHistory("plutus", historyLimit),
    loadEquityHistory("helios", historyLimit),
    loadEquityHistory("hermes", historyLimit),
  ]);

  const plutusPct  = toPctXFracSeries(plutusHistory);
  const heliosPct  = toPctXFracSeries(heliosHistory);
  const hermesPct  = toPctXFracSeries(hermesHistory);
  const plutusChange = plutusPct.length  ? plutusPct[plutusPct.length - 1].pct   : null;
  const heliosChange = heliosPct.length  ? heliosPct[heliosPct.length - 1].pct   : null;
  const hermesChange = hermesPct.length  ? hermesPct[hermesPct.length - 1].pct   : null;

  addLabel(w, "PAPER TRADING · PLUTUS · HELIOS · HERMES", 9, new Color("#64748b"), true);
  w.addSpacer(6);

  // agentRow: scale is applied only to the dollar display, not the % return.
  function agentRow(agentId, run, changePct) {
    const scale = AGENT_DISPLAY_SCALE[agentId] || 1;
    const row = w.addStack();
    row.centerAlignContent();
    const dot = row.addText("●");
    dot.font = Font.boldSystemFont(11);
    dot.textColor = AGENT_COLORS[agentId];
    row.addSpacer(4);
    addLabel(row, AGENT_LABELS[agentId], 12, Color.white(), true);
    row.addSpacer();
    const equity = run ? run.account_equity * scale : null;
    addLabel(row, fmtMoney(equity), 12, new Color("#cbd5e1"), false);
    row.addSpacer(8);
    addLabel(row, fmtPct(changePct), 11, pctColor(changePct), true);
  }

  agentRow("plutus", plutusRun,  plutusChange);
  w.addSpacer(2);
  agentRow("helios", heliosRun,  heliosChange);
  w.addSpacer(2);
  agentRow("hermes", hermesRun,  hermesChange);

  if (family !== "small") {
    w.addSpacer(6);

    // VTI date range anchored to established agents (50+ runs) so a brand-new
    // agent doesn't shift the benchmark start to today. All agents are still
    // drawn in the chart regardless of run count.
    const MIN_BENCHMARK_RUNS = 50;
    const anchorHistories = [plutusHistory, heliosHistory, hermesHistory]
      .filter((h) => h.length >= MIN_BENCHMARK_RUNS);
    const rangeSource = anchorHistories.length ? anchorHistories : [plutusHistory, heliosHistory, hermesHistory];
    const allTimes = rangeSource
      .flatMap((h) => h.map((r) => new Date(r.run_at).getTime()))
      .filter((t) => !isNaN(t));
    const rangeStartMs = allTimes.length
      ? Math.min(...allTimes)
      : Date.now() - 30 * 24 * 60 * 60 * 1000;
    const rangeEndMs = Date.now();

    let benchmarkPct = [];
    try {
      const raw = await fetchBenchmarkSeries(BENCHMARK_SYMBOL, rangeStartMs, rangeEndMs);
      benchmarkPct = toBenchmarkXFracSeries(raw, rangeStartMs, rangeEndMs);
    } catch (e) {
      console.error("benchmark fetch failed: " + e);
      benchmarkPct = [];
    }

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
    caption.font = Font.systemFont(8);
    caption.textColor = new Color("#475569");
  }

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

// --------------------------------------------------------------------------
// Entry point
// --------------------------------------------------------------------------

async function buildWidget() {
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
    addLabel(w, "Supabase error:", 12, new Color("#f87171"), true);
    const errText = w.addText(String(e.message || e));
    errText.font = Font.systemFont(10);
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

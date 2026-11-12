// Admin-only "AI Lab": how the model was trained, what it is thinking right now and why,
// and how its predictions turned out (training backtest + its own live trades).
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

const REFRESH_MS = 20000;

const pct = (v, d = 2) => (v == null || Number.isNaN(v) ? "–" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}%`);
const rs = (v) => (v == null ? "–" : `${v < 0 ? "−" : v > 0 ? "+" : ""}₹${Math.abs(Math.round(v)).toLocaleString("en-IN")}`);
const signClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

const FEATURE_INFO = {
  ret_1: "NIFTY return over the last 1 minute",
  ret_3: "NIFTY return over the last 3 minutes",
  ret_5: "NIFTY return over the last 5 minutes",
  ret_10: "NIFTY return over the last 10 minutes",
  ret_15: "NIFTY return over the last 15 minutes",
  ret_30: "NIFTY return over the last 30 minutes",
  vol_15: "Volatility of 1-min returns, last 15 min",
  vol_30: "Volatility of 1-min returns, last 30 min",
  range_15: "High-low range of the last 15 minutes",
  rsi_14: "RSI (14 minutes)",
  ema9_dist: "Distance of price from its 9-min EMA",
  ema21_dist: "Distance of price from its 21-min EMA",
  ema9_21: "9-min EMA vs 21-min EMA (trend)",
  ret_from_open: "Move since 09:15 open",
  gap: "Opening gap vs yesterday's close",
  prev_day_ret: "Yesterday's NIFTY return",
  day_range: "Today's high-low range so far",
  pos_in_range: "Where price sits in today's range (0 = low, 1 = high)",
  mins_since_open: "Minutes since market open",
  weekday: "Day of the week (0 = Monday)",
  days_to_expiry: "Days until the weekly option expiry",
  is_expiry_day: "1 on expiry day",
  ce_prem: "ATM call premium / spot",
  pe_prem: "ATM put premium / spot",
  straddle: "ATM straddle price / spot (implied move)",
  ce_pe_skew: "Call vs put premium imbalance",
  straddle_chg_5: "Straddle change over 5 minutes",
  straddle_chg_15: "Straddle change over 15 minutes",
};

// --- chart primitives ----------------------------------------------------------------

function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

/** Multi-series line chart with a crosshair tooltip, direct end labels and optional reference lines. */
function LineChart({ x, series, height = 220, yFmt = (v) => v, xFmt = (v) => v, refs = [], area = false }) {
  const [ref, W] = useWidth();
  const [hover, setHover] = useState(null);
  const labelled = series.length > 1;
  const P = { l: 52, r: labelled ? 78 : 14, t: 12, b: 26 };
  const all = series.flatMap((s) => s.values.filter((v) => v != null)).concat(refs.map((r) => r.y));
  if (!all.length || !x.length) return <div ref={ref} className="lab-empty">No data yet</div>;
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.08;
  lo -= pad; hi += pad;
  const ticks = niceTicks(lo, hi);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const iw = W - P.l - P.r, ih = height - P.t - P.b;
  const sx = (i) => P.l + (x.length === 1 ? iw / 2 : (i / (x.length - 1)) * iw);
  const sy = (v) => P.t + (1 - (v - lo) / (hi - lo)) * ih;
  const path = (vals) => vals.map((v, i) => (v == null ? null : `${sx(i)},${sy(v)}`)).filter(Boolean).join(" L");

  // direct labels at the line ends, nudged apart so they never overlap
  const ends = series.map((s) => {
    let i = s.values.length - 1;
    while (i > 0 && s.values[i] == null) i--;
    return { name: s.name, color: s.color, y: sy(s.values[i] ?? lo) };
  }).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;

  const xTicks = [0, Math.floor((x.length - 1) / 2), x.length - 1].filter((v, i, a) => a.indexOf(v) === i);
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - P.l) / iw) * (x.length - 1));
    setHover(Math.max(0, Math.min(x.length - 1, i)));
  };

  return (
    <div ref={ref} className="lab-chart">
      <svg width={W} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        onTouchStart={(e) => onMove(e.touches[0] ? { ...e, clientX: e.touches[0].clientX, currentTarget: e.currentTarget } : e)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={P.l} x2={W - P.r} y1={sy(t)} y2={sy(t)} className="lab-grid" />
            <text x={P.l - 6} y={sy(t) + 4} textAnchor="end" className="lab-axis">{yFmt(t)}</text>
          </g>
        ))}
        {lo < 0 && hi > 0 && <line x1={P.l} x2={W - P.r} y1={sy(0)} y2={sy(0)} className="lab-zero" />}
        {xTicks.map((i) => (
          <text key={i} x={sx(i)} y={height - 6} textAnchor={i === 0 ? "start" : i === x.length - 1 ? "end" : "middle"} className="lab-axis">{xFmt(x[i])}</text>
        ))}
        {refs.map((r) => (
          <g key={r.label}>
            <line x1={P.l} x2={W - P.r} y1={sy(r.y)} y2={sy(r.y)} className="lab-ref" />
            <text x={P.l + 4} y={sy(r.y) - 4} className="lab-ref-label">{r.label}</text>
          </g>
        ))}
        {series.map((s) => (
          <g key={s.name}>
            {area && <path d={`M${sx(0)},${sy(Math.max(lo, 0))} L${path(s.values)} L${sx(s.values.length - 1)},${sy(Math.max(lo, 0))} Z`} fill={s.color} opacity="0.12" />}
            <path d={`M${path(s.values)}`} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
          </g>
        ))}
        {labelled && ends.map((e) => (
          <text key={e.name} x={W - P.r + 8} y={e.y + 4} className="lab-endlabel">
            <tspan fill={e.color}>●</tspan> {e.name}
          </text>
        ))}
        {hover != null && (
          <g>
            <line x1={sx(hover)} x2={sx(hover)} y1={P.t} y2={P.t + ih} className="lab-crosshair" />
            {series.map((s) => s.values[hover] != null && (
              <circle key={s.name} cx={sx(hover)} cy={sy(s.values[hover])} r="4" fill={s.color} className="lab-dot" />
            ))}
          </g>
        )}
      </svg>
      {hover != null && (
        <div className="lab-tip" style={{ left: Math.min(sx(hover) + 12, W - 170), top: 8 }}>
          <div className="lab-tip-title">{xFmt(x[hover])}</div>
          {series.map((s) => (
            <div key={s.name} className="lab-tip-row">
              <span><i style={{ background: s.color }} />{s.name}</span><b>{s.values[hover] == null ? "–" : yFmt(s.values[hover])}</b>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Vertical bars (one or two series per group), zero-anchored, with per-group hover tooltip. */
function BarChart({ groups, series, height = 220, yFmt = (v) => v, colorBySign = false, tip }) {
  const [ref, W] = useWidth();
  const [hover, setHover] = useState(null);
  const P = { l: 56, r: 10, t: 12, b: 26 };
  const vals = groups.flatMap((g) => series.map((s) => g[s.key] ?? 0)).concat([0]);
  if (!groups.length) return <div ref={ref} className="lab-empty">No data yet</div>;
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const lo = Math.min(ticks[0], ...vals), hi = Math.max(ticks[ticks.length - 1], ...vals);
  const iw = W - P.l - P.r, ih = height - P.t - P.b;
  const sy = (v) => P.t + (1 - (v - lo) / (hi - lo || 1)) * ih;
  const gw = iw / groups.length;
  const bw = Math.max(2, Math.min(28, (gw - 4) / series.length - 2));
  const every = Math.ceil(groups.length / Math.max(2, Math.floor(iw / 64)));
  return (
    <div ref={ref} className="lab-chart">
      <svg width={W} height={height} onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={P.l} x2={W - P.r} y1={sy(t)} y2={sy(t)} className="lab-grid" />
            <text x={P.l - 6} y={sy(t) + 4} textAnchor="end" className="lab-axis">{yFmt(t)}</text>
          </g>
        ))}
        {groups.map((g, gi) => {
          const cx = P.l + gw * gi + gw / 2;
          const x0 = cx - (series.length * (bw + 2) - 2) / 2;
          return (
            <g key={gi} onMouseEnter={() => setHover(gi)} onTouchStart={() => setHover(gi)}>
              <rect x={P.l + gw * gi} y={P.t} width={gw} height={ih} fill="transparent" />
              {series.map((s, si) => {
                const v = g[s.key] ?? 0;
                const y1 = sy(Math.max(v, 0)), y2 = sy(Math.min(v, 0));
                const color = colorBySign ? (v >= 0 ? "var(--color-green)" : "var(--color-red)") : s.color;
                return <rect key={s.key} x={x0 + si * (bw + 2)} y={y1} width={bw} height={Math.max(1, y2 - y1)} rx={Math.min(3, bw / 2)}
                  fill={color} opacity={hover == null || hover === gi ? 1 : 0.45} />;
              })}
              {gi % every === 0 && <text x={cx} y={height - 6} textAnchor="middle" className="lab-axis">{g.label}</text>}
            </g>
          );
        })}
        <line x1={P.l} x2={W - P.r} y1={sy(0)} y2={sy(0)} className="lab-zero" />
      </svg>
      {hover != null && (
        <div className="lab-tip" style={{ left: Math.min(P.l + gw * hover + gw / 2 + 10, W - 190), top: 8 }}>
          <div className="lab-tip-title">{groups[hover].label}</div>
          {tip ? tip(groups[hover]) : series.map((s) => (
            <div key={s.key} className="lab-tip-row"><span><i style={{ background: s.color }} />{s.name}</span><b>{yFmt(groups[hover][s.key])}</b></div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Horizontal bars that can go negative (left, loss) or positive (right, profit) from a centre line. */
function DivergingBars({ rows, fmt = (v) => v }) {
  const max = Math.max(1e-9, ...rows.map((r) => Math.abs(r.value)));
  return (
    <div className="lab-dbars">
      {rows.map((r) => (
        <div key={r.label} className="lab-dbar" title={r.hint || ""}>
          <div className="lab-dbar-label"><span>{r.label}</span>{r.sub != null && <small>{r.sub}</small>}</div>
          <div className="lab-dbar-track">
            <div className={`lab-dbar-fill ${r.value >= 0 ? "pos" : "neg"}`}
              style={{ width: `${(Math.abs(r.value) / max) * 50}%`, [r.value >= 0 ? "left" : "right"]: "50%" }} />
            <span className="lab-dbar-mid" />
          </div>
          <div className={`lab-dbar-val ${signClass(r.value)}`}>{fmt(r.value)}</div>
        </div>
      ))}
    </div>
  );
}

function Seg({ value, options, onChange }) {
  return (
    <div className="lab-seg" role="tablist">
      {options.map((o) => (
        <button key={o.value} className={value === o.value ? "on" : ""} onClick={() => onChange(o.value)} role="tab" aria-selected={value === o.value}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Card({ title, sub, right, children, className = "" }) {
  return (
    <section className={`glass-panel lab-card ${className}`}>
      <header className="lab-card-head">
        <div><h3>{title}</h3>{sub && <p>{sub}</p>}</div>
        {right}
      </header>
      {children}
    </section>
  );
}

function Stat({ label, value, tone, hint }) {
  return (
    <div className="lab-stat" title={hint || ""}>
      <span>{label}</span>
      <b className={tone || ""}>{value}</b>
    </div>
  );
}

function TreeNode({ node }) {
  if (!node) return null;
  if (node.leaf != null || !node.feature) {
    return <div className={`lab-leaf ${signClass(node.leaf ?? node.value)}`}>predict {pct(node.leaf ?? node.value, 2)}{node.count ? <small> · {node.count.toLocaleString()} samples</small> : null}</div>;
  }
  return (
    <div className="lab-node">
      <div className="lab-split" title={FEATURE_INFO[node.feature] || ""}>
        <b>{node.feature}</b> ≤ {node.threshold}
        <small> · avg {pct(node.value, 2)} · {node.count?.toLocaleString()} samples</small>
      </div>
      {node.left || node.right ? (
        <div className="lab-branches">
          <div><span className="lab-branch-tag">yes</span><TreeNode node={node.left} /></div>
          <div><span className="lab-branch-tag">no</span><TreeNode node={node.right} /></div>
        </div>
      ) : <div className="lab-leaf muted">… deeper levels</div>}
    </div>
  );
}

// --- page ---------------------------------------------------------------------------

export default function AILab({ apiBase, token }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [updated, setUpdated] = useState(null);
  const [side, setSide] = useState("ce");
  const [whySide, setWhySide] = useState(null);
  const [split, setSplit] = useState("test");
  const [showTable, setShowTable] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/api/v1/admin/ai-lab`, { headers: { Authorization: `Bearer ${token}` } });
        if (!res.ok) throw new Error(res.status === 403 ? "This page is for admins only." : `Could not load (${res.status})`);
        const body = await res.json();
        if (alive) { setData(body); setErr(""); setUpdated(new Date()); }
      } catch (e) {
        if (alive) setErr(e.message);
      }
    };
    load();
    const id = setInterval(() => document.visibilityState === "visible" && load(), REFRESH_MS);
    return () => { alive = false; clearInterval(id); };
  }, [apiBase, token]);

  const report = data?.report;
  const live = data?.live;
  const model = report?.model;

  const curve = report?.learning_curves?.[side];
  const importance = useMemo(() => (report?.importance?.[side] || []).slice(0, 14), [report, side]);
  const calib = report?.calibration?.[split] || [];
  const wfMonths = report?.walk_forward?.months || [];
  const wfCum = useMemo(() => {
    let c = 0;
    return wfMonths.map((m) => (c += m.pnl_rs));
  }, [wfMonths]);
  const scoreLog = data?.score_log || [];
  const activeWhy = whySide || (live?.best_side || "CE").toLowerCase();
  const why = live?.explain?.[activeWhy];
  const journal = data?.journal || [];
  const closed = journal.filter((t) => t.status === "closed");
  const rightCalls = closed.filter((t) => t.model_was_right).length;

  if (err && !data) return <div className="glass-panel lab-card"><p className="lab-muted">{err}</p></div>;
  if (!data) return <div className="glass-panel lab-card"><p className="lab-muted">Loading the AI Lab…</p></div>;

  const vizCE = "var(--viz-1)", vizPE = "var(--viz-2)", vizTest = "var(--viz-3)";

  return (
    <div className="ailab">
      <div className="lab-hero">
        <div>
          <h2>🧠 AI Lab <span className="lab-badge">admin</span></h2>
          <p>
            How the trading model learned, what it is thinking right now, and how its predictions of profit and loss
            turned out. Updates every {REFRESH_MS / 1000}s{updated ? ` · last ${updated.toLocaleTimeString()}` : ""}
            {err ? ` · ${err}` : ""}.
          </p>
        </div>
        <div className="lab-hero-stats">
          <Stat label="Model" value={data.deployed ? "Deployed" : "Not deployed"} tone={data.deployed ? "pos" : "neg"} />
          <Stat label="Trained on data until" value={data.meta?.train_end || "–"} />
          <Stat label="Market feed" value={data.market_source === "upstox" ? "Upstox live" : "Simulator"} />
        </div>
      </div>

      {/* How it learns */}
      <Card title="How the model learns" sub="Reward-driven learning: every past trade's net profit or loss is the score it learns from.">
        <ol className="lab-flow">
          <li><b>1 · Watch</b><span>Every closed NIFTY minute becomes {model?.features?.length || 28} numbers (trend, volatility, gap, option premiums, days to expiry…).</span></li>
          <li><b>2 · Imagine the trade</b><span>For every minute in {(report?.data?.total_days || 0).toLocaleString()} past days it simulates buying the ATM call and the ATM put, exiting by the rule (target +{model?.exit_rule?.target_pct}% / stop −{model?.exit_rule?.stop_pct}% / day end) and paying costs.</span></li>
          <li><b>3 · Reward</b><span>The net % profit of that imagined trade is the reward. Profitable setups pull predictions up (+ points), losing setups pull them down (learning from losses).</span></li>
          <li><b>4 · Learn</b><span>Two models (call, put) of {model?.trees?.ce}+{model?.trees?.pe} decision trees are built one tree at a time, each tree correcting the previous trees' mistakes, until the validation error stops improving.</span></li>
          <li><b>5 · Decide</b><span>Live, it predicts the reward of buying CE and PE right now and buys the better one only if it beats the {pct(model?.threshold_pct, 3)} threshold.</span></li>
          <li><b>6 · Feedback</b><span>Each live AI trade is journaled with its prediction and real result (bottom of this page). Retraining with new data folds those outcomes back in.</span></li>
        </ol>
        <p className="lab-note">
          Honest note: this is supervised learning on rewards, not online reinforcement learning. The deployed model does
          not change during the day; it improves only when you retrain it on more data (see "Train with more data").
        </p>
      </Card>

      {/* Live brain */}
      <div className="lab-grid2">
        <Card title="What it thinks right now" sub={live?.status === "ok" ? `Last closed minute ${live.time} · NIFTY ${live.spot?.toFixed(2)} · ATM ${live.strike}` : "Live decision"}>
          {live?.status !== "ok" ? (
            <p className="lab-muted">⏳ {live?.reason || "Waiting for market data"}</p>
          ) : (
            <>
              <div className={`lab-decision ${live.signal && live.in_window ? "buy" : "wait"}`}>{live.decision}</div>
              <DivergingBars fmt={(v) => pct(v, 2)} rows={[
                { label: `Buy ${live.strike} CE`, sub: live.ce_ltp ? `₹${live.ce_ltp}` : null, value: live.pred_ce_pct },
                { label: `Buy ${live.strike} PE`, sub: live.pe_ltp ? `₹${live.pe_ltp}` : null, value: live.pred_pe_pct },
                { label: "Needed to trade", sub: "threshold", value: live.threshold_pct },
              ]} />
              <p className="lab-muted small">Predicted net return after costs if it bought now and followed the exit rule.</p>
            </>
          )}
        </Card>

        <Card title="Why it predicts that" sub="Each feature's push on the prediction (exact: base + pushes = prediction)"
          right={live?.status === "ok" && <Seg value={activeWhy} onChange={setWhySide} options={[{ value: "ce", label: "CE" }, { value: "pe", label: "PE" }]} />}>
          {!why ? <p className="lab-muted">Available when the live decision is ready.</p> : (
            <>
              <DivergingBars fmt={(v) => pct(v, 3)} rows={why.reasons.map((r) => ({
                label: r.feature, hint: FEATURE_INFO[r.feature],
                sub: typeof r.value === "number" ? `= ${Math.abs(r.value) >= 100 ? r.value.toFixed(0) : r.value.toFixed(4)}` : null,
                value: r.effect_pct,
              }))} />
              <div className="lab-why-sum">
                <span>Average setup (base) {pct(why.base_pct, 2)}</span>
                <span>+ top pushes {pct(why.reasons.reduce((a, r) => a + r.effect_pct, 0), 2)}</span>
                <span>+ other features {pct(why.other_pct, 2)}</span>
                <b>= {pct(activeWhy === "ce" ? live.pred_ce_pct : live.pred_pe_pct, 2)}</b>
              </div>
            </>
          )}
        </Card>
      </div>

      <Card title="Today, minute by minute" sub="Predicted reward of buying CE / PE at each closed minute. A trade needs a line above the threshold inside 09:45–14:30.">
        {scoreLog.length < 2 ? <p className="lab-muted">Fills in during market hours (needs 30 minutes of bars after 09:15).</p> : (
          <LineChart x={scoreLog.map((p) => p.time)} height={230} yFmt={(v) => `${v.toFixed(1)}%`}
            refs={[{ y: model?.threshold_pct ?? 0, label: "threshold" }]}
            series={[
              { name: "CE", color: vizCE, values: scoreLog.map((p) => p.pred_ce_pct) },
              { name: "PE", color: vizPE, values: scoreLog.map((p) => p.pred_pe_pct) },
            ]} />
        )}
      </Card>

      {/* Training */}
      <Card title="Training parameters" sub={report ? `Report built ${report.generated_at.replace("T", " ")}` : "Run ml/report.py and export to see training details"}>
        {!report ? null : (
          <div className="lab-params">
            <div>
              <h4>Model</h4>
              <dl>
                <dt>Algorithm</dt><dd>LightGBM gradient-boosted trees</dd>
                <dt>Learns to predict</dt><dd>{model.target}</dd>
                <dt>Trees (CE / PE)</dt><dd>{model.trees.ce} / {model.trees.pe} (of max {model.max_rounds}, early stop after {model.early_stopping_rounds} rounds without improvement)</dd>
                <dt>Loss</dt><dd>{model.params.objective} (δ = {model.params.alpha}) · rewards clipped to {model.label_clip.map((v) => `${v * 100}%`).join(" … ")}</dd>
                <dt>Learning rate</dt><dd>{model.params.learning_rate}</dd>
                <dt>Leaves per tree</dt><dd>{model.params.num_leaves}</dd>
                <dt>Min samples per leaf</dt><dd>{model.params.min_child_samples}</dd>
                <dt>Feature / row sampling</dt><dd>{model.params.feature_fraction * 100}% / {model.params.bagging_fraction * 100}% per tree</dd>
                <dt>L2 regularisation</dt><dd>{model.params.lambda_l2}</dd>
              </dl>
            </div>
            <div>
              <h4>Trading rules</h4>
              <dl>
                <dt>Exit rule</dt><dd>{model.exit_rule.name}: target +{model.exit_rule.target_pct}%, stop −{model.exit_rule.stop_pct}%, else hold to 15:15</dd>
                <dt>Entry threshold</dt><dd>predicted reward &gt; {pct(model.threshold_pct, 3)}</dd>
                <dt>Max trades / day</dt><dd>{model.max_trades_per_day}</dd>
                <dt>Entry window</dt><dd>09:45 – 14:30</dd>
                <dt>Open at once (live)</dt><dd>up to 3 trades, never the same contract twice (set when starting the agent). The backtests below hold one at a time; with 3 open the test year made +₹55,242 but the worst drawdown doubled to ₹53,722.</dd>
                <dt>Costs in rewards</dt><dd>slippage {(data.meta?.sim?.slippage_pct ?? 0) * 100}% · taxes {(data.meta?.sim?.taxes_pct ?? 0) * 100}% · brokerage ₹{data.meta?.sim?.brokerage_rs} · lot {data.meta?.sim?.lot_size}</dd>
              </dl>
              <h4>Data</h4>
              <table className="lab-table">
                <thead><tr><th>Split</th><th>Dates</th><th>Days</th><th>Minutes</th><th>Avg reward</th></tr></thead>
                <tbody>
                  {["train", "validation", "test"].map((k) => {
                    const d = report.data[k];
                    return <tr key={k}><td>{k}</td><td>{d.from} → {d.to}</td><td>{d.days}</td><td>{d.rows.toLocaleString()}</td><td className={signClass(d.avg_reward_pct)}>{pct(d.avg_reward_pct)}</td></tr>;
                  })}
                </tbody>
              </table>
              <p className="lab-muted small">Validation picks the threshold and exit rule; test is never touched until the final exam.</p>
            </div>
          </div>
        )}
      </Card>

      {report && (
        <>
          <div className="lab-grid2">
            <Card title="Learning curve" sub="Error after each tree is added (lower is better). Training error keeps falling; validation flattening is where it stopped."
              right={<Seg value={side} onChange={setSide} options={[{ value: "ce", label: "CE model" }, { value: "pe", label: "PE model" }]} />}>
              <LineChart x={curve.rounds} xFmt={(v) => `tree ${v}`} yFmt={(v) => v.toFixed(4)} height={240}
                series={[
                  { name: "Train", color: vizCE, values: curve.train },
                  { name: "Validation", color: vizPE, values: curve.validation },
                  { name: "Test", color: vizTest, values: curve.test },
                ]} />
            </Card>
            <Card title="What the model pays attention to" sub={`Share of total learning gain per feature (${side.toUpperCase()} model)`}>
              <div className="lab-imp">
                {importance.map((f) => (
                  <div key={f.feature} className="lab-imp-row" title={FEATURE_INFO[f.feature] || ""}>
                    <span className="lab-imp-name">{f.feature}<small>{FEATURE_INFO[f.feature]}</small></span>
                    <span className="lab-imp-bar"><i style={{ width: `${(f.gain_pct / importance[0].gain_pct) * 100}%`, background: side === "ce" ? vizCE : vizPE }} /></span>
                    <b>{f.gain_pct.toFixed(1)}%</b>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <Card title="Predicted profit/loss vs what really happened"
            sub="Every minute and side, sorted into 10 groups by predicted reward. If the model is right, the actual bars rise from left to right."
            right={<Seg value={split} onChange={setSplit} options={[{ value: "validation", label: "Validation" }, { value: "test", label: "Test" }]} />}>
            <div className="lab-stats-row">
              {(() => {
                const q = report.signal_quality[split];
                return (<>
                  <Stat label="When it says TRADE: avg result" value={pct(q.signal_avg_pct)} tone={signClass(q.signal_avg_pct)} hint={`${q.signals.toLocaleString()} minutes above threshold`} />
                  <Stat label="…win rate" value={`${Math.round(q.signal_win_rate * 100)}%`} />
                  <Stat label="Every minute (no model)" value={pct(q.all_avg_pct)} tone={signClass(q.all_avg_pct)} />
                  <Stat label="…win rate" value={`${Math.round(q.all_win_rate * 100)}%`} />
                </>);
              })()}
            </div>
            <BarChart height={240} yFmt={(v) => `${v.toFixed(0)}%`}
              groups={calib.map((b, i) => ({ ...b, label: `G${i + 1}` }))}
              series={[{ key: "pred_avg", name: "Predicted", color: vizCE }, { key: "actual_avg", name: "Actual", color: vizPE }]}
              tip={(g) => (<>
                <div className="lab-tip-row"><span>Predicted range</span><b>{pct(g.pred_lo)} … {pct(g.pred_hi)}</b></div>
                <div className="lab-tip-row"><span><i style={{ background: vizCE }} />Predicted avg</span><b>{pct(g.pred_avg)}</b></div>
                <div className="lab-tip-row"><span><i style={{ background: vizPE }} />Actual avg</span><b>{pct(g.actual_avg)}</b></div>
                <div className="lab-tip-row"><span>Actual win rate</span><b>{Math.round(g.win_rate * 100)}%</b></div>
                <div className="lab-tip-row"><span>Samples</span><b>{g.count.toLocaleString()}</b></div>
              </>)} />
            <div className="lab-legend"><span><i style={{ background: vizCE }} />Predicted average</span><span><i style={{ background: vizPE }} />Actual average</span><span className="lab-muted">G1 = most pessimistic … G10 = most optimistic</span></div>
            <p className="lab-note">Predictions sit below actual results because the model is trained with a robust loss that shrinks rare large wins. Only the ranking matters: it trades only in the top group, above the threshold.</p>
          </Card>

          <div className="lab-grid2">
            <Card title="Backtest: one lot, test year" sub={`${report.results.test.trades} trades on unseen data, ${report.data.test.from} → ${report.data.test.to}`}>
              <div className="lab-stats-row">
                <Stat label="Net P&L" value={rs(report.results.test.total_pnl_rs_1lot)} tone={signClass(report.results.test.total_pnl_rs_1lot)} />
                <Stat label="Win rate" value={`${Math.round(report.results.test.win_rate * 100)}%`} />
                <Stat label="Avg per trade" value={pct(report.results.test.avg_ret_pct)} tone={signClass(report.results.test.avg_ret_pct)} />
                <Stat label="Max drawdown" value={rs(-report.results.test.max_drawdown_rs)} tone="neg" />
              </div>
              <LineChart area height={210} x={report.equity.test.map((p) => p.day)} yFmt={(v) => rs(v)}
                series={[{ name: "Equity", color: vizCE, values: report.equity.test.map((p) => p.equity) }]} />
            </Card>
            <Card title="Keeps learning: monthly retrain" sub={`Retrained every month on all data before it, then traded the next month blind (${report.walk_forward.period || ""})`}>
              <div className="lab-stats-row">
                <Stat label="Net P&L" value={rs(report.walk_forward.total_pnl_rs_1lot)} tone={signClass(report.walk_forward.total_pnl_rs_1lot)} />
                <Stat label="Trades" value={report.walk_forward.trades} />
                <Stat label="Win rate" value={report.walk_forward.win_rate != null ? `${Math.round(report.walk_forward.win_rate * 100)}%` : "–"} />
                <Stat label="Verdict" value={report.walk_forward.verdict || "–"} tone="warn" />
              </div>
              <BarChart height={210} colorBySign yFmt={(v) => rs(v)}
                groups={wfMonths.map((m, i) => ({ ...m, label: m.month.slice(2), cum: wfCum[i] }))}
                series={[{ key: "pnl_rs", name: "Month P&L", color: vizCE }]}
                tip={(g) => (<>
                  <div className="lab-tip-row"><span>{g.pnl_rs >= 0 ? "▲ Profit" : "▼ Loss"}</span><b className={signClass(g.pnl_rs)}>{rs(g.pnl_rs)}</b></div>
                  <div className="lab-tip-row"><span>Trades</span><b>{g.trades}</b></div>
                  <div className="lab-tip-row"><span>Running total</span><b>{rs(g.cum)}</b></div>
                </>)} />
            </Card>
          </div>

          <Card title="Exit rules it tried" sub="The rule is chosen on validation only; the test column shows how each would have done afterwards.">
            <div className="lab-table-wrap">
              <table className="lab-table">
                <thead><tr><th>Rule</th><th>Val trades</th><th>Val P&L</th><th>Test trades</th><th>Test win</th><th>Test avg</th><th>Test P&L</th></tr></thead>
                <tbody>
                  {Object.entries(report.variants || {}).map(([name, v]) => (
                    <tr key={name} className={name === model.exit_rule.name ? "chosen" : ""}>
                      <td>{name}{name === model.exit_rule.name ? " ✓" : ""}</td>
                      <td>{v.val?.trades ?? 0}</td><td className={signClass(v.val?.total_pnl_rs_1lot)}>{rs(v.val?.total_pnl_rs_1lot)}</td>
                      <td>{v.test?.trades ?? 0}</td><td>{v.test?.win_rate != null ? `${Math.round(v.test.win_rate * 100)}%` : "–"}</td>
                      <td className={signClass(v.test?.avg_ret_pct)}>{pct(v.test?.avg_ret_pct)}</td>
                      <td className={signClass(v.test?.total_pnl_rs_1lot)}>{rs(v.test?.total_pnl_rs_1lot)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="lab-muted small">Rule names: t = target %, s = stop %, h = max hold minutes (eod = until 15:15).</p>
          </Card>

          <Card title="Test-year trades: prediction vs result" sub="Every backtest trade: what the model expected and what it got."
            right={<button className="lab-link" onClick={() => setShowTable((v) => !v)}>{showTable ? "Hide" : "Show"} all {report.trades.test.length}</button>}>
            <div className="lab-table-wrap">
              <table className="lab-table">
                <thead><tr><th>Date</th><th>Time</th><th>Buy</th><th>Predicted</th><th>Actual</th><th>P&L (1 lot)</th></tr></thead>
                <tbody>
                  {(showTable ? report.trades.test : report.trades.test.slice(-8)).slice().reverse().map((t, i) => (
                    <tr key={i}><td>{t.day}</td><td>{t.time}</td><td>{t.side}</td><td>{pct(t.pred_pct)}</td>
                      <td className={signClass(t.actual_pct)}>{pct(t.actual_pct)}</td><td className={signClass(t.pnl_rs)}>{rs(t.pnl_rs)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="Inside one decision tree" sub="The first of the CE model's trees, top levels. Every tree is a set of yes/no questions; the model adds up hundreds of them.">
            <div className="lab-tree"><TreeNode node={report.first_tree} /></div>
          </Card>
        </>
      )}

      <Card title="Live feedback: the AI's own trades" sub="Paper trades placed by the AI agent for any user, with the reward it predicted and what actually happened.">
        <div className="lab-stats-row">
          <Stat label="Closed AI trades" value={closed.length} />
          <Stat label="Direction called right" value={closed.length ? `${rightCalls} / ${closed.length}` : "–"} />
          <Stat label="Agents running now" value={(data.agents_running || []).length} />
        </div>
        {journal.length === 0 ? (
          <p className="lab-muted">No AI trades yet. Start "AI Options Agent (ML)" on the Trade screen (auto mode) and its trades will appear here.</p>
        ) : (
          <div className="lab-table-wrap">
            <table className="lab-table">
              <thead><tr><th>Opened</th><th>User</th><th>Contract</th><th>Predicted</th><th>Entry</th><th>Exit</th><th>Actual</th><th>Why exit</th></tr></thead>
              <tbody>
                {journal.slice(0, 60).map((t, i) => (
                  <tr key={i}>
                    <td>{t.opened_at ? new Date(t.opened_at).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "–"}</td>
                    <td>{t.user}</td><td>{t.symbol}</td><td>{pct(t.pred_pct)}</td>
                    <td>{t.entry ?? "–"}</td><td>{t.exit ?? (t.status === "open" ? "open" : "–")}</td>
                    <td className={signClass(t.actual_pct)}>{t.status === "entry rejected" ? `rejected: ${t.note || ""}` : pct(t.actual_pct)}</td>
                    <td>{t.exit_reason || "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Train with more data" sub="Training runs on your laptop (the 1 GB server only runs the finished model).">
        <ol className="lab-steps">
          <li>Put new data in <code>ml/data/raw</code> (same format as the NIFTY spot / options 1-minute files), or extend <code>ml/download_data.py</code>.</li>
          <li>Build the training set: <code>ml\.venv\Scripts\python.exe ml\build_dataset.py</code></li>
          <li>Train and pick the rule on validation: <code>ml\.venv\Scripts\python.exe ml\train.py</code> (move <code>TRAIN_END</code> / <code>VAL_END</code> forward as data grows)</li>
          <li>Honest check, monthly retrain: <code>ml\.venv\Scripts\python.exe ml\walkforward.py t100s30eod &gt; ml\data\wf.log</code></li>
          <li>Rebuild these charts: <code>ml\.venv\Scripts\python.exe ml\report.py</code></li>
          <li>Ship it: <code>ml\.venv\Scripts\python.exe ml\export_model.py</code>, then commit and deploy (or just ask Claude to do steps 2–6).</li>
        </ol>
        <p className="lab-note">Only deploy a model to auto mode if the walk-forward P&L is positive. So far it is not, so keep the agent on paper trading.</p>
      </Card>
    </div>
  );
}

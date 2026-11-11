import { useEffect, useRef, useState } from "react";
import { createChart, CrosshairMode } from "lightweight-charts";

const COLORS = {
  up: "#10b981",
  down: "#ef4444",
  line: "#06b6d4",
  text: "#9ca3af",
  grid: "rgba(255,255,255,0.05)",
  border: "rgba(255,255,255,0.12)",
};

const IST_OFFSET_SEC = 19800;

// Current IST minute as the "UTC epoch" convention the backend uses (charts render UTC → shows IST wall time)
const istMinuteTime = () => Math.floor((Date.now() / 1000 + IST_OFFSET_SEC) / 60) * 60;

/** Toggle between line and candlestick views, as two icon buttons. */
export function ChartModeToggle({ mode, onChange }) {
  const btn = (value, label, icon) => (
    <button
      type="button"
      className={`chart-mode-btn ${mode === value ? "on" : ""}`}
      onClick={() => onChange(value)}
      title={label}
      aria-label={label}
      aria-pressed={mode === value}
    >
      {icon}
    </button>
  );
  return (
    <div className="chart-mode-toggle" role="group" aria-label="Chart type">
      {btn(
        "line",
        "Line chart",
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="3 17 9 11 13 15 21 7" />
          <polyline points="15 7 21 7 21 13" />
        </svg>
      )}
      {btn(
        "candles",
        "Candlestick chart",
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <line x1="6" y1="3" x2="6" y2="21" />
          <rect x="4" y="7" width="4" height="8" fill="currentColor" stroke="none" rx="0.5" />
          <line x1="12" y1="3" x2="12" y2="21" />
          <rect x="10" y="10" width="4" height="6" rx="0.5" />
          <line x1="18" y1="5" x2="18" y2="19" />
          <rect x="16" y="6" width="4" height="9" fill="currentColor" stroke="none" rx="0.5" />
        </svg>
      )}
    </div>
  );
}

/**
 * Today's 1-minute chart for one instrument. Loads bars from the API, refreshes every 15s and moves the
 * last bar with every live price (liveLtp) so it ticks in real time between refreshes.
 */
export default function CandleChart({ apiBase, token, instrumentKey, mode = "candles", height = 220, liveLtp = null }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const barsRef = useRef([]);
  const fittedRef = useRef(false);
  const [status, setStatus] = useState("loading");

  const toSeriesData = (bars) =>
    mode === "line" ? bars.map((b) => ({ time: b.time, value: b.close })) : bars;

  // Create the chart once per mount
  useEffect(() => {
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: COLORS.text, fontSize: 11 },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      rightPriceScale: { borderColor: COLORS.border },
      timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false, rightOffset: 3 },
      crosshair: { mode: CrosshairMode.Normal },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // (Re)create the series when the chart type changes
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (seriesRef.current) chart.removeSeries(seriesRef.current);
    seriesRef.current =
      mode === "line"
        ? chart.addAreaSeries({
            lineColor: COLORS.line,
            topColor: "rgba(6,182,212,0.28)",
            bottomColor: "rgba(6,182,212,0.02)",
            lineWidth: 2,
            priceLineVisible: true,
          })
        : chart.addCandlestickSeries({
            upColor: COLORS.up,
            downColor: COLORS.down,
            wickUpColor: COLORS.up,
            wickDownColor: COLORS.down,
            borderVisible: false,
          });
    if (barsRef.current.length) seriesRef.current.setData(toSeriesData(barsRef.current));
  }, [mode]); // eslint-disable-line react-hooks/exhaustive-deps

  // Load + refresh bars for the instrument
  useEffect(() => {
    if (!instrumentKey) return;
    let cancelled = false;
    barsRef.current = [];
    fittedRef.current = false;
    seriesRef.current?.setData([]);
    setStatus("loading");

    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/api/v1/market/candles?instrument_key=${encodeURIComponent(instrumentKey)}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok || cancelled) return;
        const { bars } = await res.json();
        if (cancelled) return;
        barsRef.current = bars;
        seriesRef.current?.setData(toSeriesData(bars));
        if (!fittedRef.current && bars.length) {
          chartRef.current?.timeScale().fitContent();
          fittedRef.current = true;
        }
        setStatus(bars.length ? "ready" : "empty");
      } catch {
        if (!cancelled) setStatus((s) => (s === "ready" ? s : "error"));
      }
    };
    load();
    const id = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [instrumentKey, apiBase, token]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live tick: move the forming bar (or start a new one when the minute rolls over)
  useEffect(() => {
    if (liveLtp == null || !seriesRef.current || !barsRef.current.length) return;
    const bars = barsRef.current;
    const t = istMinuteTime();
    const last = bars[bars.length - 1];
    let bar;
    if (last.time === t) {
      bar = { ...last, high: Math.max(last.high, liveLtp), low: Math.min(last.low, liveLtp), close: liveLtp };
      bars[bars.length - 1] = bar;
    } else if (t > last.time) {
      bar = { time: t, open: liveLtp, high: liveLtp, low: liveLtp, close: liveLtp };
      bars.push(bar);
    } else {
      return;
    }
    seriesRef.current.update(mode === "line" ? { time: bar.time, value: bar.close } : bar);
  }, [liveLtp]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div style={{ position: "relative", width: "100%", height }}>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      {status !== "ready" && (
        <div className="chart-empty">
          {status === "loading" && "Loading chart…"}
          {status === "empty" && "No candles yet today — they build from live prices during market hours (9:15–15:30)."}
          {status === "error" && "Could not load chart."}
        </div>
      )}
    </div>
  );
}

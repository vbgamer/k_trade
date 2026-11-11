// Market-themed artwork drawn as SVG (no external images): candlestick backdrops, a geometric bull,
// and a scrolling ticker tape. Colours come from the theme tokens, so everything follows day/night mode.
import { useMemo } from "react";

// Deterministic pseudo-random walk so the art is identical on every render / device
const seeded = (seed) => () => {
  seed = (seed * 16807) % 2147483647;
  return (seed - 1) / 2147483646;
};

const makeCandles = (count, seed, drift = 0.35) => {
  const rnd = seeded(seed);
  const out = [];
  let price = 50;
  for (let i = 0; i < count; i++) {
    const open = price;
    const close = open + (rnd() - 0.5 + drift * 0.3) * 9;
    const high = Math.max(open, close) + rnd() * 4;
    const low = Math.min(open, close) - rnd() * 4;
    out.push({ open, close, high, low });
    price = close;
  }
  return out;
};

/** A field of candlesticks trending up, gently rising into place. */
export function CandleField({ count = 46, seed = 7, className = "", height = 260, animate = true }) {
  const candles = useMemo(() => makeCandles(count, seed), [count, seed]);
  const lo = Math.min(...candles.map((c) => c.low));
  const hi = Math.max(...candles.map((c) => c.high));
  const W = count * 22;
  const y = (v) => height - 10 - ((v - lo) / (hi - lo)) * (height - 20);
  const trend = candles.map((c, i) => `${i * 22 + 11},${y((c.open + c.close) / 2)}`).join(" ");
  return (
    <svg className={`candle-field ${animate ? "animate" : ""} ${className}`} viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      {[0.25, 0.5, 0.75].map((f) => (
        <line key={f} x1="0" x2={W} y1={height * f} y2={height * f} className="cf-grid" />
      ))}
      {candles.map((c, i) => {
        const up = c.close >= c.open;
        const x = i * 22 + 11;
        const top = y(Math.max(c.open, c.close));
        const bodyH = Math.max(2, Math.abs(y(c.open) - y(c.close)));
        return (
          <g key={i} className={`cf-candle ${up ? "up" : "down"}`} style={{ animationDelay: `${i * 45}ms` }}>
            <line x1={x} x2={x} y1={y(c.high)} y2={y(c.low)} />
            <rect x={x - 6} y={top} width="12" height={bodyH} rx="1.5" />
          </g>
        );
      })}
      <polyline points={trend} className="cf-trend" />
    </svg>
  );
}

/** Geometric (low-poly) bull head with a rising chart line; draws itself in. */
export function BullArt({ className = "" }) {
  return (
    <svg className={`bull-art ${className}`} viewBox="0 0 400 320" aria-hidden="true">
      <defs>
        <linearGradient id="bullStroke" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="var(--color-primary)" />
          <stop offset="100%" stopColor="var(--color-cyan)" />
        </linearGradient>
        <linearGradient id="bullFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--color-primary)" stopOpacity="0.16" />
          <stop offset="100%" stopColor="var(--color-cyan)" stopOpacity="0.04" />
        </linearGradient>
      </defs>
      {/* rising market line behind the bull */}
      <polyline className="bull-chart" points="0,300 50,282 90,290 135,250 175,262 220,214 262,226 300,178 340,190 400,128" />
      <polygon className="bull-head-fill" points="150,110 250,110 272,150 236,252 164,252 128,150" />
      <g className="bull-lines">
        {/* horns */}
        <path d="M150 112 C 120 104, 84 86, 62 34 C 92 70, 118 92, 142 126" />
        <path d="M250 112 C 280 104, 316 86, 338 34 C 308 70, 282 92, 258 126" />
        {/* ears */}
        <polygon points="132,126 84,142 130,152" />
        <polygon points="268,126 316,142 270,152" />
        {/* head outline */}
        <polygon points="150,110 250,110 272,150 236,252 164,252 128,150" />
        {/* facets */}
        <polyline points="150,110 184,166 200,110 216,166 250,110" />
        <polyline points="128,150 184,166 170,214 128,150" />
        <polyline points="272,150 216,166 230,214 272,150" />
        <polyline points="184,166 200,204 216,166" />
        <polyline points="170,214 200,204 230,214" />
        {/* muzzle */}
        <polygon points="170,214 230,214 240,244 200,262 160,244" />
        {/* eyes */}
        <line x1="164" y1="160" x2="184" y2="168" />
        <line x1="236" y1="160" x2="216" y2="168" />
      </g>
      <circle className="bull-nostril" cx="186" cy="238" r="4" />
      <circle className="bull-nostril" cx="214" cy="238" r="4" />
    </svg>
  );
}

/** Scrolling ticker tape. items: [{label, value, change}] */
export function TickerTape({ items }) {
  const row = items.map((it, i) => (
    <span key={i} className="tt-item">
      <span className="tt-label">{it.label}</span>
      <span className="tt-value">{it.value}</span>
      {it.change != null && (
        <span className={`tt-change ${it.change >= 0 ? "up" : "down"}`}>
          {it.change >= 0 ? "▲" : "▼"} {Math.abs(it.change).toFixed(2)}%
        </span>
      )}
    </span>
  ));
  return (
    <div className="ticker-tape" aria-label="Market ticker">
      <div className="tt-track">
        {row}
        <span aria-hidden="true" style={{ display: "contents" }}>{row}</span>
      </div>
    </div>
  );
}

/** Fixed, faint artwork behind the whole app: candlesticks along the bottom and a bull watermark. */
export function AppBackdrop() {
  return (
    <div className="app-backdrop" aria-hidden="true">
      <CandleField count={70} seed={11} height={220} className="backdrop-candles" animate={false} />
      <BullArt className="backdrop-bull" />
    </div>
  );
}

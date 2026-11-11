import { useEffect, useRef } from "react";
import { BullArt, CandleField, TickerTape } from "./MarketArt";

/** Fade/slide elements with .reveal into view as they scroll in (respects reduced-motion via CSS). */
function useReveal(rootRef) {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const els = root.querySelectorAll(".reveal");
    if (!("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.isIntersecting && (e.target.classList.add("in"), io.unobserve(e.target))),
      { threshold: 0.12 }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [rootRef]);
}

export function ThemeToggle({ theme, onToggle }) {
  const dark = theme === "dark";
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      aria-label={dark ? "Switch to day theme" : "Switch to night theme"}
      title={dark ? "Day theme" : "Night theme"}
    >
      <span className={`tt-icon ${dark ? "show" : ""}`}>
        {/* moon */}
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        </svg>
      </span>
      <span className={`tt-icon ${dark ? "" : "show"}`}>
        {/* sun */}
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="12" cy="12" r="4.2" />
          <path d="M12 2v2.5M12 19.5V22M4.2 4.2l1.8 1.8M18 18l1.8 1.8M2 12h2.5M19.5 12H22M4.2 19.8 6 18M18 6l1.8-1.8" />
        </svg>
      </span>
    </button>
  );
}

const FEATURES = [
  {
    icon: "📡",
    title: "Live NSE prices",
    text: "Connect your free Upstox account and every quote, option chain and candle comes straight from the exchange, refreshed every 2 seconds.",
  },
  {
    icon: "⚡",
    title: "One-tap option trading",
    text: "Full NIFTY option chain with live bid/ask. Tap a price to see its candles and place a market or limit order with stop-loss and target.",
  },
  {
    icon: "🤖",
    title: "AI options agent",
    text: "Machine-learning models trained on 1,236 days of NIFTY option data score the ATM call and put every minute, and can trade on their own.",
  },
  {
    icon: "🛡️",
    title: "Risk checks before every order",
    text: "Daily loss limit, size caps, trade-count limits and reward-to-risk rules run before any order, plus automatic square-off at 3:15 PM.",
  },
  {
    icon: "🧠",
    title: "Strategy engine",
    text: "Run rule-based strategies like the Heikin-Ashi WMA crossover, or the AI agent, on 1-minute candles. One engine runs at a time.",
  },
  {
    icon: "📱",
    title: "Runs in the cloud",
    text: "The engine runs on a server, not your laptop. Start it from your phone at the office; laptop and phone stay in sync in real time.",
  },
];

const STEPS = [
  { n: "01", title: "Create an account", text: "Get ₹5,00,000 of virtual capital instantly. No real money involved." },
  { n: "02", title: "Connect Upstox", text: "One login a day switches simulated prices to live NSE market data." },
  { n: "03", title: "Pick your engine", text: "Trade by hand from the option chain, start a strategy, or let the AI agent decide." },
  { n: "04", title: "Watch from anywhere", text: "Positions, P&L, charts and AI signals update live on every device you're signed in on." },
];

export default function Landing({ loggedIn, authCard, theme, onToggleTheme, onGetStarted, onOpenTab, ticker, aiModel, liveChart }) {
  const rootRef = useRef(null);
  useReveal(rootRef);

  const spot = ticker?.spot;
  const change = spot && ticker?.prev_close ? ((spot - ticker.prev_close) / ticker.prev_close) * 100 : null;
  const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
  const tapeItems = [
    { label: "NIFTY 50", value: fmt(spot), change },
    ...(ticker?.atm_strike ? [{ label: `ATM ${ticker.atm_strike} CE`, value: `₹${fmt(ticker.atm_ce)}` }] : []),
    ...(ticker?.atm_strike ? [{ label: `ATM ${ticker.atm_strike} PE`, value: `₹${fmt(ticker.atm_pe)}` }] : []),
    ...(ticker?.expiry ? [{ label: "WEEKLY EXPIRY", value: ticker.expiry }] : []),
    { label: "DATA", value: ticker?.source === "upstox" ? "● LIVE NSE" : "SIMULATED" },
    { label: "MARKET HOURS", value: "09:15 – 15:30 IST" },
    { label: "VIRTUAL CAPITAL", value: "₹5,00,000" },
  ];

  const wf = aiModel?.walk_forward;
  const bt = aiModel?.backtest;

  const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className={`landing ${loggedIn ? "landing-in-app" : ""}`} ref={rootRef}>
      {!loggedIn && (
        <nav className="landing-nav">
          <div className="brand">
            <div className="brand-mark">AT</div>
            <span className="brand-name">
              <b>ALGOTRADE</b> PRO
            </span>
          </div>
          <div className="landing-links">
            <button className="nav-link" onClick={() => scrollTo("features")}>Features</button>
            <button className="nav-link" onClick={() => scrollTo("how")}>How it works</button>
            <button className="nav-link" onClick={() => scrollTo("ai")}>AI agent</button>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <ThemeToggle theme={theme} onToggle={onToggleTheme} />
            <button className="btn btn-ghost" onClick={() => onGetStarted("login")}>Sign in</button>
          </div>
        </nav>
      )}

      {/* HERO */}
      <section className="hero">
        <div className="hero-art" aria-hidden="true">
          <CandleField count={48} seed={3} height={300} className="hero-candles" />
          <BullArt className="hero-bull" />
          <div className="hero-orb orb-1" />
          <div className="hero-orb orb-2" />
        </div>

        <div className="hero-copy reveal">
          <span className="eyebrow">
            <span className="live-dot" /> {ticker?.source === "upstox" ? "Live NSE data" : "NSE market data"} · Paper trading
          </span>
          <h1 className="hero-title">
            Your algo desk for <span className="grad-text">NIFTY options</span>
          </h1>
          <p className="hero-sub">
            Live option chain, one-tap orders, rule-based strategies and an AI agent that learns from every trade,
            running in the cloud so you can trade from your phone. Practice with virtual money before risking real money.
          </p>
          <div className="hero-ctas">
            {loggedIn ? (
              <>
                <button className="btn btn-primary btn-shine" onClick={() => onOpenTab("trade")}>Open Trade screen →</button>
                <button className="btn btn-ghost" onClick={() => onOpenTab("dashboard")}>Live dashboard</button>
              </>
            ) : (
              <>
                <button className="btn btn-primary btn-shine" onClick={() => onGetStarted("register")}>Start paper trading →</button>
                <button className="btn btn-ghost" onClick={() => scrollTo("how")}>See how it works</button>
              </>
            )}
          </div>
          <div className="hero-stats">
            <div><b>₹5L</b><span>virtual capital</span></div>
            <div><b>2s</b><span>live quotes</span></div>
            <div><b>1,236</b><span>days of training data</span></div>
            <div><b>3:15</b><span>auto square-off</span></div>
          </div>
        </div>

        {!loggedIn && (
          <div className="hero-auth reveal" id="auth" style={{ transitionDelay: "120ms" }}>
            {authCard}
          </div>
        )}
        {loggedIn && (
          <div className="hero-auth reveal" style={{ transitionDelay: "120ms" }}>
            <div className="glass-panel live-card">
              <div className="live-card-head">
                <span>NIFTY 50</span>
                <span className={ticker?.source === "upstox" ? "badge-active" : "badge-upcoming"}>
                  {ticker?.source === "upstox" ? "● LIVE" : "SIMULATED"}
                </span>
              </div>
              <div className="live-card-price">{fmt(spot)}</div>
              {change != null && (
                <div className={`live-card-change ${change >= 0 ? "up" : "down"}`}>
                  {change >= 0 ? "▲" : "▼"} {Math.abs(change).toFixed(2)}% today
                </div>
              )}
              <div className="live-card-candles">{liveChart}</div>
              <div className="live-card-links">
                <button className="btn btn-ghost" onClick={() => onOpenTab("trade")}>⚡ Trade</button>
                <button className="btn btn-ghost" onClick={() => onOpenTab("trades")}>📜 P&amp;L</button>
                <button className="btn btn-ghost" onClick={() => onOpenTab("broker")}>🔑 Broker</button>
              </div>
            </div>
          </div>
        )}
      </section>

      <TickerTape items={tapeItems} />

      {/* FEATURES */}
      <section className="landing-section" id="features">
        <div className="section-head reveal">
          <span className="eyebrow">What you get</span>
          <h2>Everything a trading desk needs, in your pocket</h2>
        </div>
        <div className="feature-grid">
          {FEATURES.map((f, i) => (
            <div key={f.title} className="glass-panel feature-card reveal" style={{ transitionDelay: `${i * 70}ms` }}>
              <div className="feature-icon">{f.icon}</div>
              <h3>{f.title}</h3>
              <p>{f.text}</p>
            </div>
          ))}
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section className="landing-section" id="how">
        <div className="section-head reveal">
          <span className="eyebrow">How it works</span>
          <h2>From sign-up to live signals in four steps</h2>
        </div>
        <div className="steps">
          {STEPS.map((s, i) => (
            <div key={s.n} className="step reveal" style={{ transitionDelay: `${i * 90}ms` }}>
              <div className="step-num">{s.n}</div>
              <h3>{s.title}</h3>
              <p>{s.text}</p>
            </div>
          ))}
        </div>
      </section>

      {/* AI */}
      <section className="landing-section" id="ai">
        <div className="ai-panel glass-panel reveal">
          <div className="ai-copy">
            <span className="eyebrow">The AI agent</span>
            <h2>Learns from profit. Learns from loss.</h2>
            <p>
              Every minute the agent turns the market into 28 numbers (momentum, volatility, premiums, time to expiry)
              and asks two LightGBM models what buying the ATM call or put is worth after costs. Winning trades
              in history pull its scores up, losing ones push them down, and every live trade is written to a
              journal it retrains on.
            </p>
            <ul className="ai-list">
              <li>Trained on NIFTY option candles from 2021 to 2026, tested on months it never saw</li>
              <li>Exits on fixed target, stop-loss, time limit or 3:15 PM, the same rules it was trained with</li>
              <li>Auto mode trades by itself; suggest mode only shows the signal</li>
            </ul>
          </div>
          <div className="ai-stats">
            <div className="ai-stat">
              <span>Unseen-data backtest</span>
              <b>{bt ? `${bt.trades} trades · ${Math.round(bt.win_rate * 100)}% win` : "—"}</b>
            </div>
            <div className="ai-stat">
              <span>Monthly-retrain test</span>
              <b className={wf && wf.total_pnl_rs_1lot < 0 ? "neg" : ""}>
                {wf ? `₹${fmt(wf.total_pnl_rs_1lot)} per lot` : "—"}
              </b>
            </div>
            <div className="ai-honest">
              Honest status: the model is better than random entries but has <b>not yet shown a profit</b> after
              costs. It runs on paper money while it collects its own trade history.
            </div>
          </div>
        </div>
      </section>

      {/* CTA */}
      {!loggedIn && (
        <section className="landing-section cta-band reveal">
          <h2>Ready to test your edge, risk-free?</h2>
          <p>Create an account in 20 seconds. Paper money only.</p>
          <button className="btn btn-primary btn-shine" onClick={() => onGetStarted("register")}>Create free account →</button>
        </section>
      )}

      <footer className="landing-footer">
        Paper trading with virtual money for learning and testing. Not investment advice, and not a SEBI-registered
        service. Options trading carries substantial risk of loss.
      </footer>
    </div>
  );
}

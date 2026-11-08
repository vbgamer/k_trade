import React, { useState, useEffect, useRef } from "react";

// Dev server (Vite) talks to the local backend; the production build is served by the backend itself.
const API_BASE =
  import.meta.env.VITE_API_BASE || (import.meta.env.DEV ? "http://localhost:8000" : window.location.origin);
const WS_BASE = API_BASE.replace(/^http/, "ws");

// FastAPI returns `detail` as a string, or as a list of field errors for invalid input (HTTP 422).
const errorDetail = (data, fallback) => {
  if (!data || !data.detail) return fallback;
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((e) => `${(e.loc || []).slice(-1)[0] || "field"}: ${e.msg}`).join("; ");
  }
  return fallback;
};

export default function App() {
  // Session State
  const [token, setToken] = useState(localStorage.getItem("token"));
  const [authMode, setAuthMode] = useState("login"); // login | register
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [userProfile, setUserProfile] = useState(null);
  const [errorMsg, setErrorMsg] = useState("");
  const [successMsg, setSuccessMsg] = useState("");

  // Navigation tab: dashboard | trades | broker
  const [activeTab, setActiveTab] = useState("dashboard");

  // Segment Selection (Active: Intraday & F&O; Upcoming: Delivery, Commodity, Currency, Crypto)
  const [selectedSegment, setSelectedSegment] = useState("fno"); // "fno" | "intraday"
  const [upcomingModal, setUpcomingModal] = useState(null);

  // Strategy & Engine Settings
  const [qty, setQty] = useState(50);
  const [maxProfit, setMaxProfit] = useState(2000);
  const [maxLoss, setMaxLoss] = useState(1000);
  const [productType, setProductType] = useState("MIS");
  const [timeframe, setTimeframe] = useState(1);
  const [bypassScheduler, setBypassScheduler] = useState(true);

  // Running engine states
  const [strategies, setStrategies] = useState([]);
  const [selectedStrategy, setSelectedStrategy] = useState(null);
  const [subscriptionId, setSubscriptionId] = useState(null);
  const [engineActive, setEngineActive] = useState(false);
  const [positions, setPositions] = useState([]);
  const [eventLogs, setEventLogs] = useState([]);
  const [priceHistory, setPriceHistory] = useState([]);
  const [currentLtp, setCurrentLtp] = useState(150.0);
  const [currentStrike, setCurrentStrike] = useState("NIFTY26JUL24300CE");

  // Portfolio & Trades state
  const [portfolioSummary, setPortfolioSummary] = useState(null);
  const [tradesList, setTradesList] = useState([]);
  const [tradeFilterSegment, setTradeFilterSegment] = useState("ALL");
  const [tradeFilterSide, setTradeFilterSide] = useState("ALL");
  const [tradesLoading, setTradesLoading] = useState(false);

  // Broker Credentials Vault
  const [brokerName, setBrokerName] = useState("paper_trading");
  const [apiKey, setApiKey] = useState("");
  const [clientId, setClientId] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [brokerStatus, setBrokerStatus] = useState({
    status: "CONNECTED",
    broker_name: "Paper Trading Simulator",
    client_id: "ALGO_PAPER_01"
  });
  const [verifyingBroker, setVerifyingBroker] = useState(false);

  // WebSocket & Terminal
  const ws = useRef(null);
  const terminalEndRef = useRef(null);

  // Load session data
  useEffect(() => {
    if (token) {
      localStorage.setItem("token", token);
      fetchUserProfile();
      fetchBrokerStatus();
      fetchStrategies();
      fetchPositions();
      fetchPortfolioSummary();
      fetchTrades();
      connectWebSocket();
    } else {
      localStorage.removeItem("token");
    }
    return () => {
      closeWebSocket();
    };
  }, [token]);

  // Refetch trades when filters change
  useEffect(() => {
    if (token && activeTab === "trades") {
      fetchTrades();
    }
  }, [tradeFilterSegment, tradeFilterSide, activeTab]);

  // Autoscroll terminal
  useEffect(() => {
    if (terminalEndRef.current) {
      terminalEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [eventLogs]);

  // --- WEBSOCKET CONNECTION ---
  // Close without triggering the auto-reconnect in onclose
  const closeWebSocket = () => {
    if (ws.current) {
      ws.current.onclose = null;
      ws.current.close();
      ws.current = null;
    }
  };

  const connectWebSocket = () => {
    closeWebSocket();

    // Browsers can't send an Authorization header on WebSocket upgrades, so the JWT goes in the query string
    const wsUrl = `${WS_BASE}/api/v1/ws/stream?token=${encodeURIComponent(token)}`;
    ws.current = new WebSocket(wsUrl);

    ws.current.onopen = () => {
      addTerminalLog("System", "WebSocket connection active: Real-time Event Bus streaming.");
    };

    ws.current.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        const { event_type, data } = payload;

        if (event_type === "TickUpdate") {
          const ltp = data.ltp;
          setCurrentLtp(ltp);
          setCurrentStrike(data.instrument_key.split("|")[-1] || data.instrument_key);
          setPriceHistory((prev) => {
            const next = [...prev, ltp];
            return next.slice(-40);
          });
        } else {
          const time = new Date().toLocaleTimeString();
          addTerminalLog(event_type, JSON.stringify(data), time);

          if (event_type === "StrategyStarted") {
            setEngineActive(true);
          } else if (event_type === "StrategyStopped" || event_type === "RiskTriggered") {
            setEngineActive(false);
            fetchPositions();
            fetchPortfolioSummary();
          } else if (event_type === "OrderFilled") {
            fetchPositions();
            fetchPortfolioSummary();
            fetchTrades();
            fetchUserProfile();
          }
        }
      } catch (err) {
        console.error("Error parsing WS packet:", err);
      }
    };

    ws.current.onclose = (event) => {
      if (event.code === 1008) {
        // Server rejected the token (expired or invalid): sign out
        setToken(null);
        return;
      }
      setTimeout(connectWebSocket, 5000);
    };
  };

  const addTerminalLog = (type, message, time = null) => {
    const ts = time || new Date().toLocaleTimeString();
    setEventLogs((prev) => [...prev, { ts, type, message }]);
  };

  // --- API DATA FETCHERS ---
  const fetchUserProfile = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/user/profile`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        setUserProfile(data);
      }
    } catch (err) {
      console.error("Error loading profile:", err);
    }
  };

  const fetchBrokerStatus = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/broker/status`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        setBrokerStatus(data);
      }
    } catch (err) {
      console.error("Error loading broker status:", err);
    }
  };

  const fetchStrategies = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/strategies`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await res.json();
      setStrategies(data);
      if (data.length > 0) setSelectedStrategy(data[0]);
    } catch (err) {
      console.error("Error fetching strategies:", err);
    }
  };

  const fetchPositions = async () => {
    if (!token) return;
    try {
      const res = await fetch(`${API_BASE}/api/v1/portfolio/positions`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await res.json();
      setPositions(data);
    } catch (err) {
      console.error("Error fetching positions:", err);
    }
  };

  const fetchPortfolioSummary = async () => {
    if (!token) return;
    try {
      const res = await fetch(`${API_BASE}/api/v1/portfolio/summary`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        setPortfolioSummary(data);
      }
    } catch (err) {
      console.error("Error fetching portfolio summary:", err);
    }
  };

  const fetchTrades = async () => {
    if (!token) return;
    setTradesLoading(true);
    try {
      let url = `${API_BASE}/api/v1/trades?limit=100`;
      if (tradeFilterSegment !== "ALL") url += `&segment=${tradeFilterSegment}`;
      if (tradeFilterSide !== "ALL") url += `&side=${tradeFilterSide}`;

      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        setTradesList(data);
      }
    } catch (err) {
      console.error("Error fetching trades:", err);
    } finally {
      setTradesLoading(false);
    }
  };

  // --- ACTIONS & HANDLERS ---
  const handleAuth = async (e) => {
    e.preventDefault();
    setErrorMsg("");
    setSuccessMsg("");

    const url =
      authMode === "login"
        ? `${API_BASE}/api/v1/auth/login`
        : `${API_BASE}/api/v1/auth/register`;

    const body =
      authMode === "login"
        ? { email, password }
        : { email, password, first_name: firstName, last_name: lastName };

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(errorDetail(data, "Authentication request failed"));
      }

      const data = await res.json();
      if (data.access_token) {
        setToken(data.access_token);
      } else {
        setSuccessMsg("Account created! Logging you in...");
        setAuthMode("login");
      }
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleStartStrategy = async () => {
    if (!selectedStrategy || strategies.length === 0) return;
    setErrorMsg("");

    try {
      const subRes = await fetch(`${API_BASE}/api/v1/subscriptions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          strategy_version_id: selectedStrategy.versions[0].id,
          quantity: Number(qty),
          product_type: productType,
          max_profit: Number(maxProfit),
          max_loss: Number(maxLoss),
          config_json: { timeframeMin: timeframe, bypass_scheduler: bypassScheduler }
        })
      });

      if (!subRes.ok) throw new Error("Failed to create strategy subscription.");
      const subData = await subRes.json();
      const subId = subData.subscription_id;
      setSubscriptionId(subId);

      const toggleRes = await fetch(`${API_BASE}/api/v1/subscriptions/${subId}/toggle`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({ active: true })
      });

      if (!toggleRes.ok) {
        const details = await toggleRes.json();
        throw new Error(errorDetail(details, "Failed to start execution runner."));
      }

      setEngineActive(true);
      addTerminalLog("Engine", `Algo runner activated [ID=${subId.slice(0, 8)}]`);
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleStopStrategy = async () => {
    if (!subscriptionId) return;
    setErrorMsg("");

    try {
      const res = await fetch(`${API_BASE}/api/v1/subscriptions/${subscriptionId}/toggle`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({ active: false })
      });

      if (!res.ok) throw new Error("Failed to stop strategy runner.");
      setEngineActive(false);
      addTerminalLog("Engine", "Stop command dispatched. Engine halted.");
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleVerifyBroker = async (e) => {
    e.preventDefault();
    setVerifyingBroker(true);
    setErrorMsg("");
    setSuccessMsg("");

    try {
      const verifyRes = await fetch(`${API_BASE}/api/v1/broker/verify`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          broker_name: brokerName,
          client_id: clientId || "ALGO_DEMO_01",
          api_key: apiKey || "paper_key_vault"
        })
      });

      if (!verifyRes.ok) {
        const errData = await verifyRes.json();
        throw new Error(errorDetail(errData, "Verification failed"));
      }

      const verifyData = await verifyRes.json();

      // Save encrypted credentials to vault
      await fetch(`${API_BASE}/api/v1/broker/credentials`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          broker_name: brokerName,
          client_id: clientId || "ALGO_DEMO_01",
          api_key: apiKey || "paper_key_vault",
          api_secret: apiSecret
        })
      });

      setSuccessMsg(verifyData.message || "Trading account verified & vault-secured!");
      fetchBrokerStatus();
      fetchUserProfile();
    } catch (err) {
      setErrorMsg(err.message);
    } finally {
      setVerifyingBroker(false);
    }
  };

  const handleExecuteDemoTrade = async (side) => {
    setErrorMsg("");
    try {
      const symbol = selectedSegment === "fno" ? "NIFTY26JUL24300CE" : "NIFTY50-INDEX";
      const seg = selectedSegment === "fno" ? "FNO" : "INTRADAY";

      const res = await fetch(`${API_BASE}/api/v1/paper-trading/execute-demo-trade`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          side,
          quantity: Number(qty),
          segment: seg,
          symbol
        })
      });

      if (!res.ok) throw new Error("Failed to execute paper trade.");
      const data = await res.json();
      addTerminalLog("PaperBroker", `Executed ${side} ${qty} qty of ${symbol} @ ₹${data.result.fill_price}`);
      fetchPositions();
      fetchPortfolioSummary();
      fetchTrades();
      fetchUserProfile();
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleResetCapital = async () => {
    if (!window.confirm("Reset Paper Trading virtual margin to ₹5,00,000?")) return;
    try {
      const res = await fetch(`${API_BASE}/api/v1/paper-trading/reset`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        fetchUserProfile();
        fetchPortfolioSummary();
        addTerminalLog("System", "Paper Trading margin reset to ₹5,00,000.");
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Sparkline drawing helper
  const drawSparkline = () => {
    if (priceHistory.length < 2) return "";
    const min = Math.min(...priceHistory);
    const max = Math.max(...priceHistory);
    const range = max - min || 1;
    const height = 80;
    const width = 450;
    const points = priceHistory.map((val, idx) => {
      const x = (idx / (priceHistory.length - 1)) * width;
      const y = height - ((val - min) / range) * height + 5;
      return `${x},${y}`;
    });
    return points.join(" ");
  };

  // --- RENDER AUTHENTICATION VIEW ---
  if (!token) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", padding: "20px" }}>
        <div className="glass-panel" style={{ width: "100%", maxWidth: "460px", padding: "40px 32px" }}>
          <div style={{ textAlign: "center", marginBottom: "28px" }}>
            <div style={{ display: "inline-flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <span style={{ width: "10px", height: "10px", borderRadius: "50%", background: "var(--color-green)", boxShadow: "var(--shadow-neon-green)" }}></span>
              <span style={{ fontSize: "0.8rem", color: "var(--color-green)", fontWeight: "600", letterSpacing: "1px" }}>ALGO TRADING ENGINE</span>
            </div>
            <h1 style={{ fontSize: "1.9rem", fontWeight: "800", color: "#fff" }}>
              <span style={{ color: "var(--color-primary)", textShadow: "var(--shadow-neon-purple)" }}>ALGOTRADE</span> PRO
            </h1>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.88rem", marginTop: "6px" }}>
              {authMode === "login"
                ? "Sign in to monitor live algos, past trades & P&L"
                : "Create an account with instant Paper Trading access"}
            </p>
          </div>

          <form onSubmit={handleAuth} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            {authMode === "register" && (
              <div style={{ display: "flex", gap: "10px" }}>
                <input
                  type="text"
                  placeholder="First Name"
                  required
                  value={firstName}
                  onChange={(e) => setFirstName(e.target.value)}
                  style={{ width: "50%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.4)", color: "#fff", borderRadius: "8px" }}
                />
                <input
                  type="text"
                  placeholder="Last Name"
                  required
                  value={lastName}
                  onChange={(e) => setLastName(e.target.value)}
                  style={{ width: "50%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.4)", color: "#fff", borderRadius: "8px" }}
                />
              </div>
            )}

            <input
              type="email"
              placeholder="Email Address"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              style={{ width: "100%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.4)", color: "#fff", borderRadius: "8px" }}
            />

            <input
              type="password"
              placeholder="Password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={{ width: "100%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.4)", color: "#fff", borderRadius: "8px" }}
            />

            {authMode === "register" && (
              <div style={{ padding: "12px", borderRadius: "8px", background: "rgba(139, 92, 246, 0.1)", border: "1px solid rgba(139, 92, 246, 0.25)", fontSize: "0.8rem", color: "var(--color-secondary)" }}>
                ⚡ Auto-configured: <strong>Paper Trading Simulator</strong> loaded with <strong>₹5,00,000 Virtual Margin</strong> and pre-seeded Intraday & F&O past trade history.
              </div>
            )}

            {errorMsg && <p style={{ color: "var(--color-red)", fontSize: "0.85rem", textAlign: "center" }}>{errorMsg}</p>}
            {successMsg && <p style={{ color: "var(--color-green)", fontSize: "0.85rem", textAlign: "center" }}>{successMsg}</p>}

            <button
              type="submit"
              style={{ padding: "13px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-primary)", color: "#fff", fontSize: "1rem", fontWeight: "600", boxShadow: "var(--shadow-neon-purple)", marginTop: "4px" }}
            >
              {authMode === "login" ? "Enter Algo Trading Platform" : "Create Account & Start Trading"}
            </button>
          </form>

          <div style={{ marginTop: "24px", textAlign: "center" }}>
            <span style={{ color: "var(--color-text-muted)", fontSize: "0.9rem" }}>
              {authMode === "login" ? "Need a new algo account? " : "Already have an account? "}
              <button
                onClick={() => {
                  setAuthMode(authMode === "login" ? "register" : "login");
                  setErrorMsg("");
                  setSuccessMsg("");
                }}
                style={{ background: "none", border: "none", color: "var(--color-cyan)", cursor: "pointer", textDecoration: "underline", fontSize: "0.9rem", fontWeight: "600" }}
              >
                {authMode === "login" ? "Register Now" : "Sign In"}
              </button>
            </span>
          </div>
        </div>
      </div>
    );
  }

  // --- RENDER MAIN SAAS DASHBOARD ---
  const netMtm = portfolioSummary?.net_mtm ?? 0.0;
  const virtualMargin = userProfile?.virtual_balance ?? 500000.0;

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      {/* Top Application Header */}
      <header className="glass-panel app-header" style={{ margin: "14px 18px", borderRadius: "14px", display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 24px", flexWrap: "wrap", gap: "15px" }}>
        
        {/* Logo & Branding */}
        <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
          <div style={{ width: "36px", height: "36px", borderRadius: "10px", background: "linear-gradient(135deg, #8b5cf6, #06b6d4)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "800", color: "#fff", fontSize: "1.1rem" }}>
            AT
          </div>
          <div>
            <h1 style={{ fontSize: "1.25rem", fontWeight: "800", color: "#fff", letterSpacing: "0.5px", margin: 0 }}>
              <span style={{ color: "var(--color-primary)" }}>ALGOTRADE</span> PRO
            </h1>
            <span style={{ fontSize: "0.72rem", color: "var(--color-text-muted)", textTransform: "uppercase", letterSpacing: "1px" }}>
              Multi-Asset Algo Engine
            </span>
          </div>
        </div>

        {/* Paper Trading Account Badge */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px", background: "rgba(0,0,0,0.35)", padding: "6px 14px", borderRadius: "10px", border: "1px solid var(--border-glow)" }}>
          <span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--color-green)", boxShadow: "var(--shadow-neon-green)" }} className="pulse-glow"></span>
          <div>
            <div style={{ fontSize: "0.75rem", color: "var(--color-cyan)", fontWeight: "600" }}>
              {brokerStatus.broker_name || "Paper Trading Active"}
            </div>
            <div style={{ fontSize: "0.82rem", color: "#fff", fontWeight: "700" }}>
              Margin: ₹{virtualMargin.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
            </div>
          </div>
          <button
            onClick={handleResetCapital}
            title="Reset Virtual Margin to ₹5,00,000"
            style={{ background: "rgba(255,255,255,0.08)", border: "1px solid rgba(255,255,255,0.15)", borderRadius: "6px", color: "var(--color-text-muted)", fontSize: "0.72rem", padding: "4px 8px", cursor: "pointer" }}
          >
            Reset
          </button>
        </div>

        {/* Center Tabs Navigation */}
        <nav className="app-nav" style={{ display: "flex", gap: "10px" }}>
          <button
            onClick={() => setActiveTab("dashboard")}
            style={{
              padding: "8px 16px",
              borderRadius: "8px",
              border: activeTab === "dashboard" ? "1px solid var(--color-cyan)" : "1px solid transparent",
              background: activeTab === "dashboard" ? "rgba(6, 182, 212, 0.15)" : "transparent",
              color: activeTab === "dashboard" ? "var(--color-cyan)" : "var(--color-text-muted)",
              fontSize: "0.92rem",
              fontWeight: "600",
              cursor: "pointer",
              transition: "all 0.2s"
            }}
          >
            📊 Live Dashboard
          </button>
          <button
            onClick={() => setActiveTab("trades")}
            style={{
              padding: "8px 16px",
              borderRadius: "8px",
              border: activeTab === "trades" ? "1px solid var(--color-primary)" : "1px solid transparent",
              background: activeTab === "trades" ? "rgba(139, 92, 246, 0.15)" : "transparent",
              color: activeTab === "trades" ? "var(--color-secondary)" : "var(--color-text-muted)",
              fontSize: "0.92rem",
              fontWeight: "600",
              cursor: "pointer",
              transition: "all 0.2s"
            }}
          >
            📜 Past Trades & P&L
          </button>
          <button
            onClick={() => setActiveTab("broker")}
            style={{
              padding: "8px 16px",
              borderRadius: "8px",
              border: activeTab === "broker" ? "1px solid var(--color-green)" : "1px solid transparent",
              background: activeTab === "broker" ? "rgba(16, 185, 129, 0.15)" : "transparent",
              color: activeTab === "broker" ? "var(--color-green)" : "var(--color-text-muted)",
              fontSize: "0.92rem",
              fontWeight: "600",
              cursor: "pointer",
              transition: "all 0.2s"
            }}
          >
            🔑 Broker & Accounts
          </button>
        </nav>

        {/* User Info & Logout */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontSize: "0.85rem", fontWeight: "600", color: "#fff" }}>
              {userProfile?.first_name || "Trader"} {userProfile?.last_name || ""}
            </div>
            <div style={{ fontSize: "0.72rem", color: "var(--color-text-muted)" }}>
              {userProfile?.email || ""}
            </div>
          </div>
          <button
            onClick={() => setToken(null)}
            style={{ padding: "8px 14px", borderRadius: "8px", border: "1px solid var(--border-glow)", background: "rgba(255,255,255,0.06)", color: "#fff", cursor: "pointer", fontSize: "0.85rem", fontWeight: "500" }}
          >
            Logout
          </button>
        </div>
      </header>

      {/* Segments Selection Bar */}
      <section className="app-section" style={{ margin: "0 18px 16px 18px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px" }}>
          <span style={{ fontSize: "0.78rem", color: "var(--color-text-muted)", fontWeight: "600", textTransform: "uppercase", letterSpacing: "1px" }}>
            Market Segments Available
          </span>
          <span style={{ fontSize: "0.75rem", color: "var(--color-cyan)" }}>
            ⚡ 2 Active Segments · 4 Upcoming Soon
          </span>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "10px" }}>
          
          {/* Active: F&O */}
          <div
            onClick={() => setSelectedSegment("fno")}
            className={`segment-card ${selectedSegment === "fno" ? "selected" : ""}`}
          >
            <div>
              <div style={{ fontWeight: "700", color: "#fff", fontSize: "0.92rem" }}>F&O (Options)</div>
              <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>Nifty / BankNifty ATM</div>
            </div>
            <span className="badge-active">ACTIVE</span>
          </div>

          {/* Active: Intraday */}
          <div
            onClick={() => setSelectedSegment("intraday")}
            className={`segment-card ${selectedSegment === "intraday" ? "selected" : ""}`}
          >
            <div>
              <div style={{ fontWeight: "700", color: "#fff", fontSize: "0.92rem" }}>Intraday (MIS)</div>
              <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>Day Trading Auto-Squareoff</div>
            </div>
            <span className="badge-active">ACTIVE</span>
          </div>

          {/* Upcoming: Equity Delivery */}
          <div
            onClick={() => setUpcomingModal("Equity Delivery (CNC)")}
            className="segment-card locked"
          >
            <div>
              <div style={{ fontWeight: "600", color: "#9ca3af", fontSize: "0.92rem" }}>Equity Delivery</div>
              <div style={{ fontSize: "0.75rem", color: "#6b7280" }}>Cash CNC Swing Algo</div>
            </div>
            <span className="badge-upcoming shimmer-badge">UPCOMING SOON</span>
          </div>

          {/* Upcoming: Commodities */}
          <div
            onClick={() => setUpcomingModal("Commodities (MCX)")}
            className="segment-card locked"
          >
            <div>
              <div style={{ fontWeight: "600", color: "#9ca3af", fontSize: "0.92rem" }}>Commodities</div>
              <div style={{ fontSize: "0.75rem", color: "#6b7280" }}>MCX Crude & Gold</div>
            </div>
            <span className="badge-upcoming shimmer-badge">UPCOMING SOON</span>
          </div>

          {/* Upcoming: Currency */}
          <div
            onClick={() => setUpcomingModal("Currency (USDINR)")}
            className="segment-card locked"
          >
            <div>
              <div style={{ fontWeight: "600", color: "#9ca3af", fontSize: "0.92rem" }}>Currency (Forex)</div>
              <div style={{ fontSize: "0.75rem", color: "#6b7280" }}>USDINR / EURINR</div>
            </div>
            <span className="badge-upcoming shimmer-badge">UPCOMING SOON</span>
          </div>

          {/* Upcoming: Crypto Futures */}
          <div
            onClick={() => setUpcomingModal("Crypto Futures")}
            className="segment-card locked"
          >
            <div>
              <div style={{ fontWeight: "600", color: "#9ca3af", fontSize: "0.92rem" }}>Crypto Futures</div>
              <div style={{ fontSize: "0.75rem", color: "#6b7280" }}>BTC & ETH 24/7</div>
            </div>
            <span className="badge-upcoming shimmer-badge">UPCOMING SOON</span>
          </div>

        </div>
      </section>

      {/* Main Containers */}
      <main className="app-main" style={{ flex: 1, padding: "0 18px 20px 18px" }}>
        
        {/* ============================================================ */}
        {/* TAB 1: LIVE DASHBOARD & STRATEGY ENGINE                      */}
        {/* ============================================================ */}
        {activeTab === "dashboard" && (
          <div className="layout-dashboard" style={{ display: "grid", gridTemplateColumns: "360px 1fr", gap: "20px" }}>
            
            {/* Left Column Controls */}
            <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              
              {/* Controls Card */}
              <div className="glass-panel" style={{ padding: "22px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid rgba(255,255,255,0.08)", paddingBottom: "10px" }}>
                  <h3 style={{ color: "var(--color-secondary)", fontSize: "1.05rem", margin: 0 }}>
                    Strategy Parameters
                  </h3>
                  <span className={selectedSegment === "fno" ? "badge-fno" : "badge-intraday"}>
                    {selectedSegment === "fno" ? "F&O (Options)" : "Intraday (MIS)"}
                  </span>
                </div>
                
                <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
                  <div>
                    <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>
                      Active Strategy Script
                    </label>
                    <select style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                      <option>Nifty ATM WMA5/SMA1 Crossover Call (Heikin-Ashi)</option>
                    </select>
                  </div>

                  <div style={{ display: "flex", gap: "10px" }}>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Candle Interval</label>
                      <select value={timeframe} onChange={(e) => setTimeframe(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                        <option value={1}>1 Minute</option>
                        <option value={3}>3 Minutes</option>
                        <option value={5}>5 Minutes</option>
                      </select>
                    </div>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Lot Qty</label>
                      <input type="number" value={qty} onChange={(e) => setQty(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: "10px" }}>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Target PnL (₹)</label>
                      <input type="number" value={maxProfit} onChange={(e) => setMaxProfit(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
                    </div>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Max Stop Loss (₹)</label>
                      <input type="number" value={maxLoss} onChange={(e) => setMaxLoss(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: "10px" }}>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Product Code</label>
                      <select value={productType} onChange={(e) => setProductType(e.target.value)} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                        <option value="MIS">MIS (Intraday)</option>
                        <option value="NRML">NRML (Overnight F&O)</option>
                      </select>
                    </div>
                    <div style={{ width: "50%", display: "flex", flexDirection: "column", justifyContent: "center" }}>
                      <label style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", display: "flex", alignItems: "center", gap: "6px", cursor: "pointer", marginTop: "16px" }}>
                        <input type="checkbox" checked={bypassScheduler} onChange={(e) => setBypassScheduler(e.target.checked)} />
                        Bypass Timing
                      </label>
                    </div>
                  </div>

                  {errorMsg && <p style={{ color: "var(--color-red)", fontSize: "0.85rem" }}>{errorMsg}</p>}

                  {engineActive ? (
                    <button
                      onClick={handleStopStrategy}
                      style={{ marginTop: "6px", padding: "14px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-red)", color: "#fff", fontWeight: "700", fontSize: "0.95rem", boxShadow: "var(--shadow-neon-red)" }}
                    >
                      HALT ENGINE RUNNER
                    </button>
                  ) : (
                    <button
                      onClick={handleStartStrategy}
                      style={{ marginTop: "6px", padding: "14px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-cyan)", color: "#fff", fontWeight: "700", fontSize: "0.95rem", boxShadow: "var(--shadow-neon-cyan)" }}
                    >
                      DEPLOY TO RUNNER QUEUE
                    </button>
                  )}
                </div>
              </div>

              {/* Quick Paper Trading Execution Actions */}
              <div className="glass-panel" style={{ padding: "18px" }}>
                <h4 style={{ color: "var(--color-cyan)", fontSize: "0.9rem", marginBottom: "8px" }}>
                  ⚡ Quick Paper Trading Actions
                </h4>
                <p style={{ fontSize: "0.78rem", color: "var(--color-text-muted)", marginBottom: "12px" }}>
                  Manually trigger a simulated paper order to test routing & watch P&L update live.
                </p>
                <div style={{ display: "flex", gap: "10px" }}>
                  <button
                    onClick={() => handleExecuteDemoTrade("BUY")}
                    style={{ flex: 1, padding: "10px", borderRadius: "6px", background: "rgba(16, 185, 129, 0.2)", color: "var(--color-green)", border: "1px solid rgba(16, 185, 129, 0.4)", fontWeight: "700", cursor: "pointer" }}
                  >
                    + BUY 50 Qty
                  </button>
                  <button
                    onClick={() => handleExecuteDemoTrade("SELL")}
                    style={{ flex: 1, padding: "10px", borderRadius: "6px", background: "rgba(239, 68, 68, 0.2)", color: "var(--color-red)", border: "1px solid rgba(239, 68, 68, 0.4)", fontWeight: "700", cursor: "pointer" }}
                  >
                    - SELL 50 Qty
                  </button>
                </div>
              </div>

              {/* Engine Status Card */}
              <div className="glass-panel" style={{ padding: "18px", display: "flex", alignItems: "center", gap: "14px" }}>
                <div style={{ width: "12px", height: "12px", borderRadius: "50%", background: engineActive ? "var(--color-green)" : "var(--color-red)", boxShadow: engineActive ? "var(--shadow-neon-green)" : "var(--shadow-neon-red)" }} className={engineActive ? "pulse-glow" : ""}></div>
                <div>
                  <h4 style={{ color: "#fff", fontSize: "0.95rem" }}>
                    Engine Runtime Node: {engineActive ? "RUNNING" : "STOPPED"}
                  </h4>
                  <p style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                    {engineActive ? "Evaluating tick crossovers via Celery/FastAPI" : "Awaiting strategy start signal"}
                  </p>
                </div>
              </div>

            </div>

            {/* Right Column Monitoring */}
            <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              
              {/* Row Stats: PnL + Option Price */}
              <div className="layout-stats" style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: "20px" }}>
                
                {/* Real-time MTM Tracker */}
                <div className="glass-panel" style={{ padding: "22px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.82rem", textTransform: "uppercase", marginBottom: "6px" }}>
                      Total Net P&L (MTM)
                    </h4>
                    <h2 style={{ fontSize: "2.5rem", fontWeight: "800", color: netMtm >= 0 ? "var(--color-green)" : "var(--color-red)", textShadow: netMtm >= 0 ? "var(--shadow-neon-green)" : "var(--shadow-neon-red)", transition: "all 0.4s ease" }}>
                      {netMtm >= 0 ? `+₹${netMtm.toFixed(2)}` : `-₹${Math.abs(netMtm).toFixed(2)}`}
                    </h2>
                    <div style={{ fontSize: "0.8rem", color: "var(--color-text-muted)", marginTop: "4px" }}>
                      Realized: ₹{portfolioSummary?.total_realized_pnl?.toFixed(2) ?? "0.00"} · Unrealized: ₹{portfolioSummary?.total_unrealized_pnl?.toFixed(2) ?? "0.00"}
                    </div>
                  </div>
                  
                  {/* Position state details */}
                  <div style={{ textAlign: "right" }}>
                    <span className={positions.length > 0 ? "badge-active" : "badge-upcoming"}>
                      {positions.length > 0 ? "POSITION ACTIVE" : "NO OPEN POSITION"}
                    </span>
                    <p style={{ color: "#fff", fontWeight: "600", marginTop: "8px" }}>
                      {positions.length > 0 ? positions[0].trading_symbol : "Waiting for crossover"}
                    </p>
                    <p style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                      {positions.length > 0 ? `${positions[0].quantity} Qty @ Avg ₹${positions[0].average_price}` : "Heikin-Ashi signals listening"}
                    </p>
                  </div>
                </div>

                {/* Option LTP Panel */}
                <div className="glass-panel" style={{ padding: "20px", display: "flex", flexDirection: "column", justifyContent: "center" }}>
                  <span className="badge-fno" style={{ alignSelf: "flex-start", marginBottom: "6px" }}>
                    ATM OPTION TICK
                  </span>
                  <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.82rem", textTransform: "uppercase", marginBottom: "4px" }}>
                    {currentStrike}
                  </h4>
                  <h3 style={{ fontSize: "2rem", color: "var(--color-cyan)", textShadow: "var(--shadow-neon-cyan)", margin: 0 }}>
                    ₹{currentLtp.toFixed(2)}
                  </h3>
                </div>
              </div>

              {/* Sparkline Price chart */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
                  <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}>
                    Real-time Option Contract Tick Sparkline ({currentStrike})
                  </h4>
                  <span style={{ fontSize: "0.78rem", color: "var(--color-cyan)" }}>
                    LTP: ₹{currentLtp.toFixed(2)}
                  </span>
                </div>
                <div style={{ background: "rgba(0,0,0,0.45)", borderRadius: "8px", padding: "10px", display: "flex", justifyContent: "center", overflow: "hidden" }}>
                  {priceHistory.length > 1 ? (
                    <svg width="450" height="90" style={{ overflow: "visible" }}>
                      <polyline
                        fill="none"
                        stroke="var(--color-cyan)"
                        strokeWidth="2.5"
                        points={drawSparkline()}
                        style={{ filter: "drop-shadow(0 0 5px rgba(6, 182, 212, 0.5))" }}
                      />
                    </svg>
                  ) : (
                    <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", padding: "25px" }}>Streaming ticks over WebSocket...</p>
                  )}
                </div>
              </div>

              {/* Retro Terminal Event logs */}
              <div className="glass-panel" style={{ padding: "20px", flex: 1, display: "flex", flexDirection: "column" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
                  <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}>
                    Engine Event Terminal
                  </h4>
                  <button
                    onClick={() => setEventLogs([])}
                    style={{ background: "none", border: "none", color: "var(--color-text-muted)", fontSize: "0.75rem", cursor: "pointer" }}
                  >
                    Clear Terminal
                  </button>
                </div>
                <div style={{ background: "#050308", border: "1px solid #1f122e", borderRadius: "8px", padding: "14px", flex: 1, overflowY: "auto", maxHeight: "250px", fontFamily: "var(--font-mono)", fontSize: "0.82rem", color: "#10b981", display: "flex", flexDirection: "column", gap: "6px" }}>
                  {eventLogs.length === 0 ? (
                    <p style={{ color: "#06b6d4" }}>[SYS] Awaiting strategy engine crossover triggers...</p>
                  ) : (
                    eventLogs.map((log, idx) => (
                      <div key={idx} style={{ display: "flex", gap: "8px" }}>
                        <span style={{ color: "rgba(16,185,129,0.5)" }}>[{log.ts}]</span>
                        <span style={{ color: log.type === "System" ? "var(--color-cyan)" : log.type === "OrderFilled" ? "var(--color-green)" : log.type === "OrderPlaced" ? "var(--color-secondary)" : "#10b981" }}>
                          [{log.type}]
                        </span>
                        <span style={{ color: "#e5e7eb" }}>{log.message}</span>
                      </div>
                    ))
                  )}
                  <div ref={terminalEndRef}></div>
                </div>
              </div>

            </div>
          </div>
        )}

        {/* ============================================================ */}
        {/* TAB 2: PAST TRADES & COMPREHENSIVE P&L                       */}
        {/* ============================================================ */}
        {activeTab === "trades" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
            
            {/* Top KPI Metrics Banner */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "16px" }}>
              
              {/* Total Realized PnL */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", textTransform: "uppercase" }}>Total Realized P&L</span>
                <h3 style={{ fontSize: "1.8rem", fontWeight: "800", color: (portfolioSummary?.total_realized_pnl ?? 0) >= 0 ? "var(--color-green)" : "var(--color-red)", marginTop: "6px" }}>
                  {(portfolioSummary?.total_realized_pnl ?? 0) >= 0 ? `+₹${(portfolioSummary?.total_realized_pnl ?? 0).toFixed(2)}` : `-₹${Math.abs(portfolioSummary?.total_realized_pnl ?? 0).toFixed(2)}`}
                </h3>
                <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", marginTop: "4px" }}>
                  After Fees: ₹{portfolioSummary?.net_pnl_after_fees?.toFixed(2) ?? "0.00"}
                </div>
              </div>

              {/* Win Rate */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", textTransform: "uppercase" }}>Win Rate</span>
                <h3 style={{ fontSize: "1.8rem", fontWeight: "800", color: "var(--color-cyan)", marginTop: "6px" }}>
                  {portfolioSummary?.win_rate_percent ?? 0}%
                </h3>
                <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", marginTop: "4px" }}>
                  {portfolioSummary?.winning_trades ?? 0} Won · {portfolioSummary?.losing_trades ?? 0} Lost
                </div>
              </div>

              {/* Total Trades Executed */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", textTransform: "uppercase" }}>Total Trades</span>
                <h3 style={{ fontSize: "1.8rem", fontWeight: "800", color: "#fff", marginTop: "6px" }}>
                  {portfolioSummary?.total_trades ?? tradesList.length}
                </h3>
                <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", marginTop: "4px" }}>
                  Intraday & F&O Execution Fills
                </div>
              </div>

              {/* Paper Trading Commission Saved */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", textTransform: "uppercase" }}>Brokerage Tracked</span>
                <h3 style={{ fontSize: "1.8rem", fontWeight: "800", color: "var(--color-secondary)", marginTop: "6px" }}>
                  ₹{portfolioSummary?.total_brokerage?.toFixed(2) ?? "0.00"}
                </h3>
                <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", marginTop: "4px" }}>
                  Zero-Commission Paper Simulation
                </div>
              </div>

            </div>

            {/* Filter Controls Bar */}
            <div className="glass-panel" style={{ padding: "16px 20px", display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "12px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "14px", flexWrap: "wrap" }}>
                <div>
                  <label style={{ fontSize: "0.78rem", color: "var(--color-text-muted)", display: "block", marginBottom: "4px" }}>Filter Segment</label>
                  <select
                    value={tradeFilterSegment}
                    onChange={(e) => setTradeFilterSegment(e.target.value)}
                    style={{ padding: "8px 12px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)", fontSize: "0.85rem" }}
                  >
                    <option value="ALL">All Segments</option>
                    <option value="FNO">F&O (Options)</option>
                    <option value="INTRADAY">Intraday (MIS)</option>
                  </select>
                </div>

                <div>
                  <label style={{ fontSize: "0.78rem", color: "var(--color-text-muted)", display: "block", marginBottom: "4px" }}>Side</label>
                  <select
                    value={tradeFilterSide}
                    onChange={(e) => setTradeFilterSide(e.target.value)}
                    style={{ padding: "8px 12px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)", fontSize: "0.85rem" }}
                  >
                    <option value="ALL">All Sides</option>
                    <option value="BUY">BUY Only</option>
                    <option value="SELL">SELL Only</option>
                  </select>
                </div>
              </div>

              <div style={{ display: "flex", gap: "10px" }}>
                <button
                  onClick={fetchTrades}
                  style={{ padding: "8px 16px", borderRadius: "6px", border: "1px solid var(--border-glow)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "0.85rem", cursor: "pointer" }}
                >
                  🔄 Refresh Trades
                </button>
              </div>
            </div>

            {/* Past Trades Table */}
            <div className="glass-panel" style={{ overflowX: "auto", padding: "0" }}>
              <table className="trade-table">
                <thead>
                  <tr>
                    <th>Date & Time</th>
                    <th>Order / Trade ID</th>
                    <th>Strategy</th>
                    <th>Instrument</th>
                    <th>Segment</th>
                    <th>Side</th>
                    <th>Qty</th>
                    <th>Fill Price</th>
                    <th>Realized P&L</th>
                    <th>Fees</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {tradesLoading ? (
                    <tr>
                      <td colSpan="11" style={{ textAlign: "center", padding: "40px", color: "var(--color-text-muted)" }}>
                        Loading trades history...
                      </td>
                    </tr>
                  ) : tradesList.length === 0 ? (
                    <tr>
                      <td colSpan="11" style={{ textAlign: "center", padding: "40px", color: "var(--color-text-muted)" }}>
                        No past trades found for current filter.
                      </td>
                    </tr>
                  ) : (
                    tradesList.map((t) => (
                      <tr key={t.id}>
                        <td style={{ fontSize: "0.82rem", color: "var(--color-text-muted)", whiteSpace: "nowrap" }}>
                          {t.executed_at ? new Date(t.executed_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "medium" }) : "-"}
                        </td>
                        <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.78rem", color: "var(--color-cyan)" }}>
                          {t.broker_order_id ? t.broker_order_id.slice(0, 14) : t.id.slice(0, 8)}
                        </td>
                        <td style={{ fontSize: "0.82rem", color: "#d1d5db" }}>
                          {t.strategy_name || "WMA5/SMA1 Crossover"}
                        </td>
                        <td style={{ fontWeight: "700", color: "#fff" }}>
                          {t.trading_symbol}
                        </td>
                        <td>
                          <span className={t.segment === "FNO" ? "badge-fno" : "badge-intraday"}>
                            {t.segment}
                          </span>
                        </td>
                        <td>
                          <span className={t.side === "BUY" ? "badge-buy" : "badge-sell"}>
                            {t.side}
                          </span>
                        </td>
                        <td style={{ fontWeight: "600" }}>
                          {t.quantity}
                        </td>
                        <td style={{ fontWeight: "600", color: "#fff" }}>
                          ₹{t.execution_price.toFixed(2)}
                        </td>
                        <td>
                          {t.realized_pnl !== 0.0 ? (
                            <span style={{ fontWeight: "700", color: t.realized_pnl > 0 ? "var(--color-green)" : "var(--color-red)" }}>
                              {t.realized_pnl > 0 ? `+₹${t.realized_pnl.toFixed(2)}` : `-₹${Math.abs(t.realized_pnl).toFixed(2)}`}
                            </span>
                          ) : (
                            <span style={{ color: "var(--color-text-muted)" }}>Open Leg</span>
                          )}
                        </td>
                        <td style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                          ₹{t.brokerage_fees.toFixed(2)}
                        </td>
                        <td>
                          <span className="badge-active">
                            {t.status}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

          </div>
        )}

        {/* ============================================================ */}
        {/* TAB 3: BROKER & TRADING ACCOUNT VAULT                        */}
        {/* ============================================================ */}
        {activeTab === "broker" && (
          <div style={{ maxWidth: "540px", margin: "20px auto" }}>
            <div className="glass-panel" style={{ padding: "30px" }}>
              <div style={{ marginBottom: "22px" }}>
                <h3 style={{ color: "var(--color-secondary)", fontSize: "1.2rem", margin: 0 }}>
                  Trading Account Credentials Vault
                </h3>
                <p style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", marginTop: "6px" }}>
                  Join your paper trading or live broker account. Orders will be routed and executed through it, while all profits and losses are tracked in real-time.
                </p>
              </div>

              {/* Current Active Broker Card */}
              <div style={{ background: "rgba(0,0,0,0.3)", borderRadius: "10px", padding: "14px 18px", border: "1px solid var(--border-glow)", marginBottom: "24px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", textTransform: "uppercase" }}>Currently Connected</div>
                  <div style={{ fontWeight: "700", color: "#fff", fontSize: "1rem" }}>
                    {brokerStatus.broker_name || "Paper Trading Simulator"}
                  </div>
                  <div style={{ fontSize: "0.8rem", color: "var(--color-cyan)" }}>
                    Client ID: {brokerStatus.client_id || "ALGO_PAPER_01"}
                  </div>
                </div>
                <span className="badge-active">CONNECTED</span>
              </div>

              <form onSubmit={handleVerifyBroker} style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
                <div>
                  <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "6px" }}>
                    Select Broker / Trading Gateway
                  </label>
                  <select
                    value={brokerName}
                    onChange={(e) => setBrokerName(e.target.value)}
                    style={{ width: "100%", padding: "12px", borderRadius: "8px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}
                  >
                    <option value="paper_trading">Paper Trading Simulator (Default - Risk-Free)</option>
                    <option value="kite">Zerodha Kite Connect API</option>
                    <option value="shoonya">Finvasia Shoonya API</option>
                  </select>
                </div>

                <div>
                  <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "6px" }}>
                    Client ID / Account User ID
                  </label>
                  <input
                    type="text"
                    required
                    placeholder={brokerName === "paper_trading" ? "e.g. ALGO_PAPER_01" : "Broker Client ID"}
                    value={clientId}
                    onChange={(e) => setClientId(e.target.value)}
                    style={{ width: "100%", padding: "12px", borderRadius: "8px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}
                  />
                </div>

                <div>
                  <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "6px" }}>
                    API Key / Access Token
                  </label>
                  <input
                    type="password"
                    required
                    placeholder={brokerName === "paper_trading" ? "paper_key_vault" : "Broker API Key"}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    style={{ width: "100%", padding: "12px", borderRadius: "8px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}
                  />
                </div>

                {brokerName !== "paper_trading" && (
                  <div>
                    <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "6px" }}>
                      API Secret / Password / TOTP
                    </label>
                    <input
                      type="password"
                      placeholder="API Secret Key"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      style={{ width: "100%", padding: "12px", borderRadius: "8px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}
                    />
                  </div>
                )}

                {errorMsg && <p style={{ color: "var(--color-red)", fontSize: "0.85rem" }}>{errorMsg}</p>}
                {successMsg && <p style={{ color: "var(--color-green)", fontSize: "0.85rem" }}>{successMsg}</p>}

                <button
                  type="submit"
                  disabled={verifyingBroker}
                  style={{ padding: "14px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-cyan)", color: "#fff", fontWeight: "700", fontSize: "0.95rem", boxShadow: "var(--shadow-neon-cyan)", marginTop: "6px" }}
                >
                  {verifyingBroker ? "Authenticating Account..." : "Test Connection & Store Credentials"}
                </button>
              </form>
            </div>
          </div>
        )}

      </main>

      {/* Upcoming Soon Segment Modal */}
      {upcomingModal && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.75)", backdropFilter: "blur(4px)", display: "flex", justifyContent: "center", alignItems: "center", zIndex: 1000, padding: "20px" }}>
          <div className="glass-panel" style={{ maxWidth: "420px", padding: "32px", textAlign: "center", border: "1px solid rgba(245, 158, 11, 0.4)", boxShadow: "0 0 25px rgba(245, 158, 11, 0.2)" }}>
            <span className="badge-upcoming shimmer-badge" style={{ fontSize: "0.82rem", padding: "4px 12px" }}>
              UPCOMING SOON
            </span>
            <h3 style={{ color: "#fff", marginTop: "16px", fontSize: "1.3rem" }}>
              {upcomingModal}
            </h3>
            <p style={{ color: "var(--color-text-muted)", fontSize: "0.88rem", marginTop: "10px", lineHeight: 1.5 }}>
              The multi-asset execution adapter for <strong>{upcomingModal}</strong> is in development for Phase 2.
              <br /><br />
              Currently, <strong>Intraday (MIS)</strong> and <strong>F&O (Options)</strong> are fully active with paper trading order routing and live P&L tracking.
            </p>
            <button
              onClick={() => setUpcomingModal(null)}
              style={{ marginTop: "20px", padding: "10px 24px", borderRadius: "8px", border: "none", background: "var(--color-primary)", color: "#fff", fontWeight: "600", cursor: "pointer", boxShadow: "var(--shadow-neon-purple)" }}
            >
              Understand
            </button>
          </div>
        </div>
      )}

    </div>
  );
}

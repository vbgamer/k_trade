import React, { useState, useEffect, useRef } from "react";

export default function App() {
  // Session State
  const [token, setToken] = useState(localStorage.getItem("token"));
  const [authMode, setAuthMode] = useState("login"); // login | register
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [successMsg, setSuccessMsg] = useState("");

  // Navigation tab
  const [activeTab, setActiveTab] = useState("dashboard");

  // Dashboard Options Strategy Settings
  const [qty, setQty] = useState(50);
  const [maxProfit, setMaxProfit] = useState(2000);
  const [maxLoss, setMaxLoss] = useState(1000);
  const [productType, setProductType] = useState("MIS");
  const [timeframe, setTimeframe] = useState(1);
  const [bypassScheduler, setBypassScheduler] = useState(true); // Default to bypass for easy local test

  // Running engine states
  const [strategies, setStrategies] = useState([]);
  const [selectedStrategy, setSelectedStrategy] = useState(null);
  const [subscriptionId, setSubscriptionId] = useState(null);
  const [engineActive, setEngineActive] = useState(false);
  const [mtm, setMtm] = useState(0.0);
  const [positions, setPositions] = useState([]);
  const [eventLogs, setEventLogs] = useState([]);
  const [priceHistory, setPriceHistory] = useState([]);
  const [currentLtp, setCurrentLtp] = useState(150.0);
  const [currentStrike, setCurrentStrike] = useState("NIFTY26JUL24300CE");

  // Broker Credentials form
  const [brokerName, setBrokerName] = useState("kite");
  const [apiKey, setApiKey] = useState("");
  const [clientId, setClientId] = useState("");

  // System audit logs
  const [auditLogs, setAuditLogs] = useState([]);

  // Websocket ref
  const ws = useRef(null);
  const terminalEndRef = useRef(null);

  // Load configuration & verify authentication
  useEffect(() => {
    if (token) {
      localStorage.setItem("token", token);
      fetchStrategies();
      fetchPositions();
      connectWebSocket();
    } else {
      localStorage.removeItem("token");
      if (ws.current) ws.current.close();
    }
    return () => {
      if (ws.current) ws.current.close();
    };
  }, [token]);

  // Autoscroll logs terminal console
  useEffect(() => {
    if (terminalEndRef.current) {
      terminalEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [eventLogs]);

  // --- WEBSOCKET CONNECTION ---
  const connectWebSocket = () => {
    if (ws.current) {
      ws.current.close();
    }
    
    // Connect to FastAPI ws gateway
    const wsUrl = `ws://${window.location.hostname}:8000/api/v1/ws/stream`;
    ws.current = new WebSocket(wsUrl);
    
    ws.current.onopen = () => {
      addTerminalLog("System", "WebSocket connection opened to Event Bus.");
    };
    
    ws.current.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        const { event_type, data } = payload;
        
        if (event_type === "TickUpdate") {
          const ltp = data.ltp;
          setCurrentLtp(ltp);
          setCurrentStrike(data.instrument_key.split("|")[-1] || data.instrument_key);
          setPriceHistory(prev => {
            const next = [...prev, ltp];
            return next.slice(-40); // Keep last 40 tick points for SVG sparkline
          });
        } else {
          // Structured logs events handler
          const time = new Date().toLocaleTimeString();
          addTerminalLog(event_type, JSON.stringify(data), time);
          
          if (event_type === "StrategyStarted") {
            setEngineActive(true);
          } else if (event_type === "StrategyStopped" || event_type === "RiskTriggered") {
            setEngineActive(false);
            fetchPositions();
          } else if (event_type === "OrderFilled") {
            fetchPositions();
          }
        }
      } catch (err) {
        console.error("Error parsing WS packet:", err);
      }
    };
    
    ws.current.onclose = () => {
      addTerminalLog("System", "WebSocket closed. Retrying connection in 5s...");
      setTimeout(connectWebSocket, 5000);
    };
  };

  const addTerminalLog = (type, message, time = null) => {
    const ts = time || new Date().toLocaleTimeString();
    setEventLogs(prev => [...prev, { ts, type, message }]);
  };

  // --- API CALLS ---
  const fetchStrategies = async () => {
    try {
      const res = await fetch("http://localhost:8000/api/v1/strategies", {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await res.json();
      setStrategies(data);
      if (data.length > 0) {
        setSelectedStrategy(data[0]);
      }
    } catch (err) {
      console.error("Error fetching strategies catalogue:", err);
    }
  };

  const fetchPositions = async () => {
    if (!token) return;
    try {
      const res = await fetch("http://localhost:8000/api/v1/portfolio/positions", {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await res.json();
      setPositions(data);
      
      // Compute total realized & unrealized PnL MTM
      const total = data.reduce((acc, pos) => acc + (pos.realized_pnl || 0) + (pos.unrealized_pnl || 0), 0);
      setMtm(Number(total.toFixed(2)));
    } catch (err) {
      console.error("Error fetching positions:", err);
    }
  };

  const handleAuth = async (e) => {
    e.preventDefault();
    setErrorMsg("");
    setSuccessMsg("");
    
    const url = authMode === "login" 
      ? "http://localhost:8000/api/v1/auth/login" 
      : "http://localhost:8000/api/v1/auth/register";
      
    const body = authMode === "login"
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
        throw new Error(data.detail || "Authentication request failed");
      }
      
      const data = await res.json();
      if (authMode === "login") {
        setToken(data.access_token);
      } else {
        setSuccessMsg("Registration successful! Please sign in.");
        setAuthMode("login");
      }
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleBrokerSubmit = async (e) => {
    e.preventDefault();
    setErrorMsg("");
    setSuccessMsg("");
    
    try {
      const res = await fetch("http://localhost:8000/api/v1/broker/credentials", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({
          broker_name: brokerName,
          api_key: apiKey,
          client_id: clientId
        })
      });
      
      if (!res.ok) throw new Error("Failed to save credentials");
      setSuccessMsg("Broker credentials encrypted and vault saved successfully!");
      setApiKey("");
      setClientId("");
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleStartStrategy = async () => {
    if (!selectedStrategy || strategies.length === 0) return;
    setErrorMsg("");
    
    try {
      // 1. Create subscription
      const subRes = await fetch("http://localhost:8000/api/v1/subscriptions", {
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
      
      // 2. Start running strategy task
      const toggleRes = await fetch(`http://localhost:8000/api/v1/subscriptions/${subId}/toggle`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({ active: true })
      });
      
      if (!toggleRes.ok) {
        const details = await toggleRes.json();
        throw new Error(details.detail || "Failed to start execution runner.");
      }
      
      setEngineActive(true);
      addTerminalLog("Engine", `Started strategy subscription runtime id=${subId}`);
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  const handleStopStrategy = async () => {
    if (!subscriptionId) return;
    setErrorMsg("");
    
    try {
      const res = await fetch(`http://localhost:8000/api/v1/subscriptions/${subscriptionId}/toggle`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`
        },
        body: JSON.stringify({ active: false })
      });
      
      if (!res.ok) throw new Error("Failed to stop strategy runtime.");
      setEngineActive(false);
      addTerminalLog("Engine", "Stop command sent. Stopping StrategyRuntime task.");
    } catch (err) {
      setErrorMsg(err.message);
    }
  };

  // SVG Sparkline drawing helper
  const drawSparkline = () => {
    if (priceHistory.length < 2) return "";
    const min = Math.min(...priceHistory);
    const max = Math.max(...priceHistory);
    const range = max - min || 1;
    const height = 80;
    const width = 450;
    const points = priceHistory.map((val, idx) => {
      const x = (idx / (priceHistory.length - 1)) * width;
      const y = height - ((val - min) / range) * height + 5; // offset margins
      return `${x},${y}`;
    });
    return points.join(" ");
  };

  // --- RENDER AUTHENTICATION IF NO JWT ---
  if (!token) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh" }}>
        <div className="glass-panel" style={{ width: "100%", maxWidth: "420px", padding: "40px 30px" }}>
          <h2 style={{ textAlign: "center", marginBottom: "25px", fontSize: "1.8rem", color: "var(--color-primary)", textShadow: "0 0 10px rgba(139, 92, 246, 0.3)" }}>
            OPTIONS SAAS PLATFORM
          </h2>
          
          <form onSubmit={handleAuth} style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
            {authMode === "register" && (
              <>
                <div style={{ display: "flex", gap: "10px" }}>
                  <input 
                    type="text" 
                    placeholder="First Name" 
                    value={firstName} 
                    onChange={e => setFirstName(e.target.value)} 
                    style={{ width: "50%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.3)", color: "#fff", borderRadius: "8px" }} 
                  />
                  <input 
                    type="text" 
                    placeholder="Last Name" 
                    value={lastName} 
                    onChange={e => setLastName(e.target.value)} 
                    style={{ width: "50%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.3)", color: "#fff", borderRadius: "8px" }} 
                  />
                </div>
              </>
            )}
            
            <input 
              type="email" 
              placeholder="Email Address" 
              required 
              value={email} 
              onChange={e => setEmail(e.target.value)} 
              style={{ width: "100%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.3)", color: "#fff", borderRadius: "8px" }} 
            />
            
            <input 
              type="password" 
              placeholder="Password" 
              required 
              value={password} 
              onChange={e => setPassword(e.target.value)} 
              style={{ width: "100%", padding: "12px", border: "1px solid var(--border-glow)", background: "rgba(0,0,0,0.3)", color: "#fff", borderRadius: "8px" }} 
            />
            
            {errorMsg && <p style={{ color: "var(--color-red)", fontSize: "0.85rem", textAlign: "center" }}>{errorMsg}</p>}
            {successMsg && <p style={{ color: "var(--color-green)", fontSize: "0.85rem", textAlign: "center" }}>{successMsg}</p>}
            
            <button 
              type="submit" 
              style={{ padding: "12px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-primary)", color: "#fff", fontSize: "1rem", fontWeight: "600", boxShadow: "var(--shadow-neon-purple)" }}
            >
              {authMode === "login" ? "Sign In" : "Register"}
            </button>
          </form>
          
          <div style={{ marginTop: "20px", textAlign: "center" }}>
            <span style={{ color: "var(--color-text-muted)", fontSize: "0.9rem" }}>
              {authMode === "login" ? "New to the platform? " : "Already have an account? "}
              <button 
                onClick={() => setAuthMode(authMode === "login" ? "register" : "login")} 
                style={{ background: "none", border: "none", color: "var(--color-cyan)", cursor: "pointer", textDecoration: "underline", fontSize: "0.9rem" }}
              >
                {authMode === "login" ? "Create Account" : "Sign In"}
              </button>
            </span>
          </div>
        </div>
      </div>
    );
  }

  // --- RENDER MAIN SAAS DASHBOARD ---
  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      {/* Navigation Header bar */}
      <header className="glass-panel" style={{ margin: "15px", borderRadius: "12px", display: "flex", justifyContent: "space-between", alignItems: "center", padding: "15px 30px" }}>
        <h1 style={{ fontSize: "1.4rem", fontWeight: "700", color: "#fff", display: "flex", alignItems: "center", gap: "10px" }}>
          <span style={{ color: "var(--color-primary)", textShadow: "var(--shadow-neon-purple)" }}>SAAS</span> OPTIONS ENGINE
        </h1>
        
        <nav style={{ display: "flex", gap: "25px" }}>
          <button 
            onClick={() => setActiveTab("dashboard")} 
            style={{ background: "none", border: "none", color: activeTab === "dashboard" ? "var(--color-cyan)" : "var(--color-text-muted)", fontSize: "1rem", cursor: "pointer", fontWeight: "500", textShadow: activeTab === "dashboard" ? "var(--shadow-neon-cyan)" : "none" }}
          >
            Dashboard
          </button>
          <button 
            onClick={() => setActiveTab("broker")} 
            style={{ background: "none", border: "none", color: activeTab === "broker" ? "var(--color-cyan)" : "var(--color-text-muted)", fontSize: "1rem", cursor: "pointer", fontWeight: "500", textShadow: activeTab === "broker" ? "var(--shadow-neon-cyan)" : "none" }}
          >
            Broker Config
          </button>
        </nav>
        
        <button 
          onClick={() => setToken(null)} 
          style={{ padding: "8px 16px", borderRadius: "6px", border: "1px solid var(--border-glow)", background: "rgba(255,255,255,0.05)", color: "#fff", cursor: "pointer", fontSize: "0.9rem" }}
        >
          Logout
        </button>
      </header>

      {/* Main Containers */}
      <main style={{ flex: 1, padding: "0 15px 20px 15px", display: "flex", flexDirection: "column", gap: "20px" }}>
        
        {activeTab === "dashboard" && (
          <div style={{ display: "grid", gridTemplateColumns: "350px 1fr", gap: "20px" }}>
            
            {/* Left Column Controls */}
            <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              
              {/* Controls Card */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <h3 style={{ marginBottom: "15px", borderBottom: "1px solid rgba(255,255,255,0.08)", paddingBottom: "10px", color: "var(--color-secondary)" }}>
                  Strategy Settings
                </h3>
                
                <div style={{ display: "flex", flexDirection: "column", gap: "15px" }}>
                  <div>
                    <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Strategy Registry</label>
                    <select style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                      <option>Nifty ATM WMA5/SMA1 Crossover</option>
                    </select>
                  </div>

                  <div style={{ display: "flex", gap: "10px" }}>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Timeframe</label>
                      <select value={timeframe} onChange={e => setTimeframe(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                        <option value={1}>1m</option>
                        <option value={3}>3m</option>
                        <option value={5}>5m</option>
                      </select>
                    </div>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Quantity</label>
                      <input type="number" value={qty} onChange={e => setQty(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: "10px" }}>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Max Profit (INR)</label>
                      <input type="number" value={maxProfit} onChange={e => setMaxProfit(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
                    </div>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Max Loss (INR)</label>
                      <input type="number" value={maxLoss} onChange={e => setMaxLoss(Number(e.target.value))} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: "10px" }}>
                    <div style={{ width: "50%" }}>
                      <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Product</label>
                      <select value={productType} onChange={e => setProductType(e.target.value)} style={{ width: "100%", padding: "10px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                        <option value="MIS">MIS (Intraday)</option>
                        <option value="NRML">NRML (Carry)</option>
                      </select>
                    </div>
                    <div style={{ width: "50%", display: "flex", flexDirection: "column", justifyContent: "center" }}>
                      <label style={{ fontSize: "0.85rem", color: "var(--color-text-muted)", display: "flex", alignItems: "center", gap: "5px", cursor: "pointer", marginTop: "15px" }}>
                        <input type="checkbox" checked={bypassScheduler} onChange={e => setBypassScheduler(e.target.checked)} />
                        Bypass Timing
                      </label>
                    </div>
                  </div>

                  {errorMsg && <p style={{ color: "var(--color-red)", fontSize: "0.85rem" }}>{errorMsg}</p>}

                  {engineActive ? (
                    <button 
                      onClick={handleStopStrategy} 
                      style={{ marginTop: "10px", padding: "14px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-red)", color: "#fff", fontWeight: "600", fontSize: "1rem", boxShadow: "var(--shadow-neon-red)" }}
                    >
                      STOP ENGINE RUNNER
                    </button>
                  ) : (
                    <button 
                      onClick={handleStartStrategy} 
                      style={{ marginTop: "10px", padding: "14px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-cyan)", color: "#fff", fontWeight: "600", fontSize: "1rem", boxShadow: "var(--shadow-neon-cyan)" }}
                    >
                      DEPLOY TO RUNNER QUEUE
                    </button>
                  )}
                </div>
              </div>

              {/* Engine Status Card */}
              <div className="glass-panel" style={{ padding: "20px", display: "flex", alignItems: "center", gap: "15px" }}>
                <div style={{ width: "12px", height: "12px", borderRadius: "50%", background: engineActive ? "var(--color-green)" : "var(--color-red)", boxShadow: engineActive ? "var(--shadow-neon-green)" : "var(--shadow-neon-red)" }} className={engineActive ? "pulse-glow" : ""}></div>
                <div>
                  <h4 style={{ color: "#fff" }}>Engine Runtime Node</h4>
                  <p style={{ fontSize: "0.85rem", color: "var(--color-text-muted)" }}>
                    {engineActive ? "Durable Task running on Celery Worker" : "Halted. Awaiting deployment command."}
                  </p>
                </div>
              </div>
            </div>

            {/* Right Column Monitoring */}
            <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              
              {/* Row Stats */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: "20px" }}>
                
                {/* Real-time MTM Tracker */}
                <div className="glass-panel" style={{ padding: "25px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", textTransform: "uppercase", marginBottom: "8px" }}>Live Unrealized / Realized P&L</h4>
                    <h2 style={{ fontSize: "2.5rem", fontWeight: "800", color: mtm >= 0 ? "var(--color-green)" : "var(--color-red)", textShadow: mtm >= 0 ? "var(--shadow-neon-green)" : "var(--shadow-neon-red)", transition: "all 0.5s ease" }}>
                      {mtm >= 0 ? `+₹${mtm}` : `-₹${Math.abs(mtm)}`}
                    </h2>
                  </div>
                  
                  {/* Position state details */}
                  <div style={{ textAlign: "right" }}>
                    <p style={{ color: "#fff", fontWeight: "600" }}>{positions.length > 0 ? "ACTIVE POSITION" : "NO POSITION"}</p>
                    <p style={{ fontSize: "0.85rem", color: "var(--color-text-muted)" }}>
                      {positions.length > 0 ? `${positions[0].trading_symbol} x ${positions[0].quantity} Qty` : "Listening for triggers..."}
                    </p>
                  </div>
                </div>

                {/* Option LTP Panel */}
                <div className="glass-panel" style={{ padding: "20px", display: "flex", flexDirection: "column", justifyContent: "center" }}>
                  <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", textTransform: "uppercase", marginBottom: "5px" }}>{currentStrike}</h4>
                  <h3 style={{ fontSize: "1.8rem", color: "var(--color-cyan)", textShadow: "var(--shadow-neon-cyan)" }}>
                    ₹{currentLtp.toFixed(2)}
                  </h3>
                </div>
              </div>

              {/* Sparkline Price chart */}
              <div className="glass-panel" style={{ padding: "20px" }}>
                <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginBottom: "15px" }}>Real-time Option Contract Ticks</h4>
                <div style={{ background: "rgba(0,0,0,0.4)", borderRadius: "8px", padding: "10px", display: "flex", justifyContent: "center" }}>
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
                    <p style={{ color: "var(--color-text-muted)", fontSize: "0.9rem", padding: "30px" }}>Awaiting WebSocket tick stream...</p>
                  )}
                </div>
              </div>

              {/* Retro Terminal Event logs */}
              <div className="glass-panel" style={{ padding: "20px", flex: 1, display: "flex", flexDirection: "column" }}>
                <h4 style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", marginBottom: "12px" }}>Engine Event Terminal</h4>
                <div style={{ background: "#050308", border: "1px solid #1f122e", borderRadius: "8px", padding: "15px", flex: 1, overflowY: "auto", maxHeight: "250px", fontFamily: "var(--font-mono)", fontSize: "0.85rem", color: "#10b981", display: "flex", flexDirection: "column", gap: "8px" }}>
                  {eventLogs.length === 0 ? (
                    <p style={{ color: "#06b6d4" }}>[SYS] Awaiting strategy engine triggers...</p>
                  ) : (
                    eventLogs.map((log, idx) => (
                      <div key={idx} style={{ display: "flex", gap: "10px" }}>
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

        {activeTab === "broker" && (
          <div className="glass-panel" style={{ maxWidth: "500px", margin: "20px auto", padding: "30px" }}>
            <h3 style={{ marginBottom: "20px", color: "var(--color-secondary)" }}>Connect Broker Vault</h3>
            
            <form onSubmit={handleBrokerSubmit} style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              <div>
                <label style={{ fontSize: "0.9rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Broker API Adapter</label>
                <select value={brokerName} onChange={e => setBrokerName(e.target.value)} style={{ width: "100%", padding: "12px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }}>
                  <option value="kite">Zerodha Kite API</option>
                  <option value="shoonya">Finvasia Shoonya API</option>
                </select>
              </div>

              <div>
                <label style={{ fontSize: "0.9rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>Client ID</label>
                <input type="text" required placeholder="Broker Client ID (e.g. AB1234)" value={clientId} onChange={e => setClientId(e.target.value)} style={{ width: "100%", padding: "12px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
              </div>

              <div>
                <label style={{ fontSize: "0.9rem", color: "var(--color-text-muted)", display: "block", marginBottom: "5px" }}>API Key</label>
                <input type="password" required placeholder="Broker Client Secret / API Key" value={apiKey} onChange={e => setApiKey(e.target.value)} style={{ width: "100%", padding: "12px", borderRadius: "6px", background: "#110c1c", color: "#fff", border: "1px solid var(--border-glow)" }} />
              </div>

              {errorMsg && <p style={{ color: "var(--color-red)", fontSize: "0.9rem" }}>{errorMsg}</p>}
              {successMsg && <p style={{ color: "var(--color-green)", fontSize: "0.9rem" }}>{successMsg}</p>}

              <button 
                type="submit" 
                style={{ padding: "12px", borderRadius: "8px", border: "none", cursor: "pointer", background: "var(--color-cyan)", color: "#fff", fontWeight: "600", fontSize: "1rem", boxShadow: "var(--shadow-neon-cyan)" }}
              >
                Store Encrypted Credentials
              </button>
            </form>
          </div>
        )}

      </main>
    </div>
  );
}

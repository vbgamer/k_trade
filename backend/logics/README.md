# Algorithmic Trading Logics & Strategies

This directory houses the proprietary quant algorithms, logic definitions, and trading rules for **AlgoTrade Pro**.

---

## Directory Structure

```
backend/logics/
├── nifty_wma5_sma1_call/
│   ├── logic.py               # Algorithmic execution logic for ATM Call buying/selling
│   └── meta.json              # Strategy metadata, parameters schema & indicators
└── nifty_wma5_sma2_put/
    ├── logic.py               # Algorithmic execution logic for ATM Put hedging & shorting
    └── meta.json              # Strategy metadata, parameters schema & indicators
```

---

## Strategy Details

### 1. Nifty ATM WMA5/SMA1 Crossover (Call Strategy)
- **Folder**: `nifty_wma5_sma1_call/`
- **Underlying**: NIFTY Index Options (At-The-Money Call options)
- **Candle Type**: Heikin Ashi (1-Minute / 3-Minute / 5-Minute)
- **Indicators**:
  - `WMA 5` (Weighted Moving Average on Close)
  - `SMA 1` (Simple Moving Average on Open)
- **Trigger**: Crossover of WMA 5 above SMA 1 indicates strong bullish momentum for CE entry.

### 2. Nifty ATM WMA5/SMA2 Crossover (Put Strategy)
- **Folder**: `nifty_wma5_sma2_put/`
- **Underlying**: NIFTY Index Options (At-The-Money Put options)
- **Candle Type**: Heikin Ashi
- **Indicators**:
  - `WMA 5` (Weighted Moving Average on Close)
  - `SMA 2` (Simple Moving Average on Open)
- **Trigger**: Crossover of WMA 5 above SMA 2 indicates downward momentum for PE entry.

---

## Strategy Integration
These logic rules are wired into the real-time execution engine in `backend/app/engine/runtime.py` and `backend/app/strategies/`.

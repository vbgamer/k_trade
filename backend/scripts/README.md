# Backend Utility & Operational Scripts

This folder contains command-line scripts to seed data, verify system health, and simulate market ticks.

---

## Available Scripts

### 1. `seed_demo_data.py`
Seeds the database with test user accounts, Paper Trading broker connections, and realistic Intraday and F&O trades with P&L calculations.
```bash
python scripts/seed_demo_data.py
# or via manage.py
python manage.py seed
```

### 2. `simulate_market_ticks.py`
Simulates realistic live market tick data (Nifty & BankNifty strike options) for real-time testing.
```bash
python scripts/simulate_market_ticks.py [duration_seconds]
```

### 3. `verify_system.py`
Performs an automated system diagnostics check on database connectivity, table integrity, and portfolio totals.
```bash
python scripts/verify_system.py
```

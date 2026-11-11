"""ML agent: tree evaluator, IST minute bars, exit rules, live decision + journal, status API."""
import asyncio
import math
from datetime import date, datetime

import pytest

from app.core.memory_bus import (
    MINUTE_BAR_KEY, ai_status, chain_history, in_memory_quotes, index_daily, market_snapshot, minute_bars,
)
from app.engine import minute_bars as mb
from app.ml import agent
from app.ml.features import FEATURE_NAMES
from app.ml.gbm import GBMModel
from app.services.upstox_session import IST

INDEX_KEY = "NSE_INDEX|Nifty 50"
DAY = date(2026, 11, 10)
EXPIRY = date(2026, 11, 17)


def _ist(h, m, s=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=IST)


# --- evaluator -------------------------------------------------------------------

def _tree(feature_idx, threshold, left, right):
    return {"tree_structure": {"split_feature": feature_idx, "threshold": threshold, "decision_type": "<=",
                               "missing_type": "None", "default_left": True,
                               "left_child": {"leaf_value": left}, "right_child": {"leaf_value": right}}}


def test_gbm_sums_trees_and_treats_nan_as_zero():
    m = GBMModel({"feature_names": ["a", "b"], "tree_info": [_tree(0, 1.0, -1.0, 2.0), _tree(1, 0.5, 0.25, 0.75)]})
    assert m.predict({"a": 0.5, "b": 1.0}) == pytest.approx(-1.0 + 0.75)
    assert m.predict({"a": 3.0, "b": 0.0}) == pytest.approx(2.0 + 0.25)
    assert m.predict({"a": float("nan"), "b": 0.0}) == pytest.approx(-1.0 + 0.25)  # NaN -> 0 -> left


def test_exported_models_cover_feature_list():
    models = agent.load_models()
    assert models is not None, "run ml/export_model.py to deploy a model"
    assert models["ce"].feature_names == FEATURE_NAMES
    assert models["pe"].feature_names == FEATURE_NAMES


# --- minute bars -------------------------------------------------------------------

def test_minute_bars_align_to_ist_minutes_and_backfill():
    minute_bars.clear()
    mb._state["day"] = None
    t0 = _ist(10, 0, 5).timestamp()
    assert mb.update("K", 100.0, t0) is None
    mb.update("K", 103.0, t0 + 20)
    mb.update("K", 99.0, t0 + 40)
    closed = mb.update("K", 101.0, _ist(10, 1, 2).timestamp())
    assert closed == {"minute": 600, "open": 100.0, "high": 103.0, "low": 99.0, "close": 99.0}
    now = _ist(10, 1, 30).timestamp()
    assert [b["minute"] for b in mb.closed_bars("K", now)] == [600]

    added = mb.backfill("K", [{"minute": m, "open": 1, "high": 1, "low": 1, "close": 1} for m in (555, 556, 600, 601)], now)
    assert added == 2  # 600 already live, 601 still forming
    assert [b["minute"] for b in minute_bars["K"]] == [555, 556, 600, 601]

    mb.update("K", 5.0, datetime(2026, 11, 11, 9, 15, tzinfo=IST).timestamp())  # next day resets
    assert [b["minute"] for b in minute_bars["K"]] == [555]


# --- exits ------------------------------------------------------------------------

SIM = {"target_pct": 0.15, "stop_pct": 0.08, "max_hold_min": 30}


@pytest.mark.parametrize("bar,ltp,held,minute,expected", [
    ({"low": 91, "high": 101}, 95, 3, 600, "stop"),
    ({"low": 95, "high": 116}, 110, 3, 600, "target"),
    ({"low": 91, "high": 116}, 100, 3, 600, "stop"),  # both touched: stop first, like the backtest
    ({"low": 97, "high": 103}, 100, 30, 600, "time"),
    ({"low": 97, "high": 103}, 100, 3, 915, "squareoff"),
    ({"low": 97, "high": 103}, 100, 3, 600, None),
    (None, 91.5, 3, 600, "stop"),
])
def test_exit_rules_match_training(bar, ltp, held, minute, expected):
    assert agent.exit_reason(entry_price=100.0, bar=bar, ltp=ltp, held_minutes=held,
                             minute_now=minute, sim=SIM) == expected


# --- live decision ----------------------------------------------------------------

def _seed_market(last_minute):
    """A synthetic uptrending day from 09:15 to last_minute with an ATM 24000 chain."""
    minute_bars.clear()
    chain_history.clear()
    mb._state["day"] = DAY
    bars = []
    for i, m in enumerate(range(9 * 60 + 15, last_minute + 1)):
        px = 23950 + i * 1.5 + 3 * math.sin(i / 3)
        bars.append({"minute": m, "open": px - 1, "high": px + 2, "low": px - 2, "close": px})
    minute_bars[INDEX_KEY] = bars
    spot = bars[-1]["close"]
    k = agent.atm_strike(spot)
    rows = []
    for j in range(-3, 4):
        s = k + 50 * j
        rows.append({"strike": s,
                     "ce": {"instrument_key": f"NSE_FO|{s}CE", "trading_symbol": f"NIFTY{s}CE",
                            "ltp": 120 - 20 * j, "bid": 119.9 - 20 * j, "ask": 120.1 - 20 * j, "oi": 1e6, "prev_oi": 9e5},
                     "pe": {"instrument_key": f"NSE_FO|{s}PE", "trading_symbol": f"NIFTY{s}PE",
                            "ltp": 110 + 20 * j, "bid": 109.9 + 20 * j, "ask": 110.1 + 20 * j, "oi": 1.2e6, "prev_oi": 1e6}})
    for b in bars:
        chain_history.setdefault(k, {})[b["minute"]] = (118.0, 112.0)
    market_snapshot.update(expiry=EXPIRY.isoformat(), rows=rows, spot=spot, spot_prev_close=23900.0)
    index_daily.update(prev_close=23900.0, prev_day_ret=0.004)
    return k, rows


def _agent(threshold, mode="auto"):
    from app.strategies.aiOptionsAgent.strategy import AIOptionsAgent
    a = AIOptionsAgent({"quantity": 65, "user_id": "u-test", "ai_mode": mode, "ai_threshold": threshold})
    return a


def _candle(minute):
    return {"instrument_key": MINUTE_BAR_KEY, "minute": minute, "time": f"{DAY} {minute // 60:02d}:{minute % 60:02d}"}


def test_live_features_complete_and_finite():
    k, rows = _seed_market(10 * 60 + 30)
    f, row, why = agent.build_live_features(
        bars=minute_bars[INDEX_KEY], rows=rows, chain_hist=chain_history, today=DAY, expiry=EXPIRY,
        prev_close=23900.0, prev_day_ret=0.004)
    assert why == "ok" and row["strike"] == k
    assert set(FEATURE_NAMES) <= set(f)
    assert all(math.isfinite(f[n]) for n in FEATURE_NAMES)
    assert f["days_to_expiry"] == 7 and f["mins_since_open"] == 76


def test_agent_waits_without_a_full_day_of_bars():
    _seed_market(10 * 60 + 30)
    minute_bars[INDEX_KEY] = minute_bars[INDEX_KEY][20:]  # started late, no backfill
    a = _agent(threshold=-1.0)
    a.clock = lambda: _ist(10, 31, 1)
    assert asyncio.run(a.on_candle(_candle(10 * 60 + 30), [])) is None
    assert "full day" in ai_status["u-test"]["message"]


def test_agent_buys_when_prediction_clears_threshold_and_journals_features():
    k, _ = _seed_market(10 * 60 + 30)
    a = _agent(threshold=-1.0)  # everything clears
    a.clock = lambda: _ist(10, 31, 1)
    sig = asyncio.run(a.on_candle(_candle(10 * 60 + 30), []))
    assert sig["action"] == "BUY" and sig["quantity"] == 65
    assert sig["instrument_key"] in (f"NSE_FO|{k}CE", f"NSE_FO|{k}PE")
    assert sig["stop_loss"] < sig["entry_price"] < sig["target"]
    assert set(FEATURE_NAMES) <= set(sig["journal"]["features"])
    assert ai_status["u-test"]["signal"] is True


def test_agent_suggest_mode_and_threshold_never_trade():
    _seed_market(10 * 60 + 30)
    a = _agent(threshold=-1.0, mode="suggest")
    a.clock = lambda: _ist(10, 31, 1)
    assert asyncio.run(a.on_candle(_candle(10 * 60 + 30), [])) is None
    assert ai_status["u-test"]["message"].startswith("SUGGESTION")

    b = _agent(threshold=10.0)
    b.clock = lambda: _ist(10, 31, 1)
    assert asyncio.run(b.on_candle(_candle(10 * 60 + 30), [])) is None
    assert ai_status["u-test"]["message"].startswith("No edge")


def test_agent_respects_entry_window_and_trade_limit():
    _seed_market(14 * 60 + 40)
    a = _agent(threshold=-1.0)
    a.clock = lambda: _ist(14, 41, 1)
    assert asyncio.run(a.on_candle(_candle(14 * 60 + 40), [])) is None  # after 14:30

    _seed_market(10 * 60 + 30)
    a.clock = lambda: _ist(10, 31, 1)
    a.trades_today = a.max_trades
    assert asyncio.run(a.on_candle(_candle(10 * 60 + 30), [])) is None


def test_agent_exits_on_stop_and_at_squareoff():
    _seed_market(10 * 60 + 30)
    key = "NSE_FO|24000CE"
    pos = {"instrument_key": key, "trading_symbol": "NIFTY24000CE", "quantity": 65, "average_price": 100.0}
    a = _agent(threshold=-1.0)
    a.sim = SIM  # pin the exit rules (the deployed model's rule can change on retrain)
    a.clock = lambda: _ist(10, 31, 1)
    a.entry = {"key": key, "minute": 10 * 60 + 20, "price": 100.0}
    in_memory_quotes[key] = {"ltp": 99.0, "bid": 98.9, "ask": 99.1}
    minute_bars[key] = [{"minute": 630, "open": 100, "high": 101, "low": 97, "close": 99},
                        {"minute": 631, "open": 99, "high": 99, "low": 99, "close": 99}]
    assert asyncio.run(a.on_candle(_candle(630), [pos])) is None  # -3%: hold

    minute_bars[key][0]["low"] = 90.0  # dipped through the 8% stop during the last minute
    sig = asyncio.run(a.on_candle(_candle(630), [pos]))
    assert sig["action"] == "SELL" and sig["quantity"] == 65 and sig["journal"]["exit_reason"] == "stop"

    minute_bars[key][0]["low"] = 97.0
    a.clock = lambda: _ist(15, 15, 3)
    sig = asyncio.run(a.on_candle(_candle(914), [pos]))
    assert sig["journal"]["exit_reason"] == "squareoff"


def test_agent_ignores_other_candles():
    a = _agent(threshold=-1.0)
    assert asyncio.run(a.on_candle({"instrument_key": "NSE_FO|1", "minute": 600}, [])) is None


# --- API / catalogue ----------------------------------------------------------------

def test_ai_strategy_registered_and_status_endpoint(client, make_user):
    _, _, token = make_user()
    h = {"Authorization": f"Bearer {token}"}
    slugs = {s["slug"] for s in client.get("/api/v1/strategies", headers=h).json()}
    assert {"aiOptionsAgent", "kaushalcustomWMASMACall1"} <= slugs
    res = client.get("/api/v1/ai/status", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] is None and body["model"]["deployed"] is True


def test_starting_a_strategy_stops_a_different_running_one(client, make_user):
    _, _, token = make_user()
    h = {"Authorization": f"Bearer {token}"}
    strategies = {s["slug"]: s for s in client.get("/api/v1/strategies", headers=h).json()}
    ids = []
    for slug in ("kaushalcustomWMASMACall1", "aiOptionsAgent"):
        sub = client.post("/api/v1/subscriptions", headers=h, json={
            "strategy_version_id": strategies[slug]["versions"][0]["id"], "quantity": 65,
            "product_type": "MIS", "max_profit": 1000, "max_loss": 2000,
            "config_json": {"bypass_scheduler": True, "ai_mode": "suggest"}}).json()["subscription_id"]
        assert client.post(f"/api/v1/subscriptions/{sub}/toggle", headers=h, json={"active": True}).status_code == 200
        ids.append(sub)
    status = {s["subscription_id"]: s["status"] for s in client.get("/api/v1/subscriptions", headers=h).json()}
    assert status[ids[0]] == "stopped" and status[ids[1]] == "running"
    client.post(f"/api/v1/subscriptions/{ids[1]}/toggle", headers=h, json={"active": False})


def test_market_candles_endpoint_returns_ist_minute_bars(client, make_user):
    _, _, token = make_user()
    h = {"Authorization": f"Bearer {token}"}
    import time as _t
    now = _t.time()
    today, minute = mb.ist_minute(now)
    mb._state["day"] = today
    minute_bars["NSE_FO|CHART"] = [
        {"minute": minute - 1, "open": 100.0, "high": 104.0, "low": 99.0, "close": 103.0},
        {"minute": minute, "open": 103.0, "high": 103.5, "low": 102.0, "close": 102.5},
    ]
    res = client.get("/api/v1/market/candles", params={"instrument_key": "NSE_FO|CHART"}, headers=h)
    assert res.status_code == 200
    bars = res.json()["bars"]
    assert [b["close"] for b in bars] == [103.0, 102.5]
    # time is the IST wall-clock minute as a UTC epoch (what the chart displays)
    t = datetime.utcfromtimestamp(bars[0]["time"])
    assert (t.hour * 60 + t.minute) == minute - 1 and t.date() == today
    assert client.get("/api/v1/market/candles", params={"instrument_key": "NSE_FO|NONE"}, headers=h).json()["bars"] == []
    assert client.get("/api/v1/market/candles", params={"instrument_key": "X"}).status_code in (401, 403)


def test_backfilled_bars_are_json_safe_when_they_close():
    """Upstox candles carry datetime fields; a backfilled bar closing must still publish as JSON."""
    import json as _json
    minute_bars.clear()
    mb._state["day"] = None
    now = _ist(11, 55, 1).timestamp()
    hist = [{"ts": _ist(11, 54), "date": DAY, "minute": 714, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]
    assert mb.backfill("K", hist, now) == 1
    closed = mb.update("K", 1.6, now)
    assert closed["minute"] == 714
    _json.dumps(closed)  # must not raise


# --- admin AI Lab -----------------------------------------------------------------

def test_explanations_add_up_to_the_prediction():
    models = agent.load_models()
    assert models["ce"].explainable, "re-export models with ml/export_model.py (keeps internal_value)"
    _seed_market(10 * 60 + 30)
    f, _, _ = agent.build_live_features(
        bars=minute_bars[INDEX_KEY], rows=market_snapshot["rows"], chain_hist=chain_history, today=DAY,
        expiry=EXPIRY, prev_close=23900.0, prev_day_ret=0.004)
    for side in ("ce", "pe"):
        ex = models[side].explain(f)
        assert ex["base"] + sum(ex["contributions"].values()) == pytest.approx(models[side].predict(f), abs=1e-12)


def test_live_decision_and_minute_score_log():
    from app.core.memory_bus import ai_score_log
    from app.ml import live
    _seed_market(10 * 60 + 30)
    d = live.decide(_ist(10, 31, 5))
    assert d["status"] == "ok" and d["minute"] == 630 and d["time"] == "10:31" and d["in_window"]
    assert d["decision"].startswith(("BUY", "WAIT"))
    assert len(d["explain"]["ce"]["reasons"]) == live.TOP_REASONS
    ai_score_log.update(day=None, points=[])
    assert live.record_minute(_ist(10, 31, 5))["minute"] == 630
    assert live.record_minute(_ist(10, 31, 40)) is None  # one point per minute
    assert len(ai_score_log["points"]) == 1
    market_snapshot.pop("expiry")
    assert live.decide(_ist(10, 31, 5))["status"] == "waiting"


def test_journal_pairs_entries_with_exits():
    from app.ml import live
    rows = [
        {"user_id": "u", "user": "a@x", "at": "t1", "symbol": "N1CE",
         "metadata": {"action": "BUY", "instrument_key": "K1", "side": "CE", "pred_ce": 0.05, "pred_pe": -0.1,
                      "threshold": 0.002, "result": {"status": "FILLED", "fill_price": 100.0}}},
        {"user_id": "u", "user": "a@x", "at": "t2", "symbol": "N1CE",
         "metadata": {"action": "SELL", "instrument_key": "K1", "exit_reason": "stop",
                      "result": {"status": "FILLED", "fill_price": 70.0}}},
        {"user_id": "u", "user": "a@x", "at": "t3", "symbol": "N2PE",
         "metadata": {"action": "BUY", "instrument_key": "K2", "side": "PE", "pred_ce": 0.0, "pred_pe": 0.03,
                      "result": {"status": "FILLED", "fill_price": 50.0}}},
    ]
    newest, oldest = live.pair_journal(rows)
    assert newest["status"] == "open" and newest["pred_pct"] == 3.0
    assert oldest["status"] == "closed" and oldest["actual_pct"] == -30.0 and oldest["exit_reason"] == "stop"
    assert oldest["pred_pct"] == 5.0 and oldest["model_was_right"] is False


def test_ai_lab_is_admin_only(client, make_user, monkeypatch):
    from app.core.config import settings
    email, _, token = make_user()
    h = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
    assert client.get("/api/v1/admin/ai-lab", headers=h).status_code == 403
    assert client.get("/api/v1/user/profile", headers=h).json()["is_admin"] is False
    monkeypatch.setattr(settings, "ADMIN_EMAILS", f"someone@else.com, {email.upper()}")
    assert client.get("/api/v1/user/profile", headers=h).json()["is_admin"] is True
    res = client.get("/api/v1/admin/ai-lab", headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deployed"] and body["report"]["model"]["features"] == FEATURE_NAMES
    assert {"train", "validation", "test", "rounds"} <= set(body["report"]["learning_curves"]["ce"])
    assert body["journal"] == [] and "status" in body["live"]
    assert client.get("/api/v1/admin/ai-lab").status_code in (401, 403)

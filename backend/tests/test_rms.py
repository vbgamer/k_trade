import inspect

import pytest

import app.engine.rms as rms
from app.engine.rms import DayStats, RMSService, is_exit_order, size_from_risk


class Sub:
    def __init__(self, quantity=50, max_loss=0.0, config_json=None):
        self.quantity = quantity
        self.max_loss = max_loss
        self.config_json = config_json or {}


def test_rms_module_imports_time():
    # Regression: liquidation builds client_order_id with time.time(); the missing import
    # stopped the strategy but left its positions open.
    assert getattr(rms, "time", None) is not None


def test_rms_pre_trade_rejects_oversized_orders():
    class Sub:
        quantity = 50

    assert rms.RMSService.validate_pre_trade_limits(Sub(), "BUY", 100, []) is True
    assert rms.RMSService.validate_pre_trade_limits(Sub(), "BUY", 101, []) is False


def test_rms_falls_back_to_memory_ltp_when_redis_down():
    src = inspect.getsource(rms.RMSService.run_rms_circuit_breaker)
    assert "in_memory_ltps" in src


# --- Pre-trade gate ----------------------------------------------------------

KEY = "NSE_OPTION|NIFTY26JUL24300CE"
LONG_50 = [{"instrument_key": KEY, "quantity": 50}]


def test_exit_detection():
    assert is_exit_order("SELL", 50, KEY, LONG_50)
    assert is_exit_order("SELL", 25, KEY, LONG_50)
    assert not is_exit_order("SELL", 75, KEY, LONG_50)  # would flip to short
    assert not is_exit_order("BUY", 50, KEY, LONG_50)  # adds to the long
    assert not is_exit_order("SELL", 50, "NSE_OPTION|OTHER", LONG_50)


def test_max_trades_per_day_blocks_entries_but_not_exits():
    sub = Sub(config_json={"max_trades_per_day": 2})
    stats = DayStats(entries_today=2)
    entry = RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY, stats)
    assert not entry.allowed and "Max trades" in entry.reason
    assert RMSService.evaluate_pre_trade(sub, "SELL", 50, LONG_50, KEY, stats).allowed
    assert RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY, DayStats(entries_today=1)).allowed


def test_daily_realized_loss_blocks_new_entries():
    sub = Sub(max_loss=2000)
    blocked = RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY, DayStats(realized_pnl_today=-2000))
    assert not blocked.allowed and "Daily loss" in blocked.reason
    assert RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY, DayStats(realized_pnl_today=-1999)).allowed
    assert RMSService.evaluate_pre_trade(sub, "SELL", 50, LONG_50, KEY, DayStats(realized_pnl_today=-5000)).allowed


def test_min_reward_to_risk():
    sub = Sub(config_json={"min_reward_to_risk": 1.5})
    poor = RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY, entry_price=100, stop_loss=90, target=110)
    assert not poor.allowed and "Reward-to-risk" in poor.reason
    assert RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY, entry_price=100, stop_loss=90, target=115).allowed
    # Signals without stop/target are not judged on reward-to-risk
    assert RMSService.evaluate_pre_trade(sub, "BUY", 50, [], KEY).allowed


def test_size_from_risk_rounds_down_to_lots():
    assert size_from_risk(2000, 100, 90, lot_size=75) == 150  # 2000 / (10*75) = 2.67 lots -> 2
    assert size_from_risk(500, 100, 90, lot_size=75) == 0  # one lot already risks 750
    assert size_from_risk(1000, 100, 100) == 0  # zero-distance stop is rejected


# --- Wired into ExecutionService ---------------------------------------------

def test_execution_enforces_max_trades_per_day(client, monkeypatch):
    import app.broker.mock_live_adapter as adapter
    from app.core.database import async_session_maker
    from app.db.models import User, StrategySubscription, StrategyVersion
    from app.services.execution import ExecutionService
    from sqlalchemy.future import select

    monkeypatch.setattr(adapter.random, "random", lambda: 0.5)  # disable simulated broker drops

    async def scenario():
        async with async_session_maker() as db:
            user = User(email="rms_exec@example.com", password_hash="x")
            db.add(user)
            await db.flush()
            version = (await db.execute(select(StrategyVersion))).scalars().first()
            sub = StrategySubscription(
                user_id=user.id, strategy_version_id=version.id, quantity=50,
                config_json={"max_trades_per_day": 1},
            )
            db.add(sub)
            await db.commit()

            svc = ExecutionService(db)
            first = await svc.execute_trade(sub.id, "BUY", KEY, 50, "rms_t1_buy")
            exit_ = await svc.execute_trade(sub.id, "SELL", KEY, 50, "rms_t1_sell")
            second = await svc.execute_trade(sub.id, "BUY", KEY, 50, "rms_t2_buy")
            retry = await svc.execute_trade(sub.id, "BUY", KEY, 50, "rms_t2_buy")
            return first, exit_, second, retry

    first, exit_, second, retry = client.portal.call(scenario)
    assert first["status"] == "FILLED"
    assert exit_["status"] == "FILLED"
    assert second["status"] == "REJECTED" and "Max trades" in second["rejection_reason"]
    assert retry.get("duplicate") is True

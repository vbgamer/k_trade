import asyncio
import time
from datetime import date, datetime, timezone

import httpx
import pytest

from app.broker.mock_live_adapter import paper_fill_from_quote
from app.broker.upstox import (
    UpstoxAuthError, UpstoxClient, authorization_url, build_chain_rows, nearest_expiry
)
from app.core.memory_bus import in_memory_quotes, market_snapshot
from app.services import upstox_session


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def run(coro):
    return asyncio.run(coro)


CE_KEY, PE_KEY = "NSE_FO|1001", "NSE_FO|1002"


# --- Client parsing -------------------------------------------------------------

def test_quotes_are_keyed_by_instrument_token_with_best_bid_ask():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.url.params["instrument_key"] == f"{CE_KEY},NSE_INDEX|Nifty 50"
        return httpx.Response(200, json={"status": "success", "data": {
            "NSE_FO:NIFTY26NOV24300CE": {
                "instrument_token": CE_KEY, "last_price": 120.5, "net_change": 10.5, "oi": 5000, "volume": 900,
                "depth": {"buy": [{"price": 120.4}], "sell": [{"price": 120.7}]},
            },
            "NSE_INDEX:Nifty 50": {"instrument_token": "NSE_INDEX|Nifty 50", "last_price": 24310.2, "depth": {}},
        }})

    client = UpstoxClient("tok", transport=httpx.MockTransport(handler))
    quotes = run(client.get_quotes([CE_KEY, "NSE_INDEX|Nifty 50"]))
    assert quotes[CE_KEY] == {"ltp": 120.5, "bid": 120.4, "ask": 120.7, "oi": 5000, "volume": 900, "prev_close": 110.0}
    assert quotes["NSE_INDEX|Nifty 50"]["ltp"] == 24310.2
    assert quotes["NSE_INDEX|Nifty 50"]["bid"] == 0.0


def test_expired_token_raises_auth_error():
    client = UpstoxClient("old", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(UpstoxAuthError):
        run(client.get_quotes([CE_KEY]))


def test_authorization_url_carries_redirect_and_state():
    url = authorization_url("key123", "https://lakxmi.duckdns.org/api/v1/broker/upstox/callback", "st")
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
    assert "client_id=key123" in url and "state=st" in url
    assert "redirect_uri=https%3A%2F%2Flakxmi.duckdns.org%2Fapi%2Fv1%2Fbroker%2Fupstox%2Fcallback" in url


def test_nearest_expiry_skips_past_dates():
    contracts = [{"expiry": "2026-11-03"}, {"expiry": "2026-11-17"}, {"expiry": "2026-11-10"}]
    assert nearest_expiry(contracts, date(2026, 11, 9)) == "2026-11-10"
    assert nearest_expiry(contracts, date(2026, 12, 1)) is None


def test_chain_rows_window_around_atm():
    chain = [
        {"strike_price": s, "underlying_spot_price": 24312.0,
         "call_options": {"instrument_key": f"NSE_FO|C{s}", "market_data": {"ltp": 1.0}, "option_greeks": {"delta": 0.5}},
         "put_options": {"instrument_key": f"NSE_FO|P{s}", "market_data": {"ltp": 2.0}, "option_greeks": {}}}
        for s in range(24000, 24650, 50)
    ]
    contracts = [{"instrument_key": "NSE_FO|C24300", "trading_symbol": "NIFTY 24300 CE 10 NOV 26", "lot_size": 65}]
    snap = build_chain_rows(chain, contracts, strikes_each_side=2)
    assert [r["strike"] for r in snap["rows"]] == [24200, 24250, 24300, 24350, 24400]
    assert snap["atm_strike"] == 24300 and snap["atm_ce_key"] == "NSE_FO|C24300"
    assert snap["rows"][2]["ce"]["trading_symbol"] == "NIFTY 24300 CE 10 NOV 26"
    assert snap["lot_size"] == 65


# --- Session expiry -----------------------------------------------------------

def test_token_expires_at_330_am_ist():
    issued_day = datetime(2026, 11, 9, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    assert upstox_session.token_expiry(issued_day).isoformat() == "2026-11-10T03:30:00+05:30"
    issued_night = datetime(2026, 11, 8, 20, 30, tzinfo=timezone.utc)  # 02:00 IST on the 9th
    assert upstox_session.token_expiry(issued_night).isoformat() == "2026-11-09T03:30:00+05:30"


# --- Paper fills at live prices ------------------------------------------------

@pytest.fixture
def live_quote():
    in_memory_quotes[CE_KEY] = {"ltp": 100.0, "bid": 99.8, "ask": 100.3, "ts": time.time(), "live": True}
    yield
    in_memory_quotes.pop(CE_KEY, None)


def test_market_orders_cross_the_spread(live_quote):
    buy = paper_fill_from_quote("b1", "BUY", CE_KEY, "MARKET", None)
    sell = paper_fill_from_quote("s1", "SELL", CE_KEY, "MARKET", None)
    assert (buy["status"], buy["fill_price"], buy["slippage"]) == ("FILLED", 100.3, 0.3)
    assert (sell["status"], sell["fill_price"]) == ("FILLED", 99.8)


def test_limit_orders_fill_only_when_marketable(live_quote):
    assert paper_fill_from_quote("l1", "BUY", CE_KEY, "LIMIT", 100.0)["status"] == "REJECTED"
    assert paper_fill_from_quote("l2", "BUY", CE_KEY, "LIMIT", 101.0)["fill_price"] == 100.3


def test_stale_or_missing_live_price_is_rejected(live_quote):
    in_memory_quotes[CE_KEY]["ts"] -= 60
    assert paper_fill_from_quote("x", "BUY", CE_KEY, "MARKET", None)["status"] == "REJECTED"
    assert paper_fill_from_quote("y", "BUY", "NSE_FO|unknown", "MARKET", None)["status"] == "REJECTED"


# --- API --------------------------------------------------------------------------

def test_callback_rejects_unknown_state(client):
    res = client.get("/api/v1/broker/upstox/callback?code=abc&state=forged", follow_redirects=False)
    assert res.status_code == 303 and "upstox_error" in res.headers["location"]


def test_login_and_callback_store_session(client, make_user, monkeypatch):
    from app.core.config import settings
    import app.main as main

    monkeypatch.setattr(settings, "UPSTOX_API_KEY", "key123")
    monkeypatch.setattr(settings, "UPSTOX_API_SECRET", "secret")
    monkeypatch.setattr(settings, "UPSTOX_REDIRECT_URI", "https://example.org/api/v1/broker/upstox/callback")

    async def fake_exchange(self, code, api_key, api_secret, redirect_uri):
        assert (code, api_key, api_secret) == ("authcode", "key123", "secret")
        return {"access_token": "live-token", "user_name": "Test Trader"}

    monkeypatch.setattr(main.UpstoxClient, "exchange_code", fake_exchange)
    _, _, token = make_user()
    login_url = client.get("/api/v1/broker/upstox/login", headers=auth(token)).json()["url"]
    state = httpx.URL(login_url).params["state"]

    res = client.get(f"/api/v1/broker/upstox/callback?code=authcode&state={state}", follow_redirects=False)
    try:
        assert res.headers["location"] == "/?broker=upstox_connected"
        status = client.get("/api/v1/broker/upstox/status", headers=auth(token)).json()
        assert status["connected"] is True and status["user_name"] == "Test Trader"
        # The state is single-use
        again = client.get(f"/api/v1/broker/upstox/callback?code=authcode&state={state}", follow_redirects=False)
        assert "upstox_error" in again.headers["location"]
    finally:
        client.post("/api/v1/broker/upstox/disconnect", headers=auth(token))
    assert client.get("/api/v1/broker/upstox/status", headers=auth(token)).json()["connected"] is False


def test_manual_order_fills_at_live_ask(client, make_user, live_quote):
    _, _, token = make_user()
    res = client.post("/api/v1/orders", headers=auth(token), json={
        "instrument_key": CE_KEY, "trading_symbol": "NIFTY 24300 CE", "side": "BUY", "quantity": 65,
    })
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "FILLED" and res.json()["fill_price"] == 100.3
    positions = client.get("/api/v1/portfolio/positions", headers=auth(token)).json()
    assert any(p["instrument_key"] == CE_KEY and p["quantity"] == 65 for p in positions)


def test_index_is_not_tradable(client, make_user):
    _, _, token = make_user()
    res = client.post("/api/v1/orders", headers=auth(token), json={
        "instrument_key": "NSE_INDEX|Nifty 50", "side": "BUY", "quantity": 65,
    })
    assert res.status_code == 400


def test_snapshot_endpoint_serves_option_chain(client, make_user):
    _, _, token = make_user()
    snap = {}
    for _ in range(50):  # the feed loop fills the chain within a tick or two
        snap = client.get("/api/v1/market/snapshot", headers=auth(token)).json()
        if snap.get("rows"):
            break
        time.sleep(0.1)
    assert snap["source"] == "simulator"
    assert snap["atm_ce_key"] and snap["lot_size"] and len(snap["rows"]) >= 5
    atm = next(r for r in snap["rows"] if r["strike"] == snap["atm_strike"])
    assert atm["ce"]["instrument_key"] == snap["atm_ce_key"] and atm["ce"]["ltp"] > 0


# --- Strategy only trades the ATM call --------------------------------------------

def test_strategy_ignores_non_atm_candles(monkeypatch):
    from app.strategies.kaushalcustomWMASMACall1.strategy import KaushalCustomWMASMACall1
    strat = KaushalCustomWMASMACall1({"quantity": 65})
    monkeypatch.setitem(market_snapshot, "atm_ce_key", CE_KEY)
    for key in ("NSE_INDEX|Nifty 50", PE_KEY):
        assert run(strat.on_candle({"instrument_key": key}, [])) is None
    held = [{"instrument_key": PE_KEY, "quantity": 65, "trading_symbol": "x"}]
    assert run(strat.on_candle({"instrument_key": CE_KEY}, held)) is None

"""
Upstox API v2 client: OAuth login, market quotes and NIFTY option chain.
Used for market data only; orders are filled by the paper engine at these live prices.
Docs: https://upstox.com/developer/api-documentation/open-api/
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger("upstox")

API_BASE = "https://api.upstox.com"
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


class UpstoxError(Exception):
    pass


class UpstoxAuthError(UpstoxError):
    """Token missing, expired (Upstox tokens end at ~3:30 AM IST) or revoked."""


def authorization_url(api_key: str, redirect_uri: str, state: str) -> str:
    query = urlencode({
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return f"{API_BASE}/v2/login/authorization/dialog?{query}"


class UpstoxClient:
    def __init__(self, access_token: Optional[str] = None, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.access_token = access_token
        self._transport = transport  # injectable for tests

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=10.0, transport=self._transport)

    @staticmethod
    def _check(res: httpx.Response) -> Dict[str, Any]:
        if res.status_code == 401:
            raise UpstoxAuthError("Upstox session expired or invalid. Reconnect from the Broker tab.")
        try:
            body = res.json()
        except ValueError:
            raise UpstoxError(f"Upstox returned HTTP {res.status_code} with a non-JSON body")
        if res.status_code >= 400 or body.get("status") == "error":
            errors = body.get("errors") or [{}]
            msg = errors[0].get("message") or f"HTTP {res.status_code}"
            if errors[0].get("errorCode") in ("UDAPI100050", "UDAPI100016"):  # invalid / expired token
                raise UpstoxAuthError(msg)
            raise UpstoxError(msg)
        return body

    async def exchange_code(self, code: str, api_key: str, api_secret: str, redirect_uri: str) -> Dict[str, Any]:
        """Swap the one-time authorization code for an access token."""
        async with self._client() as client:
            res = await client.post(
                "/v2/login/authorization/token",
                data={
                    "code": code,
                    "client_id": api_key,
                    "client_secret": api_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        body = self._check(res)
        if not body.get("access_token"):
            raise UpstoxError("Upstox did not return an access token")
        return body

    async def get_quotes(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Full market quotes, keyed by our instrument_key (e.g. "NSE_FO|43885").
        Returns {"ltp", "bid", "ask", "oi", "volume", "prev_close"} per instrument.
        """
        out: Dict[str, Dict[str, Any]] = {}
        async with self._client() as client:
            for i in range(0, len(instrument_keys), 500):  # API limit per call
                batch = instrument_keys[i:i + 500]
                res = await client.get("/v2/market-quote/quotes", params={"instrument_key": ",".join(batch)})
                for q in (self._check(res).get("data") or {}).values():
                    key = q.get("instrument_token")
                    if not key:
                        continue
                    depth = q.get("depth") or {}
                    buy, sell = depth.get("buy") or [{}], depth.get("sell") or [{}]
                    ohlc = q.get("ohlc") or {}
                    ltp = float(q.get("last_price") or 0.0)
                    net_change = q.get("net_change")
                    out[key] = {
                        "ltp": ltp,
                        "bid": float(buy[0].get("price") or 0.0),
                        "ask": float(sell[0].get("price") or 0.0),
                        "oi": q.get("oi"),
                        "volume": q.get("volume"),
                        "prev_close": round(ltp - net_change, 2) if net_change is not None else ohlc.get("close"),
                    }
        return out

    async def _candles(self, path: str) -> List[Dict[str, Any]]:
        """Candle endpoints return [[ts, o, h, l, c, volume, oi], ...] newest first; we return oldest first."""
        async with self._client() as client:
            res = await client.get(path)
        raw = (self._check(res).get("data") or {}).get("candles") or []
        out = []
        for c in raw:
            ts = datetime.fromisoformat(c[0])
            out.append({"ts": ts, "date": ts.date(), "minute": ts.hour * 60 + ts.minute,
                        "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])})
        return sorted(out, key=lambda x: x["ts"])

    async def get_intraday_candles(self, instrument_key: str, interval: str = "1minute") -> List[Dict[str, Any]]:
        """Today's candles so far (lets the agent recover a full day of bars after a restart)."""
        return await self._candles(f"/v2/historical-candle/intraday/{quote(instrument_key, safe='')}/{interval}")

    async def get_daily_candles(self, instrument_key: str, to_date: date, from_date: date) -> List[Dict[str, Any]]:
        return await self._candles(
            f"/v2/historical-candle/{quote(instrument_key, safe='')}/day/{to_date.isoformat()}/{from_date.isoformat()}"
        )

    async def get_option_contracts(self, underlying_key: str = NIFTY_INDEX_KEY) -> List[Dict[str, Any]]:
        async with self._client() as client:
            res = await client.get("/v2/option/contract", params={"instrument_key": underlying_key})
        return self._check(res).get("data") or []

    async def get_option_chain(self, expiry: str, underlying_key: str = NIFTY_INDEX_KEY) -> List[Dict[str, Any]]:
        async with self._client() as client:
            res = await client.get("/v2/option/chain", params={"instrument_key": underlying_key, "expiry_date": expiry})
        return self._check(res).get("data") or []


def nearest_expiry(contracts: List[Dict[str, Any]], today: Optional[date] = None) -> Optional[str]:
    today_s = (today or date.today()).isoformat()
    expiries = sorted({c["expiry"] for c in contracts if c.get("expiry") and c["expiry"] >= today_s})
    return expiries[0] if expiries else None


def build_chain_rows(chain: List[Dict[str, Any]], contracts: List[Dict[str, Any]], strikes_each_side: int) -> Dict[str, Any]:
    """
    Turn the Upstox option-chain payload into the snapshot shape used by the app:
    ATM strike +/- N strikes, each with CE/PE instrument key, symbol, prices and greeks.
    """
    if not chain:
        return {"rows": []}
    by_key = {c["instrument_key"]: c for c in contracts if c.get("instrument_key")}
    spot = float(chain[0].get("underlying_spot_price") or 0.0)
    strikes = sorted(chain, key=lambda r: r["strike_price"])
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i]["strike_price"] - spot))
    window = strikes[max(0, atm_idx - strikes_each_side): atm_idx + strikes_each_side + 1]

    def leg(opt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not opt or not opt.get("instrument_key"):
            return None
        md, greeks = opt.get("market_data") or {}, opt.get("option_greeks") or {}
        contract = by_key.get(opt["instrument_key"], {})
        return {
            "instrument_key": opt["instrument_key"],
            "trading_symbol": contract.get("trading_symbol") or opt["instrument_key"],
            "ltp": md.get("ltp"),
            "bid": md.get("bid_price"),
            "ask": md.get("ask_price"),
            "oi": md.get("oi"),
            "prev_oi": md.get("prev_oi"),
            "volume": md.get("volume"),
            "prev_close": md.get("close_price"),
            "iv": greeks.get("iv"),
            "delta": greeks.get("delta"),
            "theta": greeks.get("theta"),
        }

    rows = [{"strike": r["strike_price"], "ce": leg(r.get("call_options")), "pe": leg(r.get("put_options"))} for r in window]
    atm = strikes[atm_idx]
    lot_size = next((c.get("lot_size") for c in contracts if c.get("lot_size")), None)
    return {
        "spot": spot,
        "atm_strike": atm["strike_price"],
        "atm_ce_key": (atm.get("call_options") or {}).get("instrument_key"),
        "atm_pe_key": (atm.get("put_options") or {}).get("instrument_key"),
        "lot_size": lot_size,
        "rows": rows,
    }

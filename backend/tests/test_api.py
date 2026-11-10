import hashlib

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.security import hash_password, verify_password, _LEGACY_SALT


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def legacy_hash(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), _LEGACY_SALT, 100000).hex()


# --- Passwords -------------------------------------------------------------

def test_bcrypt_hash_roundtrip():
    h = hash_password("hunter22!")
    assert h.startswith("$2")
    assert verify_password("hunter22!", h) == (True, False)
    assert verify_password("wrong", h) == (False, False)


def test_legacy_pbkdf2_hash_is_accepted_and_flagged_for_rehash():
    legacy = legacy_hash("oldpass123")
    assert verify_password("oldpass123", legacy) == (True, True)
    assert verify_password("nope", legacy) == (False, False)


def test_login_upgrades_legacy_hash(client):
    from sqlalchemy.future import select
    from app.core.database import async_session_maker
    from app.db.models import User

    email = "legacy@example.com"

    async def insert_user():
        async with async_session_maker() as db:
            db.add(User(email=email, password_hash=legacy_hash("oldpass123")))
            await db.commit()

    async def read_hash():
        async with async_session_maker() as db:
            return (await db.execute(select(User).where(User.email == email))).scalar_one().password_hash

    client.portal.call(insert_user)
    res = client.post("/api/v1/auth/login", json={"email": email, "password": "oldpass123"})
    assert res.status_code == 200
    assert client.portal.call(read_hash).startswith("$2")


# --- Auth & validation -------------------------------------------------------

def test_register_and_login(client, make_user):
    email, password, _ = make_user()
    res = client.post("/api/v1/auth/login", json={"email": email.upper(), "password": password})
    assert res.status_code == 200
    profile = client.get("/api/v1/user/profile", headers=auth(res.json()["access_token"]))
    assert profile.json()["email"] == email


def test_wrong_password_rejected(client, make_user):
    email, _, _ = make_user()
    res = client.post("/api/v1/auth/login", json={"email": email, "password": "not-it"})
    assert res.status_code == 400


@pytest.mark.parametrize("body", [
    {"email": "not-an-email", "password": "supersecret1"},
    {"email": "a@example.com", "password": "short"},
    {"email": "a@example.com", "password": "supersecret1", "is_admin": True},
])
def test_register_rejects_invalid_input(client, body):
    assert client.post("/api/v1/auth/register", json=body).status_code == 422


def test_signup_does_not_create_fake_trades(client, make_user):
    _, _, token = make_user()
    assert client.get("/api/v1/trades", headers=auth(token)).json() == []


def test_seed_demo_disabled_without_demo_mode(client, make_user):
    _, _, token = make_user()
    assert client.post("/api/v1/paper-trading/seed-demo", headers=auth(token)).status_code == 404


def test_protected_endpoints_require_token(client):
    assert client.get("/api/v1/user/profile").status_code in (401, 403)
    assert client.get("/api/v1/user/profile", headers=auth("garbage")).status_code == 401


def test_subscription_validation(client, make_user):
    _, _, token = make_user()
    version_id = client.get("/api/v1/strategies").json()[0]["versions"][0]["id"]
    bad = client.post("/api/v1/subscriptions", headers=auth(token),
                      json={"strategy_version_id": version_id, "quantity": -5})
    assert bad.status_code == 422
    missing = client.post("/api/v1/subscriptions", headers=auth(token),
                          json={"strategy_version_id": "does-not-exist"})
    assert missing.status_code == 404
    ok = client.post("/api/v1/subscriptions", headers=auth(token),
                     json={"strategy_version_id": version_id, "quantity": 50})
    assert ok.status_code == 201


def test_live_broker_verification_is_not_faked(client, make_user):
    _, _, token = make_user()
    res = client.post("/api/v1/broker/verify", headers=auth(token),
                      json={"broker_name": "kite", "client_id": "X", "api_key": "Y"})
    assert res.status_code == 501


def test_cannot_toggle_another_users_subscription(client, make_user):
    _, _, token_a = make_user()
    _, _, token_b = make_user()
    version_id = client.get("/api/v1/strategies").json()[0]["versions"][0]["id"]
    sub_id = client.post("/api/v1/subscriptions", headers=auth(token_a),
                         json={"strategy_version_id": version_id}).json()["subscription_id"]
    res = client.post(f"/api/v1/subscriptions/{sub_id}/toggle", headers=auth(token_b), json={"active": False})
    assert res.status_code == 404


# --- WebSocket --------------------------------------------------------------

def test_websocket_rejects_missing_or_bad_token(client):
    for url in ("/api/v1/ws/stream", "/api/v1/ws/stream?token=garbage"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url) as ws:
                ws.receive_text()
        assert exc.value.code == 1008


def _next_non_tick(ws, limit=200):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["event_type"] not in ("TickUpdate", "QuoteUpdate"):
            return msg
    return None


def test_order_events_only_reach_their_owner(client, make_user):
    _, _, token_a = make_user()
    _, _, token_b = make_user()

    with client.websocket_connect(f"/api/v1/ws/stream?token={token_a}") as ws_a, \
         client.websocket_connect(f"/api/v1/ws/stream?token={token_b}") as ws_b:
        res = client.post("/api/v1/paper-trading/execute-demo-trade", headers=auth(token_a),
                          json={"side": "BUY", "quantity": 50})
        assert res.status_code == 200
        assert _next_non_tick(ws_a)["event_type"] == "OrderFilled"

        # User B then places a SELL. The first non-tick event B receives must be its own SELL,
        # proving A's BUY fill was never delivered to B.
        client.post("/api/v1/paper-trading/execute-demo-trade", headers=auth(token_b),
                    json={"side": "SELL", "quantity": 50})
        msg_b = _next_non_tick(ws_b)
        assert msg_b["event_type"] == "OrderFilled"
        assert msg_b["data"]["side"] == "SELL"


# --- Single-server (cloud) mode -----------------------------------------------

def test_registration_can_be_disabled(client, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ALLOW_REGISTRATION", False)
    res = client.post("/api/v1/auth/register", json={"email": "late@example.com", "password": "supersecret1"})
    assert res.status_code == 403


def test_in_process_mode_resumes_running_strategies(client, make_user, monkeypatch):
    import app.main as main
    from app.core.config import settings
    from app.core.database import async_session_maker
    from app.db.models import StrategySubscription

    _, _, token = make_user()
    version_id = client.get("/api/v1/strategies").json()[0]["versions"][0]["id"]
    sub_id = client.post("/api/v1/subscriptions", headers=auth(token),
                         json={"strategy_version_id": version_id,
                               "config_json": {"bypass_scheduler": True}}).json()["subscription_id"]

    async def mark_running():
        async with async_session_maker() as db:
            sub = await db.get(StrategySubscription, sub_id)
            sub.status = "running"
            await db.commit()

    started = []
    monkeypatch.setattr(settings, "RUN_STRATEGIES_IN_PROCESS", True)
    monkeypatch.setattr(main, "start_strategy_in_process", started.append)
    client.portal.call(mark_running)
    client.portal.call(main.resume_running_strategies)
    assert sub_id in started

    # Toggling on in this mode never goes through Celery
    monkeypatch.setattr(main.start_strategy_task, "delay", lambda *_: pytest.fail("Celery used"))
    res = client.post(f"/api/v1/subscriptions/{sub_id}/toggle", headers=auth(token), json={"active": True})
    assert res.status_code == 200 and started.count(sub_id) == 2


def test_starting_a_strategy_stops_older_running_copies(client, make_user, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "start_strategy_in_process", lambda *_: None)
    monkeypatch.setattr(main.settings, "RUN_STRATEGIES_IN_PROCESS", True)

    _, _, token = make_user()
    version_id = client.get("/api/v1/strategies").json()[0]["versions"][0]["id"]
    body = {"strategy_version_id": version_id, "config_json": {"bypass_scheduler": True}}
    first = client.post("/api/v1/subscriptions", headers=auth(token), json=body).json()["subscription_id"]
    second = client.post("/api/v1/subscriptions", headers=auth(token), json=body).json()["subscription_id"]
    for sub_id in (first, second):
        assert client.post(f"/api/v1/subscriptions/{sub_id}/toggle", headers=auth(token), json={"active": True}).status_code == 200

    runs = {s["subscription_id"]: s["status"] for s in client.get("/api/v1/subscriptions", headers=auth(token)).json()}
    assert runs == {first: "stopped", second: "running"}

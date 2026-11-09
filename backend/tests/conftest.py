import os
import sys
import tempfile

from cryptography.fernet import Fernet

# Isolated settings for the test run. Must be set before any `app` import.
_tmpdir = tempfile.mkdtemp(prefix="algotrade_test_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(_tmpdir, 'test.db')}"
os.environ["SECRET_KEY"] = "test-secret-key-" + "x" * 32
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"  # unreachable on purpose: exercises the memory-bus fallback
os.environ["RABBITMQ_URL"] = "amqp://guest:guest@127.0.0.1:1//"
os.environ["DEMO_MODE"] = "false"
os.environ["UPSTOX_API_KEY"] = ""  # never use the developer's real Upstox app in tests
os.environ["UPSTOX_API_SECRET"] = ""

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user(client):
    """Registers a fresh user and returns (email, password, token)."""

    def _make(password="supersecret1"):
        email = f"user_{os.urandom(4).hex()}@example.com"
        res = client.post("/api/v1/auth/register", json={"email": email, "password": password})
        assert res.status_code == 201, res.text
        return email, password, res.json()["access_token"]

    return _make

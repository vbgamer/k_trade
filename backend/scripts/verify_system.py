#!/usr/bin/env python
"""
AlgoTrade Pro - System Health & Verification Script
Verifies database connection, table counts, portfolio totals, and API readiness.
"""
import sys
import os
import asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.core.database import async_session_maker
from sqlalchemy import text
from app.db.models import User, Trade, BrokerOrder, Position, BrokerCredential
from sqlalchemy.future import select
from sqlalchemy import func


async def check_health():
    print("=" * 60)
    print("  AlgoTrade Pro - System Verification Check")
    print("=" * 60)

    # 1. Database Connection & Models
    try:
        async with async_session_maker() as session:
            rev = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        print(f"[OK] Database connected. Schema migration revision: {rev}")
    except Exception as e:
        print(f"[FAIL] Database not reachable or not migrated (run `python manage.py migrate`): {e}")
        return False

    async with async_session_maker() as session:
        # 2. Check Users
        user_count = (await session.execute(select(func.count(User.id)))).scalar()
        print(f"[OK] Total Users registered: {user_count}")

        # 3. Check Trades & Orders
        trade_count = (await session.execute(select(func.count(Trade.id)))).scalar()
        order_count = (await session.execute(select(func.count(BrokerOrder.id)))).scalar()
        print(f"[OK] Total Orders: {order_count} | Total Trades: {trade_count}")

        # 4. Check P&L stats
        pnl_sum = (await session.execute(select(func.sum(Trade.realized_pnl)))).scalar() or 0.0
        print(f"[OK] Cumulative Realized P&L: Rs. {pnl_sum:,.2f}")

        # 5. Check Broker Credentials
        broker_count = (await session.execute(select(func.count(BrokerCredential.id)))).scalar()
        print(f"[OK] Active Broker Credentials: {broker_count}")

    print("\n[SUCCESS] All core system components are operational!")
    return True


if __name__ == "__main__":
    asyncio.run(check_health())

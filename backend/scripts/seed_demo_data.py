#!/usr/bin/env python
"""
AlgoTrade Pro - Seed Demo Data Script
Populates the database with default strategies, a test user, Paper Trading broker credentials,
and realistic past Intraday and F&O trades with P&L.
"""
import sys
import os
import asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.core.database import async_session_maker
from app.core.migrations import run_migrations
from app.core.security import hash_password, encrypt
from app.db.models import User, BrokerCredential
from app.services.bootstrap import ensure_default_strategies
from app.services.demo_seed import seed_user_past_trades
from sqlalchemy.future import select


async def async_seed():
    print("Applying database migrations...")
    await asyncio.to_thread(run_migrations)

    async with async_session_maker() as db:
        await ensure_default_strategies(db)

    async with async_session_maker() as session:
        # Check if test user exists
        user_res = await session.execute(
            select(User).where(User.email == "vedant@algotrade.pro")
        )
        user = user_res.scalars().first()

        if not user:
            print("Creating demo user: vedant@algotrade.pro ...")
            user = User(
                email="vedant@algotrade.pro",
                password_hash=hash_password("password123"),
                first_name="Vedant",
                last_name="Bhosale",
                is_active=True,
                virtual_balance=500000.0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            print(f"Created user ID: {user.id}")
        else:
            print(f"Found existing user: {user.email} (ID: {user.id})")

        # Check broker credential
        cred_res = await session.execute(
            select(BrokerCredential).where(
                BrokerCredential.user_id == user.id,
                BrokerCredential.broker_name == "paper_trading"
            )
        )
        cred = cred_res.scalars().first()
        if not cred:
            print("Creating Paper Trading broker credential...")
            cred = BrokerCredential(
                user_id=user.id,
                broker_name="paper_trading",
                client_id_encrypted=encrypt("ALGO_DEMO_01"),
                api_key_encrypted=encrypt("simulated_paper_key"),
                api_secret_encrypted=encrypt("simulated_paper_secret"),
            )
            session.add(cred)
            await session.commit()
            print("Paper Trading credentials linked.")
        else:
            print("Broker credential already configured.")

        # Seed trades
        print("Checking & seeding past Intraday and F&O trades...")
        await seed_user_past_trades(session, user.id)
        
        # Check final balance
        await session.refresh(user)
        print(f"Seeding completed successfully! User balance: Rs. {user.virtual_balance:,.2f}")


def main():
    asyncio.run(async_seed())


if __name__ == "__main__":
    main()

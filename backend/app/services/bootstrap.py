from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import StrategyDefinition, StrategyVersion

DEFAULT_STRATEGY_SLUG = "kaushalcustomWMASMACall1"
AI_STRATEGY_SLUG = "aiOptionsAgent"

BUILT_IN_STRATEGIES = [
    {
        "slug": DEFAULT_STRATEGY_SLUG,
        "name": "Nifty ATM WMA5/SMA1 Crossover Call",
        "description": "NIFTY ATM option using Heikin Ashi candles. Crossovers of WMA5 close and SMA1 open triggers trades.",
        "parameters_schema_json": {"timeframeMin": 1, "quantity": 50},
    },
    {
        "slug": AI_STRATEGY_SLUG,
        "name": "AI Options Agent (ML)",
        "description": "LightGBM models trained on 2021-2026 NIFTY option data score the ATM CE and PE every minute "
                       "and buy the better side when the expected net reward clears the trained threshold. "
                       "Exits on target / stop / time limit / 15:15. Paper trading only.",
        "parameters_schema_json": {"quantity": 65, "ai_mode": "auto", "max_trades_per_day": 3},
    },
]


async def ensure_default_strategies(db: AsyncSession) -> None:
    """Registers built-in strategy definitions in the catalogue if they are missing."""
    for spec in BUILT_IN_STRATEGIES:
        result = await db.execute(select(StrategyDefinition).where(StrategyDefinition.slug == spec["slug"]))
        if result.scalar_one_or_none():
            continue
        definition = StrategyDefinition(
            slug=spec["slug"], name=spec["name"], description=spec["description"],
            category="Options", is_public=True,
        )
        db.add(definition)
        await db.commit()
        await db.refresh(definition)
        db.add(StrategyVersion(
            strategy_definition_id=definition.id, semver="1.0.0",
            parameters_schema_json=spec["parameters_schema_json"],
        ))
        await db.commit()

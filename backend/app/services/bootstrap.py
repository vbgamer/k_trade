from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import StrategyDefinition, StrategyVersion

DEFAULT_STRATEGY_SLUG = "kaushalcustomWMASMACall1"


async def ensure_default_strategies(db: AsyncSession) -> None:
    """Registers built-in strategy definitions in the catalogue if they are missing."""
    query = select(StrategyDefinition).where(StrategyDefinition.slug == DEFAULT_STRATEGY_SLUG)
    result = await db.execute(query)
    if result.scalar_one_or_none():
        return

    definition = StrategyDefinition(
        slug=DEFAULT_STRATEGY_SLUG,
        name="Nifty ATM WMA5/SMA1 Crossover Call",
        description="NIFTY ATM option using Heikin Ashi candles. Crossovers of WMA5 close and SMA1 open triggers trades.",
        category="Options",
        is_public=True
    )
    db.add(definition)
    await db.commit()
    await db.refresh(definition)

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        semver="1.0.0",
        parameters_schema_json={"timeframeMin": 1, "quantity": 50}
    )
    db.add(version)
    await db.commit()

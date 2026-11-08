import os
from alembic import command
from alembic.config import Config

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_migrations() -> None:
    """Upgrade the database to the latest migration (same as `alembic upgrade head`)."""
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")

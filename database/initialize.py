from __future__ import annotations

import argparse
import logging
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

# The models import registers the tables with SQLAlchemy's metadata.
from database import models  # noqa: F401
from database.session import Base, engine


logger = logging.getLogger(__name__)


def init_database(*, drop_existing: bool = False) -> None:
    """Create the database schema on the configured engine."""
    try:
        if drop_existing:
            logger.warning("Dropping existing tables before re-creating schema.")
            Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
    except SQLAlchemyError as exc:
        logger.exception("Failed to initialise database schema: %s", exc)
        raise
    else:
        logger.info("Database schema initialised successfully.")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialise the database schema for the CiteSure service."
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop existing tables before creating the schema.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    init_database(drop_existing=args.drop_existing)


if __name__ == "__main__":
    main()

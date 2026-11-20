import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


def lock_season(db: Session, season_id: uuid.UUID) -> None:
    """Serialize slot generation and scheduling runs for one season.

    Transaction-scoped: released automatically on commit or rollback.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"skeddie:season:{season_id}"},
    )

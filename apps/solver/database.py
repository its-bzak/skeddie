import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

# On the host, settings live in the repo-root .env (skeddie/.env). In Docker
# they come from docker-compose and the file may not exist (and /app has no
# grandparent), so only load it when it is actually there.
_here = Path(__file__).resolve()
if len(_here.parents) > 2:
    _root_env = _here.parents[2] / ".env"
    if _root_env.is_file():
        load_dotenv(dotenv_path=_root_env)

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# The role row-level security applies to (see packages/db/migrations/004_row_security.sql).
APP_ROLE = "skeddie_app"
_SCOPED_USER = "scoped_user_id"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def scope_session(session: Session, user_id: uuid.UUID) -> None:
    """Run everything this session does as the application role, on behalf of `user_id`.

    Applied at the start of every transaction (sessions commit more than once
    during a scheduling run). Both settings are transaction-local, so they
    reset on commit or rollback and can never leak to another request through
    the connection pool.
    """
    session.info[_SCOPED_USER] = str(user_id)
    if not event.contains(session, "after_begin", _apply_scope):
        event.listen(session, "after_begin", _apply_scope)
    if session.in_transaction():
        _set_scope(session.connection(), session.info[_SCOPED_USER])


def unscope_session(session: Session) -> None:
    session.info.pop(_SCOPED_USER, None)
    if event.contains(session, "after_begin", _apply_scope):
        event.remove(session, "after_begin", _apply_scope)
    if session.in_transaction():
        connection = session.connection()
        connection.execute(text("RESET ROLE"))
        connection.execute(text("SELECT set_config('skeddie.user_id', '', true)"))


def _apply_scope(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    user_id = session.info.get(_SCOPED_USER)
    if user_id:
        _set_scope(connection, user_id)


def _set_scope(connection: Connection, user_id: str) -> None:
    connection.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    connection.execute(text("SELECT set_config('skeddie.user_id', :user_id, true)"), {"user_id": user_id})

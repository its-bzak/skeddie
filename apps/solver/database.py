import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

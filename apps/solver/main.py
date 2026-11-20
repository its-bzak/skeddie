import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from database import SessionLocal, engine
from routers import scheduling
from services.runs import fail_interrupted_runs

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs execute inside this process, so any left queued or running were
    # interrupted by a restart and will never finish on their own.
    with SessionLocal() as db:
        interrupted = fail_interrupted_runs(db)
    if interrupted:
        log.warning("Marked %d interrupted scheduling run(s) as errors", interrupted)
    yield


app = FastAPI(title="Skeddie Solver API", lifespan=lifespan)
app.include_router(scheduling.router)


@app.get("/health")
def health_check():
    with engine.connect() as conn:
        db_version = conn.execute(text("SELECT version();")).scalar()
        table_count = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';"
        )).scalar()
        migrations = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars().all()
    return {
        "status": "ok",
        "postgres_version": db_version,
        "tables_loaded": table_count,
        "migrations": migrations,
    }

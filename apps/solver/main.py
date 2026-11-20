from fastapi import FastAPI
from sqlalchemy import text

from database import engine
from routers import scheduling

app = FastAPI(title="Skeddie Solver API")
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

import os
from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI(title="Skeddie Solver API")
engine = create_engine(os.environ["DATABASE_URL"])

@app.get("/health")
def health_check():
    with engine.connect() as conn:
        db_version = conn.execute(text("SELECT version();")).scalar()
        table_count = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';"
        )).scalar()
    return {
        "status": "ok",
        "postgres_version": db_version,
        "tables_loaded": table_count,
    }
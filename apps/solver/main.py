from fastapi import FastAPI

app = FastAPI()

@app.get("/api/health")
async def root():
    return {"status": "running"}
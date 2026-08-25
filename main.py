# main.py
from fastapi import FastAPI

from app.api.trip import router as trip_router
from app.api.google_oauth import router as google_oauth_router

app = FastAPI(title="Smart Tourism Assistant — AI Backend")

app.include_router(trip_router)
app.include_router(google_oauth_router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "smart-tourism-ai-backend"}
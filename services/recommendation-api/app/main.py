"""Recommendation API — stub.

Will implement the 3-stage funnel (candidate generation -> ranking -> re-ranking)
described in docs/architecture.md. For now, just scaffolding + health check so the
service wires into docker-compose and the Jellyfin plugin has something to point at.
"""

from fastapi import FastAPI

app = FastAPI(title="Muse Recommendation API", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/recommendations/{user_id}")
async def recommendations(user_id: str) -> dict:
    # TODO: stage 1 candidate generation (Qdrant similarity + co-occurrence + backlog + trending)
    # TODO: stage 2 ranking (weighted scoring function incl. confidence)
    # TODO: stage 3 re-ranking (diversity cap, explore-dial, user overlay, confidence placement)
    return {"user_id": user_id, "recommendations": []}

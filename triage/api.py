"""Minimal HTTP wrapper: uv run uvicorn triage.api:app --port 8000

POST /triage with one message object or a list of them. The same pipeline as
the CLI; one bad record never fails the request.
"""
from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, Header

from .ingest import normalise
from .pipeline import make_classifier, triage_all

app = FastAPI(title="GAJAN message triage")
_clf = make_classifier("auto")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "classifier": getattr(_clf, "name", "rules (offline mode)")}


@app.post("/triage")
async def triage(payload: Any = Body(...),
                 x_gemini_api_key: str | None = Header(default=None)) -> list[dict]:
    """Bring your own key: send X-Gemini-Api-Key to use your own Gemini quota for this
    request. The key is used for this request only and never stored or logged.
    Without the header, the server's own key (if configured) or offline rules are used."""
    clf = make_classifier("gemini", api_key=x_gemini_api_key) if x_gemini_api_key else _clf
    records = payload if isinstance(payload, list) else [payload]
    results = await triage_all(normalise(records), clf)
    return [r.model_dump(mode="json") for r in results]

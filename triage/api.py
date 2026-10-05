"""Web app and HTTP API.  Start with:  uv run uvicorn triage.api:app --port 8000

Bring your own key (BYOK): the server holds no credentials of its own. Each request
carries the caller's credentials in headers, which are used for that request only
and never stored, logged or echoed back:

  X-Gemini-Api-Key   a Gemini API key from AI Studio
  X-GCP-Project      a Google Cloud project id; Gemini is called through Vertex AI using
                     the server's `gcloud auth application-default login` credentials.
                     Meant for running locally. Disable it on a shared deployment with
                     TRIAGE_ALLOW_GCP_PROJECT=0, or anyone could bill the server's projects.
  X-Gemini-Model     optional model override

With no credentials the offline rules classifier runs and everything goes to a human.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from . import report
from .ingest import normalise
from .pipeline import triage_all

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
ALLOW_GCP_PROJECT = os.environ.get("TRIAGE_ALLOW_GCP_PROJECT", "1") != "0"
MAX_RECORDS = 500

app = FastAPI(title="GAJAN message triage")


def _byok_classifier(api_key: str | None, gcp_project: str | None, model: str | None):
    """Build a classifier from the caller's own credentials only. Never falls back to
    server environment keys, so one user can never spend another's quota."""
    api_key = (api_key or "").strip() or None
    gcp_project = (gcp_project or "").strip() or None
    if gcp_project and not ALLOW_GCP_PROJECT:
        raise HTTPException(403, "Google Cloud project mode is disabled on this server; send X-Gemini-Api-Key.")
    if gcp_project and not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", gcp_project):
        raise HTTPException(400, "That does not look like a Google Cloud project id.")
    if not (api_key or gcp_project):
        return None
    from .gemini import DEFAULT_MODEL, GeminiClassifier
    return GeminiClassifier(api_key=api_key, gcp_project=None if api_key else gcp_project,
                            model=(model or "").strip() or DEFAULT_MODEL)


def _summary(results) -> dict:
    calls = [r for r in results if r.classifier.startswith("llm:")]
    cost = sum(r.usage.cost_usd for r in results)
    failed = [r for r in results if r.classifier == "rules (llm failed)"]
    first_error = ""
    if failed:
        first_error = next((t.detail for t in failed[0].trace if t.stage == "classifier"), "")[:300]
    return {
        "messages": len(results),
        "automation": sum(r.routing.handler == "automation" for r in results),
        "human": sum(r.routing.handler == "human" for r in results),
        "priority": dict(Counter(r.routing.priority for r in results)),
        "status": dict(Counter(r.status for r in results)),
        "model_calls": len(calls),
        "failed_calls": len(failed),
        "first_error": first_error,
        "cost_usd": round(cost, 6),
        "cost_per_1000": round(cost / len(calls) * 1000, 3) if calls else 0.0,
        "classifier": calls[0].classifier if calls else (results[0].classifier if results else "none"),
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "gcp_project_mode": ALLOW_GCP_PROJECT}


@app.get("/sample")
async def sample() -> Any:
    return json.loads((ROOT / "data" / "messages.json").read_text())


@app.post("/run")
async def run(payload: Any = Body(...),
              x_gemini_api_key: str | None = Header(default=None),
              x_gcp_project: str | None = Header(default=None),
              x_gemini_model: str | None = Header(default=None)) -> dict:
    """Triage a batch and return results, a summary and the observation report."""
    records = payload if isinstance(payload, list) else [payload]
    if len(records) > MAX_RECORDS:
        raise HTTPException(413, f"At most {MAX_RECORDS} messages per request.")
    clf = _byok_classifier(x_gemini_api_key, x_gcp_project, x_gemini_model)
    results = await triage_all(normalise(records), clf, concurrency=8)
    label = getattr(clf, "name", "rules (offline mode)")
    return {
        "summary": _summary(results),
        "results": [r.model_dump(mode="json") for r in results],
        "report_markdown": report.build(results, label, "web upload", []),
    }


@app.post("/triage")
async def triage(payload: Any = Body(...),
                 x_gemini_api_key: str | None = Header(default=None),
                 x_gcp_project: str | None = Header(default=None),
                 x_gemini_model: str | None = Header(default=None)) -> list[dict]:
    """Plain API: one message object or a list in, a list of results out."""
    records = payload if isinstance(payload, list) else [payload]
    if len(records) > MAX_RECORDS:
        raise HTTPException(413, f"At most {MAX_RECORDS} messages per request.")
    clf = _byok_classifier(x_gemini_api_key, x_gcp_project, x_gemini_model)
    results = await triage_all(normalise(records), clf, concurrency=8)
    return [r.model_dump(mode="json") for r in results]

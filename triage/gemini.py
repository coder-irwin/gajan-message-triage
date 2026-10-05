"""Gemini-backed classifier. Same contract as the Claude one: label only, no tools,
strict JSON schema, pydantic re-validation, and any failure becomes a ClassifierError
so the pipeline falls back to rules and a human.

Key: GEMINI_API_KEY (or GOOGLE_API_KEY), or a Google Cloud project for Vertex AI.
Model: GEMINI_MODEL, default gemini-3.5-flash-lite. The cheaper gemini-2.5-flash-lite is closed
to new Gemini API keys (it still works through Vertex AI), so it cannot be the default.
"""
from __future__ import annotations

import asyncio
import copy
import os
import random

from google import genai
from google.genai import errors, types

from .llm import OUTPUT_SCHEMA, SYSTEM_PROMPT, ClassifierError, build_user_content
from .models import Analysis, InboundMessage, Usage

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# USD per million tokens: input, output (thinking bills as output), cached input.
# Source: ai.google.dev/gemini-api/docs/pricing, checked 2026-10-06.
PRICES = {
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01),
    "gemini-2.5-flash": (0.30, 2.50, 0.03),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
    "gemini-3.5-flash": (1.50, 9.00, 0.15),
}
RETRYABLE = {429, 500, 502, 503, 504}


def _strip_additional_properties(schema: dict) -> dict:
    """Gemini's schema support is a subset of JSON Schema; drop the one keyword it may reject.
    Extra keys are still rejected later by pydantic validation, so nothing is lost."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


GEMINI_SCHEMA = _strip_additional_properties(OUTPUT_SCHEMA)


def cost_of(model: str, usage: Usage) -> float:
    inp, out, cached = PRICES.get(model, PRICES["gemini-3.5-flash-lite"])
    return (usage.input_tokens * inp + usage.cache_read_input_tokens * cached + usage.output_tokens * out) / 1_000_000


class GeminiClassifier:
    def __init__(self, client: genai.Client | None = None, model: str = DEFAULT_MODEL, max_attempts: int = 6,
                 api_key: str | None = None, gcp_project: str | None = None):
        """Bring your own credentials, in this order of precedence:
        1. api_key passed in (CLI hidden prompt or the HTTP X-Gemini-Api-Key header)
        2. gcp_project passed in: Gemini through Vertex AI, billed to that Google Cloud
           project, authenticated with `gcloud auth application-default login`
        3. GEMINI_API_KEY / GOOGLE_API_KEY from the environment or .env
        The key is held in memory only and never logged or written to output."""
        self.via = "gemini-api"
        if client is None:
            if api_key:
                client = genai.Client(api_key=api_key)
            elif gcp_project:
                client = genai.Client(vertexai=True, project=gcp_project,
                                      location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
                self.via = f"vertex:{gcp_project}"
            else:
                client = genai.Client()
        self.client = client
        self.model = model
        self.max_attempts = max_attempts

    @property
    def name(self) -> str:
        return f"llm:{self.model}" + (f" via {self.via}" if self.via != "gemini-api" else "")

    def _config(self) -> types.GenerateContentConfig:
        kw: dict = dict(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=GEMINI_SCHEMA,
            temperature=0,
            max_output_tokens=4000,
            # No tools are given to the model; make that explicit and silence the SDK's AFC notice.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        # Classification does not need reasoning: turn thinking off (2.5) or to its minimum (3.x).
        if self.model.startswith("gemini-2.5"):
            kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        else:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level="minimal")
        return types.GenerateContentConfig(**kw)

    async def classify(self, msg: InboundMessage) -> tuple[Analysis, Usage]:
        resp = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.aio.models.generate_content(
                    model=self.model, contents=build_user_content(msg), config=self._config())
                break
            except errors.APIError as exc:
                code = getattr(exc, "code", None)
                if code in RETRYABLE and attempt < self.max_attempts:
                    # Free-tier keys hit per-minute limits quickly; back off and retry.
                    await asyncio.sleep(min(60, 2 ** attempt) + random.random())
                    continue
                raise ClassifierError(f"Gemini API error {code}: {str(exc)[:160]}") from exc
            except Exception as exc:  # network errors, timeouts
                if attempt < self.max_attempts:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise ClassifierError(f"Gemini call failed: {type(exc).__name__}") from exc

        um = getattr(resp, "usage_metadata", None)
        prompt = getattr(um, "prompt_token_count", 0) or 0
        cached = getattr(um, "cached_content_token_count", 0) or 0
        usage = Usage(
            input_tokens=max(0, prompt - cached),
            cache_read_input_tokens=cached,
            output_tokens=(getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0),
        )
        usage.cost_usd = cost_of(self.model, usage)

        cands = getattr(resp, "candidates", None) or []
        finish = str(getattr(cands[0], "finish_reason", "")) if cands else ""
        if not cands:
            block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            raise ClassifierError(f"Gemini returned no candidates (blocked: {block})")
        if "SAFETY" in finish or "PROHIBITED" in finish or "BLOCKLIST" in finish:
            raise ClassifierError(f"Gemini declined ({finish})")
        if "MAX_TOKENS" in finish:
            raise ClassifierError("Gemini output truncated (MAX_TOKENS)")
        text = getattr(resp, "text", None)
        if not text:
            raise ClassifierError(f"Gemini returned no text (finish_reason {finish})")
        try:
            analysis = Analysis.model_validate_json(text)
        except Exception as exc:
            raise ClassifierError(f"schema validation failed: {str(exc)[:200]}") from exc
        if not analysis.intents:
            raise ClassifierError("model returned no intents")
        return analysis, usage

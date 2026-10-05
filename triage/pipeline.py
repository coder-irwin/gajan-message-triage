"""Orchestration: normalise -> signals -> classify -> policy, one message at a time,
with bounded concurrency. A failure on one message never affects another; the
failed message is emitted as an error result in a human queue.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Protocol

from . import rules, signals
from .models import Analysis, InboundMessage, Routing, TriageResult, Usage
from .policy import KNOWN_BRANDS, SLA, decide


def _support_queue(brand: str) -> str:
    return f"{brand}:support" if brand in KNOWN_BRANDS else "group:unrouted"


class Classifier(Protocol):
    name: str
    async def classify(self, msg: InboundMessage) -> tuple[Analysis, Usage]: ...


def make_classifier(mode: str) -> Classifier | None:
    """mode: 'auto' uses Claude when credentials exist, else rules. 'rules' forces offline."""
    if mode == "rules":
        return None
    has_creds = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    if mode == "auto" and not has_creds:
        return None
    from .llm import ClaudeClassifier
    return ClaudeClassifier()


def _error_result(msg: InboundMessage, err: str, started: float) -> TriageResult:
    return TriageResult(
        message_id=msg.id, brand=msg.brand, channel=msg.channel, received_at=msg.received_at,
        status="error", classifier="none", confidence=0.0,
        confidence_notes=[f"internal error: {err}"],
        routing=Routing(handler="human", queue=_support_queue(msg.brand), action="Agent reads and routes manually",
                        priority="P3", response_sla=SLA["P3"], reasons=["H1 processing error"]),
        input_problems=msg.problems, latency_ms=int((time.monotonic() - started) * 1000),
    )


async def triage_one(msg: InboundMessage, clf: Classifier | None) -> TriageResult:
    started = time.monotonic()
    try:
        sig = signals.extract(msg.text)

        # Empty text: no model call. On WhatsApp this is usually an image, voice note or sticker
        # whose media we were not given.
        if not msg.text:
            return TriageResult(
                message_id=msg.id, brand=msg.brand, channel=msg.channel, received_at=msg.received_at,
                status="ok", classifier="none (empty message)", confidence=1.0,
                confidence_notes=["no text to classify; routing is deterministic"],
                risk_flags=sig.flags,
                routing=Routing(handler="human", queue=_support_queue(msg.brand),
                                action="Fetch any media attachment from the channel API; if none, ask the customer to resend",
                                priority="P3", response_sla=SLA["P3"], reasons=["H2 hard-stop flags: empty_message"]),
                input_problems=msg.problems, latency_ms=int((time.monotonic() - started) * 1000),
            )

        usage, degraded, status = Usage(), None, "ok"
        if clf is None:
            analysis, classifier_name = rules.classify(msg, sig), "rules (offline mode)"
            status = "degraded"
        else:
            try:
                analysis, usage = await clf.classify(msg)
                classifier_name = clf.name
            except Exception as exc:  # ClassifierError or anything unexpected from the SDK
                analysis, classifier_name = rules.classify(msg, sig), "rules (llm failed)"
                degraded, status = str(exc)[:200], "degraded"

        d = decide(msg, sig, analysis, classifier_name, degraded)
        return TriageResult(
            message_id=msg.id, brand=msg.brand, channel=msg.channel, received_at=msg.received_at,
            status=status, classifier=classifier_name, language=analysis.language, summary=analysis.summary,
            open_questions=analysis.open_questions, input_problems=msg.problems, usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000), **d,
        )
    except Exception as exc:  # last line of defence: never let one record kill the batch
        return _error_result(msg, f"{type(exc).__name__}: {exc}"[:300], started)


async def triage_all(msgs: list[InboundMessage], clf: Classifier | None, concurrency: int = 8) -> list[TriageResult]:
    sem = asyncio.Semaphore(concurrency)

    async def guarded(m: InboundMessage) -> TriageResult:
        async with sem:
            return await triage_one(m, clf)

    return list(await asyncio.gather(*(guarded(m) for m in msgs)))
